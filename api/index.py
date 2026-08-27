"""
yt-dlp API — Vercel Serverless FastAPI Application.

Exposes yt-dlp capabilities (info extraction, subtitles, stream URLs,
streaming upload to R2, and Groq Whisper transcription).
"""

from __future__ import annotations

import os
import sys
import requests

from dotenv import load_dotenv
load_dotenv()  # Load .env variables for local development

from fastapi import FastAPI, Query, Request, Response, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# Add project root to path so we can import lib/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.ytdlp_helper import extract_info, get_subtitle_content, get_stream_url
from lib.r2_helper import check_object_exists, generate_presigned_url, stream_upload_to_r2, get_s3_client

# ---------------------------------------------------------------------------
# App & CORS
# ---------------------------------------------------------------------------

app = FastAPI(
    title="yt-dlp API",
    description="YouTube media extraction API with R2 streaming and Groq transcription.",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["Authorization", "X-API-Key"],
)

# ---------------------------------------------------------------------------
# API Key Authentication
# ---------------------------------------------------------------------------

API_KEY = os.environ.get("API_KEY", "")

async def verify_api_key(request: Request):
    """Validate API key from header or query param."""
    if not API_KEY:
        return
    if request.headers.get("X-API-Key") == API_KEY:
        return
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and auth[7:] == API_KEY:
        return
    if request.query_params.get("api_key") == API_KEY:
        return
    raise HTTPException(status_code=401, detail="Invalid or missing API key")

# ---------------------------------------------------------------------------
# Routes - Core yt-dlp
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return {
        "service": "yt-dlp API",
        "endpoints": ["/api/info", "/api/subtitles", "/api/stream", "/api/download", "/api/transcribe"],
        "docs": "/docs",
    }

@app.get("/api/info", dependencies=[Depends(verify_api_key)])
async def api_info(url: str = Query(...)):
    """Extract video metadata without downloading."""
    try:
        info = extract_info(url)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    formats = []
    for f in info.get("formats", []):
        fmt = {
            "format_id": f.get("format_id"),
            "ext": f.get("ext"),
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "acodec": f.get("acodec"),
            "vcodec": f.get("vcodec"),
        }
        has_audio = f.get("acodec") and f.get("acodec") != "none"
        has_video = f.get("vcodec") and f.get("vcodec") != "none"
        if has_audio and has_video: fmt["type"] = "video+audio"
        elif has_audio: fmt["type"] = "audio"
        elif has_video: fmt["type"] = "video"
        else: fmt["type"] = "unknown"
        formats.append(fmt)

    return {
        "title": info.get("title"),
        "duration": info.get("duration"),
        "formats": formats,
        "subtitles": list(info.get("subtitles", {}).keys()),
    }

@app.get("/api/subtitles", dependencies=[Depends(verify_api_key)])
async def api_subtitles(
    url: str = Query(...),
    lang: str = Query("en"),
    format: str = Query("vtt"),
    auto: bool = Query(True),
):
    """Get subtitle/caption content."""
    if format not in ("vtt", "srt", "json3"):
        raise HTTPException(status_code=400, detail="Use vtt, srt, or json3.")
    try:
        content = get_subtitle_content(url, lang=lang, fmt=format, auto=auto)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    if content is None:
        raise HTTPException(status_code=404, detail=f"No subtitles found for '{lang}'")

    ctype = {"vtt": "text/vtt", "srt": "text/plain", "json3": "application/json"}
    return Response(content=content, media_type=f"{ctype[format]}; charset=utf-8")

@app.get("/api/stream", dependencies=[Depends(verify_api_key)])
async def api_stream(
    url: str = Query(...),
    type: str = Query("audio"),
    quality: str = Query("worst"),
):
    """Get direct stream URL(s) (prefers https over m3u8)."""
    try:
        result = get_stream_url(url, type_=type, quality=quality)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not result.get("url"):
        raise HTTPException(status_code=404, detail="Could not extract stream URL")
    return result

