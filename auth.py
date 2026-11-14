"""Password login for hosted deployments (enabled when DEALS_PASSWORD is set).

A signed, expiring session cookie gates the dashboard and the API. /api/ingest stays on its own token so the
scanner and bookmarklet keep working. Failed logins are throttled per client IP.
"""
import hashlib
import hmac
import secrets
import time
import urllib.parse
from typing import Dict, List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from config import DASHBOARD_PASSWORD, PUBLIC_URL, STATIC_DIR
from db import get_conn, get_setting

COOKIE = "deals_session"
SESSION_SECONDS = 30 * 24 * 3600
OPEN_PATHS = {"/login", "/api/ingest", "/static/style.css", "/healthz"}
MAX_FAILS, FAIL_WINDOW = 8, 15 * 60

router = APIRouter()
_fails: Dict[str, List[float]] = {}


def enabled() -> bool:
    return bool(DASHBOARD_PASSWORD)


def _secret() -> bytes:
    val = get_setting("session_secret")
    if not val:
        val = secrets.token_urlsafe(32)
        conn = get_conn()
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('session_secret', ?)", (val,))
        conn.commit()
    # Changing the password logs every session out.
    return hashlib.sha256((val + "\x00" + DASHBOARD_PASSWORD).encode()).digest()


def _sign(exp: int) -> str:
    return f"{exp}.{hmac.new(_secret(), str(exp).encode(), hashlib.sha256).hexdigest()}"


def valid_session(token: Optional[str]) -> bool:
    if not token or "." not in token:
        return False
    exp_s, _ = token.split(".", 1)
    if not exp_s.isdigit() or int(exp_s) < time.time():
        return False
    return hmac.compare_digest(token, _sign(int(exp_s)))


def is_open(path: str) -> bool:
    return not enabled() or path in OPEN_PATHS


def deny(request: Request) -> Response:
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Login required."}, status_code=401)
    return RedirectResponse("/login", status_code=303)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _throttled(ip: str) -> bool:
    """Per-IP limit, plus a global one so rotating (or spoofed forwarded) IPs cannot brute-force."""
    now = time.time()
    for key in (ip, "*"):
        _fails[key] = [t for t in _fails.get(key, []) if now - t < FAIL_WINDOW]
    return len(_fails[ip]) >= MAX_FAILS or len(_fails["*"]) >= MAX_FAILS * 5


@router.get("/login", include_in_schema=False)
def login_page() -> Response:
    resp = FileResponse(STATIC_DIR / "login.html")
    resp.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'self'; form-action 'self'; frame-ancestors 'none'"
    return resp


@router.post("/login", include_in_schema=False)
async def login(request: Request) -> Response:
    ip = _client_ip(request)
    if _throttled(ip):
        return RedirectResponse("/login?e=locked", status_code=303)
    form = urllib.parse.parse_qs((await request.body()).decode("utf-8", "replace"))
    supplied = (form.get("password") or [""])[0]
    if not enabled() or not hmac.compare_digest(supplied.encode(), DASHBOARD_PASSWORD.encode()):
        for key in (ip, "*"):
            _fails.setdefault(key, []).append(time.time())
        return RedirectResponse("/login?e=1", status_code=303)
    _fails.pop(ip, None)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE, _sign(int(time.time()) + SESSION_SECONDS), max_age=SESSION_SECONDS, httponly=True,
                    secure=PUBLIC_URL.startswith("https://"), samesite="strict", path="/")
    return resp


@router.post("/logout", include_in_schema=False)
def logout() -> Response:
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE, path="/")
    return resp
