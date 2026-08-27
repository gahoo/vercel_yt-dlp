"""
yt-dlp API — Vercel Serverless FastAPI Application.

Exposes yt-dlp capabilities (info extraction, subtitles, stream URLs,
and proxied downloads) as a RESTful API deployed on Vercel.
"""

from __future__ import annotations

import os
import sys

from fastapi import FastAPI, Query, Request, Response, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

# Add project root to path so we can import lib/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.ytdlp_helper import extract_info, get_subtitle_content, get_stream_url, download_media

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="yt-dlp API",
    description="YouTube media extraction API powered by yt-dlp, deployed on Vercel.",
    version="1.0.0",
)

# ---------------------------------------------------------------------------
# CORS — allow any frontend/webpage to call this API
# ---------------------------------------------------------------------------

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
    """
    Validate API key from either:
      - Header: X-API-Key: <key>
      - Header: Authorization: Bearer <key>
      - Query param: ?api_key=<key>

    If API_KEY env var is not set, authentication is disabled (open access).
    """
    if not API_KEY:
        return  # No key configured → open access

    # Check X-API-Key header
    key = request.headers.get("X-API-Key")
    if key == API_KEY:
        return

    # Check Authorization: Bearer <key>
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and auth[7:] == API_KEY:
        return

    # Check query parameter
    key = request.query_params.get("api_key")
    if key == API_KEY:
        return

    raise HTTPException(status_code=401, detail="Invalid or missing API key")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/")
async def root():
    """Health check and API info."""
    return {
        "service": "yt-dlp API",
        "version": "1.0.0",
        "endpoints": ["/api/info", "/api/subtitles", "/api/stream", "/api/download"],
        "docs": "/docs",
    }


@app.get("/api/info", dependencies=[Depends(verify_api_key)])
async def api_info(
    url: str = Query(..., description="Video URL (e.g. https://youtube.com/watch?v=xxx)"),
):
    """
    Extract video metadata: title, duration, available formats, subtitles, thumbnails.
    Does not download the video.
    """
    try:
        info = extract_info(url)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Build a clean response with the most useful fields
    formats = []
    for f in info.get("formats", []):
        fmt = {
            "format_id": f.get("format_id"),
            "ext": f.get("ext"),
            "format_note": f.get("format_note"),
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "acodec": f.get("acodec"),
            "vcodec": f.get("vcodec"),
            "abr": f.get("abr"),
            "vbr": f.get("vbr"),
            "width": f.get("width"),
            "height": f.get("height"),
            "fps": f.get("fps"),
        }
        # Classify type
        has_audio = f.get("acodec") and f.get("acodec") != "none"
        has_video = f.get("vcodec") and f.get("vcodec") != "none"
        if has_audio and has_video:
            fmt["type"] = "video+audio"
        elif has_audio:
            fmt["type"] = "audio"
        elif has_video:
            fmt["type"] = "video"
        else:
            fmt["type"] = "unknown"
        formats.append(fmt)

    return {
        "title": info.get("title"),
        "duration": info.get("duration"),
        "uploader": info.get("uploader"),
        "upload_date": info.get("upload_date"),
        "view_count": info.get("view_count"),
        "thumbnail": info.get("thumbnail"),
        "description": info.get("description"),
        "webpage_url": info.get("webpage_url"),
        "formats": formats,
        "subtitles": {
            lang: [{"ext": s.get("ext"), "name": s.get("name")} for s in subs]
            for lang, subs in info.get("subtitles", {}).items()
        },
        "automatic_captions": list(info.get("automatic_captions", {}).keys()),
    }


@app.get("/api/subtitles", dependencies=[Depends(verify_api_key)])
async def api_subtitles(
    url: str = Query(..., description="Video URL"),
    lang: str = Query("en", description="Subtitle language code (e.g. en, zh-Hans, ja)"),
    format: str = Query("vtt", description="Subtitle format: vtt, srt, json3"),
    auto: bool = Query(True, description="Fall back to auto-generated captions if no manual subtitles"),
):
    """
    Get subtitle/caption content for a video in the specified language and format.
    """
    if format not in ("vtt", "srt", "json3"):
        raise HTTPException(status_code=400, detail=f"Unsupported subtitle format: {format}. Use vtt, srt, or json3.")

    try:
        content = get_subtitle_content(url, lang=lang, fmt=format, auto=auto)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    if content is None:
        raise HTTPException(status_code=404, detail=f"No subtitles found for language '{lang}'")

    # Set appropriate content type
    content_type_map = {
        "vtt": "text/vtt; charset=utf-8",
        "srt": "text/plain; charset=utf-8",
        "json3": "application/json; charset=utf-8",
    }

    return Response(
        content=content,
        media_type=content_type_map[format],
    )


@app.get("/api/stream", dependencies=[Depends(verify_api_key)])
async def api_stream(
    url: str = Query(..., description="Video URL"),
    type: str = Query("audio", description="Stream type: audio, video, both"),
    quality: str = Query("worst", description="Quality: worst, best, or a specific format_id"),
):
    """
    Get direct stream URL(s) for the video. The client can download directly
    from the returned URL.

    Default: worst quality audio (smallest, most compatible, no IP binding issues).
    """
    if type not in ("audio", "video", "both"):
        raise HTTPException(status_code=400, detail=f"Invalid type: {type}. Use audio, video, or both.")

    try:
        result = get_stream_url(url, type_=type, quality=quality)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not result.get("url"):
        raise HTTPException(status_code=404, detail="Could not extract stream URL for the given parameters")

    return result


@app.get("/api/download", dependencies=[Depends(verify_api_key)])
async def api_download(
    url: str = Query(..., description="Video URL"),
    format: str = Query("worstaudio", description="yt-dlp format selector (e.g. worstaudio, worst, bestaudio, or format_id)"),
):
    """
    Download media through the server and return the file directly.

    No ffmpeg, no transcoding — returns the raw stream as-is.
    Default format is 'worstaudio' (smallest audio file).

    Note: Vercel has a 4.5MB response body limit. Large files may fail.
    Use /api/stream to get a direct URL for larger files.
    """
    try:
        filepath, content_type, file_size = download_media(url, format_selector=format)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    filename = os.path.basename(filepath)

    return FileResponse(
        path=filepath,
        media_type=content_type,
        filename=filename,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )
