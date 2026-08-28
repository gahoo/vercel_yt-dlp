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
from fastapi.responses import JSONResponse, RedirectResponse

# Add project root to path so we can import lib/

from lib.ytdlp_helper import extract_info, get_subtitle_content, get_stream_url, resolve_format
from lib.r2_helper import (
    get_object_metadata, generate_presigned_url, stream_upload_to_r2, get_s3_client,
    upload_cookies_to_r2, get_cookies_status, delete_cookies_from_r2,
    delete_object, delete_objects_by_prefix,
    upload_metadata_to_r2, get_metadata_from_r2
)

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
    allow_methods=["GET", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "X-API-Key", "Content-Type"],
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
        "endpoints": ["/api/info", "/api/subtitles", "/api/stream", "/api/download", "/api/transcribe", "/api/cookies"],
        "docs": "/docs",
    }

@app.get("/api/info", dependencies=[Depends(verify_api_key)])
def api_info(
    url: str = Query(None, description="Video URL"),
    id: str = Query(None, description="Video ID (can be used instead of url)"),
    overwrite: bool = Query(False, description="Force refresh metadata cache"),
    include_formats: bool = Query(True, alias="formats", description="Whether to include the full formats list in the response"),
    player_client: str = Query(None, description="YouTube player client, e.g. 'ios', 'mediaconnect,ios,web'"),
):
    """Extract video metadata without downloading. Uses R2 cache to speed up requests."""
    target = url or id
    if not target:
        raise HTTPException(status_code=400, detail="Must provide 'url' or 'id'")
        
    bucket = os.environ.get("R2_BUCKET_NAME")
    
    # Try to extract video ID for caching
    import re
    extracted_id = id
    if not extracted_id and url:
        match = re.search(r'(?:v=|youtu\.be/|/v/|/embed/|/shorts/)([^&?]+)', url)
        if match:
            extracted_id = match.group(1)
            
    # Check cache
    if bucket and extracted_id and not overwrite:
        cached_meta = get_metadata_from_r2(extracted_id, bucket, max_age_hours=24)
        if cached_meta:
            cached_meta["_cache_hit"] = True
            if not include_formats and "formats" in cached_meta:
                del cached_meta["formats"]
            return JSONResponse(content=cached_meta)

    try:
        info = extract_info(target, player_client=player_client)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    formats = []
    for f in info.get("formats", []):
        fmt = {
            "format_id": f.get("format_id"),
            "ext": f.get("ext"),
            "resolution": f.get("resolution") or (f"{f.get('width')}x{f.get('height')}" if f.get("width") else None),
            "fps": f.get("fps"),
            "tbr": f.get("tbr"),
            "vbr": f.get("vbr"),
            "abr": f.get("abr"),
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "acodec": f.get("acodec"),
            "vcodec": f.get("vcodec"),
            "format_note": f.get("format_note"),
        }
        has_audio = f.get("acodec") and f.get("acodec") != "none"
        has_video = f.get("vcodec") and f.get("vcodec") != "none"
        if has_audio and has_video: fmt["type"] = "video+audio"
        elif has_audio: fmt["type"] = "audio"
        elif has_video: fmt["type"] = "video"
        else: fmt["type"] = "unknown"
        formats.append(fmt)

    result = {
        "id": info.get("id"),
        "title": info.get("title"),
        "duration": info.get("duration"),
        "formats": formats,
        "subtitles": list(info.get("subtitles", {}).keys()),
    }
    
    # Save to cache ALWAYS with formats
    if bucket and extracted_id:
        upload_metadata_to_r2(extracted_id, result, bucket)
        
    if not include_formats:
        del result["formats"]
        
    return JSONResponse(content=result)

@app.get("/api/subtitles", dependencies=[Depends(verify_api_key)])
def api_subtitles(
    url: str = Query(None, description="Video URL"),
    id: str = Query(None, description="Video ID (can be used instead of url)"),
    lang: str = Query("en"),
    format: str = Query("vtt"),
    auto: bool = Query(True),
    player_client: str = Query(None, description="YouTube player client, e.g. 'ios', 'mediaconnect,ios,web'"),
):
    """Get subtitle/caption content."""
    target = url or id
    if not target:
        raise HTTPException(status_code=400, detail="Must provide 'url' or 'id'")

    if format not in ("vtt", "srt", "json3"):
        raise HTTPException(status_code=400, detail="Use vtt, srt, or json3.")
    try:
        content = get_subtitle_content(target, lang=lang, fmt=format, auto=auto, player_client=player_client)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    if content is None:
        raise HTTPException(status_code=404, detail=f"No subtitles found for '{lang}'")

    ctype = {"vtt": "text/vtt", "srt": "text/plain", "json3": "application/json"}
    return Response(content=content, media_type=f"{ctype[format]}; charset=utf-8")

