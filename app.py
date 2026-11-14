"""
FastAPI backend for AU PC Deals Finder (Facebook Marketplace AU + Gumtree AU).

Security model (local single-user tool):
  * Bound to 127.0.0.1; Host header is checked (blocks DNS-rebinding).
  * Browser requests from any other origin are rejected, except /api/ingest which needs a secret token.
  * Webhook / bot secrets are never returned to the browser.
  * Listing URLs are restricted to Facebook / Gumtree https hosts before they are stored or visited.
"""
import asyncio
import hashlib
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, validator

from db import get_all_settings, get_conn, get_setting, init_db, utcnow_iso
from evaluator import evaluate_listing
import engine_ops
import export_xlsx
import pricing
from queries import build_deep_queries
from notifier import send_discord_alert, send_mac_notification, send_ntfy_alert, send_telegram_alert
from scrapers import (
    FB_FULL_SWEEP_QUERIES,
    GUMTREE_LOCATIONS,
    build_fb_marketplace_url,
    inspect_facebook_item_details,
    inspect_gumtree_item_details,
    is_safe_image_url,
    is_safe_listing_url,
    scrape_facebook_exhaustive,
    scrape_facebook_marketplace_au,
    scrape_gumtree_au,
)

log = logging.getLogger("app")
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

scan_lock = asyncio.Lock()
inspect_sem = asyncio.Semaphore(2)
SWEEP_STATES = ("NSW", "VIC", "QLD", "WA", "SA", "ACT", "TAS")  # NT has no Gumtree / FB city mapping
DEEP_CHUNK = 8  # queries per browser session; results are saved after every chunk
scan_state: Dict[str, Any] = {"running": False, "mode": None, "last_run": None, "last_summary": None, "progress": None}
deep_task: Optional["asyncio.Task[None]"] = None
deep_cancel = asyncio.Event()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    engine_ops.reevaluate_all()          # catalog / model changes apply to stored listings on startup
    task = asyncio.create_task(scheduler_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="AU PC Part & Bundle Deal Sniper", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

# Order matters: the last middleware added is the outermost.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])


@app.middleware("http")
async def origin_guard(request: Request, call_next):
    """Reject cross-site browser calls to the API (CSRF / data theft from other tabs)."""
    path = request.url.path
    if path.startswith("/api/") and path != "/api/ingest":
        origin = request.headers.get("origin")
        if origin and origin not in LOCAL_ORIGINS:
            return JSONResponse({"detail": "Cross-origin requests are not allowed."}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "Cross-site requests are not allowed."}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=LOCAL_ORIGINS + INGEST_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "X-Ingest-Token"],
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' https: data:; connect-src 'self'; object-src 'none'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------
def get_settings_dict() -> Dict[str, str]:
    return get_all_settings()


def get_reference_parts() -> List[Dict[str, Any]]:
    conn = get_conn()
    try:
        cur = conn.execute("SELECT * FROM reference_parts ORDER BY category, avg_used_aud DESC")
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _clip(value: Any, n: int) -> str:
    return str(value if value is not None else "").strip()[:n]


