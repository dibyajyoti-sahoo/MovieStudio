"""Convert non-browser video/audio formats to web-friendly MP4 (H.264 + AAC)
in the background, with progress, so every browser can play them.

Many containers/codecs (.mkv, .avi, .wmv, x265, yuv444, …) don't decode in
browsers and show a black screen. We re-encode to H.264/AAC in an MP4 with the
moov atom up front (+faststart) and yuv420p pixels for the widest support.

Each conversion runs in a background thread and reports percent through an
in-memory job registry the admin app polls.
"""
from __future__ import annotations

import re
import secrets
import shutil
import subprocess
import threading
from pathlib import Path

from hub import unique_dest

# job_id -> dict(status, percent, error, path, filename)
_jobs: dict[str, dict] = {}
_lock = threading.Lock()

_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_OUT_RE = re.compile(r"out_time=(\d+):(\d+):(\d+(?:\.\d+)?)")


def ffmpeg_exe() -> str | None:
    """Prefer a system ffmpeg; fall back to the pip-bundled static binary."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def available() -> bool:
    return ffmpeg_exe() is not None


def get_job(job_id: str) -> dict | None:
    with _lock:
        j = _jobs.get(job_id)
        return dict(j) if j else None


def _set(job_id: str, **kw) -> None:
    with _lock:
        _jobs.setdefault(job_id, {}).update(kw)


def start(src: Path, out_dir: Path) -> str:
    """Begin converting `src` into an MP4 in `out_dir`. Returns a job id."""
    job_id = secrets.token_urlsafe(8)
    _set(job_id, status="starting", percent=0, error=None, path=None, filename=None)
    threading.Thread(target=_run, args=(job_id, src, out_dir), daemon=True).start()
    return job_id


def _duration(exe: str, src: Path) -> float:
    """Total seconds, parsed from ffmpeg's banner (no ffprobe dependency)."""
    try:
        p = subprocess.run([exe, "-i", str(src)], capture_output=True, text=True)
        m = _DUR_RE.search(p.stderr)
        if m:
            h, mi, s = m.groups()
            return int(h) * 3600 + int(mi) * 60 + float(s)
    except Exception:
        pass
    return 0.0


def _run(job_id: str, src: Path, out_dir: Path) -> None:
    try:
        exe = ffmpeg_exe()
        if not exe:
            raise RuntimeError("ffmpeg is not available on the server.")
        out = unique_dest(out_dir, src.stem + ".mp4")
        total = _duration(exe, src)
        _set(job_id, status="converting", filename=out.name)
        cmd = [
            exe, "-y", "-i", str(src),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats", str(out),
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
        for line in proc.stdout:
            line = line.strip()
            if total > 0 and line.startswith("out_time="):
                m = _OUT_RE.search(line)
                if m:
                    h, mi, s = m.groups()
                    secs = int(h) * 3600 + int(mi) * 60 + float(s)
                    _set(job_id, percent=round(min(99.0, secs / total * 100), 1))
            elif line == "progress=end":
                _set(job_id, percent=100)
        proc.wait()
        if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            out.unlink(missing_ok=True)
            raise RuntimeError("Conversion failed — the file may be corrupt or unsupported.")
        _set(job_id, status="done", percent=100, path=str(out))
    except Exception as e:
        _set(job_id, status="error", error=str(e) or "Conversion failed.")
