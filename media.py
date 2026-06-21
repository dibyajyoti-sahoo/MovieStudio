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
_CHUNK = 1024 * 1024  # 1 MiB


def safe_path(filename: str) -> Path | None:
    """Resolve a filename inside UPLOAD_DIR, refusing path traversal."""
    p = (UPLOAD_DIR / filename).resolve()
    if UPLOAD_DIR.resolve() not in p.parents or not p.is_file():
        return None
    return p


def serve_video(filename: str, request: Request) -> Response:
    path = safe_path(filename)
    if path is None:
        return Response(status_code=404)

    file_size = path.stat().st_size
    media_type = mimetypes.guess_type(str(path))[0] or "video/mp4"
    range_header = request.headers.get("range")

    if range_header is None:
        return FileResponse(path, media_type=media_type)

    m = _RANGE_RE.match(range_header)
    if not m:
        return Response(status_code=416)

    start = int(m.group(1)) if m.group(1) else 0
    end = int(m.group(2)) if m.group(2) else file_size - 1
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
    }
    return StreamingResponse(iter_file(), status_code=206, headers=headers)
