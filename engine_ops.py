"""
DB-side engine operations: context loading, re-valuation, learned prices + trends,
sold/gone detection, deep reads and trip planning data.
"""
import json
import statistics
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import classify as clf
import pricing
import routing
from db import get_all_settings, get_conn, utcnow_iso
from evaluator import evaluate_listing

EVAL_COLS = (
    "is_bundle", "matched_parts_json", "estimated_value_aud", "profit_margin_aud", "discount_pct",
    "deal_score", "deal_tier", "net_profit_aud", "max_buy_aud", "offer_aud", "roi_pct", "confidence",
    "kind", "flags", "strategy", "suburb", "drive_min", "travel_cost_aud", "needs_read", "offer_message",
)


def load_ctx() -> Tuple[pricing.Econ, List[Dict[str, Any]], List[str], Dict[str, str]]:
    s = get_all_settings()
    conn = get_conn()
    parts = [dict(r) for r in conn.execute("SELECT * FROM reference_parts")]
    conn.close()
    try:
        ex = json.loads(s.get("exclude_keywords", "[]"))
    except ValueError:
        ex = []
    return pricing.Econ.from_settings(s), parts, ex, s


def eval_to_row(res: Dict[str, Any]) -> Dict[str, Any]:
    """Evaluator result -> listings column values."""
    return {
        "is_bundle": 1 if res["is_bundle"] else 0,
        "matched_parts_json": json.dumps(res["matched_parts"]),
        "estimated_value_aud": res["estimated_value_aud"], "profit_margin_aud": res["profit_margin_aud"],
        "discount_pct": res["discount_pct"], "deal_score": res["deal_score"], "deal_tier": res["deal_tier"],
        "net_profit_aud": res["net_profit_aud"], "max_buy_aud": res["max_buy_aud"],
        "offer_aud": res["offer_aud"], "roi_pct": res["roi_pct"], "confidence": res["confidence"],
        "kind": res["kind"], "flags": ",".join(res["flags"]), "strategy": res["strategy"],
        "suburb": res["suburb"], "drive_min": res["drive_min"], "travel_cost_aud": res["travel_cost_aud"],
        "needs_read": 1 if res["needs_read"] else 0, "offer_message": res["offer_message"],
    }


def reevaluate_all() -> Dict[str, int]:
    """Re-value every stored listing (run whenever prices, settings or the catalog change)."""
    econ, parts, ex, _ = load_ctx()
    conn = get_conn()
    rows = [dict(r) for r in conn.execute("SELECT * FROM listings WHERE status != 'HIDDEN'")]
    changed = hidden = 0
    for r in rows:
        res = evaluate_listing(r["title"], r["description"] or "", r["price_aud"], parts, ex, econ,
                               r["location"], r["state"], r["source"])
        if res["excluded"] or res["deal_tier"] == "UNMATCHED":
            conn.execute("UPDATE listings SET status='HIDDEN' WHERE id=?", (r["id"],))
            hidden += 1
            continue
        vals = eval_to_row(res)
        sets = ", ".join(f"{k}=?" for k in vals)
        conn.execute(f"UPDATE listings SET {sets} WHERE id=?", (*vals.values(), r["id"]))
        changed += 1
    conn.commit()
    conn.close()
    return {"revalued": changed, "hidden": hidden}


def set_prices(updates: List[Dict[str, Any]]) -> Dict[str, Any]:
    """updates: [{part_id | canonical_name, new_retail_aud}] from the parts scraper."""
    conn = get_conn()
    n = 0
    for u in updates:
        retail = float(u.get("new_retail_aud") or 0)
        if retail < 0 or retail > 100000:
            continue
        if u.get("part_id") is not None:
            cur = conn.execute("UPDATE reference_parts SET new_retail_aud=?, price_source='savvy-retail', updated_at=? "
                               "WHERE id=?", (retail, utcnow_iso(), int(u["part_id"])))
        else:
            cur = conn.execute("UPDATE reference_parts SET new_retail_aud=?, price_source='savvy-retail', updated_at=? "
                               "WHERE canonical_name=?", (retail, utcnow_iso(), str(u.get("canonical_name", ""))))
        n += cur.rowcount
    conn.commit()
    conn.close()
    return {"updated_parts": n, **reevaluate_all()}


# ---------------------------------------------------------------------------------------------
# Learned used values + trend
# ---------------------------------------------------------------------------------------------
def _trimmed_weighted_median(vals: List[Tuple[float, float]]) -> float:
    """vals: (price, weight). Drop outliers beyond 40%-160% of the median, then weighted median."""
    med = statistics.median(p for p, _ in vals)
    kept = [(p, w) for p, w in vals if 0.6 * med <= p <= 1.4 * med] or vals
    kept.sort()
    total = sum(w for _, w in kept)
    acc = 0.0
    for p, w in kept:
        acc += w
        if acc >= total / 2:
            return p
    return kept[-1][0]


ASK_TO_SOLD = 0.93   # asking prices run ~7% above what things actually sell for