# ---------------------------------------------------------------------------
# Routes - R2 Upload & Transcription
# ---------------------------------------------------------------------------

@app.get("/api/download", dependencies=[Depends(verify_api_key)])
async def api_download(
    url: str = Query(...),
    format: str = Query("bestaudio"),
):
    """
    Stream download audio from YouTube and upload directly to R2.
    Returns a pre-signed URL for the client to download the file.
    If the file already exists in R2, returns the URL immediately.
    """
    bucket = os.environ.get("R2_BUCKET_NAME")
    if not bucket:
        raise HTTPException(status_code=500, detail="R2_BUCKET_NAME not configured")

    try:
        info = get_stream_url(url, type_="audio", quality=format)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to get stream: {e}")

    audio_url = info.get("url")
    if not audio_url or "manifest" in audio_url or info.get("protocol") == "m3u8_native":
        raise HTTPException(status_code=400, detail="Direct HTTPS URL required, got manifest. Try different format.")

    video_id = info.get("id", "audio")
    ext = info.get("ext", "m4a")
    object_key = f"audio/{video_id}.{ext}"
    content_type = "audio/mp4" if ext == "m4a" else ("audio/webm" if ext == "webm" else "application/octet-stream")

    # 1. Check if exists (Cache hit)
    if check_object_exists(bucket, object_key):
        presigned = generate_presigned_url(bucket, object_key)
        return {"status": "cached", "key": object_key, "url": presigned}

    # 2. Stream upload (Cache miss)
    try:
        stream_upload_to_r2(audio_url, bucket, object_key, content_type)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload to R2 failed: {e}")

    # 3. Generate URL
    presigned = generate_presigned_url(bucket, object_key)
    return {"status": "uploaded", "key": object_key, "url": presigned}


@app.get("/api/transcribe", dependencies=[Depends(verify_api_key)])
async def api_transcribe(
    key: str = Query(None, description="R2 object key (e.g., audio/123.m4a)"),
    url: str = Query(None, description="Direct audio URL to stream from"),
    model: str = Query("whisper-large-v3", description="Groq Whisper model"),
):
    """
    Transcribe audio using Groq Whisper.
    Streams audio directly from R2 (using 'key') or a generic 'url' to the Groq API.
    """
    groq_api_key = os.environ.get("GROQ_API_KEY")
    if not groq_api_key:
        raise HTTPException(status_code=500, detail="GROQ_API_KEY not configured")

    if not key and not url:
        raise HTTPException(status_code=400, detail="Must provide either 'key' or 'url'")

    stream = None
    response_obj = None # To close requests response if URL was used
    
    try:
        if key:
            bucket = os.environ.get("R2_BUCKET_NAME")
            client = get_s3_client()
            try:
                s3_obj = client.get_object(Bucket=bucket, Key=key)
                stream = s3_obj["Body"]
            except Exception as e:
                raise HTTPException(status_code=404, detail=f"Failed to get object from R2: {e}")
            filename = os.path.basename(key)
        else:
            try:
                response_obj = requests.get(url, stream=True, timeout=10)
                response_obj.raise_for_status()
                stream = response_obj.raw
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Failed to fetch audio from URL: {e}")
            # Try to guess filename from URL or default
            filename = os.path.basename(url.split("?")[0]) or "audio.mp3"
            if "." not in filename: filename += ".mp3"

        # Prepare multipart stream for Groq API
        headers = {"Authorization": f"Bearer {groq_api_key}"}
        files = {
            "file": (filename, stream),
        }
        data = {
            "model": model,
            "response_format": "json"
        }

        # Send to Groq
        groq_resp = requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers=headers,
            files=files,
            data=data,
            timeout=120 # Groq is very fast, usually <10s even for long audio
        )
        
        if groq_resp.status_code != 200:
            raise HTTPException(status_code=groq_resp.status_code, detail=f"Groq API error: {groq_resp.text}")

        return groq_resp.json()

    finally:
        # Cleanup streams
        if stream and hasattr(stream, "close"):
            stream.close()
        if response_obj:
            response_obj.close()
