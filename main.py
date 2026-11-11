import asyncio
import datetime
import hashlib
import hmac
import json
import os
import re
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional
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

# Cloudflare R2 Configuration
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID", "af8651613e2e25e240c53c5f963af33b")
R2_HOST = f"{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
R2_ENDPOINT = f"https://{R2_HOST}"
R2_BUCKET = os.getenv("R2_BUCKET", "youtube-mp3-audio")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "6c8169cee48b15d863b9617bb39aef05")
R2_SECRET_ACCESS_KEY = os.getenv(
    "R2_SECRET_ACCESS_KEY",
    "faa941a19b0b544c0872d39c7992207b048a908579b99f6b48b6e407a1981ba1",
)
R2_PUBLIC_URL = os.getenv(
    "R2_PUBLIC_URL",
    "https://pub-fae585a35e904eef917809546d0ca06a.r2.dev",
).rstrip("/")
WARP_PROXY = os.getenv("WARP_PROXY", "socks5://127.0.0.1:40000")

DATA_DIR.mkdir(parents=True, exist_ok=True)

# Active background R2 upload tasks keyed by track_id
ACTIVE_R2_TASKS: Dict[str, asyncio.Task] = {}


class ConvertRequest(BaseModel):
    url: str


class ProgressRequest(BaseModel):
    position: float


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def sign_r2_request(
    method: str,
    key_name: str,
    query_params: Optional[dict] = None,
    body: bytes = b"",
    content_type: str = "",
    payload_hash_override: Optional[str] = None,
) -> tuple[str, dict]:
    """Generate AWS SigV4 signed URL and headers for Cloudflare R2 without external SDKs."""
    query_params = query_params or {}
    t = datetime.datetime.now(datetime.timezone.utc)
    amz_date = t.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = t.strftime("%Y%m%d")
    canonical_uri = f"/{R2_BUCKET}/{key_name}"

    sorted_q = sorted(query_params.items())
    canonical_querystring = "&".join(
        f"{urllib.parse.quote(str(k), safe='')}={urllib.parse.quote(str(v), safe='')}"
        for k, v in sorted_q
    )

    payload_hash = payload_hash_override or hashlib.sha256(body).hexdigest()
    headers_to_sign = {
        "host": R2_HOST,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    if content_type:
        headers_to_sign["content-type"] = content_type

    sorted_h = sorted(headers_to_sign.items())
    canonical_headers = "".join(f"{k}:{v}\n" for k, v in sorted_h)
    signed_headers = ";".join(k for k, _ in sorted_h)

    canonical_request = (
        f"{method}\n{canonical_uri}\n{canonical_querystring}\n"
        f"{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )
    credential_scope = f"{date_stamp}/auto/s3/aws4_request"
    req_hash = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    string_to_sign = f"AWS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n{req_hash}"

    k_date = _hmac_sha256(("AWS4" + R2_SECRET_ACCESS_KEY).encode("utf-8"), date_stamp)
    k_region = _hmac_sha256(k_date, "auto")
    k_service = _hmac_sha256(k_region, "s3")
    k_signing = _hmac_sha256(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    auth = (
        f"AWS4-HMAC-SHA256 Credential={R2_ACCESS_KEY_ID}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    req_headers = {
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
        "Authorization": auth,
    }
    if content_type:
        req_headers["Content-Type"] = content_type

    url = f"{R2_ENDPOINT}{canonical_uri}"
    if canonical_querystring:
        url += f"?{canonical_querystring}"
    return url, req_headers


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


def update_track_fields(track_id: str, **fields) -> Optional[dict]:
    items = load_playlist()
    track = next((t for t in items if t["id"] == track_id), None)
    if not track:
        return None
    track.update(fields)
    save_playlist(items)
    return track


async def query_ytstream_audio(video_id: str) -> dict:
    """
    Uses YTStream (ytstream-download-youtube-videos.p.rapidapi.com) to extract direct
    audio stream. Prefers smallest audio stream (itag 249 -> 250 -> 140 -> 251) as default.
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
                # Sort by contentLength / bitrate ascending so smallest size is chosen first
                # (itag 249 ~50kbps opus is ~1.2GB for 60h vs 3.36GB for itag 140)
                chosen = (
                    next((f for f in audio_formats if f.get("itag") == 249), None)
                    or next((f for f in audio_formats if f.get("itag") == 250), None)
                    or next((f for f in audio_formats if f.get("itag") == 140), None)
                    or next((f for f in audio_formats if f.get("itag") == 251), None)
                    or (audio_formats[0] if audio_formats else None)
                )
                if chosen:
                    mime_raw = (chosen.get("mimeType") or "audio/webm").split(";")[0].strip()
                    ext = "m4a" if "mp4" in mime_raw else "webm"
                    thumbs = data.get("thumbnail") or []
                    thumb_url = (
                        thumbs[-1].get("url")
                        if isinstance(thumbs, list) and thumbs
                        else f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
                    )
                    return {
                        "link": chosen["url"],
                        "mime_type": mime_raw,
                        "ext": ext,
                        "content_length": int(chosen.get("contentLength") or 0),
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
                    "mime_type": "audio/mpeg",
                    "ext": "mp3",
                    "content_length": int(d2.get("filesize") or 0),
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


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


async def _upload_part_file_to_r2(
    r2_key: str,
    upload_id: str,
    part_number: int,
    part_file: Path,
) -> str:
    """Uploads a part file from disk to R2 using curl so 0 MB of audio bytes are held in Python RAM."""
    payload_hash = await asyncio.to_thread(_sha256_file, part_file)
    for attempt in range(4):
        try:
            url, hdrs = sign_r2_request(
                "PUT",
                r2_key,
                {"partNumber": str(part_number), "uploadId": upload_id},
                payload_hash_override=payload_hash,
            )
            cmd = [
                "curl",
                "-s",
                "-i",
                "--max-time",
                "60",
                "-X",
                "PUT",
                "-H",
                f"x-amz-date: {hdrs['x-amz-date']}",
                "-H",
                f"x-amz-content-sha256: {hdrs['x-amz-content-sha256']}",
                "-H",
                f"Authorization: {hdrs['Authorization']}",
                "--upload-file",
                str(part_file),
                url,
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_data, _ = await asyncio.wait_for(proc.communicate(), timeout=65.0)
            resp_headers = stdout_data.decode("utf-8", errors="replace")
            if "200 OK" in resp_headers or "HTTP/2 200" in resp_headers or "HTTP/1.1 200" in resp_headers:
                for line in resp_headers.splitlines():
                    if line.lower().startswith("etag:"):
                        etag = line.split(":", 1)[1].strip()
                        if etag:
                            return etag
        except Exception as exc:
            print(f"[R2] Part {part_number} upload attempt {attempt + 1} error: {exc}")
        await asyncio.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Failed to upload part {part_number} to R2 after 4 attempts")


async def background_upload_to_r2(
    track_id: str,
    video_id: str,
    source_stream_url: str,
    r2_key: str,
    mime_type: str,
    expected_size: int = 0,
) -> None:
    """
    Streams audio from YouTube CDN in 5 MB chunks via a temporary 5 MB file in /tmp
    using native curl processes -> < 1 MB Python RAM used (prevents OOM kills on low-RAM VPS!).
    """
    PART_SIZE = 5 * 1024 * 1024  # 5 MB (exact S3 minimum part size, minimal temp footprint)
    upload_id: Optional[str] = None
    tmp_part_file = Path(f"/tmp/r2_part_{track_id}.tmp")

    try:
        update_track_fields(track_id, r2_status="uploading", r2_progress=0)

        timeout_cfg = httpx.Timeout(connect=30.0, read=60.0, write=60.0, pool=30.0)
        async with httpx.AsyncClient(timeout=timeout_cfg, follow_redirects=True) as client:
            # 1. Initiate Multipart Upload on Cloudflare R2
            init_url, init_hdrs = sign_r2_request("POST", r2_key, {"uploads": ""}, b"", mime_type)
            init_resp = await client.post(init_url, content=b"", headers=init_hdrs)
            if init_resp.status_code != 200:
                raise RuntimeError(f"R2 initiate multipart failed ({init_resp.status_code}): {init_resp.text}")

            root = ET.fromstring(init_resp.text)
            ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
            upload_id_el = root.find("s3:UploadId", ns)
            if upload_id_el is None or not upload_id_el.text:
                raise RuntimeError("Missing UploadId from R2 response")
            upload_id = upload_id_el.text

            parts_etags: List[tuple[int, str]] = []
            part_number = 1

            user_agent = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            )
            socks_proxy = WARP_PROXY.replace("socks5://", "socks5h://")

            # Probe total size using curl
            total_size = expected_size
            if not total_size:
                probe_proc = await asyncio.create_subprocess_exec(
                    "curl", "-s", "-L", "-I", "--proxy", socks_proxy, "--max-time", "20",
                    "-r", "0-0", "-A", user_agent, source_stream_url,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                p_out, _ = await probe_proc.communicate()
                for line in p_out.decode("utf-8", errors="replace").splitlines():
                    if line.lower().startswith("content-range:") and "/" in line:
                        try:
                            total_size = int(line.split("/")[-1].strip())
                        except ValueError:
                            pass
            if not total_size:
                raise RuntimeError("Could not determine total audio stream byte length")

            offset = 0
            while offset < total_size:
                end_byte = min(offset + PART_SIZE - 1, total_size - 1)
                expected_chunk_len = end_byte - offset + 1

                # Download 5 MB range directly to /tmp/r2_part_<id>.tmp (0 MB in Python RAM!)
                downloaded_ok = False
                for attempt in range(5):
                    try:
                        if tmp_part_file.exists():
                            tmp_part_file.unlink()
                        proc = await asyncio.create_subprocess_exec(
                            "curl",
                            "-s",
                            "-L",
                            "--proxy",
                            socks_proxy,
                            "--max-time",
                            "45",
                            "-r",
                            f"{offset}-{end_byte}",
                            "-A",
                            user_agent,
                            "-o",
                            str(tmp_part_file),
                            source_stream_url,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        await asyncio.wait_for(proc.communicate(), timeout=50.0)
                        if proc.returncode == 0 and tmp_part_file.exists() and tmp_part_file.stat().st_size == expected_chunk_len:
                            downloaded_ok = True
                            break
                    except Exception as chunk_err:
                        print(f"[R2] Range {offset}-{end_byte} attempt {attempt + 1} error: {repr(chunk_err)}")
                    await asyncio.sleep(2.0 * (attempt + 1))

                if not downloaded_ok:
                    raise RuntimeError(f"Failed to fetch byte range {offset}-{end_byte}")

                etag = await _upload_part_file_to_r2(r2_key, upload_id, part_number, tmp_part_file)
                parts_etags.append((part_number, etag))
                offset += expected_chunk_len
                part_number += 1

                pct = min(99, round((offset / total_size) * 100))
                update_track_fields(track_id, r2_status="uploading", r2_progress=pct)
                await asyncio.sleep(0.05)

            total_uploaded = offset

            # 3. Complete Multipart Upload
            parts_xml = "".join(
                f"<Part><PartNumber>{pnum}</PartNumber><ETag>{etag}</ETag></Part>"
                for pnum, etag in parts_etags
            )
            complete_body = f"<CompleteMultipartUpload>{parts_xml}</CompleteMultipartUpload>".encode("utf-8")
            comp_url, comp_hdrs = sign_r2_request(
                "POST",
                r2_key,
                {"uploadId": upload_id},
                complete_body,
                "application/xml",
            )
            comp_resp = await client.post(comp_url, content=complete_body, headers=comp_hdrs)
            if comp_resp.status_code != 200:
                raise RuntimeError(f"R2 complete multipart failed ({comp_resp.status_code}): {comp_resp.text}")

            r2_public_link = f"{R2_PUBLIC_URL}/{r2_key}"
            update_track_fields(
                track_id,
                r2_key=r2_key,
                r2_url=r2_public_link,
                audio_url=r2_public_link,
                r2_status="ready",
                r2_progress=100,
            )
            print(f"[R2] Successfully backed up {video_id} -> {r2_public_link} ({total_uploaded} bytes)")

    except Exception as exc:
        print(f"[R2] Background upload failed for {track_id} ({video_id}): {exc}")
        update_track_fields(track_id, r2_status="failed", r2_error=str(exc))
        if upload_id:
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    abort_url, abort_hdrs = sign_r2_request("DELETE", r2_key, {"uploadId": upload_id})
                    await client.delete(abort_url, headers=abort_hdrs)
            except Exception:
                pass
    finally:
        if tmp_part_file.exists():
            try:
                tmp_part_file.unlink()
            except OSError:
                pass
        ACTIVE_R2_TASKS.pop(track_id, None)


async def delete_from_r2(r2_key: str) -> None:
    if not r2_key:
        return
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            url, hdrs = sign_r2_request("DELETE", r2_key)
            await client.delete(url, headers=hdrs)
    except Exception as exc:
        print(f"[R2] Failed to delete {r2_key}: {exc}")


def trigger_r2_backup_if_needed(track: dict, stream_url: str, mime_type: str = "audio/webm", expected_size: int = 0) -> None:
    track_id = track["id"]
    if track.get("r2_status") == "ready" and track.get("r2_url"):
        return
    if track_id in ACTIVE_R2_TASKS and not ACTIVE_R2_TASKS[track_id].done():
        return

    ext = track.get("ext") or ("m4a" if "mp4" in mime_type else "webm")
    r2_key = track.get("r2_key") or f"{track['youtube_id']}_{track_id}.{ext}"
    task = asyncio.create_task(
        background_upload_to_r2(
            track_id=track_id,
            video_id=track["youtube_id"],
            source_stream_url=stream_url,
            r2_key=r2_key,
            mime_type=mime_type,
            expected_size=expected_size,
        )
    )
    ACTIVE_R2_TASKS[track_id] = task


@app.on_event("startup")
async def retry_pending_r2_backups_on_startup():
    """Automatically retry/resume R2 backup for any track in playlist that hasn't finished backing up."""
    items = load_playlist()
    for track in items:
        if track.get("r2_status") != "ready" and track.get("audio_url"):
            ext = track.get("ext") or "webm"
            mime = "audio/mp4" if ext == "m4a" else ("audio/mpeg" if ext == "mp3" else "audio/webm")
            trigger_r2_backup_if_needed(track, track["audio_url"], mime)


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

    items = load_playlist()
    existing = next((t for t in items if t.get("youtube_id") == video_id), None)

    # If already backed up on Cloudflare R2, return immediately without calling RapidAPI!
    if existing and existing.get("r2_status") == "ready" and existing.get("r2_url"):
        existing["audio_url"] = existing["r2_url"]
        save_playlist(items)
        return {"status": "ok", "track": existing, "tracks": items}

    try:
        data = await query_ytstream_audio(video_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Conversion failed: {exc}")

    direct_link = data["link"]
    title = data["title"]
    duration = data["duration"]
    uploader = data["channelTitle"]
    thumbnail = data["thumbnail"]
    mime_type = data.get("mime_type", "audio/webm")
    ext = data.get("ext", "webm")
    content_length = data.get("content_length", 0)

    if existing:
        existing["audio_url"] = existing.get("r2_url") or direct_link
        existing["title"] = title
        existing["duration"] = duration
        existing["uploader"] = uploader
        existing["thumbnail"] = thumbnail
        existing["ext"] = ext
        save_playlist(items)
        trigger_r2_backup_if_needed(existing, direct_link, mime_type, content_length)
        return {"status": "ok", "track": existing, "tracks": items}

    track_id = uuid.uuid4().hex[:10]
    r2_key = f"{video_id}_{track_id}.{ext}"
    track = {
        "id": track_id,
        "youtube_id": video_id,
        "title": title,
        "uploader": uploader,
        "duration": duration,
        "thumbnail": thumbnail,
        "ext": ext,
        "audio_url": direct_link,
        "r2_key": r2_key,
        "r2_url": None,
        "r2_status": "uploading",
        "r2_progress": 0,
        "source_url": f"https://www.youtube.com/watch?v={video_id}",
        "created_at": int(time.time()),
    }

    items.insert(0, track)
    save_playlist(items)

    # Start background upload to Cloudflare R2 while user immediately listens to direct_link!
    trigger_r2_backup_if_needed(track, direct_link, mime_type, content_length)

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

    # If the background upload to Cloudflare R2 is ready, switch to permanent R2 link (0 RapidAPI calls!)
    if track.get("r2_status") == "ready" and track.get("r2_url"):
        track["audio_url"] = track["r2_url"]
        save_playlist(items)
        return {"status": "ok", "track": track}

    video_id = track.get("youtube_id")
    if not video_id:
        raise HTTPException(status_code=400, detail="Track missing youtube_id")

    try:
        data = await query_ytstream_audio(video_id)
        track["audio_url"] = data["link"]
        save_playlist(items)
        trigger_r2_backup_if_needed(
            track,
            data["link"],
            data.get("mime_type", "audio/webm"),
            data.get("content_length", 0),
        )
        return {"status": "ok", "track": track}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to refresh link: {exc}")


@app.get("/api/download/{track_id}")
async def download_track(track_id: str):
    items = load_playlist()
    track: Optional[dict] = next((t for t in items if t["id"] == track_id), None)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")

    if track.get("r2_status") == "ready" and track.get("r2_url"):
        return RedirectResponse(url=track["r2_url"])

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
    track: Optional[dict] = next((t for t in items if t["id"] == track_id), None)

    # Cancel any in-progress upload task
    task = ACTIVE_R2_TASKS.pop(track_id, None)
    if task and not task.done():
        task.cancel()

    # Delete stored audio object from Cloudflare R2 to free up your 10GB quota
    if track and track.get("r2_key"):
        await delete_from_r2(track["r2_key"])

    updated = [t for t in items if t["id"] != track_id]
    save_playlist(updated)
    return {"status": "ok", "tracks": updated}


@app.get("/health")
async def health():
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
