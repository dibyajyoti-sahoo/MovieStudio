"""Central config. Override via environment variables."""
import os

ADMIN_PORT = int(os.environ.get("MH_ADMIN_PORT", "8001"))
VIEWER_PORT = int(os.environ.get("MH_VIEWER_PORT", "8002"))
HOST = os.environ.get("MH_HOST", "0.0.0.0")

# Public host that viewers should use in the shared link. Defaults to the
# machine's LAN address at runtime if not set (see main.py).
PUBLIC_HOST = os.environ.get("MH_PUBLIC_HOST", "")

# Exact viewer URL shown in the admin "share" box. Use this when the viewer
# app sits behind its own domain (e.g. https://moviehouse-viewer.example.com/).
# If empty, the link is built from PUBLIC_HOST + VIEWER_PORT.
VIEWER_URL = os.environ.get("MH_VIEWER_URL", "").strip()

# Max upload size in bytes (default 4 GiB). Set MH_MAX_UPLOAD=0 to disable.
MAX_UPLOAD = int(os.environ.get("MH_MAX_UPLOAD", str(4 * 1024 * 1024 * 1024)))

# --- SSO (your auth server) --------------------------------------------------
# Base URL of the auth server. Admin login calls <SSO_BASE_URL>/auth/signin.
SSO_BASE_URL = os.environ.get(
    "MH_SSO_BASE_URL", "https://secure.dibyajyoti.dpdns.org"
).rstrip("/")

# Optional allowlist: only these emails may be admin. Comma-separated.
# Empty (default) = any account that signs in successfully is allowed.
_allow = os.environ.get("MH_ADMIN_EMAILS", "").strip()
ADMIN_EMAILS = {e.strip().lower() for e in _allow.split(",") if e.strip()}

# Admin session cookie lifetime (seconds). Default 12 hours.
SESSION_TTL = int(os.environ.get("MH_SESSION_TTL", str(12 * 3600)))
COOKIE_NAME = "mh_admin"

# Require admin to approve each viewer ("knock to enter")?
#   MH_REQUIRE_APPROVAL=1  -> viewers must request access and be approved
#   default (off)          -> anyone who opens the viewer link watches directly
REQUIRE_APPROVAL = os.environ.get("MH_REQUIRE_APPROVAL", "0").lower() in (
    "1", "true", "yes", "on"
)
