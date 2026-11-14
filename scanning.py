"""Scans (live / deep), automatic deep reads and the background scheduler."""
import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException

import engine_ops
from alerts import check_health_and_alert, dispatch_auto_alerts
from config import DEEP_CHUNK, SWEEP_STATES
from db import get_conn, get_setting, session as db_session, utcnow_iso
from evaluator import evaluate_listing
from ingest import get_reference_parts, get_settings_dict, ingest_raw_listings, public_summary
from models import DeepScanRequest, LiveScanRequest
from queries import build_deep_queries
from fb import FB_BROAD_QUERIES, FB_CATEGORIES
from scrapers import (
    FB_FULL_SWEEP_QUERIES, inspect_facebook_item_details,
    inspect_gumtree_item_details, scrape_facebook_exhaustive, scrape_facebook_marketplace_au, scrape_gumtree_au,
)

log = logging.getLogger("app")
router = APIRouter()

scan_lock = asyncio.Lock()
inspect_sem = asyncio.Semaphore(2)
scan_state: Dict[str, Any] = {"running": False, "mode": None, "last_run": None, "last_summary": None, "progress": None}
deep_task: Optional["asyncio.Task[None]"] = None
deep_cancel = asyncio.Event()




@router.get("/api/scan/status")
def scan_status() -> Dict[str, Any]:
    return scan_state


@router.post("/api/scan/live")
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
        prog = {"states": states, "state": states[0], "chunk": 0, "chunks_total": (len(chunks) + 1) * len(states),
                "queries_total": len(queries), "pages": 0, "listings_seen": 0, "matched": 0, "errors": [], "stopped": None}
        scan_state.update(running=True, mode="deep", progress=prog)
        started = utcnow_iso()
        try:
            for st in states:
                prog["state"] = st
                for chunk in chunks + [[]]:            # the final empty chunk sweeps whole categories
                    if deep_cancel.is_set():
                        prog["stopped"] = "Cancelled"
                        return
                    errors: List[str] = []
                    page_stats: Dict[str, int] = {}
                    items = await scrape_facebook_exhaustive(
                        chunk, state=st, concurrency=6, max_pages_per_query=max_pages,
                        errors=errors, stats=page_stats, categories=FB_CATEGORIES if not chunk else ())
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
                    if fb_seen == 0 and any(e.startswith("Facebook") for e in errors):
                        prog["stopped"] = "Facebook is showing a login wall. Stopped to avoid getting blocked."
                        return
        except Exception as e:  # never let the background task die silently
            log.exception("Deep scan crashed")
            prog["stopped"] = f"Crashed: {type(e).__name__}"
        finally:
            scan_state.update(running=False, last_run=utcnow_iso(),
                              last_summary={"started": started, **{k: prog[k] for k in ("listings_seen", "matched", "stopped")}})


@router.post("/api/scan/deep", status_code=202)
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


@router.post("/api/scan/cancel")
def cancel_scan() -> Dict[str, Any]:
    deep_cancel.set()
    return {"ok": True}


@router.post("/api/deep-read")
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
    econ, parts, exclude_keywords, _, csig = engine_ops.load_ctx_sig()
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
        sig = engine_ops.listing_sig(csig, deal["title"], desc, float(deal["price_aud"]),
                                     deal["location"], deal["state"], deal["source"])
        with db_session() as c:
            # a deep-read listing has had its full text read: it is no longer "needs read"
            if engine_ops.apply_eval(c, deal["id"], res, description=desc, clear_needs_read=True, sig=sig) \
                    and res["deal_tier"] in ("STEAL", "GOOD_DEAL", "OFFER"):
                promoted += 1
        await asyncio.sleep(1.5)
    return {"candidates": len(rows), "read": read, "gone": gone, "now_deals": promoted}


SCHED_BASE_S = 600           # newest-first pass every ~10 minutes
SCHED_MAX_S = 7200
sched_state: Dict[str, Any] = {"next_at": 0.0, "backoff": 0, "rotation": 0, "last_nightly": None, "last_note": ""}


async def run_quick_pass(per_pass: int = 30) -> None:
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
            queries = [q for q in build_deep_queries(get_reference_parts(), extra) if q not in FB_BROAD_QUERIES]
            if not queries:
                return
            # Broad searches every pass (a new listing of anything shows up in them within minutes), plus a
            # rotating slice of the specific ones so every part is re-checked about once an hour.
            start = sched_state["rotation"] % len(queries)
            batch = FB_BROAD_QUERIES + (queries + queries)[start:start + per_pass]
            sched_state["rotation"] = start + per_pass
            errors: List[str] = []
            items = await scrape_facebook_marketplace_au(
                batch, state=state if state != "ALL" else "NSW", max_per_query=24,
                newest_only=True, concurrency=3, errors=errors)
            fb_seen = len(items)
            for q in batch[:3]:
                items += await scrape_gumtree_au(q, state=state if state != "ALL" else "NSW",
                                                 radius_km=radius_km, max_results=16, errors=errors)
            summary = ingest_raw_listings(items)
            await dispatch_auto_alerts(summary["_saved_ids"])
            if fb_seen == 0 and any(e.startswith("Facebook") for e in errors):
                sched_state["backoff"] = min(sched_state["backoff"] + 1, 5)   # double the wait each time
                sched_state["last_note"] = "Facebook not returning listings: backing off"
            else:
                sched_state["backoff"] = 0
                sched_state["last_note"] = f"ok: {len(items)} seen, {summary['matched_deals']} matched"
            await check_health_and_alert()
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


@router.get("/api/scheduler")
def scheduler_status() -> Dict[str, Any]:
    return {"enabled": get_setting("scheduler_enabled", "0") == "1", **{
        k: (str(v) if k == "last_nightly" else v) for k, v in sched_state.items()}}
