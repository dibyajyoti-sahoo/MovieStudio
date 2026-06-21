"""Shared state + WebSocket broadcaster used by BOTH the admin and viewer apps.

Admin and viewer apps run in the same Python process (see main.py) so they
share this one Hub instance: the playback state, the admin connections, and the
guest registry.

Access model: a viewer must "knock" with a name and be APPROVED by an admin
before they receive any video state. Admins can deny a pending request or kick
an approved viewer at any time.
"""
from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

from fastapi import WebSocket

UPLOAD_DIR = Path(__file__).parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)


@dataclass
class PlaybackState:
    """The single source of truth for what should be playing right now."""
    filename: str | None = None
    is_playing: bool = False
    rate: float = 1.0
    position: float = 0.0
    last_update: float = field(default_factory=time.time)
    subtitle: str | None = None
    audio_track: str | None = None

    def live_position(self) -> float:
        if self.is_playing:
            return self.position + (time.time() - self.last_update) * self.rate
        return self.position

    def snapshot(self) -> dict:
        d = asdict(self)
        d["position"] = self.live_position()
        d["server_time"] = time.time()
        return d


@dataclass
class Guest:
    gid: str
    name: str
    ws: WebSocket
    status: str = "pending"   # "pending" | "approved"
    joined: float = field(default_factory=time.time)


class Hub:
    def __init__(self) -> None:
        self.state = PlaybackState()
        self._admins: set[WebSocket] = set()
        self._guests: dict[str, Guest] = {}      # gid -> Guest
        self._lock = asyncio.Lock()

    # --- admins ---------------------------------------------------------------
    async def connect_admin(self, ws: WebSocket) -> None:
        await ws.accept()
        self._admins.add(ws)
        await self._send(ws, {"type": "state", **self.state.snapshot()})
        await self._send_guest_list(ws)

    def disconnect_admin(self, ws: WebSocket) -> None:
        self._admins.discard(ws)

    # --- viewers / guests -----------------------------------------------------
    async def connect_viewer(self, ws: WebSocket) -> None:
        """Accept the socket but send nothing until the guest is approved."""
        await ws.accept()

    async def knock(self, ws: WebSocket, name: str) -> str:
        """Register a pending join request and notify all admins."""
        gid = secrets.token_urlsafe(8)
        name = (name or "Guest").strip()[:40] or "Guest"
        self._guests[gid] = Guest(gid=gid, name=name, ws=ws, status="pending")
        await self._send(ws, {"type": "pending"})
        await self.broadcast_guests()
        return gid

    async def approve(self, gid: str) -> None:
        g = self._guests.get(gid)
        if not g:
            return
        g.status = "approved"
        await self._send(g.ws, {"type": "approved"})
        await self._send(g.ws, {"type": "state", **self.state.snapshot()})
        await self.broadcast_guests()

    async def deny(self, gid: str) -> None:
        await self._remove_guest(gid, reason="denied")

    async def kick(self, gid: str) -> None:
        await self._remove_guest(gid, reason="kicked")

    async def _remove_guest(self, gid: str, reason: str) -> None:
        g = self._guests.pop(gid, None)
        if not g:
            return
        await self._send(g.ws, {"type": reason})
        try:
            await g.ws.close(code=4403)
        except Exception:
            pass
        await self.broadcast_guests()

    def disconnect_viewer(self, ws: WebSocket) -> None:
        gid = next((k for k, v in self._guests.items() if v.ws is ws), None)
        if gid:
            self._guests.pop(gid, None)

    def find_gid(self, ws: WebSocket) -> str | None:
        return next((k for k, v in self._guests.items() if v.ws is ws), None)

    # --- broadcasting ---------------------------------------------------------
    def _approved(self) -> list[Guest]:
        return [g for g in self._guests.values() if g.status == "approved"]

    async def broadcast(self) -> None:
        msg = {"type": "state", **self.state.snapshot()}
        for ws in list(self._admins) + [g.ws for g in self._approved()]:
            await self._send(ws, msg)

    def _guest_payload(self) -> dict:
        return {
            "type": "guests",
            "guests": [
                {"gid": g.gid, "name": g.name, "status": g.status}
                for g in sorted(self._guests.values(), key=lambda x: x.joined)
            ],
            "approved": len(self._approved()),
            "pending": sum(1 for g in self._guests.values() if g.status == "pending"),
        }

    async def broadcast_guests(self) -> None:
        payload = self._guest_payload()
        for ws in list(self._admins):
            await self._send(ws, payload)

    async def _send_guest_list(self, ws: WebSocket) -> None:
        await self._send(ws, self._guest_payload())

    # --- admin drives playback ------------------------------------------------
    async def apply_admin_event(self, event: dict) -> None:
        action = event.get("action")
        s = self.state

        if action == "load":
            s.filename = event.get("filename")
            s.position = 0.0
            s.is_playing = False
            s.rate = 1.0
            s.subtitle = None
            s.audio_track = None
        elif action == "play":
            s.position = float(event.get("position", s.live_position()))
            s.is_playing = True
        elif action == "pause":
            s.position = float(event.get("position", s.live_position()))
            s.is_playing = False
        elif action == "seek":
            s.position = float(event.get("position", 0.0))
        elif action == "rate":
            s.position = s.live_position()
            s.rate = float(event.get("rate", 1.0))
        elif action == "subtitle":
            s.subtitle = event.get("subtitle")
        elif action == "audio_track":
            s.audio_track = event.get("audio_track")
        elif action == "sync":
            s.position = float(event.get("position", s.live_position()))
            s.is_playing = bool(event.get("is_playing", s.is_playing))
        else:
            return

        s.last_update = time.time()
        await self.broadcast()

    @staticmethod
    async def _send(ws: WebSocket, msg: dict) -> None:
        try:
            await ws.send_json(msg)
        except Exception:
            pass


hub = Hub()
