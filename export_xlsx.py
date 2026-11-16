"""Excel export: deals, pickup trips, parts price guide and price trends."""
import io
import json
import re
from typing import Any, Dict, List, Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

import engine_ops
from db import get_conn

HEAD_FILL = PatternFill("solid", fgColor="1F2937")
TIER_FILL = {"STEAL": "C6EFCE", "GOOD_DEAL": "DDEBF7", "OFFER": "FFF2CC", "SUSPECT": "F8CBAD"}
FIELDS = [
    ("Tier", "deal_tier", 12), ("Title", "title", 52), ("Source", "source", 18), ("Suburb", "suburb", 16),
    ("State", "state", 7), ("Drive (min)", "drive_min", 10), ("Ask $", "price_aud", 10),
    ("Max buy $", "max_buy_aud", 11), ("Offer $", "offer_aud", 10), ("Net profit $", "net_profit_aud", 12),
    ("Return %", "roi_pct", 10), ("Est. value $", "estimated_value_aud", 12), ("Confidence", "confidence", 11),
    ("Kind", "kind", 9), ("Plan", "strategy", 11), ("Parts", "_parts", 50), ("Flags", "flags", 34),
    ("Posted", "posted_at", 18), ("Found", "discovered_at", 19), ("Link", "url", 40), ("Message to send", "offer_message", 60),
    ("Description", "_desc", 80),
]


def _sheet(ws, headers: List[str], widths: List[int], rows: List[List[Any]]) -> None:
    ws.append(headers)
    for c in ws[1]:
        c.font, c.fill = Font(bold=True, color="FFFFFF"), HEAD_FILL
        c.alignment = Alignment(vertical="center")
    for r in rows:
        ws.append(r)
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    if rows:
        ws.auto_filter.ref = ws.dimensions


def _listed(d: Dict[str, Any]) -> str:
    """When the ad was listed, Sydney time; falls back to when we first found it."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Australia/Sydney")
    try:
        if d.get("listed_at"):
            return datetime.fromtimestamp(d["listed_at"], tz).strftime("%d %b %Y %H:%M")
        found = datetime.fromisoformat(d["discovered_at"]).replace(tzinfo=timezone.utc)
        return found.astimezone(tz).strftime("%d %b %Y %H:%M")
    except (ValueError, KeyError, TypeError):
        return ""


def build_summary() -> bytes:
    """One sheet: Title, Description, Price, Location, Listed, Link."""
    conn = get_conn()
    listings = [dict(r) for r in conn.execute(
        "SELECT * FROM listings WHERE status!='HIDDEN' AND gone_at='' ORDER BY "
        "(CASE WHEN listed_at > 0 THEN listed_at ELSE CAST(strftime('%s', discovered_at) AS INTEGER) END) DESC")]
    conn.close()
    wb = Workbook()
    ws = wb.active
    ws.title = "Listings"
    rows = []
    for d in listings:
        desc = re.sub(r"\s+", " ", (d.get("description") or "").replace("[Deep Inspected] ", "")).strip()[:300]
        loc = ", ".join(x for x in (str(d.get("suburb") or "").title(), str(d.get("state") or "")) if x)
        rows.append([d.get("title"), desc, d.get("price_aud"), loc, _listed(d), d.get("url")])
    _sheet(ws, ["Title", "Description", "Price", "Location", "Listed", "Link"], [48, 70, 10, 20, 18, 45], rows)
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(vertical="top", wrap_text=c.column in (1, 2))
        row[2].number_format = '"$"#,##0'
        if row[5].value:
            row[5].hyperlink, row[5].font = row[5].value, Font(color="0563C1", underline="single")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_workbook(tiers: Optional[List[str]] = None) -> bytes:
    conn = get_conn()
    q = "SELECT * FROM listings WHERE status!='HIDDEN' AND gone_at=''"
    args: List[Any] = []
    if tiers:
        q += f" AND deal_tier IN ({','.join('?' * len(tiers))})"
        args = tiers
    listings = [dict(r) for r in conn.execute(q + " ORDER BY net_profit_aud DESC", args)]
    parts = [dict(r) for r in conn.execute(
        "SELECT canonical_name, category, socket_or_series, avg_used_aud, new_retail_aud, learned_used_aud, "
        "learned_n, trend_pct, price_source, updated_at FROM reference_parts ORDER BY category, canonical_name")]
    conn.close()

    wb = Workbook()
    ws = wb.active
    ws.title = "Deals"
    rows = []
    for d in listings:
        try:
            d["_parts"] = ", ".join(p["canonical_name"] for p in json.loads(d["matched_parts_json"]))
        except ValueError:
            d["_parts"] = ""
        d["_desc"] = (d.get("description") or "").replace("[Deep Inspected] ", "")[:32000]   # Excel cell limit 32,767
        rows.append([d.get(k) for _, k, _ in FIELDS])
    _sheet(ws, [h for h, _, _ in FIELDS], [w for _, _, w in FIELDS], rows)
    for row in ws.iter_rows(min_row=2):
        fill = TIER_FILL.get(row[0].value)
        if fill:
            row[0].fill = PatternFill("solid", fgColor=fill)
        for c in row:
            c.alignment = Alignment(vertical="top", wrap_text=c.column in (2, 16, 17, 21, 22))
        link = row[19]
        if link.value:
            link.hyperlink, link.font = link.value, Font(color="0563C1", underline="single")

    trips = engine_ops.plan_pickup_trips()
    _sheet(wb.create_sheet("Pickup trips"),
           ["Trip", "Stops", "Suburbs (in order)", "Total km", "Total min", "Trip cost $", "Separate cost $", "Saving $", "Net after travel $", "Items"],
           [6, 6, 38, 10, 10, 12, 14, 10, 16, 80],
           [[i, len(t["stops"]), " → ".join(s.title() for s in t["suburbs"]), t["total_km"], t["total_min"],
             t["trip_cost_aud"], t["separate_cost_aud"], t["saving_aud"], t["net_after_travel_aud"],
             " | ".join(f"{s['title'][:40]} (${s['price_aud']:.0f})" for s in t["stops"])]
            for i, t in enumerate(trips, 1)])

    _sheet(wb.create_sheet("Price guide"),
           ["Part", "Category", "Series/socket", "Guide used $", "New retail $", "Learned used $", "Learned n", "Trend %", "Source", "Updated"],
           [44, 12, 14, 13, 13, 14, 10, 9, 16, 20],
           [[p["canonical_name"], p["category"], p["socket_or_series"], p["avg_used_aud"], p["new_retail_aud"] or None,
             p["learned_used_aud"] or None, p["learned_n"], p["trend_pct"], p["price_source"], p["updated_at"]] for p in parts])

    trends: List[Dict[str, Any]] = engine_ops.price_trends()
    _sheet(wb.create_sheet("Falling prices"), ["Part", "Category", "Trend %", "Learned used $", "Listings", "Warning"],
           [44, 12, 9, 14, 9, 34],
           [[t["canonical_name"], t["category"], t["trend_pct"], t["learned_used_aud"], t["learned_n"], t["warning"]] for t in trends])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
