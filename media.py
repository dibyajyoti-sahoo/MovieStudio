"""Range-aware video streaming shared by admin and viewer apps.

Browsers need HTTP Range responses (206 Partial Content) to seek inside a
video. Starlette's FileResponse handles this in recent versions, but we
implement it explicitly so seeking is guaranteed regardless of version.
"""
from __future__ import annotations

import mimetypes
import re
from pathlib import Path

from fastapi import Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from hub import UPLOAD_DIR

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
_CHUNK = 1024 * 1024  # 1 MiB read size
# Cap an open-ended ("bytes=N-") range to this window so every response finishes
# quickly. The player then asks for the next window. This keeps single responses
# short — important behind Cloudflare Tunnel (≈100s request limit) and stops
# Cloudflare caching/mangling one giant range, which broke the 2nd viewer.
_RANGE_WINDOW = 8 * 1024 * 1024  # 8 MiB

# Never let an intermediary (Cloudflare) cache video; each viewer streams from origin.
_NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate, private"}


def safe_path(filename: str) -> Path | None:
    """Resolve a (possibly nested, e.g. '2026-06/movie.mp4') filename inside
    UPLOAD_DIR, refusing path traversal."""
    base = UPLOAD_DIR.resolve()
    p = (base / filename).resolve()
    try:
        p.relative_to(base)
    except ValueError:
        return None
    if not p.is_file():
        return None
    return p


def intro_file() -> Path | None:
    """The lobby loop video, if the host dropped one in uploads/ as intro.<ext>."""
    for ext in ("mp4", "webm", "ogg", "ogv", "m4v", "mov"):
        p = UPLOAD_DIR / f"intro.{ext}"
        if p.is_file():
            return p
    return None


def serve_video(filename: str, request: Request) -> Response:
    path = safe_path(filename)
    if path is None:
        return Response(status_code=404)

    file_size = path.stat().st_size
    media_type = mimetypes.guess_type(str(path))[0] or "video/mp4"
    range_header = request.headers.get("range")

    if range_header is None:
        return FileResponse(path, media_type=media_type,
                            headers={"Accept-Ranges": "bytes", **_NO_CACHE})

    m = _RANGE_RE.match(range_header)
    if not m:
        return Response(status_code=416)

    start = int(m.group(1)) if m.group(1) else 0
    open_ended = not m.group(2)
    end = int(m.group(2)) if m.group(2) else file_size - 1
    if open_ended:
        # Serve a bounded window; the browser will request the next one.
        end = min(start + _RANGE_WINDOW - 1, file_size - 1)
    end = min(end, file_size - 1)
    if start > end:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{file_size}"})

    length = end - start + 1

    def iter_file():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(_CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Content-Type": media_type,
        **_NO_CACHE,
    }
    return StreamingResponse(iter_file(), status_code=206, headers=headers)
