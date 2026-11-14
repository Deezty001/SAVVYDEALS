"""Shared constants."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"

STATES = ("NSW", "VIC", "QLD", "WA", "SA", "ACT", "TAS", "NT")
STATES_AND_ALL = STATES + ("ALL",)
PORT = os.environ.get("DEALS_PORT", "8050")
LOCAL_ORIGINS = [f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"]
# Origins allowed to push listings (bookmarklet runs on the marketplace page itself).
INGEST_ORIGINS = ["https://www.facebook.com", "https://www.gumtree.com.au"]
MAX_INGEST_BATCH = 500
DEFAULT_FB_URL = "https://www.facebook.com/marketplace/"

SWEEP_STATES = ("NSW", "VIC", "QLD", "WA", "SA", "ACT", "TAS")  # NT has no Gumtree / FB city mapping
DEEP_CHUNK = 8  # queries per browser session; results are saved after every chunk