def source_for_url(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return "Gumtree AU" if "gumtree" in host else "Facebook Marketplace"


def ingest_raw_listings(raw_items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Validate, evaluate and store listings. Returns a summary incl. ids touched this run."""
    settings = get_settings_dict()
    try:
        exclude_keywords = json.loads(settings.get("exclude_keywords", "[]"))
    except ValueError:
        exclude_keywords = []
    ref_parts = get_reference_parts()
    econ = pricing.Econ.from_settings(settings)

    conn = get_conn()
    cur = conn.cursor()
    added = matched = steals = price_drops = rejected = 0
    saved_ids: List[str] = []
    now = utcnow_iso()

    for item in raw_items[:MAX_INGEST_BATCH]:
        try:
            title = _clip(item.get("title"), 200)
            desc = _clip(item.get("description"), 6000)
            price_aud = float(item.get("price_aud") or 0)
            url = _clip(item.get("url"), 500)
            image_url = _clip(item.get("image_url"), 800)
        except (TypeError, ValueError, AttributeError):
            rejected += 1
            continue

        if not title or not (0 < price_aud < 1_000_000):
            continue
        if not is_safe_listing_url(url) or not is_safe_image_url(image_url):
            rejected += 1
            continue

        lid = _clip(item.get("id"), 40)
        if not re.fullmatch(r"[A-Za-z0-9_]{1,40}", lid or ""):
            lid = "lst_" + hashlib.md5(url.encode()).hexdigest()[:12]

        cur.execute(
            "SELECT price_aud, description, posted_at, status, notified, discovered_at, alerted_price FROM listings WHERE id = ?",
            (lid,),
        )
        existing = cur.fetchone()
        posted_badge = _clip(item.get("posted_at"), 80) or "Live Ad"

        if existing:
            old_price = float(existing["price_aud"])
            old_desc = existing["description"] or ""
            if len(old_desc) > len(desc) and "[Deep Inspected]" in old_desc:
                desc = old_desc
            if price_aud < old_price:
                posted_badge = f"📉 Dropped -${int(old_price - price_aud)} (Was ${int(old_price)})"
                price_drops += 1
            elif "Dropped" in (existing["posted_at"] or "") and "Dropped" not in posted_badge:
                posted_badge = existing["posted_at"]

        state = _clip(item.get("state") or settings.get("state", "NSW"), 4).upper()
        if state not in STATES_AND_ALL:
            state = settings.get("state", "NSW")
        location = _clip(item.get("location") or settings.get("city", "Sydney"), 120)

        eval_res = evaluate_listing(
            title=title, description=desc, price_aud=price_aud, reference_parts=ref_parts,
            exclude_keywords=exclude_keywords, econ=econ, location=location, state=state,
            source=source_for_url(url),
        )

        if eval_res["excluded"] or eval_res["deal_tier"] == "UNMATCHED":
            # A listing we already stored that now fails evaluation (edited, relisted, faulty) is hidden, not left stale.
            if existing and existing["status"] != "HIDDEN":
                cur.execute("UPDATE listings SET status = 'HIDDEN' WHERE id = ?", (lid,))
            continue

        matched += 1
        if eval_res["deal_tier"] == "STEAL":
            steals += 1

        fp, relists = engine_ops.relist_info(cur, lid, title, price_aud)
        if relists:
            eval_res["flags"].append(f"relist_or_crosspost:{relists}")
        vals = engine_ops.eval_to_row(eval_res)
        vals["flags"] = ",".join(eval_res["flags"])
        base = {
            "id": lid, "source": source_for_url(url), "title": title, "description": desc,
            "price_aud": price_aud, "location": location, "state": state, "url": url,
            "image_url": image_url, "status": existing["status"] if existing else "NEW",
            "notified": existing["notified"] if existing else 0, "posted_at": posted_badge,
            "discovered_at": existing["discovered_at"] if existing else now,  # keep the first-seen time
            "last_seen": now, "fingerprint": fp, "relist_count": relists, "gone_at": "",
            "alerted_price": existing["alerted_price"] if existing else 0,
        }
        base.update(vals)
        cols = ", ".join(base)
        cur.execute(f"INSERT OR REPLACE INTO listings ({cols}) VALUES ({', '.join('?' * len(base))})",
                    tuple(base.values()))
        saved_ids.append(lid)
        added += 1

    conn.commit()
    conn.close()
    return {
        "processed": len(raw_items),
        "matched_deals": matched,
        "steals": steals,
        "price_drops": price_drops,
        "saved": added,
        "rejected": rejected,
        "_saved_ids": saved_ids,
    }


def qualifies_for_alert(deal: Dict[str, Any], econ: pricing.Econ) -> bool:
    """Only high-confidence deals that meet the profit AND return targets."""
    flags = deal.get("flags") or ""
    return (deal["deal_tier"] in ("STEAL", "GOOD_DEAL") and deal["confidence"] >= 65
            and deal["net_profit_aud"] >= econ.min_net_profit and deal["roi_pct"] >= econ.min_roi_pct
            and "interstate_pc" not in flags and not deal.get("needs_read") and not deal.get("gone_at"))


async def dispatch_auto_alerts(saved_ids: List[str]) -> int:
    """
    Alert on qualifying deals: new ones, and ones that already existed but a price drop has
    just made qualify (re-alert). Channels: Discord, Telegram, ntfy phone push, macOS.
    """
    s = get_settings_dict()
    tier = s.get("auto_notify_tier", "GOOD_DEAL")
    econ = pricing.Econ.from_settings(s)
    has_discord = bool(s.get("discord_webhook_url"))
    has_telegram = bool(s.get("telegram_bot_token") and s.get("telegram_chat_id"))
    has_ntfy = bool(s.get("ntfy_topic"))
    has_mac = s.get("mac_notify", "1") == "1"
    if tier == "OFF" or not saved_ids or not (has_discord or has_telegram or has_ntfy or has_mac):
        return 0
    allowed = ("STEAL",) if tier == "STEAL" else ("STEAL", "GOOD_DEAL")

    conn = get_conn()
    marks = ",".join("?" * len(saved_ids))
    rows = [dict(r) for r in conn.execute(
        f"SELECT * FROM listings WHERE id IN ({marks}) AND status != 'HIDDEN' ORDER BY net_profit_aud DESC", saved_ids)]
    conn.close()

    sent = 0
    for deal in rows:
        if deal["deal_tier"] not in allowed or not qualifies_for_alert(deal, econ):
            continue
        reason = ""
        if deal["notified"]:
            # already alerted: only again if the price has dropped meaningfully since that alert
            if not (deal["alerted_price"] and deal["price_aud"] <= deal["alerted_price"] * 0.95):
                continue
            reason = f"📉 PRICE DROP ${deal['alerted_price']:.0f} → ${deal['price_aud']:.0f}"
        if sent >= 10:  # never flood the channel after a big first scan
            break
        ok_any = False
        if has_discord:
            ok_any |= (await send_discord_alert(s["discord_webhook_url"], deal, reason))[0]
        if has_telegram:
            ok_any |= (await send_telegram_alert(s["telegram_bot_token"], s["telegram_chat_id"], deal, reason))[0]
        if has_ntfy:
            ok_any |= (await send_ntfy_alert(s["ntfy_topic"], deal, reason))[0]
        if has_mac:
            ok_any |= (await send_mac_notification(deal, reason))[0]
        if ok_any:
            c = get_conn()
            c.execute("UPDATE listings SET notified = 1, alerted_price = ? WHERE id = ?", (deal["price_aud"], deal["id"]))
            c.commit()
            c.close()
            sent += 1
    return sent


def public_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in summary.items() if not k.startswith("_")}


# --------------------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------------------
class SmartPasteInput(BaseModel):
    raw_text: str = Field(..., max_length=8000)
    source: str = "Facebook Marketplace"
    url: str = DEFAULT_FB_URL
    save_to_feed: bool = True


class QuickRegionUpdate(BaseModel):
    state: str

    @validator("state")
    def _state(cls, v: str) -> str:
        v = v.upper()
        if v not in STATES_AND_ALL:
            raise ValueError("Unknown state")
        return v


class LiveScanRequest(BaseModel):
    custom_query: Optional[str] = Field(None, max_length=80)
    sources: List[str] = ["facebook", "gumtree"]
    full_fb_sweep: bool = False

    @validator("sources", each_item=True)
    def _src(cls, v: str) -> str:
        if v not in ("facebook", "gumtree"):
            raise ValueError("Unknown source")
        return v


class DeepScanRequest(BaseModel):
    include_gumtree: bool = True
    max_pages_per_query: int = Field(25, ge=1, le=200)


class StatusUpdateInput(BaseModel):
    status: str

    @validator("status")
    def _status(cls, v: str) -> str:
        if v not in ("NEW", "SAVED", "HIDDEN"):
            raise ValueError("Invalid status")
        return v


class ReferencePriceUpdate(BaseModel):
    avg_used_aud: float = Field(..., gt=0, lt=100000)
    good_deal_aud: float = Field(..., gt=0, lt=100000)
    steal_aud: float = Field(..., gt=0, lt=100000)

    @validator("steal_aud")
    def _order(cls, v: float, values: Dict[str, Any]) -> float:
        good, avg = values.get("good_deal_aud"), values.get("avg_used_aud")
        if good is not None and v > good:
            raise ValueError("Steal price must be at or below Good Deal price")
        if good is not None and avg is not None and good > avg:
            raise ValueError("Good Deal price must be at or below the average")
        return v


class SettingsUpdate(BaseModel):
    state: str
    city: str = Field(..., max_length=60)
    radius_km: int = Field(50, ge=1, le=500)
    min_discount_pct: float = Field(20, ge=0, le=100)
    min_profit_aud: float = Field(50, ge=0, le=100000)
    auto_notify_tier: str = "GOOD_DEAL"
    # None = leave the stored secret untouched; "" = clear it; anything else = replace it.
    discord_webhook_url: Optional[str] = None
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = Field(None, max_length=40)
    search_queries: List[str] = Field(..., max_items=60)
    exclude_keywords: List[str] = Field(..., max_items=200)
    # Engine / routing / alert extras (all optional: omitted = unchanged)
    home_suburb: Optional[str] = Field(None, max_length=60)
    min_roi_pct: Optional[float] = Field(None, ge=0, le=1000)
    hourly_value_aud: Optional[float] = Field(None, ge=0, le=500)
    cost_per_km_aud: Optional[float] = Field(None, ge=0, le=5)
    gst_registered: Optional[bool] = None
    mac_notify: Optional[bool] = None
    scheduler_enabled: Optional[bool] = None
    ntfy_topic: Optional[str] = Field(None, max_length=64)

    @validator("ntfy_topic")
    def _ntfy(cls, v: Optional[str]) -> Optional[str]:
        if v and not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", v):
            raise ValueError("ntfy topic must be 8-64 letters, digits, _ or -")
        return v

    @validator("state")
    def _state(cls, v: str) -> str:
        v = v.upper()
        if v not in STATES:
            raise ValueError("Unknown state")
        return v

    @validator("auto_notify_tier")
    def _tier(cls, v: str) -> str:
        if v not in ("OFF", "STEAL", "GOOD_DEAL"):
            raise ValueError("Invalid alert tier")
        return v

    @validator("discord_webhook_url")
    def _discord(cls, v: Optional[str]) -> Optional[str]:
        # Only real Discord webhooks: otherwise this endpoint is a way to make the server POST anywhere.
        if v and not re.fullmatch(r"https://(?:canary\.|ptb\.)?(?:discord|discordapp)\.com/api/webhooks/\d+/[\w-]+", v):
            raise ValueError("Not a valid Discord webhook URL")
        return v

    @validator("telegram_bot_token")
    def _tg(cls, v: Optional[str]) -> Optional[str]:
        if v and not re.fullmatch(r"\d{5,15}:[A-Za-z0-9_-]{30,60}", v):
            raise ValueError("Not a valid Telegram bot token")
        return v

    @validator("search_queries", "exclude_keywords", each_item=True)
    def _short(cls, v: str) -> str:
        return v.strip()[:80]


class IngestBatchPayload(BaseModel):
    listings: List[Dict[str, Any]] = Field(..., max_items=MAX_INGEST_BATCH)


# --------------------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def serve_dashboard() -> Response:
    resp = FileResponse(STATIC_DIR / "index.html")
    resp.headers["Content-Security-Policy"] = CSP
    return resp


# --------------------------------------------------------------------------------------
# Deals
# --------------------------------------------------------------------------------------
def matches_state(row: Dict[str, Any], target_state: str) -> bool:
    if not target_state or target_state.upper() == "ALL":
        return True
    st = target_state.upper()
    loc_upper = (row.get("location") or "").upper()
    if f", {st}" in loc_upper:
        return True
    if st == "ACT" and any(x in loc_upper for x in ("CANBERRA", "QUEANBEYAN", "YASS", "GOULBURN", "BELCONNEN", "GUNGAHLIN", "TUGGERANONG")):
        return True
    if not any(f", {s}" in loc_upper for s in STATES):
        return (row.get("state") or "").upper() == st
    return False


@app.get("/api/deals")
def list_deals(
    tier: Optional[str] = None,
    source: Optional[str] = None,
    state: Optional[str] = None,
    socket_or_series: Optional[str] = None,
    bundles_only: bool = False,
    saved_only: bool = False,
    drops_only: bool = False,
    search: Optional[str] = None,
    sort_by: str = "score",
    limit: int = 300,
) -> Dict[str, Any]:
    conn = get_conn()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM listings WHERE status != 'HIDDEN' ORDER BY deal_score DESC, discovered_at DESC")]
    conn.close()

    state_rows = [r for r in rows if matches_state(r, state or "ALL")]
    filtered = []
    for r in state_rows:
        try:
            r["matched_parts"] = json.loads(r["matched_parts_json"])
        except ValueError:
            r["matched_parts"] = []
        del r["matched_parts_json"]
        if saved_only and r.get("status") != "SAVED":
            continue
        if drops_only and "Dropped" not in (r.get("posted_at") or ""):
            continue
        if tier and tier != "ALL":
            if tier == "GOOD_OR_BETTER":
                if r["deal_tier"] not in ("STEAL", "GOOD_DEAL"):
                    continue
            elif r["deal_tier"] != tier:
                continue
        if source and source != "ALL" and r["source"] != source:
            continue
        if bundles_only and not r["is_bundle"]:
            continue
        if socket_or_series and socket_or_series != "ALL":
            if not any(socket_or_series.lower() in p["socket_or_series"].lower() for p in r["matched_parts"]):
                continue
        if search:
            q = search.lower()
            if q not in r["title"].lower() and q not in (r["description"] or "").lower() and q not in (r["location"] or "").lower():
                continue
        filtered.append(r)

    sorters = {
        "profit": (lambda x: x["profit_margin_aud"], True),
        "price_asc": (lambda x: x["price_aud"], False),
        "price_desc": (lambda x: x["price_aud"], True),
        "discount": (lambda x: x["discount_pct"], True),
        "newest": (lambda x: x["discovered_at"], True),
    }
    key, rev = sorters.get(sort_by, (lambda x: x["deal_score"], True))
    filtered.sort(key=key, reverse=rev)

    stats = {
        "total_deals": len(state_rows),
        "fb_count": sum(1 for r in state_rows if "Facebook" in r["source"]),
        "gt_count": sum(1 for r in state_rows if "Gumtree" in r["source"]),
        "steals_count": sum(1 for r in state_rows if r["deal_tier"] == "STEAL"),
        "good_deals_count": sum(1 for r in state_rows if r["deal_tier"] == "GOOD_DEAL"),
        "bundles_count": sum(1 for r in state_rows if r["is_bundle"]),
        "drops_count": sum(1 for r in state_rows if "Dropped" in (r.get("posted_at") or "")),
        "saved_count": sum(1 for r in state_rows if r.get("status") == "SAVED"),
        "max_profit_aud": max((r["profit_margin_aud"] for r in state_rows), default=0.0),
    }
    limit = max(1, min(limit, 1000))
    return {"deals": filtered[:limit], "total_matching": len(filtered), "stats": stats}


@app.get("/api/scan/status")
def scan_status() -> Dict[str, Any]:
    return scan_state


@app.post("/api/scan/live")
async def trigger_live_scan(payload: Optional[LiveScanRequest] = None) -> Dict[str, Any]:
    """Scrape live Facebook / Gumtree listings, evaluate and store them. One scan at a time."""
    if scan_lock.locked():
        raise HTTPException(status_code=409, detail="A scan is already running.")

    async with scan_lock:
        scan_state.update(running=True, mode="quick", progress=None)
        try:
            settings = get_settings_dict()
            state = settings.get("state", "NSW")
            radius_km = int(settings.get("radius_km", "50") or 50)

            if payload and payload.custom_query and payload.custom_query.strip():
                queries, include_newest = [payload.custom_query.strip()], True
            elif payload and payload.full_fb_sweep:
                queries, include_newest = FB_FULL_SWEEP_QUERIES, False
            else:
                queries, include_newest = [
                    "rtx 3070", "rtx 3080", "rtx 4070", "rtx 4070 super", "rtx 4080",
                    "rx 7800 xt", "7800x3d", "5800x3d", "9800x3d", "gaming pc rtx",
                ], True

            sources = payload.sources if (payload and payload.sources) else ["facebook", "gumtree"]
            all_scraped: List[Dict[str, Any]] = []
            errors: List[str] = []
            gt_count = fb_count = 0

            if "facebook" in sources:
                fb_items = await scrape_facebook_marketplace_au(
                    queries, state=state, max_per_query=24,
                    include_newest_pass=include_newest, concurrency=6, errors=errors,
                )
                fb_count = len(fb_items)
                all_scraped.extend(fb_items)

            if "gumtree" in sources:
                for q in queries[:8]:
                    gt_items = await scrape_gumtree_au(q, state=state, radius_km=radius_km, max_results=16, errors=errors)
                    gt_count += len(gt_items)
                    all_scraped.extend(gt_items)

            summary = ingest_raw_listings(all_scraped)
            summary["alerts_sent"] = await dispatch_auto_alerts(summary["_saved_ids"])
            result = public_summary(summary)
            result.update({
                "queries_run": queries,
                "raw_scraped_count": len(all_scraped),
                "gumtree_scraped": gt_count,
                "facebook_scraped": fb_count,
                "errors": sorted(set(errors))[:10],
            })
            scan_state["last_run"] = utcnow_iso()
            scan_state["last_summary"] = result
            return result
        finally:
            scan_state["running"] = False


async def run_deep_scan(states: List[str], include_gumtree: bool, max_pages: int) -> None:
    """Exhaustive sweep: every catalog-derived query split into price bands, saved chunk by chunk."""
    async with scan_lock:
        deep_cancel.clear()
        settings = get_settings_dict()
        radius_km = int(settings.get("radius_km", "50") or 50)
        try:
            extra = json.loads(settings.get("search_queries", "[]"))
        except ValueError:
            extra = []
        queries = build_deep_queries(get_reference_parts(), extra)
        chunks = [queries[i:i + DEEP_CHUNK] for i in range(0, len(queries), DEEP_CHUNK)]
        prog = {"states": states, "state": states[0], "chunk": 0, "chunks_total": len(chunks) * len(states),
                "queries_total": len(queries), "pages": 0, "listings_seen": 0, "matched": 0, "errors": [], "stopped": None}
        scan_state.update(running=True, mode="deep", progress=prog)
        started = utcnow_iso()
        try:
            for st in states:
                prog["state"] = st
                for chunk in chunks:
                    if deep_cancel.is_set():
                        prog["stopped"] = "Cancelled"
                        return
                    errors: List[str] = []
                    page_stats: Dict[str, int] = {}
                    items = await scrape_facebook_exhaustive(
                        chunk, state=st, concurrency=6, max_pages_per_query=max_pages,
                        errors=errors, stats=page_stats)
                    prog["pages"] = prog.get("pages", 0) + page_stats.get("pages", 0)
                    fb_seen = len(items)
                    if include_gumtree:
                        for q in chunk:
                            if deep_cancel.is_set():
                                break
                            items += await scrape_gumtree_au(q, state=st, radius_km=radius_km, max_results=30,
                                                             errors=errors, pages=3)
                    summary = ingest_raw_listings(items)
                    await dispatch_auto_alerts(summary["_saved_ids"])
                    prog["chunk"] += 1
                    prog["listings_seen"] += len(items)
                    prog["matched"] += summary["matched_deals"]
                    prog["errors"] = sorted(set(prog["errors"]) | set(errors))[:10]
                    # Facebook is blocking us: stop instead of hammering it and risking the session.
                    if fb_seen == 0 and any("login wall" in e for e in errors):
                        prog["stopped"] = "Facebook is showing a login wall. Stopped to avoid getting blocked."
                        return
        except Exception as e:  # never let the background task die silently
            log.exception("Deep scan crashed")
            prog["stopped"] = f"Crashed: {type(e).__name__}"
        finally:
            scan_state.update(running=False, last_run=utcnow_iso(),
                              last_summary={"started": started, **{k: prog[k] for k in ("listings_seen", "matched", "stopped")}})


@app.post("/api/scan/deep", status_code=202)
async def start_deep_scan(payload: Optional[DeepScanRequest] = None) -> Dict[str, Any]:
    """Start an exhaustive background sweep. Poll /api/scan/status for progress."""
    global deep_task
    if scan_lock.locked():
        raise HTTPException(status_code=409, detail="A scan is already running.")
    payload = payload or DeepScanRequest()
    state = get_setting("state", "NSW")
    states = list(SWEEP_STATES) if state == "ALL" else [state if state in SWEEP_STATES else "NSW"]
    deep_task = asyncio.create_task(run_deep_scan(states, payload.include_gumtree, payload.max_pages_per_query))
    return {"started": True, "states": states}


@app.post("/api/scan/cancel")
def cancel_scan() -> Dict[str, Any]:
    deep_cancel.set()
    return {"ok": True}


@app.post("/api/deals/{deal_id}/inspect")
async def deep_inspect_deal(deal_id: str) -> Dict[str, Any]:
    """Open the Facebook listing, read the full seller specs and re-value the build."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM listings WHERE id = ?", (deal_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Deal not found")
    deal = dict(row)

    async with inspect_sem:
        info = await inspect_facebook_item_details(deal["url"])
    if not info.get("ok") or not info.get("full_description"):
        return {"ok": False, "message": info.get("error") or "Could not extract additional text from listing page."}

    enriched_desc = f"[Deep Inspected] {info['full_description']}"
    if info.get("listed_time"):
        enriched_desc = f"{info['listed_time']} • {enriched_desc}"

    econ, parts, exclude_keywords, _ = engine_ops.load_ctx()
    eval_res = evaluate_listing(
        title=deal["title"], description=enriched_desc, price_aud=float(deal["price_aud"]),
        reference_parts=parts, exclude_keywords=exclude_keywords, econ=econ,
        location=deal["location"], state=deal["state"], source=deal["source"],
    )

    conn = get_conn()
    if eval_res["excluded"]:
        conn.execute("UPDATE listings SET status = 'HIDDEN', description = ? WHERE id = ?", (enriched_desc, deal_id))
        conn.commit()
        conn.close()
        return {"ok": True, "excluded": True, "message": f"Listing hidden after deep inspection ({eval_res['reason']})."}

    vals = engine_ops.eval_to_row(eval_res)
    sets = ", ".join(f"{k}=?" for k in vals)
    conn.execute(f"UPDATE listings SET description=?, {sets} WHERE id=?", (enriched_desc, *vals.values(), deal_id))
    conn.commit()
    conn.close()
    return {
        "ok": True, "excluded": False, "full_description": enriched_desc,
        "matched_parts": eval_res["matched_parts"], "estimated_value_aud": eval_res["estimated_value_aud"],
        "net_profit_aud": eval_res["net_profit_aud"], "max_buy_aud": eval_res["max_buy_aud"],
        "confidence": eval_res["confidence"], "deal_tier": eval_res["deal_tier"],
    }


@app.post("/api/smart-paste")
def smart_paste_evaluator(payload: SmartPasteInput) -> Dict[str, Any]:
    raw = payload.raw_text.strip()
    if not raw:
        raise HTTPException(status_code=400, detail="Paste some listing text first.")

    url = payload.url.strip() or DEFAULT_FB_URL
    if not is_safe_listing_url(url):
        raise HTTPException(status_code=400, detail="Listing URL must be an https Facebook or Gumtree link.")

    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    title = lines[0] if lines else "Pasted PC Listing"

    price_aud = 0.0
    for pm in re.findall(r"(?:A?\$|\bAUD\s*)(\d{1,4}(?:,\d{3})*(?:\.\d{1,2})?)", raw, re.IGNORECASE):
        val = float(pm.replace(",", ""))
        if 30 <= val <= 15000:
            price_aud = val
            break

    if price_aud == 0.0:
        model_numbers = {3060, 3070, 3080, 3090, 4060, 4070, 4080, 4090, 5070, 5080, 5090, 5600, 5700, 5800,
                         7600, 7700, 7800, 7900, 9070, 9600, 9700, 9800, 9950, 1700, 1851, 3200, 3600, 6000}
        for nm in re.findall(r"\b(\d{3,4})\b", raw):
            if int(nm) not in model_numbers:
                price_aud = float(nm)
                break

    if price_aud <= 0:
        return {"ok": False, "error": "Could not detect an AUD price (e.g. $650). Include '$price' anywhere in the text.",
                "detected_price_aud": 0}

    settings = get_settings_dict()
    state, city = settings.get("state", "NSW"), settings.get("city", "Sydney")
    exclude_keywords = json.loads(settings.get("exclude_keywords", "[]"))

    res = evaluate_listing(
        title=title[:120], description=raw, price_aud=price_aud,
        reference_parts=get_reference_parts(), exclude_keywords=exclude_keywords,
        econ=pricing.Econ.from_settings(settings), location=f"{city}, {state}", state=state,
    )

    if payload.save_to_feed and not res["excluded"] and res["deal_tier"] != "UNMATCHED":
        # Manual checks usually share the generic marketplace URL, so key them on content, not URL.
        ingest_raw_listings([{
            "id": "man_" + hashlib.md5(f"{url}|{raw}".encode()).hexdigest()[:12],
            "title": title[:120], "description": raw, "price_aud": price_aud,
            "location": f"{city}, {state}", "state": state, "url": url, "posted_at": "Manual Check",
        }])

    res["ok"] = True
    res["detected_title"] = title[:120]
    res["detected_price_aud"] = price_aud
    return res


@app.post("/api/region")
def switch_au_region(payload: QuickRegionUpdate) -> Dict[str, Any]:
    city_map = {"NSW": "Sydney", "VIC": "Melbourne", "QLD": "Brisbane", "WA": "Perth", "SA": "Adelaide",
                "ACT": "Canberra", "TAS": "Hobart", "NT": "Darwin", "ALL": "Australia-Wide"}
    state = payload.state
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('state', ?)", (state,))
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('city', ?)", (city_map[state],))
    conn.commit()
    conn.close()
    return {"ok": True, "state": state, "city": city_map[state]}


@app.post("/api/ingest")
async def ingest_from_browser(payload: IngestBatchPayload, request: Request) -> Dict[str, Any]:
    """Bookmarklet / Playwright scanner entry point. Requires the secret X-Ingest-Token header."""
    import hmac
    expected = get_setting("ingest_token")
    supplied = request.headers.get("x-ingest-token", "")
    if not expected or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Missing or invalid ingest token.")
    summary = ingest_raw_listings(payload.listings)
    summary["alerts_sent"] = await dispatch_auto_alerts(summary["_saved_ids"])
    return public_summary(summary)


@app.get("/api/ingest-config")
def ingest_config() -> Dict[str, str]:
    """Same-origin only (see origin_guard). Used to build the bookmarklet."""
    return {"endpoint": f"{LOCAL_ORIGINS[0]}/api/ingest", "token": get_setting("ingest_token")}


@app.patch("/api/deals/{deal_id}/status")
def update_deal_status(deal_id: str, payload: StatusUpdateInput) -> Dict[str, Any]:
    conn = get_conn()
    cur = conn.execute("UPDATE listings SET status = ? WHERE id = ?", (payload.status, deal_id))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Deal not found")
    return {"ok": True, "status": payload.status}


@app.post("/api/deals/{deal_id}/notify")
async def notify_deal(deal_id: str) -> Dict[str, Any]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM listings WHERE id = ?", (deal_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Deal not found")

    deal, settings, results = dict(row), get_settings_dict(), []
    if settings.get("discord_webhook_url"):
        ok, msg = await send_discord_alert(settings["discord_webhook_url"], deal)
        results.append({"channel": "Discord", "ok": ok, "message": msg})
    if settings.get("telegram_bot_token") and settings.get("telegram_chat_id"):
        ok, msg = await send_telegram_alert(settings["telegram_bot_token"], settings["telegram_chat_id"], deal)
        results.append({"channel": "Telegram", "ok": ok, "message": msg})
    if not results:
        return {"ok": False, "message": "No Discord webhook or Telegram bot configured yet. Add one under Settings."}
    return {"ok": any(r["ok"] for r in results), "results": results}


@app.delete("/api/deals/{deal_id}")
def hide_deal(deal_id: str) -> Dict[str, Any]:
    conn = get_conn()
    conn.execute("UPDATE listings SET status = 'HIDDEN' WHERE id = ?", (deal_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


# --------------------------------------------------------------------------------------
# Catalog & settings
# --------------------------------------------------------------------------------------
@app.get("/api/catalog")
def list_catalog() -> Dict[str, Any]:
    parts = get_reference_parts()
    for p in parts:
        p["regex_patterns"] = json.loads(p["regex_patterns"])
        p["exclude_patterns"] = json.loads(p["exclude_patterns"])
    return {"parts": parts}


@app.put("/api/catalog/{part_id}")
def update_catalog_part(part_id: int, payload: ReferencePriceUpdate) -> Dict[str, Any]:
    conn = get_conn()
    cur = conn.execute("""
        UPDATE reference_parts SET avg_used_aud = ?, good_deal_aud = ?, steal_aud = ?, updated_at = ? WHERE id = ?
    """, (payload.avg_used_aud, payload.good_deal_aud, payload.steal_aud, utcnow_iso(), part_id))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Part not found")
    engine_ops.reevaluate_all()
    return {"ok": True}


@app.get("/api/settings")
def get_settings() -> Dict[str, Any]:
    s = get_settings_dict()
    state = s.get("state", "NSW")
    gt_slug = GUMTREE_LOCATIONS.get(state, GUMTREE_LOCATIONS["NSW"])["slug"]
    queries = json.loads(s.get("search_queries", "[]"))
    return {
        "state": state,
        "city": s.get("city", "Sydney"),
        "radius_km": int(s.get("radius_km", "50") or 50),
        "min_discount_pct": float(s.get("min_discount_pct", "20") or 20),
        "min_profit_aud": float(s.get("min_profit_aud", "50") or 50),
        "auto_notify_tier": s.get("auto_notify_tier", "GOOD_DEAL"),
        # Secrets are write-only: the browser only learns whether they are set.
        "discord_configured": bool(s.get("discord_webhook_url")),
        "telegram_configured": bool(s.get("telegram_bot_token") and s.get("telegram_chat_id")),
        "telegram_chat_id": s.get("telegram_chat_id", ""),
        "ntfy_configured": bool(s.get("ntfy_topic")),
        "home_suburb": s.get("home_suburb", "Sydney CBD"),
        "min_roi_pct": float(s.get("min_roi_pct", "25") or 25),
        "hourly_value_aud": float(s.get("hourly_value_aud", "30") or 30),
        "cost_per_km_aud": float(s.get("cost_per_km_aud", "0.35") or 0.35),
        "gst_registered": s.get("gst_registered", "1") == "1",
        "mac_notify": s.get("mac_notify", "1") == "1",
        "scheduler_enabled": s.get("scheduler_enabled", "0") == "1",
        "search_queries": queries,
        "exclude_keywords": json.loads(s.get("exclude_keywords", "[]")),
        "quick_links": [
            {
                "query": q,
                "fb_url": build_fb_marketplace_url(q, state),
                "gt_url": f"https://www.gumtree.com.au/s-{gt_slug}/{q.replace(' ', '+')}/k0",
            }
            for q in queries
        ],
    }


@app.put("/api/settings")
def save_settings(payload: SettingsUpdate) -> Dict[str, Any]:
    updates = {
        "state": payload.state,
        "city": payload.city.strip(),
        "radius_km": str(payload.radius_km),
        "min_discount_pct": str(payload.min_discount_pct),
        "min_profit_aud": str(payload.min_profit_aud),
        "auto_notify_tier": payload.auto_notify_tier,
        "search_queries": json.dumps([q for q in payload.search_queries if q]),
        "exclude_keywords": json.dumps([k for k in payload.exclude_keywords if k]),
    }
    for key in ("discord_webhook_url", "telegram_bot_token", "telegram_chat_id", "ntfy_topic", "home_suburb"):
        val = getattr(payload, key)
        if val is not None:
            updates[key] = val.strip()
    for key in ("min_roi_pct", "hourly_value_aud", "cost_per_km_aud"):
        val = getattr(payload, key)
        if val is not None:
            updates[key] = str(val)
    for key in ("gst_registered", "mac_notify", "scheduler_enabled"):
        val = getattr(payload, key)
        if val is not None:
            updates[key] = "1" if val else "0"

    conn = get_conn()
    for k, v in updates.items():
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v))
    conn.commit()
    conn.close()
    engine_ops.reevaluate_all()          # targets / home suburb / GST change every valuation
    return {"ok": True}


# --------------------------------------------------------------------------------------
# Engine endpoints: prices, trends, trips, re-valuation
# --------------------------------------------------------------------------------------
class PriceUpdate(BaseModel):
    part_id: Optional[int] = None
    canonical_name: Optional[str] = Field(None, max_length=120)
    new_retail_aud: float = Field(..., ge=0, le=100000)


class PricesPayload(BaseModel):
    prices: List[PriceUpdate] = Field(..., max_items=500)


def _require_ingest_token(request: Request) -> None:
    import hmac
    expected = get_setting("ingest_token")
    if not expected or not hmac.compare_digest(request.headers.get("x-ingest-token", ""), expected):
        raise HTTPException(status_code=401, detail="Missing or invalid ingest token.")


@app.post("/api/prices")
def push_prices(payload: PricesPayload, request: Request) -> Dict[str, Any]:
    """The parts scraper pushes fresh new-retail prices; every stored listing is re-valued."""
    _require_ingest_token(request)
    return engine_ops.set_prices([p.dict() for p in payload.prices])


@app.post("/api/reevaluate")
def reevaluate() -> Dict[str, Any]:
    return engine_ops.reevaluate_all()


@app.post("/api/learn")
def learn() -> Dict[str, Any]:
    """Learn used values + trends from collected listings, then re-value everything."""
    res = engine_ops.learn_used_values()
    res.update(engine_ops.reevaluate_all())
    return res


@app.get("/api/export.xlsx")
def export_xlsx_file(tier: Optional[str] = None) -> Response:
    """Excel workbook: Deals, Pickup trips, Price guide, Falling prices. ?tier=GOOD_OR_BETTER|STEAL|OFFER narrows Deals."""
    tiers = {"GOOD_OR_BETTER": ["STEAL", "GOOD_DEAL"], "STEAL": ["STEAL"], "OFFER": ["OFFER"]}.get(tier or "")
    data = export_xlsx.build_workbook(tiers)
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="au-pc-deals.xlsx"'})


@app.get("/api/trends")
def trends() -> Dict[str, Any]:
    return {"parts": engine_ops.price_trends()}


@app.get("/api/trips")
def trips() -> Dict[str, Any]:
    """Qualifying pickups grouped into drive trips, with suburbs, route time and saving."""
    return {"home_suburb": get_setting("home_suburb", "Sydney CBD"), "trips": engine_ops.plan_pickup_trips()}


@app.post("/api/deep-read")
async def deep_read(limit: int = 10) -> Dict[str, Any]:
    return await auto_deep_read(max(1, min(limit, 30)))


# --------------------------------------------------------------------------------------
# Automatic deep reads, background scheduler
# --------------------------------------------------------------------------------------
async def auto_deep_read(limit: int = 8) -> Dict[str, Any]:
    """Open the most promising low-confidence listings, re-value them from the full text, drop sold ones."""
    conn = get_conn()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM listings WHERE status!='HIDDEN' AND gone_at='' AND needs_read=1 "
        "AND description NOT LIKE '%[Deep Inspected]%' ORDER BY net_profit_aud DESC LIMIT ?", (limit,))]
    conn.close()
    econ, parts, exclude_keywords, _ = engine_ops.load_ctx()
    read = gone = promoted = 0
    for deal in rows:
        async with inspect_sem:
            if "Gumtree" in deal["source"]:
                info = await inspect_gumtree_item_details(deal["url"])
            else:
                info = await inspect_facebook_item_details(deal["url"])
        if info.get("gone"):
            engine_ops.mark_gone(deal["id"])
            gone += 1
            continue
        text = info.get("full_description")
        if not info.get("ok") or not text:
            continue
        read += 1
        desc = f"[Deep Inspected] {text}"
        res = evaluate_listing(deal["title"], desc, float(deal["price_aud"]), parts, exclude_keywords, econ,
                               deal["location"], deal["state"], deal["source"])
        c = get_conn()
        if res["excluded"] or res["deal_tier"] == "UNMATCHED":
            c.execute("UPDATE listings SET status='HIDDEN', description=? WHERE id=?", (desc, deal["id"]))
        else:
            vals = engine_ops.eval_to_row(res)
            # a deep-read listing has had its full text read: it is no longer "needs read"
            vals["needs_read"] = 0
            if res["deal_tier"] in ("STEAL", "GOOD_DEAL", "OFFER"):
                promoted += 1
            sets = ", ".join(f"{k}=?" for k in vals)
            c.execute(f"UPDATE listings SET description=?, {sets} WHERE id=?", (desc, *vals.values(), deal["id"]))
        c.commit()
        c.close()
        await asyncio.sleep(1.5)
    return {"candidates": len(rows), "read": read, "gone": gone, "now_deals": promoted}


