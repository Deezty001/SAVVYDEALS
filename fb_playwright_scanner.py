"""
Playwright Persistent Browser Session Scanner for Facebook Marketplace AU & Gumtree AU.

Why this exists:
Facebook Marketplace Australia often blocks unauthenticated headless HTTP requests or redirects to a login wall.
This CLI launches a persistent Chromium profile (`./fb_browser_profile`), lets you stay logged into Facebook,
scrolls through your configured Australian city & search queries, extracts listing cards (title, AUD price,
suburb, URL, thumbnail), and posts them directly to your local AU PC Deals Finder API (`http://127.0.0.1:8050/api/ingest`).

Usage:
  .venv/bin/python fb_playwright_scanner.py --state NSW --queries "rtx 4070,7800x3d,gaming pc rtx"
"""
import argparse
import asyncio
import re
import sqlite3
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List

import httpx

PROFILE_DIR = Path(__file__).parent / "fb_browser_profile"
API_BASE = "http://127.0.0.1:8050"

FB_CITY_MAP = {
    "NSW": "sydney",
    "VIC": "melbourne",
    "QLD": "brisbane",
    "WA": "perth",
    "SA": "adelaide",
    "ACT": "canberra",
    "TAS": "hobart",
}


STATE_SUFFIX_RE = re.compile(r",\s*(NSW|VIC|QLD|WA|SA|ACT|TAS|NT)\b")


def read_ingest_token() -> str:
    """The API requires this token; it lives in the local database."""
    try:
        conn = sqlite3.connect(str(Path(__file__).parent / "deals.db"))
        row = conn.execute("SELECT value FROM settings WHERE key = 'ingest_token'").fetchone()
        conn.close()
        return row[0] if row else ""
    except sqlite3.Error:
        return ""


def parse_price(text: str) -> float:
    m = re.search(r"A?\$(\d[\d,]*)", text)
    if not m:
        return 0.0
    return float(m.group(1).replace(",", ""))


async def scan_facebook_marketplace(state: str, queries: List[str], headless: bool = False) -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("Playwright is not installed yet. Install with:")
        print("  .venv/bin/pip install playwright && .venv/bin/playwright install chromium")
        return

    city_slug = FB_CITY_MAP.get(state.upper(), "sydney")
    collected: List[Dict[str, Any]] = []

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=headless,
            viewport={"width": 1366, "height": 900},
            locale="en-AU"
        )
        page = await context.new_page()

        for q in queries:
            encoded = urllib.parse.quote(q.strip())
            url = f"https://www.facebook.com/marketplace/{city_slug}/search?query={encoded}&sortBy=creation_time_descend"
            print(f"[FB Marketplace AU] Scanning: {q} ({city_slug.title()}) -> {url}")
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(3500)

            # Scroll once to load fresh listings
            await page.mouse.wheel(0, 1800)
            await page.wait_for_timeout(2000)

            cards = await page.evaluate("""() => {
                const anchors = Array.from(document.querySelectorAll('a[href*="/marketplace/item/"]'));
                return anchors.map(a => {
                    const lines = (a.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
                    const img = a.querySelector('img');
                    return {
                        href: a.href.split('?')[0],
                        lines: lines,
                        image_url: img ? img.src : ''
                    };
                });
            }""")

            for c in cards:
                lines = c.get("lines", [])
                if len(lines) < 2:
                    continue
                price_val = 0.0
                title_candidates = []
                location_str = f"{city_slug.title()}, {state.upper()}"
                for line in lines:
                    if "$" in line and price_val == 0.0:
                        price_val = parse_price(line)
                    elif STATE_SUFFIX_RE.search(line):
                        # ", NSW" style suffix only; bare "SA"/"WA"/"AU" matched titles like "SAPPHIRE"
                        location_str = line
                    else:
                        title_candidates.append(line)

                title = max(title_candidates, key=len) if title_candidates else lines[0]
                if price_val > 0 and title:
                    collected.append({
                        "source": "Facebook Marketplace",
                        "title": title,
                        "description": " | ".join(lines),
                        "price_aud": price_val,
                        "location": location_str,
                        "state": state.upper(),
                        "url": c["href"],
                        "image_url": c.get("image_url", ""),
                        "posted_at": "Live FB Scan"
                    })

        await context.close()

    if collected:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{API_BASE}/api/ingest",
                json={"listings": collected},
                headers={"X-Ingest-Token": read_ingest_token()},
            )
            if resp.status_code != 200:
                print(f"[FB Marketplace AU] Ingest rejected (HTTP {resp.status_code}): {resp.text[:200]}")
            else:
                print(f"[FB Marketplace AU] Sent {len(collected)} raw listings to Deal Engine -> {resp.json()}")
    else:
        print("[FB Marketplace AU] No listings extracted (check if login is required in browser window).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scan Facebook Marketplace AU for PC Part Deals")
    parser.add_argument("--state", default="NSW", help="Australian State (NSW, VIC, QLD, WA, SA, ACT, TAS)")
    parser.add_argument(
        "--queries",
        default="rtx 3080,rtx 4070,rtx 5070,rx 7800 xt,7800x3d,9800x3d,gaming pc rtx",
        help="Comma-separated search queries"
    )
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    args = parser.parse_args()
    q_list = [x.strip() for x in args.queries.split(",") if x.strip()]
    asyncio.run(scan_facebook_marketplace(args.state, q_list, headless=args.headless))
