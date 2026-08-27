"""
yt-dlp helper utilities.

Wraps yt-dlp Python API for use in Vercel serverless functions.
All downloads use /tmp as the working directory (only writable path on Vercel).
"""

from __future__ import annotations

import os
import glob
import tempfile
from typing import Any

import yt_dlp

TMP_DIR = "/tmp"


def _base_opts() -> dict[str, Any]:
    """Base yt-dlp options shared across all operations."""
    return {
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        # Restrict to safe operations in serverless
        "noplaylist": True,
        "socket_timeout": 30,
    }


def extract_info(url: str) -> dict[str, Any]:
    """
    Extract video metadata without downloading.

    Returns a sanitized info dict containing title, duration, formats,
    subtitles, thumbnails, etc.
    """
    opts = _base_opts()
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return ydl.sanitize_info(info)


def get_subtitle_content(url: str, lang: str = "en", fmt: str = "vtt", auto: bool = True) -> str | None:
    """
    Download subtitle for a specific language and return its text content.

    Args:
        url: Video URL.
        lang: Language code (e.g. "en", "zh-Hans").
        fmt: Subtitle format ("vtt", "srt", "json3").
        auto: Whether to fall back to auto-generated captions.

    Returns:
        Subtitle text content, or None if not available.
    """
    # Use a unique temp directory per request to avoid collisions
    with tempfile.TemporaryDirectory(dir=TMP_DIR) as tmpdir:
        opts = _base_opts()
        opts.update({
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": auto,
            "subtitleslangs": [lang],
            "subtitlesformat": fmt,
            "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
        })

        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

        # Find the subtitle file — yt-dlp names it like {id}.{lang}.{fmt}
        pattern = os.path.join(tmpdir, f"*.{lang}.{fmt}")
        files = glob.glob(pattern)
        if not files:
            # Try broader pattern (auto-generated subs may have different naming)
            pattern = os.path.join(tmpdir, f"*.{fmt}")
            files = glob.glob(pattern)

        if files:
            with open(files[0], "r", encoding="utf-8") as f:
                return f.read()

        return None


def get_stream_url(url: str, type_: str = "audio", quality: str = "worst") -> dict[str, Any]:
    """
    Extract direct stream URL(s) without downloading.

    Prefers HTTPS direct URLs over HLS/DASH manifests, which are not
    directly usable by simple HTTP clients.

    Args:
        url: Video URL.
        type_: "audio", "video", or "both".
        quality: "worst", "best", or a specific format_id.

    Returns:
        Dict with url, ext, filesize, codec info, and http_headers.
    """
    # Build format selector — prefer https protocol to avoid m3u8 manifests
    if quality in ("worst", "best"):
        if type_ == "audio":
            # Prefer direct https audio; fall back to any worstaudio
            format_selector = f"{quality}audio[protocol=https]/{quality}audio"
        elif type_ == "video":
            format_selector = f"{quality}video[protocol=https]/{quality}video"
        else:  # both
            format_selector = f"{quality}[protocol=https]/{quality}"
    else:
        # Assume it's a format_id
        format_selector = quality

    opts = _base_opts()
    opts["format"] = format_selector

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    # For merged formats, yt-dlp may put the actual URLs in requested_formats
    requested = info.get("requested_formats")
    if requested and len(requested) == 1:
        src = requested[0]
    elif requested:
        # Multiple streams (video+audio separate) — return both
        src = info
    else:
        src = info

    return {
        "url": src.get("url") or info.get("url"),
        "ext": src.get("ext") or info.get("ext"),
        "filesize": src.get("filesize") or src.get("filesize_approx") or info.get("filesize") or info.get("filesize_approx"),
        "acodec": src.get("acodec") or info.get("acodec"),
        "vcodec": src.get("vcodec") or info.get("vcodec"),
        "abr": src.get("abr") or info.get("abr"),
        "format_id": src.get("format_id") or info.get("format_id"),
        "format_note": src.get("format_note") or info.get("format_note"),
        "protocol": src.get("protocol") or info.get("protocol"),
        "http_headers": src.get("http_headers") or info.get("http_headers", {}),
    }


def download_media(url: str, format_selector: str = "worstaudio") -> tuple[str, str, int]:
    """
    Download media to /tmp and return file info.

    No ffmpeg, no post-processing — downloads the raw stream as-is.

    Args:
        url: Video URL.
        format_selector: yt-dlp format string (e.g. "worstaudio", "worst", format_id).

    Returns:
        Tuple of (file_path, content_type, file_size).
    """
    tmpdir = tempfile.mkdtemp(dir=TMP_DIR)
    outtmpl = os.path.join(tmpdir, "%(id)s.%(ext)s")

    # For standard quality selectors, prefer HTTPS direct streams
    # HLS/DASH manifests (m3u8) require ffmpeg to process
    if format_selector in ("worstaudio", "bestaudio", "worst", "best",
                           "worstvideo", "bestvideo"):
        format_selector = f"{format_selector}[protocol=https]/{format_selector}"

    opts = _base_opts()
    opts.update({
        "format": format_selector,
        "outtmpl": outtmpl,
        # No post-processors — raw download only
    })

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    ext = info.get("ext", "unknown")
    video_id = info.get("id", "media")
    filepath = os.path.join(tmpdir, f"{video_id}.{ext}")

    # Map extension to MIME type
    mime_map = {
        "webm": "audio/webm",
        "m4a": "audio/mp4",
        "mp4": "video/mp4",
        "opus": "audio/opus",
        "ogg": "audio/ogg",
        "mp3": "audio/mpeg",
        "wav": "audio/wav",
        "flac": "audio/flac",
    }
    content_type = mime_map.get(ext, "application/octet-stream")
    file_size = os.path.getsize(filepath)

    return filepath, content_type, file_size