SCHED_BASE_S = 600           # newest-first pass every ~10 minutes
SCHED_MAX_S = 7200
sched_state: Dict[str, Any] = {"next_at": 0.0, "backoff": 0, "rotation": 0, "last_nightly": None, "last_note": ""}


async def run_quick_pass(per_pass: int = 6) -> None:
    """Newest-first pass over a rotating slice of the part searches (all parts covered over time)."""
    if scan_lock.locked():
        return
    async with scan_lock:
        scan_state.update(running=True, mode="auto", progress=None)
        try:
            settings = get_settings_dict()
            state = settings.get("state", "NSW")
            radius_km = int(settings.get("radius_km", "50") or 50)
            try:
                extra = json.loads(settings.get("search_queries", "[]"))
            except ValueError:
                extra = []
            queries = build_deep_queries(get_reference_parts(), extra)
            if not queries:
                return
            start = sched_state["rotation"] % len(queries)
            batch = (queries + queries)[start:start + per_pass]
            sched_state["rotation"] = start + per_pass
            errors: List[str] = []
            items = await scrape_facebook_marketplace_au(
                batch, state=state if state != "ALL" else "NSW", max_per_query=24,
                include_newest_pass=True, concurrency=3, errors=errors)
            fb_seen = len(items)
            for q in batch[:3]:
                items += await scrape_gumtree_au(q, state=state if state != "ALL" else "NSW",
                                                 radius_km=radius_km, max_results=16, errors=errors)
            summary = ingest_raw_listings(items)
            await dispatch_auto_alerts(summary["_saved_ids"])
            if fb_seen == 0 and any("login wall" in e for e in errors):
                sched_state["backoff"] = min(sched_state["backoff"] + 1, 5)   # double the wait each time
                sched_state["last_note"] = "Facebook login wall: backing off"
            else:
                sched_state["backoff"] = 0
                sched_state["last_note"] = f"ok: {len(items)} seen, {summary['matched_deals']} matched"
            read = await auto_deep_read(5)
            await dispatch_auto_alerts(summary["_saved_ids"])      # deals promoted by deep reads
            engine_ops.mark_stale_gone()
            scan_state["last_run"] = utcnow_iso()
            scan_state["last_summary"] = {"auto": True, **public_summary(summary), "deep_read": read}
        except Exception as e:
            log.exception("Scheduled pass failed")
            sched_state["backoff"] = min(sched_state["backoff"] + 1, 5)
            sched_state["last_note"] = f"error: {type(e).__name__}"
        finally:
            scan_state["running"] = False


