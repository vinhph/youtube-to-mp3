import asyncio
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import List, Optional
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

app = FastAPI(title="YouTube to MP3")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "downloads"
PLAYLIST_FILE = DATA_DIR / "playlist.json"

YTSTREAM_HOST = "ytstream-download-youtube-videos.p.rapidapi.com"
MP3_HOST = "youtube-mp36.p.rapidapi.com"
RAPIDAPI_KEY = os.getenv(
    "RAPIDAPI_KEY",
    "16985c26a1mshd5e05e158b27119p15fb0djsn7519a2758829",
)

DATA_DIR.mkdir(parents=True, exist_ok=True)


class ConvertRequest(BaseModel):
    url: str


class ProgressRequest(BaseModel):
    position: float


def extract_youtube_id(url_or_id: str) -> Optional[str]:
    text = url_or_id.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", text):
        return text

    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()

    if host in ("youtu.be", "www.youtu.be"):
        vid = parsed.path.lstrip("/").split("/")[0]
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
            return vid

    if "youtube.com" in host or "youtube-nocookie.com" in host:
        qs = parse_qs(parsed.query)
        if "v" in qs and qs["v"]:
            vid = qs["v"][0]
            if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
                return vid
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0] in ("shorts", "embed", "live", "v"):
            vid = parts[1]
            if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
                return vid

    match = re.search(r"(?:v=|youtu\.be/|embed/|shorts/)([A-Za-z0-9_-]{11})", text)
    if match:
        return match.group(1)

    return None


def load_playlist() -> List[dict]:
    if not PLAYLIST_FILE.exists():
        return []
    try:
        data = json.loads(PLAYLIST_FILE.read_text(encoding="utf-8"))
        return [item for item in data if item.get("youtube_id") or item.get("audio_url")]
    except Exception:
        return []


def save_playlist(items: List[dict]) -> None:
    PLAYLIST_FILE.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")


