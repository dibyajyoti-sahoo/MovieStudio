# 🎬 MovieHouse

A self-hosted **synced watch-party** server. You (the admin) upload a video and
control playback — play, pause, seek, speed, captions, audio track. Everyone you
share the viewer link with sees exactly what you're playing, in real time. They
can **only watch** — they can't touch the controls.

It runs as **two ports in one process** so they share state:

| Role   | URL                        | Can do                                            |
|--------|----------------------------|---------------------------------------------------|
| Admin  | `http://<host>:8001/`      | Sign in, upload, play/pause/seek, change speed, captions, audio, approve viewers. |
| Viewer | `http://<host>:8002/`      | Enter a name, request access, then watch (no controls). |

## Viewer access (knock-to-enter)

Viewers can't just watch by opening the link. The flow:

1. A viewer opens `:8002`, types their **name**, and clicks *Request access*.
2. On the admin dashboard a live **"🙋 Rahul wants to join"** row appears with
   **Approve** / **Deny** buttons.
3. Only after you Approve does that viewer start receiving the video and sync to
   your playback. Denied viewers are turned away; you can **Remove** an approved
   viewer at any time and they're cut off immediately.

Pending (un-approved) viewers never receive any video data.

## Run it

```bash
# from the project folder, with the venv active
uv sync            # or: pip install -e .
python main.py
```

You'll see something like:

```
  ADMIN  (you):     http://192.168.1.20:8001/
  VIEWERS (share):  http://192.168.1.20:8002/
```

Open the admin URL yourself. Send the viewer URL to anyone on the **same
network**. To let people join from anywhere, put it behind a tunnel:

```bash
ngrok http 8002      # gives a public https URL for viewers
```

## How it works

- **One process, two apps** (`admin.py`, `viewer.py`) share a single in-memory
  `Hub` (`hub.py`) holding the playback state and all WebSocket connections.
- The admin's player events (play/pause/seek/ratechange) are pushed over a
  WebSocket to the hub, which rebroadcasts the new state to every viewer.
- State stores *position + server timestamp*, so a viewer (or a late joiner)
  computes the true live position and snaps into sync; a heartbeat every few
  seconds corrects drift.
- Video is streamed with HTTP **Range** support (`media.py`) so seeking works.

## Supported video formats

Browser-native playback only (no transcoding): **mp4, webm, m4v, mov, ogg/ogv**.
MP4 (H.264 + AAC) is the safest. MKV/AVI must be converted first:

```bash
ffmpeg -i movie.mkv -c:v libx264 -c:a aac movie.mp4
```

Captions (`<track>` / embedded text tracks) and multiple audio tracks are
exposed *best-effort* — browser support for in-file audio-track switching is
limited. For guaranteed multi-audio/subtitle switching you'd transcode/extract
with ffmpeg (not included).

## Admin login (SSO)

The admin port is protected by your SSO auth server (SuperTokens-backed).
Login is **signin-only**:

1. The admin opens `:8001`, gets redirected to `/login`, enters email + password.
2. MovieHouse calls `POST <SSO_BASE_URL>/auth/signin` with HTTP Basic
   (`base64("email:password")`).
3. On `status: "OK"` a local HttpOnly session cookie is issued (valid 12h by
   default). The password is never stored, and no other SSO endpoint is used.
4. That cookie guards every admin route, upload, and the control WebSocket.
   A wrong password returns `WRONG_CREDENTIALS_ERROR` and login fails.

Viewers on `:8002` need no login.

By default any account that signs in successfully can be admin. To restrict it,
set `MH_ADMIN_EMAILS` to a comma-separated allowlist.

## Config (env vars)

| Var               | Default                              | Meaning                                  |
|-------------------|--------------------------------------|------------------------------------------|
| `MH_ADMIN_PORT`   | `8001`                               | Admin port                               |
| `MH_VIEWER_PORT`  | `8002`                               | Viewer port                              |
| `MH_HOST`         | `0.0.0.0`                            | Bind address                             |
| `MH_PUBLIC_HOST`  | (LAN IP)                             | Host used in the shared viewer link      |
| `MH_MAX_UPLOAD`   | `4 GiB`                              | Max upload size in bytes                 |
| `MH_SSO_BASE_URL` | `https://secure.dibyajyoti.dpdns.org`| Your auth server base URL                |
| `MH_ADMIN_EMAILS` | (empty = allow all)                  | Comma-separated admin email allowlist    |
| `MH_SESSION_TTL`  | `43200` (12h)                        | Admin session lifetime in seconds        |

## Files

```
main.py        run both servers (entrypoint)
hub.py         shared playback state + WebSocket broadcaster
media.py       range-aware video streaming
auth.py        SSO signin + local admin sessions
admin.py       admin app (port 8001, login-protected)
viewer.py      viewer app (port 8002, open)
templates/     admin.html, login.html, viewer.html
uploads/       uploaded videos (git-ignored)
```

## Note on security

The admin port now requires SSO login. Sessions are in-memory (cleared on
restart). Cookies are HttpOnly + SameSite=Lax; when you serve over HTTPS, also
mark the cookie `secure=True` in `admin.py`. Anyone who can reach `:8002` can
watch — that port is intentionally open.
```
