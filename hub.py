"""Shared state + guest registry used by BOTH the admin and viewer apps.

No WebSockets: the admin mutates state via HTTP, and viewers poll for it.
Admin and viewer apps run in the same process (see main.py) so they share this
one Hub instance.

Access model: a viewer "knocks" with a name (POST /knock) and gets a token
(gid). They poll GET /state?gid=... ; until an admin approves them they only see
their access status, not the video state. Admins approve / deny / kick via HTTP.
"""
from __future__ import annotations

import secrets
import time
from datetime import datetime
from dataclasses import dataclass, field, asdict
from pathlib import Path

UPLOAD_DIR = Path(__file__).parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

# Length of the pre-movie countdown, in seconds (3 → 2 → 1 → "Welcome").
COUNTDOWN_SECS = 4.0


# --- month-wise storage helpers ---------------------------------------------
def current_month() -> str:
    """The folder name for "now", e.g. '2026-06'."""
    return datetime.now().strftime("%Y-%m")


def month_dir(month: str | None = None) -> Path:
    """Return (creating if needed) uploads/<YYYY-MM>/ for the given month."""
    sub = month or current_month()
    d = UPLOAD_DIR / sub
    d.mkdir(parents=True, exist_ok=True)
    return d


def month_label(month: str) -> str:
    """Human label for a month folder, e.g. '2026-06' -> 'June 2026'."""
    try:
        return datetime.strptime(month, "%Y-%m").strftime("%B %Y")
    except ValueError:
        return month or "Other"


def unique_dest(directory: Path, filename: str) -> Path:
    """A non-colliding destination path inside `directory` for `filename`."""
    name = Path(filename).name or "file"
    dest = directory / name
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    i = 1
    while True:
        cand = directory / f"{stem} ({i}){suffix}"
        if not cand.exists():
            return cand
        i += 1


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
    # Server time at which the pre-movie countdown began (None = no countdown).
    countdown_start: float | None = None
    # Intermission/break: when it started and how long (seconds). None = no break.
    interval_start: float | None = None
    interval_secs: float = 0.0

    def live_position(self) -> float:
        if self.is_playing:
            return self.position + (time.time() - self.last_update) * self.rate
        return self.position

    def snapshot(self) -> dict:
        d = asdict(self)
        d["position"] = self.live_position()
        d["server_time"] = time.time()
        d["countdown_secs"] = COUNTDOWN_SECS
        return d


@dataclass
class Guest:
    gid: str
    name: str
    status: str = "pending"          # pending | approved | denied | kicked
    joined: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)


class Hub:
    # A guest is considered gone if we haven't heard a poll in this long.
    GUEST_TTL = 12.0

    def __init__(self) -> None:
        self.state = PlaybackState()
        self._guests: dict[str, Guest] = {}

    # --- admin drives playback ------------------------------------------------
    def apply_admin_event(self, event: dict) -> dict:
        action = event.get("action")
        s = self.state

        if action == "load":
            s.filename = event.get("filename")
            s.position = 0.0
            s.is_playing = False
            s.rate = 1.0
            s.subtitle = None
            s.audio_track = None
            s.countdown_start = None
            s.interval_start = None
            s.interval_secs = 0.0
        elif action == "start_show":
            # Begin the synced countdown before a movie. Playback stays paused
            # at the start until the countdown elapses and the admin plays.
            s.filename = event.get("filename", s.filename)
            s.position = 0.0
            s.is_playing = False
            s.rate = 1.0
            s.subtitle = None
            s.audio_track = None
            s.countdown_start = time.time()
            s.interval_start = None
            s.interval_secs = 0.0
        elif action == "interval_start":
            # Pause for an intermission and start the break timer.
            s.position = s.live_position()
            s.is_playing = False
            s.interval_start = time.time()
            s.interval_secs = max(1.0, float(event.get("secs", 300)))
        elif action == "interval_extend":
            if s.interval_start is not None:
                s.interval_secs += max(1.0, float(event.get("secs", 60)))
        elif action == "interval_resume":
            s.interval_start = None
            s.interval_secs = 0.0
            s.position = float(event.get("position", s.position))
            s.is_playing = True
        elif action == "play":
            s.position = float(event.get("position", s.live_position()))
            s.is_playing = True
            s.countdown_start = None
            s.interval_start = None
            s.interval_secs = 0.0
        elif action == "pause":
            s.position = float(event.get("position", s.live_position()))
            s.is_playing = False
        elif action == "seek":
            s.position = float(event.get("position", 0.0))
            s.countdown_start = None
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

        s.last_update = time.time()
        return self.state.snapshot()

    # --- guests ---------------------------------------------------------------
    def knock(self, name: str) -> str:
        self._prune()
        gid = secrets.token_urlsafe(8)
        name = (name or "Guest").strip()[:40] or "Guest"
        self._guests[gid] = Guest(gid=gid, name=name, status="pending")
        return gid

    def set_status(self, gid: str, status: str) -> bool:
        g = self._guests.get(gid)
        if not g:
            return False
        g.status = status
        g.last_seen = time.time()
        return True

    def viewer_state(self, gid: str) -> dict:
        """Polled by viewers. Returns access status (+ playback if approved)."""
        g = self._guests.get(gid)
        if not g:
            return {"access": "none"}
        g.last_seen = time.time()
        if g.status == "approved":
            return {"access": "approved", **self.state.snapshot()}
        return {"access": g.status}

    def guests_payload(self) -> dict:
        """Polled by admins to render pending requests + watchers."""
        self._prune()
        guests = sorted(self._guests.values(), key=lambda x: x.joined)
        return {
            "guests": [
                {"gid": g.gid, "name": g.name, "status": g.status} for g in guests
            ],
            "approved": sum(1 for g in guests if g.status == "approved"),
            "pending": sum(1 for g in guests if g.status == "pending"),
        }

    def _prune(self) -> None:
        now = time.time()
        dead = [gid for gid, g in self._guests.items()
                if now - g.last_seen > self.GUEST_TTL]
        for gid in dead:
            self._guests.pop(gid, None)


hub = Hub()
