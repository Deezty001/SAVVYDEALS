"""Shared constants."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"

STATES = ("NSW", "VIC", "QLD", "WA", "SA", "ACT", "TAS", "NT")
STATES_AND_ALL = STATES + ("ALL",)
PORT = os.environ.get("DEALS_PORT", "8050")
LOCAL_ORIGINS = [f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"]
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]

# Hosted deployment (e.g. Coolify). DEALS_PUBLIC_URL=https://deals.example.com adds that host / origin.
PUBLIC_URL = os.environ.get("DEALS_PUBLIC_URL", "").strip().rstrip("/")
if PUBLIC_URL:
    LOCAL_ORIGINS.insert(0, PUBLIC_URL)
    ALLOWED_HOSTS.append(PUBLIC_URL.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0])
# Dashboard password. Unset = no login (fine on 127.0.0.1, required anywhere public).
DASHBOARD_PASSWORD = os.environ.get("DEALS_PASSWORD", "")
# eBay marketplace account deletion notifications (see ebay_notify.py). Unset = endpoint disabled.
EBAY_VERIFICATION_TOKEN = os.environ.get("EBAY_VERIFICATION_TOKEN", "").strip()
# eBay Browse API credentials (App ID = Client ID, Cert ID = Client Secret). Unset = eBay source disabled.
EBAY_APP_ID = os.environ.get("EBAY_APP_ID", "").strip()
EBAY_CERT_ID = os.environ.get("EBAY_CERT_ID", "").strip()
# Optional outbound proxy for Facebook / Gumtree requests, e.g. http://user:pass@au.residential-proxy.example:8000.
# Facebook blocks datacenter IPs (VPS); an Australian residential proxy is what gets past that.
PROXY = os.environ.get("DEALS_PROXY", "").strip()
PROXIES = {"http": PROXY, "https": PROXY} if PROXY else None
# Playwright browser fallback for Facebook. Off in the slim Docker image (no browser installed).
BROWSER_ENABLED = os.environ.get("DEALS_BROWSER", "1").lower() not in ("0", "false", "no", "")
# Origins allowed to push listings (bookmarklet runs on the marketplace page itself).
INGEST_ORIGINS = ["https://www.facebook.com", "https://www.gumtree.com.au"]
MAX_INGEST_BATCH = 500
DEFAULT_FB_URL = "https://www.facebook.com/marketplace/"

SWEEP_STATES = ("NSW", "VIC", "QLD", "WA", "SA", "ACT", "TAS")  # NT has no Gumtree / FB city mapping
DEEP_CHUNK = 8  # queries per browser session; results are saved after every chunk
