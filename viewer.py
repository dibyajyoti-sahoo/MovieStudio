"""Viewer app — runs on VIEWER_PORT. Watch-only: playback follows the admin.

No WebSockets. The viewer knocks (POST /knock) to request access, then polls
GET /state?gid=... a few times a second to stay in sync once approved.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from hub import hub
from media import serve_video, intro_file

app = FastAPI(title="MovieHouse Viewer")
TEMPLATES = Path(__file__).parent / "templates"


@app.get("/", response_class=HTMLResponse)
async def index():
    html = (TEMPLATES / "viewer.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/intro")
async def intro(request: Request):
    """The looping lobby video shown until the host starts a movie."""
    p = intro_file()
    if p is None:
        return Response(status_code=404)
    return serve_video(p.name, request)


@app.get("/video/{filename:path}")
async def video(filename: str, request: Request):
    return serve_video(filename, request)


@app.post("/knock")
async def knock(request: Request):
    """Viewer requests access with a name. Returns a token (gid) to poll with."""
    body = await request.json()
    name = (body or {}).get("name", "Guest")
    gid = hub.knock(name)
    return {"gid": gid}


@app.get("/state")
async def state(gid: str = ""):
    """Polled by the viewer. Returns access status, plus playback if approved."""
    return JSONResponse(hub.viewer_state(gid))
