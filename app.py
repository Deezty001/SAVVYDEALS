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
import re
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import engine_ops
import export_xlsx
import pricing
import scanning
from alerts import dispatch_auto_alerts
import auth
from config import ALLOWED_HOSTS, DEFAULT_FB_URL, INGEST_ORIGINS, LOCAL_ORIGINS, PUBLIC_URL, STATES, STATIC_DIR
from db import get_conn, get_setting, init_db, session as db_session, utcnow_iso
from evaluator import evaluate_listing
from ingest import get_reference_parts, get_settings_dict, ingest_raw_listings, public_summary
from models import (
    IngestBatchPayload, QuickRegionUpdate, ReferencePriceUpdate, SettingsUpdate, SmartPasteInput,
    StatusUpdateInput, PricesPayload,
)
from scanning import inspect_sem
from notifier import send_discord_alert, send_telegram_alert
from scrapers import GUMTREE_LOCATIONS, build_fb_marketplace_url, inspect_facebook_item_details, is_safe_listing_url

log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    engine_ops.reevaluate_all()          # catalog / model changes apply to stored listings on startup
    task = asyncio.create_task(scanning.scheduler_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="AU PC Part & Bundle Deal Sniper", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

# Order matters: the last middleware added is the outermost.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)


@app.middleware("http")
async def origin_guard(request: Request, call_next):
    """Reject cross-site browser calls to the API (CSRF / data theft from other tabs)."""
    path = request.url.path
    if not auth.is_open(path) and not auth.valid_session(request.cookies.get(auth.COOKIE)):
        return auth.deny(request)
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
    elif path.startswith("/static/") and not path.startswith("/static/fonts/"):
        response.headers["Cache-Control"] = "no-cache"  # always revalidate, so a deploy shows up straight away
    return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=LOCAL_ORIGINS + INGEST_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "X-Ingest-Token"],
)

app.include_router(auth.router)
app.include_router(scanning.router)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; "
    "img-src 'self' https: data:; connect-src 'self'; object-src 'none'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------
# --------------------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------------------
@app.get("/healthz", include_in_schema=False)
def healthz() -> Dict[str, str]:
    return {"status": "ok"}


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
    where, args = ["status != 'HIDDEN'"], []
    if saved_only:
        where.append("status = 'SAVED'")
    if drops_only:
        where.append("posted_at LIKE '%Dropped%'")
    if tier and tier != "ALL":
        if tier == "GOOD_OR_BETTER":
            where.append("deal_tier IN ('STEAL','GOOD_DEAL')")
        else:
            where.append("deal_tier = ?")
            args.append(tier)
    if source and source != "ALL":
        where.append("source = ?")
        args.append(source)
    if bundles_only:
        where.append("is_bundle = 1")
    if search:
        like = "%" + search.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(lower(title) LIKE ? ESCAPE '\\' OR lower(description) LIKE ? ESCAPE '\\' "
                     "OR lower(location) LIKE ? ESCAPE '\\')")
        args += [like, like, like]
    series = socket_or_series if socket_or_series and socket_or_series != "ALL" else None
    if series:
        where.append("lower(matched_parts_json) LIKE ?")
        args.append("%" + series.lower() + "%")
    sort_sql = {
        "profit": "profit_margin_aud DESC", "price_asc": "price_aud ASC", "price_desc": "price_aud DESC",
        "discount": "discount_pct DESC", "newest": "(CASE WHEN listed_at > 0 THEN listed_at ELSE CAST(strftime('%s', discovered_at) AS INTEGER) END) DESC",
    }.get(sort_by, "deal_score DESC")
    order = f"{sort_sql}, deal_score DESC, discovered_at DESC"

    conn = get_conn()
    # Light pass over every live listing for the region-scoped counters (no description / JSON columns).
    light = [dict(r) for r in conn.execute(
        "SELECT id, location, state, source, deal_tier, is_bundle, posted_at, status, profit_margin_aud "
        "FROM listings WHERE status != 'HIDDEN'")]
    state_rows = [r for r in light if matches_state(r, state or "ALL")]
    in_state = {r["id"] for r in state_rows}
    page, total, limit = [], 0, max(1, min(limit, 1000))
    for row in conn.execute(f"SELECT * FROM listings WHERE {' AND '.join(where)} ORDER BY {order}", args):
        if row["id"] not in in_state:
            continue
        r = dict(row)
        try:
            r["matched_parts"] = json.loads(r["matched_parts_json"])
        except ValueError:
            r["matched_parts"] = []
        if series and not any(series.lower() in p["socket_or_series"].lower() for p in r["matched_parts"]):
            continue
        total += 1
        if len(page) < limit:
            del r["matched_parts_json"]
            page.append(r)
    conn.close()

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
    return {"deals": page, "total_matching": total, "stats": stats}


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

    econ, parts, exclude_keywords, _, csig = engine_ops.load_ctx_sig()
    eval_res = evaluate_listing(
        title=deal["title"], description=enriched_desc, price_aud=float(deal["price_aud"]),
        reference_parts=parts, exclude_keywords=exclude_keywords, econ=econ,
        location=deal["location"], state=deal["state"], source=deal["source"],
    )
    sig = engine_ops.listing_sig(csig, deal["title"], enriched_desc, float(deal["price_aud"]),
                                 deal["location"], deal["state"], deal["source"])
    with db_session() as conn:
        stored = engine_ops.apply_eval(conn, deal_id, eval_res, description=enriched_desc, clear_needs_read=True, sig=sig)
    if not stored:
        return {"ok": True, "excluded": True, "message": f"Listing hidden after deep inspection ({eval_res['reason']})."}
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
    return {"endpoint": f"{PUBLIC_URL or LOCAL_ORIGINS[0]}/api/ingest", "token": get_setting("ingest_token")}


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


@app.get("/api/health")
def scrape_health() -> Dict[str, Any]:
    """Per-source collection health: healthy / degraded / down / idle, with reasons and recent attempts."""
    import health
    return health.summary()


@app.get("/api/trends")
def trends() -> Dict[str, Any]:
    return {"parts": engine_ops.price_trends()}


@app.get("/api/trips")
def trips() -> Dict[str, Any]:
    """Qualifying pickups grouped into drive trips, with suburbs, route time and saving."""
    return {"home_suburb": get_setting("home_suburb", "Sydney CBD"), "trips": engine_ops.plan_pickup_trips()}


