"""Admin app — runs on ADMIN_PORT. Full control: upload + play/pause/seek/
speed/subtitles/audio. Controls update shared state over plain HTTP; viewers
poll for it (no WebSockets).

Protected by SSO login (see auth.py): you must sign in with your auth-server
account before you can see the dashboard, upload, or control playback.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

import config
import auth
import drive
import transcode
from hub import hub, UPLOAD_DIR, month_dir, month_label, unique_dest
from media import serve_video, intro_file

app = FastAPI(title="MovieHouse Admin")
TEMPLATES = Path(__file__).parent / "templates"

# Formats that play natively in most browsers (no conversion needed).
BROWSER_EXT = {".mp4", ".webm", ".ogg", ".ogv", ".m4v", ".mov", ".m4a", ".mp3", ".aac"}

# Everything we accept and store as-is. Browser-playable ones play inline;
# others are stored/served and play depending on the viewer's browser/codecs.
ALLOWED_EXT = BROWSER_EXT | {
    # video
    ".mkv", ".avi", ".wmv", ".flv", ".f4v", ".mpeg", ".mpg", ".mpe", ".m2v",
    ".3gp", ".3g2", ".ts", ".mts", ".m2ts", ".vob", ".divx", ".mxf", ".rm",
    ".rmvb", ".asf", ".dat", ".ogm", ".mp2",
    # audio
    ".wav", ".flac", ".opus", ".wma", ".alac", ".aiff", ".amr",
}


# Jobs already pushed into shared playback state (avoid re-loading every poll).
_drive_loaded: set[str] = set()
_tc_loaded: set[str] = set()
# drive job_id -> transcode job_id, so a downloaded non-browser file is
# converted exactly once.
_drive_transcode: dict[str, str] = {}


def _maybe_transcode(src: Path) -> str | None:
    """If `src` isn't browser-playable, move it into an originals/ subfolder and
    start converting it to MP4. Returns the transcode job id, else None."""
    if src.suffix.lower() in BROWSER_EXT:
        return None
    orig_dir = src.parent / "originals"
    orig_dir.mkdir(exist_ok=True)
    orig = unique_dest(orig_dir, src.name)
    shutil.move(str(src), str(orig))
    return transcode.start(orig, src.parent)


def _list_groups() -> dict:
    """Walk uploads/ recursively and group videos by their month folder
    (newest month first). Loose files at the root fall under 'Other'.
    Kept originals (under originals/) are hidden — only playable files show."""
    base = UPLOAD_DIR.resolve()
    groups: dict[str, list] = {}
    for p in base.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in ALLOWED_EXT:
            continue
        rel = p.relative_to(base)
        if "originals" in rel.parts or p.name.lower().startswith("intro."):
            continue
        month = rel.parts[0] if len(rel.parts) > 1 else ""
        groups.setdefault(month, []).append(
            {"path": rel.as_posix(), "name": p.name, "month": month})
    ordered = sorted(groups.keys(), key=lambda m: (m == "", m), reverse=True)
    return {
        "groups": [
            {"month": m, "label": month_label(m) if m else "Other",
             "videos": sorted(groups[m], key=lambda v: v["name"].lower())}
            for m in ordered
        ],
        "current": hub.state.filename,
    }


def _render(name: str, **ctx: str) -> str:
    html = (TEMPLATES / name).read_text(encoding="utf-8")
    for k, v in ctx.items():
        html = html.replace("{{" + k + "}}", v)
    return html


def _current_email(request: Request) -> str | None:
    return auth.session_email(request.cookies.get(config.COOKIE_NAME))


# --- auth routes -------------------------------------------------------------
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    if _current_email(request):
        return RedirectResponse("/", status_code=303)
    block = f'<div class="error">{error}</div>' if error else ""
    return HTMLResponse(_render("login.html", error_block=block))


@app.post("/login")
async def login(email: str = Form(...), password: str = Form(...)):
    result = await auth.sso_signin(email.strip(), password)
    status = result.get("status")
    if status != "OK":
        msg = "Wrong email or password." if status == "WRONG_CREDENTIALS_ERROR" \
              else f"Login failed ({status})."
        return RedirectResponse(f"/login?error={msg}", status_code=303)
    if not auth.email_allowed(email.strip()):
        return RedirectResponse("/login?error=This account is not allowed to be admin.",
                                status_code=303)
    sid = auth.create_session(email.strip())
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(config.COOKIE_NAME, sid, httponly=True, samesite="lax",
                    max_age=config.SESSION_TTL)
    return resp


@app.post("/logout")
async def logout(request: Request):
    auth.destroy_session(request.cookies.get(config.COOKIE_NAME))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(config.COOKIE_NAME)
    return resp


# --- protected app routes ----------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    email = _current_email(request)
    if not email:
        return RedirectResponse("/login", status_code=303)
    if config.VIEWER_URL:
        viewer_url = config.VIEWER_URL
    else:
        viewer_host = config.PUBLIC_HOST or request.url.hostname or "localhost"
        viewer_url = f"http://{viewer_host}:{config.VIEWER_PORT}/"
    return HTMLResponse(_render("admin.html", viewer_url=viewer_url,
                                current=hub.state.filename or "", email=email))


@app.post("/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    if not _current_email(request):
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        return JSONResponse(
            {"error": f"Unsupported type '{ext}'. Supported: "
                      f"{', '.join(sorted(ALLOWED_EXT))}."},
            status_code=400,
        )
    dest = unique_dest(month_dir(), Path(file.filename).name)
    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    # Non-browser formats (.mkv, .avi, …) get converted to MP4 so every viewer
    # can play them; the client polls /transcode_progress and loads the result.
    tc = _maybe_transcode(dest)
    if tc:
        return {"filename": dest.name, "ready": False, "transcode_job": tc}
    rel = dest.relative_to(UPLOAD_DIR).as_posix()
    hub.apply_admin_event({"action": "load", "filename": rel})
    return {"filename": rel, "ready": True}


@app.post("/upload_drive")
async def upload_drive(request: Request):
    """Start downloading a public Google Drive file into this month's folder.
    Returns a job id the client polls via /drive_progress/{job_id}."""
    if not _current_email(request):
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    body = await request.json()
    url = (body or {}).get("url", "")
    if not drive.extract_id(url):
        return JSONResponse(
            {"error": "That doesn't look like a Google Drive link."},
            status_code=400)
    job_id = drive.start_download(url, month_dir(), ALLOWED_EXT)
    return {"job_id": job_id}


@app.get("/drive_progress/{job_id}")
async def drive_progress(request: Request, job_id: str):
    if not _current_email(request):
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    job = drive.get_job(job_id)
    if not job:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    # On completion: browser-playable files load straight away; others kick off a
    # one-time MP4 conversion and the client follows transcode_job instead.
    if job.get("status") == "done" and job.get("path"):
        src = Path(job["path"])
        if src.suffix.lower() not in BROWSER_EXT:
            tc = _drive_transcode.get(job_id)
            if not tc and src.exists():
                tc = _maybe_transcode(src)
                _drive_transcode[job_id] = tc
            job["transcode_job"] = tc
        else:
            rel = src.relative_to(UPLOAD_DIR).as_posix()
            job["rel"] = rel
            if job_id not in _drive_loaded:
                _drive_loaded.add(job_id)
                hub.apply_admin_event({"action": "load", "filename": rel})
    return job


@app.get("/transcode_progress/{job_id}")
async def transcode_progress(request: Request, job_id: str):
    if not _current_email(request):
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    job = transcode.get_job(job_id)
    if not job:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    if job.get("status") == "done" and job.get("path"):
        rel = Path(job["path"]).relative_to(UPLOAD_DIR).as_posix()
        job["rel"] = rel
        if job_id not in _tc_loaded:
            _tc_loaded.add(job_id)
            hub.apply_admin_event({"action": "load", "filename": rel})
    return job


@app.get("/videos")
async def list_videos(request: Request):
    if not _current_email(request):
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    return _list_groups()


@app.get("/intro")
async def intro(request: Request):
    if not _current_email(request):
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    p = intro_file()
    if p is None:
        return Response(status_code=404)
    return serve_video(p.name, request)


@app.get("/video/{filename:path}")
async def video(request: Request, filename: str):
    if not _current_email(request):
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    return serve_video(filename, request)


# --- realtime-ish control over plain HTTP (replaces the WebSocket) -----------
@app.post("/control")
async def control(request: Request):
    """Apply a playback action (play/pause/seek/rate/load/subtitle/audio_track/
    sync). Returns the new state. Admin-only."""
    if not _current_email(request):
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    event = await request.json()
    if not isinstance(event, dict):
        return JSONResponse({"error": "bad payload"}, status_code=400)
    return hub.apply_admin_event(event)


@app.get("/now")
async def now(request: Request):
    """Current shared playback snapshot, so a reloaded admin tab can resume from
    the live position instead of resetting everyone to the start."""
    if not _current_email(request):
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    return hub.state.snapshot()


@app.get("/guests")
async def guests(request: Request):
    """Admin polls this to see pending requests + who's watching."""
    if not _current_email(request):
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    return hub.guests_payload()


@app.post("/guests")
async def guest_action(request: Request):
    """Approve / deny / kick a viewer. Body: {action, gid}. Admin-only."""
    if not _current_email(request):
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    body = await request.json()
    action, gid = body.get("action"), body.get("gid")
    mapping = {"approve": "approved", "deny": "denied", "kick": "kicked"}
    if action not in mapping or not gid:
        return JSONResponse({"error": "bad request"}, status_code=400)
    hub.set_status(gid, mapping[action])
    return hub.guests_payload()
