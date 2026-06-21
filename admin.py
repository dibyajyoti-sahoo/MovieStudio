"""Admin app — runs on ADMIN_PORT. Full control: upload + play/pause/seek/
speed/subtitles/audio. Every control is broadcast to viewers in real time.

Protected by SSO login (see auth.py): you must sign in with your auth-server
account before you can see the dashboard, upload, or control playback.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile, File, WebSocket, WebSocketDisconnect, Form
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
    await hub.apply_admin_event({"action": "load", "filename": safe})
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


@app.websocket("/ws")
async def ws_admin(ws: WebSocket):
    # Guard the control channel: only a logged-in admin may drive playback.
    if not auth.session_email(ws.cookies.get(config.COOKIE_NAME)):
        await ws.close(code=4401)  # 4401 = our "unauthenticated" code
        return
    await hub.connect_admin(ws)
    try:
        while True:
            raw = await ws.receive_text()
            try:
                event = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(event, dict):
                continue
            action = event.get("action")
            if action == "approve":
                await hub.approve(event.get("gid"))
            elif action == "deny":
                await hub.deny(event.get("gid"))
            elif action == "kick":
                await hub.kick(event.get("gid"))
            else:
                await hub.apply_admin_event(event)
    except WebSocketDisconnect:
        pass
    finally:
        hub.disconnect_admin(ws)