async def query_ytstream_audio(video_id: str) -> dict:
    """
    Uses YTStream (ytstream-download-youtube-videos.p.rapidapi.com) to extract direct
    audio stream (itag 140 m4a or itag 251 opus). Supports 24h+ videos with 0 conversion wait!
    Falls back to youtube-mp36 if needed.
    """
    headers = {
        "x-rapidapi-host": YTSTREAM_HOST,
        "x-rapidapi-key": RAPIDAPI_KEY,
    }
    api_url = f"https://{YTSTREAM_HOST}/dl"

    async with httpx.AsyncClient(timeout=45.0, follow_redirects=True) as client:
        resp = await client.get(api_url, params={"id": video_id}, headers=headers)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "OK":
                adaptive = data.get("adaptiveFormats") or []
                audio_formats = [
                    f for f in adaptive
                    if "audio" in (f.get("mimeType") or "") and f.get("url")
                ]
                # Prefer itag 140 (audio/mp4 m4a - supported on all browsers including iOS Safari),
                # then itag 251 (high quality opus), then any audio stream
                chosen = (
                    next((f for f in audio_formats if f.get("itag") == 140), None)
                    or next((f for f in audio_formats if f.get("itag") == 251), None)
                    or (audio_formats[0] if audio_formats else None)
                )
                if chosen:
                    thumbs = data.get("thumbnail") or []
                    thumb_url = (
                        thumbs[-1].get("url")
                        if isinstance(thumbs, list) and thumbs
                        else f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
                    )
                    return {
                        "link": chosen["url"],
                        "title": data.get("title") or f"YouTube ({video_id})",
                        "duration": round(float(data.get("lengthSeconds") or 0)),
                        "channelTitle": data.get("channelTitle") or "YouTube",
                        "thumbnail": thumb_url,
                    }

        # Fallback to youtube-mp36 if YTStream did not return a stream
        mp3_headers = {
            "x-rapidapi-host": MP3_HOST,
            "x-rapidapi-key": RAPIDAPI_KEY,
        }
        mp3_url = f"https://{MP3_HOST}/dl"
        for _ in range(30):
            r2 = await client.get(mp3_url, params={"id": video_id}, headers=mp3_headers)
            if r2.status_code != 200:
                raise RuntimeError(f"RapidAPI HTTP {r2.status_code}: {r2.text}")
            d2 = r2.json()
            status = d2.get("status")
            if status == "ok" and d2.get("link"):
                return {
                    "link": d2["link"],
                    "title": d2.get("title") or f"YouTube ({video_id})",
                    "duration": round(float(d2.get("duration") or 0)),
                    "channelTitle": d2.get("channelTitle") or "YouTube",
                    "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                }
            elif status == "processing":
                await asyncio.sleep(1.5)
            else:
                msg = d2.get("msg") or d2.get("message") or str(d2)
                raise RuntimeError(f"RapidAPI error: {msg}")

        raise RuntimeError("Timed out waiting for audio stream extraction.")


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = BASE_DIR / "templates" / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.get("/api/playlist")
async def get_playlist():
    items = load_playlist()
    return {"tracks": items}


@app.post("/api/convert")
async def convert_video(payload: ConvertRequest):
    video_id = extract_youtube_id(payload.url)
    if not video_id:
        raise HTTPException(
            status_code=400,
            detail="Invalid YouTube link or video ID. Please paste a valid YouTube URL or 11-character video ID.",
        )

    try:
        data = await query_ytstream_audio(video_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Conversion failed: {exc}")

    direct_link = data["link"]
    title = data["title"]
    duration = data["duration"]
    uploader = data["channelTitle"]
    thumbnail = data["thumbnail"]

    items = load_playlist()
    existing = next((t for t in items if t.get("youtube_id") == video_id), None)

    if existing:
        existing["audio_url"] = direct_link
        existing["title"] = title
        existing["duration"] = duration
        existing["uploader"] = uploader
        existing["thumbnail"] = thumbnail
        save_playlist(items)
        return {"status": "ok", "track": existing, "tracks": items}

    track = {
        "id": uuid.uuid4().hex[:10],
        "youtube_id": video_id,
        "title": title,
        "uploader": uploader,
        "duration": duration,
        "thumbnail": thumbnail,
        "audio_url": direct_link,
        "source_url": f"https://www.youtube.com/watch?v={video_id}",
        "created_at": int(time.time()),
    }

    items.insert(0, track)
    save_playlist(items)

    return {"status": "ok", "track": track, "tracks": items}


@app.post("/api/progress/{track_id}")
async def update_track_progress(track_id: str, payload: ProgressRequest):
    items = load_playlist()
    track: Optional[dict] = next((t for t in items if t["id"] == track_id), None)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")

    track["last_position"] = max(0.0, float(payload.position))
    save_playlist(items)
    return {"status": "ok", "last_position": track["last_position"]}


@app.get("/api/refresh/{track_id}")
async def refresh_track_link(track_id: str):
    items = load_playlist()
    track: Optional[dict] = next((t for t in items if t["id"] == track_id), None)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")

    video_id = track.get("youtube_id")
    if not video_id:
        raise HTTPException(status_code=400, detail="Track missing youtube_id")

    try:
        data = await query_ytstream_audio(video_id)
        track["audio_url"] = data["link"]
        save_playlist(items)
        return {"status": "ok", "track": track}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to refresh link: {exc}")


@app.get("/api/download/{track_id}")
async def download_track(track_id: str):
    items = load_playlist()
    track: Optional[dict] = next((t for t in items if t["id"] == track_id), None)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")

    video_id = track.get("youtube_id")
    if video_id:
        try:
            data = await query_ytstream_audio(video_id)
            track["audio_url"] = data["link"]
            save_playlist(items)
        except Exception:
            pass

    return RedirectResponse(url=track["audio_url"])


@app.delete("/api/playlist/{track_id}")
async def delete_track(track_id: str):
    items = load_playlist()
    updated = [t for t in items if t["id"] != track_id]
    save_playlist(updated)
    return {"status": "ok", "tracks": updated}


@app.get("/health")
async def health():
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
