"""
Scrape health: every collection attempt is recorded, so a silent break (layout change, login wall,
block) shows up on the dashboard and as an alert instead of as "no deals today".

Status per source:
  healthy   recent attempts succeed on the primary strategy
  degraded  working, but on a fallback strategy, partially parsing, or intermittently failing
  down      recent attempts all fail
  idle      nothing attempted recently
"""
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from db import get_conn, session

OK_STATUSES = ("ok", "empty")
WINDOW_RUNS = 12                 # judge a source on its most recent attempts
IDLE_AFTER_HOURS = 24
ALERT_COOLDOWN_S = 6 * 3600


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record(source: str, query: str, strategy: str, status: str, nodes: int = 0, parsed: int = 0,
           skipped: int = 0, ms: int = 0, note: str = "") -> None:
    """Never raises: health tracking must not break a scan."""
    try:
        with session() as conn:
            conn.execute(
                "INSERT INTO scrape_runs (ts, source, query, strategy, status, nodes, parsed, skipped, ms, note) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (_now(), source, query[:80], strategy, status, nodes, parsed, skipped, ms, note[:200]))
            conn.execute("DELETE FROM scrape_runs WHERE ts < ?",
                         ((datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ"),))
    except Exception:
        pass


def assess(runs: List[Dict[str, Any]], primary: str) -> Dict[str, Any]:
    """Pure: judge a list of recent runs (newest first). Separated from the DB so it can be tested."""
    if not runs:
        return {"status": "idle", "reasons": ["no recent attempts"]}
    ok = [r for r in runs if r["status"] in OK_STATUSES]
    reasons: List[str] = []
    if not ok:
        worst = max({r["status"] for r in runs}, key=lambda s: sum(1 for r in runs if r["status"] == s))
        why = {
            "login_wall": "Facebook is showing a login wall",
            "rate_limited": "Facebook is rate limiting us",
            "blocked": "requests are being blocked",
            "layout_change": "Facebook's page layout changed: listing data not found",
        }.get(worst, f"attempts failing ({worst})")
        return {"status": "down", "reasons": [why]}
    ratio = len(ok) / len(runs)
    if ratio < 0.7:
        reasons.append(f"only {len(ok)} of the last {len(runs)} attempts succeeded")
    if any(r["strategy"] != primary for r in ok[:5]):
        reasons.append("using a fallback method (slower, more fragile)")
    parsed = sum(r["parsed"] for r in ok)
    nodes = sum(r["nodes"] for r in ok)
    if nodes >= 20 and parsed / nodes < 0.6:
        reasons.append(f"only {parsed} of {nodes} listings could be read; layout may be shifting")
    if any("dom_fallback" in (r.get("note") or "") for r in ok[:5]):
        reasons.append("structured data missing; reading page text instead")
    return {"status": "degraded" if reasons else "healthy", "reasons": reasons}


def summary() -> Dict[str, Any]:
    conn = get_conn()
    try:
        since_idle = (datetime.now(timezone.utc) - timedelta(hours=IDLE_AFTER_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        since_hour = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        out: Dict[str, Any] = {}
        for source, primary in (("facebook", "http"), ("gumtree", "http")):
            runs = [dict(r) for r in conn.execute(
                "SELECT * FROM scrape_runs WHERE source=? AND ts>=? ORDER BY id DESC LIMIT ?",
                (source, since_idle, WINDOW_RUNS))]
            a = assess(runs, primary)
            last_ok = conn.execute("SELECT ts FROM scrape_runs WHERE source=? AND status IN ('ok','empty') "
                                   "ORDER BY id DESC LIMIT 1", (source,)).fetchone()
            hour = conn.execute("SELECT count(*), coalesce(sum(parsed),0) FROM scrape_runs WHERE source=? AND ts>=?",
                                (source, since_hour)).fetchone()
            out[source] = {**a, "last_ok_at": last_ok[0] if last_ok else None,
                           "attempts_last_hour": hour[0], "listings_seen_last_hour": hour[1],
                           "last_strategy": runs[0]["strategy"] if runs else None,
                           "recent": [{"ts": r["ts"], "query": r["query"], "strategy": r["strategy"],
                                       "status": r["status"], "parsed": r["parsed"], "note": r["note"]}
                                      for r in runs[:8]]}
        return out
    finally:
        conn.close()


_last_alert: Dict[str, float] = {}
_last_state: Dict[str, str] = {}


def alert_due(source: str, status: str) -> Optional[str]:
    """Return an alert message when a source newly goes down / recovers, rate limited per source."""
    key = f"{source}:{status}"
    if status not in ("down", "healthy"):
        return None
    prev = _last_state.get(source)
    _last_state[source] = status
    if status == "healthy":
        return f"{source.title()} scanning recovered" if prev == "down" else None
    if time.time() - _last_alert.get(key, 0) < ALERT_COOLDOWN_S:
        return None
    _last_alert[key] = time.time()
    return f"{source.title()} scanning is DOWN. No new listings are being collected."