def learn_used_values(today: Optional[str] = None) -> Dict[str, Any]:
    """
    Learn used value per part from collected listings: private, single-part, price-sane ads only,
    outliers trimmed, listings that disappeared quickly weighted double (they were priced right).
    Also writes a daily snapshot and computes a 14-day-vs-prior-14-day trend.
    """
    today = today or datetime.utcnow().date().isoformat()
    conn = get_conn()
    per_part: Dict[int, List[Tuple[float, float]]] = {}
    for r in conn.execute("SELECT * FROM listings WHERE status != 'HIDDEN' AND kind='private'"):
        try:
            mp = [p for p in json.loads(r["matched_parts_json"]) if not p.get("inferred")]
        except ValueError:
            continue
        if len(mp) != 1 or r["is_bundle"] or "bare_model_number" in (r["flags"] or ""):
            continue
        price = float(r["price_aud"])
        if price < 30 or "too_cheap" in (r["flags"] or ""):
            continue
        w = 1.0
        if r["gone_at"]:
            try:
                days = (datetime.fromisoformat(r["gone_at"]) - datetime.fromisoformat(r["discovered_at"])).days
                w = 2.0 if days <= 3 else 1.0
            except ValueError:
                pass
        per_part.setdefault(int(mp[0]["id"]), []).append((price, w))

    learned = 0
    for pid, vals in per_part.items():
        if len(vals) < 5:
            continue
        v = round(_trimmed_weighted_median(vals) * ASK_TO_SOLD, 2)
        conn.execute("UPDATE reference_parts SET learned_used_aud=?, learned_n=?, price_source='learned' WHERE id=?",
                     (v, len(vals), pid))
        conn.execute("INSERT OR REPLACE INTO price_snapshots (part_id, day, median_ask, n) VALUES (?,?,?,?)",
                     (pid, today, statistics.median(p for p, _ in vals), len(vals)))
        learned += 1

    # trend: median of snapshots in the last 14 days vs the 14 before
    d0 = datetime.fromisoformat(today).date()
    recent_from, prior_from = (d0 - timedelta(days=14)).isoformat(), (d0 - timedelta(days=28)).isoformat()
    trending = 0
    for (pid,) in conn.execute("SELECT DISTINCT part_id FROM price_snapshots").fetchall():
        rec = [r[0] for r in conn.execute(
            "SELECT median_ask FROM price_snapshots WHERE part_id=? AND day>?", (pid, recent_from))]
        pri = [r[0] for r in conn.execute(
            "SELECT median_ask FROM price_snapshots WHERE part_id=? AND day>? AND day<=?", (pid, prior_from, recent_from))]
        if rec and pri:
            t = (statistics.median(rec) / statistics.median(pri) - 1) * 100
            conn.execute("UPDATE reference_parts SET trend_pct=? WHERE id=?", (round(t, 1), pid))
            trending += 1
    conn.commit()
    conn.close()
    return {"parts_learned": learned, "parts_with_trend": trending}


def price_trends(min_abs: float = 5.0) -> List[Dict[str, Any]]:
    conn = get_conn()
    rows = [dict(r) for r in conn.execute(
        "SELECT id, canonical_name, category, trend_pct, learned_used_aud, learned_n FROM reference_parts "
        "WHERE ABS(trend_pct) >= ? ORDER BY trend_pct", (min_abs,))]
    conn.close()
    for r in rows:
        r["warning"] = ("FALLING: avoid holding stock" if r["trend_pct"] <= -5 else "rising")
    return rows


# ---------------------------------------------------------------------------------------------
# Gone / sold detection and relists
# ---------------------------------------------------------------------------------------------
def mark_stale_gone(days: int = 3) -> int:
    """Listings not seen by any scan for `days` days are treated as sold/removed."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    conn = get_conn()
    cur = conn.execute("UPDATE listings SET gone_at=? WHERE gone_at='' AND last_seen<? AND last_seen!=''",
                       (utcnow_iso(), cutoff))
    conn.commit()
    n = cur.rowcount
    conn.close()
    return n


def mark_gone(listing_id: str) -> None:
    conn = get_conn()
    conn.execute("UPDATE listings SET gone_at=?, status='HIDDEN' WHERE id=? AND gone_at=''", (utcnow_iso(), listing_id))
    conn.commit()
    conn.close()


def relist_info(conn, lid: str, title: str, price: float) -> Tuple[str, int]:
    """(fingerprint, number of other listings that look like the same ad: relists / cross-posts)."""
    fp = clf.fingerprint(title, price)
    n = conn.execute("SELECT COUNT(*) FROM listings WHERE fingerprint=? AND id!=? AND ABS(price_aud-?)<=?",
                     (fp, lid, price, max(60.0, price * 0.25))).fetchone()[0] if fp else 0
    return fp, int(n)


# ---------------------------------------------------------------------------------------------
# Trips
# ---------------------------------------------------------------------------------------------
def plan_pickup_trips(min_confidence: float = 60.0) -> List[Dict[str, Any]]:
    econ, _, _, _ = load_ctx()
    conn = get_conn()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM listings WHERE status!='HIDDEN' AND gone_at='' AND deal_tier IN ('STEAL','GOOD_DEAL','OFFER') "
        "AND confidence>=? AND flags NOT LIKE '%interstate%'", (min_confidence,))]
    conn.close()
    stops = [{"id": r["id"], "title": r["title"], "suburb": r["suburb"], "state": r["state"],
              "price_aud": r["price_aud"], "url": r["url"],
              # net before this item's solo travel cost, so the trip cost isn't double counted
              "net_profit_aud": r["net_profit_aud"] + r["travel_cost_aud"]} for r in rows]
    trips = routing.plan_trips(stops, econ.home_suburb, econ.hourly_value, econ.cost_per_km)
    urls = {s["id"]: s["url"] for s in stops}
    for t in trips:
        for st in t["stops"]:
            st["url"] = urls.get(st["id"], "")
    return trips