@app.get("/api/stream", dependencies=[Depends(verify_api_key)])
def api_stream(
    url: str = Query(None, description="Video URL"),
    id: str = Query(None, description="Video ID (can be used instead of url)"),
    type: str = Query("audio", description="'audio', 'video', or 'both' (ignored if format is an ID)"),
    quality: str = Query("worst", description="Legacy quality selector (e.g. 'worst', 'bestaudio')"),
    format: str = Query(None, description="Format selector (e.g. '140', overrides 'quality')"),
    player_client: str = Query(None, description="YouTube player client, e.g. 'ios', 'mediaconnect,ios,web'"),
):
    """Get direct stream URL(s) (prefers https over m3u8)."""
    target = url or id
    if not target:
        raise HTTPException(status_code=400, detail="Must provide 'url' or 'id'")

    target_format = format or quality

    try:
        result = get_stream_url(target, type_=type, quality=target_format, player_client=player_client)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not result.get("url"):
        raise HTTPException(status_code=404, detail="Could not extract stream URL")
    return result

# ---------------------------------------------------------------------------
# Routes - R2 Upload & Transcription
# ---------------------------------------------------------------------------

@app.get("/api/download", dependencies=[Depends(verify_api_key)])
def api_download(
    url: str = Query(None, description="Video URL"),
    id: str = Query(None, description="Video ID (can be used instead of url)"),
    quality: str = Query("bestaudio", description="Legacy quality selector (e.g. 'worst', 'bestaudio')"),
    format: str = Query(None, description="Exact yt-dlp format ID (e.g. '140'). Overrides 'quality'."),
    player_client: str = Query("default", description="Client to bypass age-gate/bot-blocks (e.g. 'ios', 'web')"),
    overwrite: bool = Query(False, description="Force re-download and overwrite R2 cache"),
    redirect: bool = Query(False, description="Redirect directly to the R2 url instead of returning JSON"),
    r2stream: bool = Query(False, description="Use true memory-to-memory streaming (experimental)"),
):
    """
    Stream download audio from YouTube and upload directly to R2.
    Returns a pre-signed URL for the client to download the file.
    If the file already exists in R2, returns the URL immediately.
    When redirect=true, responds with a 302 redirect for direct download.
    """
    target = url or id
    if not target:
        raise HTTPException(status_code=400, detail="Must provide 'url' or 'id'")

    target_format = format or quality

    bucket = os.environ.get("R2_BUCKET_NAME")
    if not bucket:
        raise HTTPException(status_code=500, detail="R2_BUCKET_NAME not configured")

    import re
    extracted_id = id
    if not extracted_id and url:
        match = re.search(r'(?:v=|youtu\.be/|/v/|/embed/|/shorts/)([^&?]+)', url)
        if match:
            extracted_id = match.group(1)

    def make_response(status_val, presigned_url, extra_meta=None, uploaded_at=None):
        resp = {
            "status": status_val,
            "id": video_id,
            "title": info.get("title") if info else None,
            "format_id": info.get("format_id") if info else None,
            "ext": ext,
            "filesize": (info.get("filesize") or info.get("filesize_approx")) if info else None,
            "key": object_key,
            "url": presigned_url,
            "uploaded_at": uploaded_at
        }
        if extra_meta:
            resp.update(extra_meta)
        return JSONResponse(content=resp, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

    import urllib.parse
    
    def get_cd(title_val, ext_val):
        """Generate ContentDisposition string for forcing filename."""
        if not title_val or not ext_val:
            return None
        safe_name = urllib.parse.quote(f"{title_val}.{ext_val}")
        return f"attachment; filename*=UTF-8''{safe_name}"

    is_specific_format = target_format not in ("bestaudio", "worst", "best", "bestvideo")
    
    # 1. Precise Media Cache Probe
    if not overwrite and extracted_id and bucket:
        cached_meta = get_metadata_from_r2(extracted_id, bucket)
        if cached_meta:
            resolved = resolve_format(cached_meta.get("formats", []), target_format)
            if resolved:
                r_format_id = resolved.get("format_id")
                r_ext = resolved.get("ext", "m4a")
                probe_key = f"media/{extracted_id}_{r_format_id}"
                
                meta_data = get_object_metadata(bucket, probe_key)
                if meta_data:
                    # R2 object metadata dict keys are lowercase
                    title_encoded = meta_data["metadata"].get("title")
                    title = urllib.parse.unquote(title_encoded) if title_encoded else cached_meta.get("title")
                    
                    cd = get_cd(title, r_ext)
                    presigned = generate_presigned_url(bucket, probe_key, response_content_disposition=cd)
                    if redirect:
                        return RedirectResponse(url=presigned, status_code=302)
                    return JSONResponse(content={
                        "status": "cached",
                        "id": extracted_id,
                        "title": title,
                        "format_id": r_format_id,
                        "ext": r_ext,
                        "filesize": meta_data.get("filesize") or resolved.get("filesize"),
                        "key": probe_key,
                        "url": presigned,
                        "uploaded_at": meta_data.get("uploaded_at"),
                        "fast_cache_hit": True
                    }, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
        elif is_specific_format:
            # FALLBACK: No global metadata cache, but format is specific (e.g. 140).
            # Now we don't need a loop! Just one exact probe.
            probe_key = f"media/{extracted_id}_{target_format}"
            meta_data = get_object_metadata(bucket, probe_key)
            if meta_data:
                title_encoded = meta_data["metadata"].get("title")
                title = urllib.parse.unquote(title_encoded) if title_encoded else None
                r_ext = meta_data["metadata"].get("ext", "unknown")
                
                cd = get_cd(title, r_ext)
                presigned = generate_presigned_url(bucket, probe_key, response_content_disposition=cd)
                if redirect:
                    return RedirectResponse(url=presigned, status_code=302)
                return JSONResponse(content={
                    "status": "cached",
                    "id": extracted_id,
                    "title": title,
                    "format_id": target_format,
                    "ext": r_ext,
                    "filesize": meta_data.get("filesize"),
                    "key": probe_key,
                    "url": presigned,
                    "uploaded_at": meta_data.get("uploaded_at"),
                    "fast_cache_hit": "fallback"
                }, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

    try:
        info = get_stream_url(target, type_="audio", quality=target_format, player_client=player_client)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to get stream: {e}")

    audio_url = info.get("url")
    if not audio_url or "manifest" in audio_url or info.get("protocol") == "m3u8_native":
        raise HTTPException(status_code=400, detail="Direct HTTPS URL required, got manifest. Try different format.")

    video_id = info.get("id", "audio")
    ext = info.get("ext", "m4a")
    
    resolved_format_id = info.get("format_id") or target_format.replace("/", "_")
    object_key = f"media/{video_id}_{resolved_format_id}"
    
    content_type = "audio/mp4" if ext == "m4a" else ("audio/webm" if ext == "webm" else "application/octet-stream")

    title = info.get("title", video_id)
    cd = get_cd(title, ext)

    # 1. Check if exists (Cache hit)
    if not overwrite:
        meta_data = get_object_metadata(bucket, object_key)
        if meta_data:
            presigned = generate_presigned_url(bucket, object_key, response_content_disposition=cd)
            if redirect:
                return RedirectResponse(url=presigned, status_code=302)
            return make_response("cached", presigned, uploaded_at=meta_data.get("uploaded_at"))

    # 2. Stream upload (Cache miss)
    try:
        s3_meta = {
            "title": urllib.parse.quote(title),
            "ext": ext
        }
        stream_upload_to_r2(audio_url, bucket, object_key, content_type, extra_metadata=s3_meta, r2stream=r2stream)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload to R2 failed: {e}")

    # 3. Generate URL
    presigned = generate_presigned_url(bucket, object_key, response_content_disposition=cd)
    
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    
    if redirect:
        return RedirectResponse(url=presigned, status_code=302)
    return make_response("uploaded", presigned, uploaded_at=now_iso)


@app.get("/api/transcribe", dependencies=[Depends(verify_api_key)])
def api_transcribe(
    key: str = Query(None, description="R2 object key (e.g., audio/123_140.m4a)"),
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
            # Auto-prepend media/ if it's a simplified key like "dQw4w9WgXcQ_140"
            if not key.startswith("media/") and not key.startswith("audio/") and not key.startswith("video/") and "/" not in key:
                key = f"media/{key}"
                
            bucket = os.environ.get("R2_BUCKET_NAME")
            client = get_s3_client()
            try:
                s3_obj = client.get_object(Bucket=bucket, Key=key)
                stream = s3_obj["Body"]
            except Exception as e:
                raise HTTPException(status_code=404, detail=f"Failed to get object from R2: {e}")
            filename = os.path.basename(key) + ".m4a" # Whisper expects an extension to infer type
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

# ---------------------------------------------------------------------------
# Routes - Cookie Management
# ---------------------------------------------------------------------------

@app.put("/api/cookies", dependencies=[Depends(verify_api_key)])
async def api_cookies_upload(
    request: Request,
    expires: str = Query(None, description="TTL like '4h', '30m', '1d', or ISO datetime. Auto-detects from cookie content if omitted."),
):
    """
    Upload Netscape-format cookies for YouTube authentication.
    Send the cookie file content as the raw request body.

    The expiry is determined by (in order of priority):
    1. The `expires` query parameter (e.g. '4h', '1d')
    2. Auto-detection from the earliest YouTube auth cookie expiry in the file
    """
    bucket = os.environ.get("R2_BUCKET_NAME")
    if not bucket:
        raise HTTPException(status_code=500, detail="R2_BUCKET_NAME not configured")

    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Request body is empty. Send Netscape-format cookie text.")

    cookie_content = body.decode("utf-8")

    # Basic validation: check it looks like a Netscape cookie file
    lines = [l.strip() for l in cookie_content.splitlines() if l.strip() and not l.strip().startswith("#")]
    if not lines:
        raise HTTPException(status_code=400, detail="No valid cookie lines found. Expected Netscape cookie format.")

    try:
        result = upload_cookies_to_r2(cookie_content, bucket, expires=expires)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to upload cookies: {e}")

    return result


@app.get("/api/cookies", dependencies=[Depends(verify_api_key)])
def api_cookies_status():
    """
    Check the status of stored cookies (uploaded_at, expires_at, expired).
    Does not return cookie content for security.
    """
    bucket = os.environ.get("R2_BUCKET_NAME")
    if not bucket:
        raise HTTPException(status_code=500, detail="R2_BUCKET_NAME not configured")

    status = get_cookies_status(bucket)
    if not status:
        return {"status": "none", "message": "No cookies stored"}

    return {"status": "active" if not status["expired"] else "expired", **status}


@app.delete("/api/cookies", dependencies=[Depends(verify_api_key)])
def api_cookies_delete():
    """Delete stored cookies from R2."""
    bucket = os.environ.get("R2_BUCKET_NAME")
    if not bucket:
        raise HTTPException(status_code=500, detail="R2_BUCKET_NAME not configured")

    try:
        delete_cookies_from_r2(bucket)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete cookies: {e}")

    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# Routes - Cache Management
# ---------------------------------------------------------------------------

@app.delete("/api/cache", dependencies=[Depends(verify_api_key)])
def api_cache_delete(
    key: str = Query(None, description="Exact object key to delete (e.g. 'audio/dQw4w9WgXcQ_140.m4a')"),
    prefix: str = Query(None, description="Prefix to bulk delete (e.g. 'audio/dQw4w9WgXcQ')"),
):
    """
    Delete cached media from R2.
    Provide either `key` for exact deletion or `prefix` for bulk deletion.
    """
    if not key and not prefix:
        raise HTTPException(status_code=400, detail="Must provide either 'key' or 'prefix'")
        
    bucket = os.environ.get("R2_BUCKET_NAME")
    if not bucket:
        raise HTTPException(status_code=500, detail="R2_BUCKET_NAME not configured")

    try:
        if prefix:
            deleted = delete_objects_by_prefix(bucket, prefix)
            return {"status": "deleted", "count": len(deleted), "keys": deleted}
        else:
            success = delete_object(bucket, key)
            if not success:
                # delete_object returns True even if the object doesn't exist, 
                # but might return False on actual connection error.
                raise HTTPException(status_code=500, detail="Failed to delete object")
            return {"status": "deleted", "key": key}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Delete failed: {e}")
