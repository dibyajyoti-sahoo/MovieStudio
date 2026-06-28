"""Download a publicly-shared Google Drive file straight to disk, with live
progress, so it can be stored and streamed like a normal upload.

Google serves small files directly, but for large files it first returns an
HTML "can't scan for viruses" confirmation page. We parse that page's download
form and follow it. Each download runs in a background thread and reports
progress through an in-memory job registry that the admin app polls.

Only the public download endpoints are used — the file's link sharing must be
set to "Anyone with the link".
"""
from __future__ import annotations

import re
import secrets
import threading
from pathlib import Path
from urllib.parse import unquote

import httpx

from hub import unique_dest

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_CHUNK = 1024 * 256  # 256 KiB

_ID_PATTERNS = [
    re.compile(r"/file/d/([A-Za-z0-9_-]{10,})"),
    re.compile(r"[?&]id=([A-Za-z0-9_-]{10,})"),
    re.compile(r"/d/([A-Za-z0-9_-]{10,})"),
]

# job_id -> dict(status, downloaded, total, filename, error, path)
_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def extract_id(url: str) -> str | None:
    """Pull the Drive file id out of any common share-link shape, or accept a
    bare id."""
    url = (url or "").strip()
    if not url:
        return None
    for rx in _ID_PATTERNS:
        m = rx.search(url)
        if m:
            return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", url):
        return url
    return None


def get_job(job_id: str) -> dict | None:
    with _lock:
        j = _jobs.get(job_id)
        return dict(j) if j else None


def _set(job_id: str, **kw) -> None:
    with _lock:
        _jobs.setdefault(job_id, {}).update(kw)


def start_download(url: str, dest_dir: Path, allowed_ext: set[str]) -> str:
    """Kick off a background download. Returns a job id to poll with."""
    job_id = secrets.token_urlsafe(8)
    _set(job_id, status="starting", downloaded=0, total=0,
         filename=None, error=None, path=None)
    t = threading.Thread(
        target=_run, args=(job_id, url, dest_dir, allowed_ext), daemon=True)
    t.start()
    return job_id


def _run(job_id: str, url: str, dest_dir: Path, allowed_ext: set[str]) -> None:
    try:
        fid = extract_id(url)
        if not fid:
            raise ValueError("That doesn't look like a Google Drive link.")
        _download(job_id, fid, dest_dir, allowed_ext)
    except Exception as e:  # surface a readable message to the UI
        _set(job_id, status="error", error=str(e) or "Download failed.")


def _download(job_id: str, fid: str, dest_dir: Path, allowed_ext: set[str]) -> None:
    base = f"https://drive.google.com/uc?export=download&id={fid}"
    with httpx.Client(follow_redirects=True, timeout=None,
                      headers={"User-Agent": _UA}) as client:
        with client.stream("GET", base) as r:
            r.raise_for_status()
            ctype = r.headers.get("content-type", "")
            if "text/html" not in ctype:
                _save(job_id, r, dest_dir, allowed_ext)
                return
            html = b"".join(r.iter_bytes()).decode("utf-8", "ignore")

        action, params = _parse_form(html)
        if not action or "id" not in params:
            raise ValueError(
                "Could not access this Drive file. Make sure link sharing is "
                "set to 'Anyone with the link'.")
        with client.stream("GET", action, params=params) as r2:
            r2.raise_for_status()
            if "text/html" in r2.headers.get("content-type", ""):
                raise ValueError(
                    "Drive returned a web page instead of the file. Check that "
                    "the link is public and not a folder.")
            _save(job_id, r2, dest_dir, allowed_ext)


def _save(job_id: str, resp: httpx.Response, dest_dir: Path,
          allowed_ext: set[str]) -> None:
    name = _filename_from(resp, job_id)
    if allowed_ext and Path(name).suffix.lower() not in allowed_ext:
        raise ValueError(f"Unsupported file type '{Path(name).suffix}'.")
    dest = unique_dest(dest_dir, name)
    total = int(resp.headers.get("content-length") or 0)
    _set(job_id, status="downloading", total=total, filename=dest.name)
    done = 0
    try:
        with open(dest, "wb") as f:
            for chunk in resp.iter_bytes(_CHUNK):
                f.write(chunk)
                done += len(chunk)
                _set(job_id, downloaded=done)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    _set(job_id, status="done", path=str(dest), downloaded=done,
         total=total or done)


def _parse_form(html: str) -> tuple[str | None, dict]:
    am = re.search(r'action="([^"]+)"', html)
    action = am.group(1).replace("&amp;", "&") if am else \
        "https://drive.usercontent.google.com/download"
    params: dict[str, str] = {}
    for tag in re.findall(r"<input\b[^>]*>", html):
        n = re.search(r'name="([^"]+)"', tag)
        v = re.search(r'value="([^"]*)"', tag)
        if n:
            params[n.group(1)] = v.group(1) if v else ""
    return action, params


def _filename_from(resp: httpx.Response, fid_fallback: str) -> str:
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd)
    if m:
        return Path(unquote(m.group(1))).name
    m = re.search(r'filename="?([^";]+)"?', cd)
    if m:
        return Path(m.group(1).strip()).name
    return f"drive-{fid_fallback}.mp4"
