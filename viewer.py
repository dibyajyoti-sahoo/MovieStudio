"""Viewer app — runs on VIEWER_PORT. Watch-only: playback follows the admin
via WebSocket. Viewers cannot control anything; their player has no controls.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from hub import hub
from media import serve_video

app = FastAPI(title="MovieHouse Viewer")
TEMPLATES = Path(__file__).parent / "templates"


@app.get("/", response_class=HTMLResponse)
async def index():
    html = (TEMPLATES / "viewer.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/video/{filename}")
async def video(filename: str, request: Request):
    return serve_video(filename, request)


@app.websocket("/ws")
async def ws_viewer(ws: WebSocket):
    await hub.connect_viewer(ws)  # accepted, but no state until approved
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue  # ignore non-JSON frames (e.g. stale clients, pings)
            if isinstance(msg, dict) and msg.get("type") == "knock":
                await hub.knock(ws, msg.get("name", "Guest"))
            # viewers cannot drive playback; everything else is ignored
    except WebSocketDisconnect:
        pass
    finally:
        hub.disconnect_viewer(ws)
        await hub.broadcast_guests()