async def run_nightly() -> None:
    state = get_setting("state", "NSW")
    states = list(SWEEP_STATES) if state == "ALL" else [state if state in SWEEP_STATES else "NSW"]
    await run_deep_scan(states, True, 3)
    engine_ops.mark_stale_gone()
    engine_ops.learn_used_values()       # learn used values, snapshot prices, update trends
    engine_ops.reevaluate_all()
    await auto_deep_read(25)


async def scheduler_loop() -> None:
    import time
    from datetime import datetime
    while True:
        try:
            await asyncio.sleep(30)
            if get_setting("scheduler_enabled", "0") != "1":
                continue
            now = datetime.now()
            if now.hour == 2 and sched_state["last_nightly"] != now.date():
                sched_state["last_nightly"] = now.date()
                await run_nightly()
                continue
            if time.time() < sched_state["next_at"]:
                continue
            await run_quick_pass()
            wait = min(SCHED_BASE_S * (2 ** sched_state["backoff"]), SCHED_MAX_S)
            sched_state["next_at"] = time.time() + wait
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Scheduler loop error")
            await asyncio.sleep(60)


@app.get("/api/scheduler")
def scheduler_status() -> Dict[str, Any]:
    return {"enabled": get_setting("scheduler_enabled", "0") == "1", **{
        k: (str(v) if k == "last_nightly" else v) for k, v in sched_state.items()}}
