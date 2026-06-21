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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import config
import auth
from hub import hub, UPLOAD_DIR
from media import serve_video

app = FastAPI(title="MovieHouse Admin")
TEMPLATES = Path(__file__).parent / "templates"

ALLOWED_EXT = {".mp4", ".webm", ".ogg", ".ogv", ".m4v", ".mov"}


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
            {"error": f"Unsupported type '{ext}'. Browser-playable formats: "
                      f"{', '.join(sorted(ALLOWED_EXT))}. Convert MKV/AVI with ffmpeg first."},
            status_code=400,
        )
    safe = Path(file.filename).name
    dest = UPLOAD_DIR / safe
    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    hub.apply_admin_event({"action": "load", "filename": safe})
    return {"filename": safe}


@app.get("/videos")
async def list_videos(request: Request):
    if not _current_email(request):
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    files = sorted(p.name for p in UPLOAD_DIR.iterdir()
                   if p.suffix.lower() in ALLOWED_EXT)
    return {"videos": files, "current": hub.state.filename}


@app.get("/video/{filename}")
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
