"""Validate, evaluate and store scraped listings."""
import hashlib
import re
from typing import Any, Dict, List
from urllib.parse import urlparse

import engine_ops
from config import MAX_INGEST_BATCH, STATES_AND_ALL
from db import get_all_settings, get_conn, utcnow_iso
from evaluator import evaluate_listing
from scrapers import is_safe_image_url, is_safe_listing_url


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
    if "ebay" in host:
        return "eBay AU"
    return "Gumtree AU" if "gumtree" in host else "Facebook Marketplace"


def ingest_raw_listings(raw_items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Validate, evaluate and store listings. Returns a summary incl. ids touched this run."""
    econ, ref_parts, exclude_keywords, settings, csig = engine_ops.load_ctx_sig()

    conn = get_conn()
    cur = conn.cursor()
    added = matched = steals = price_drops = rejected = sold = 0
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

        if item.get("gone"):
            # Sold / removed on the marketplace itself: retire any stored copy, never store a dead listing.
            cur.execute("UPDATE listings SET gone_at=?, status='HIDDEN' WHERE id=? AND gone_at=''", (now, lid))
            sold += 1
            continue

        cur.execute(
            "SELECT price_aud, description, posted_at, status, notified, discovered_at, alerted_price, listed_at FROM listings WHERE id = ?",
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

        if item.get("pending"):
            eval_res["flags"].append("pending_sale")
        fp, relists = engine_ops.relist_info(cur, lid, title, price_aud)
        if relists:
            eval_res["flags"].append(f"relist_or_crosspost:{relists}")
        vals = engine_ops.eval_to_row(eval_res)
        vals["flags"] = ",".join(eval_res["flags"])
        vals["eval_sig"] = engine_ops.listing_sig(csig, title, desc, price_aud, location, state, source_for_url(url))
        base = {
            "id": lid, "source": source_for_url(url), "title": title, "description": desc,
            "price_aud": price_aud, "location": location, "state": state, "url": url,
            "image_url": image_url, "status": existing["status"] if existing else "NEW",
            "notified": existing["notified"] if existing else 0, "posted_at": posted_badge,
            "discovered_at": existing["discovered_at"] if existing else now,  # keep the first-seen time
            "last_seen": now, "fingerprint": fp, "relist_count": relists, "gone_at": "",
            "alerted_price": existing["alerted_price"] if existing else 0,
            "listed_at": int(item.get("listed_epoch") or 0) or (existing["listed_at"] if existing else 0),
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
        "sold_or_removed": sold,
        "_saved_ids": saved_ids,
    }


def public_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in summary.items() if not k.startswith("_")}


