"""Run BOTH servers in one process so they share playback state:

    ADMIN  -> http://<host>:8001/   (upload + full control)
    VIEWER -> http://<host>:8002/   (watch-only, synced)

Usage:
    python main.py
Environment overrides: MH_ADMIN_PORT, MH_VIEWER_PORT, MH_HOST, MH_PUBLIC_HOST
"""
from __future__ import annotations

import asyncio
import socket

import uvicorn

import config
from admin import app as admin_app
from viewer import app as viewer_app


def lan_ip() -> str:
    """Best-effort LAN IP so the printed viewer link is shareable on the network."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


async def main() -> None:
    host_for_links = config.PUBLIC_HOST or lan_ip()

    # proxy_headers + forwarded_allow_ips so the app trusts X-Forwarded-* from
    # the reverse proxy / tunnel (correct https scheme, client IP, and WS upgrade).
    admin_cfg = uvicorn.Config(admin_app, host=config.HOST, port=config.ADMIN_PORT,
                               log_level="info", proxy_headers=True, forwarded_allow_ips="*")
    viewer_cfg = uvicorn.Config(viewer_app, host=config.HOST, port=config.VIEWER_PORT,
                                log_level="info", proxy_headers=True, forwarded_allow_ips="*")
    admin_server = uvicorn.Server(admin_cfg)
    viewer_server = uvicorn.Server(viewer_cfg)

    print("\n" + "=" * 60)
    print("  🎬 MovieHouse is running")
    print(f"  ADMIN  (you):     http://{host_for_links}:{config.ADMIN_PORT}/")
    print(f"  VIEWERS (share):  http://{host_for_links}:{config.VIEWER_PORT}/")
    print("=" * 60 + "\n")

    await asyncio.gather(admin_server.serve(), viewer_server.serve())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
