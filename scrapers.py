"""
High-Concurrency Live Scrapers & Deep Inspector for:
1. Facebook Marketplace Australia (Primary — Multi-Tab Parallel Sweep + Deep Ad Spec Extraction)
2. Gumtree Australia (Safari 17 TLS Impersonation)
"""
import asyncio
import hashlib
import json
import logging
import re
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests
from config import PROXIES
from fb import (  # noqa: F401  (Facebook collection lives in fb.py; re-exported for existing imports)
    AU_STATE_SUFFIXES, FB_CITY_SLUGS, PROFILE_DIR, _launch_browser, _launch_profile_context,
    inspect_facebook_item_details, scrape_facebook_exhaustive, scrape_facebook_marketplace_au,
)
import health

log = logging.getLogger("scrapers")

# Created by fb_login.py. When present, scans reuse that logged-in session: logged-out Facebook
# caps every search at 24 results and shows a login dialog, so it cannot return "everything".

ALLOWED_LISTING_HOSTS = ("www.facebook.com", "facebook.com", "m.facebook.com", "www.gumtree.com.au", "gumtree.com.au")


def is_safe_listing_url(url: str, hosts: Tuple[str, ...] = ALLOWED_LISTING_HOSTS) -> bool:
    """Only https URLs on known marketplace hosts (blocks javascript:, file:, localhost, etc)."""
    try:
        u = urllib.parse.urlparse(url)
    except ValueError:
        return False
    return u.scheme == "https" and (u.hostname or "").lower() in hosts


def is_safe_image_url(url: str) -> bool:
    if not url:
        return True
    try:
        u = urllib.parse.urlparse(url)
    except ValueError:
        return False
    return u.scheme == "https" and bool(u.hostname)


GUMTREE_LOCATIONS = {
    "NSW": {"slug": "sydney", "loc_id": "l3003435", "label": "Sydney, NSW"},
    "VIC": {"slug": "melbourne", "loc_id": "l3001317", "label": "Melbourne, VIC"},
    "QLD": {"slug": "brisbane", "loc_id": "l3005721", "label": "Brisbane, QLD"},
    "WA": {"slug": "perth", "loc_id": "l3008303", "label": "Perth, WA"},
    "SA": {"slug": "adelaide", "loc_id": "l3006878", "label": "Adelaide, SA"},
    "ACT": {"slug": "canberra", "loc_id": "l3002977", "label": "Canberra, ACT"},
    "TAS": {"slug": "hobart", "loc_id": "l3000211", "label": "Hobart, TAS"},
    "ALL": {"slug": "australia", "loc_id": "", "label": "Australia"}
}

# Comprehensive Facebook Marketplace AU sweep queries covering every GPU, CPU, Socket & PC build
FB_FULL_SWEEP_QUERIES = [
    "rtx 3070", "rtx 3080", "rtx 3080 ti", "rtx 3090",
    "rtx 4060 ti", "rtx 4070", "rtx 4070 super", "rtx 4070 ti", "rtx 4080", "rtx 4090",
    "rtx 5070", "rtx 5080",
    "rx 6800 xt", "rx 7800 xt", "rx 7900 xtx", "rx 9070 xt",
    "5800x3d", "7800x3d", "9800x3d",
    "b650", "x670", "z790",
    "gaming pc rtx", "custom gaming pc"
]


def parse_aud_price(price_str: str) -> Optional[float]:
    if not price_str:
        return None
    cleaned = price_str.replace(",", "").strip()
    lower = cleaned.lower()
    if "free" in lower or "swap" in lower or "contact" in lower:
        return None
    match = re.search(r"(?:A?\$)?\s*(\d+(?:\.\d{1,2})?)", cleaned)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None
    return None


def _scrape_gumtree_sync(query: str, state: str = "NSW", radius_km: int = 50, max_results: int = 18, errors: Optional[List[str]] = None, page: int = 1) -> List[Dict[str, Any]]:
    loc_info = GUMTREE_LOCATIONS.get(state.upper(), GUMTREE_LOCATIONS["NSW"])
    encoded_q = urllib.parse.quote_plus(query)

    page_seg = f"page-{page}/" if page > 1 else ""
    if loc_info["loc_id"]:
        url = f"https://www.gumtree.com.au/s-{loc_info['slug']}/{page_seg}{encoded_q}/k0{loc_info['loc_id']}r{radius_km}"
    else:
        url = f"https://www.gumtree.com.au/s-{page_seg}{encoded_q}/k0"

    results: List[Dict[str, Any]] = []
    try:
        resp = cffi_requests.get(url, impersonate="safari17_0", timeout=15, proxies=PROXIES)
        if resp.status_code != 200:
            log.warning("Gumtree returned HTTP %s for %r", resp.status_code, query)
            if errors is not None:
                errors.append(f"Gumtree HTTP {resp.status_code} for '{query}'")
            health.record("gumtree", query, "http", "rate_limited" if resp.status_code == 429 else "blocked")
            return results
        html = resp.text
    except Exception as e:
        log.warning("Gumtree request failed for %r: %s", query, e)
        if errors is not None:
            errors.append(f"Gumtree request failed for '{query}': {type(e).__name__}")
        health.record("gumtree", query, "http", "error", note=type(e).__name__)
        return results

    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("a.user-ad-row-new-design, a[href*='/web/listing/'], a[href*='/s-ad/']")
    seen_urls = set()

    for a in cards:
        href = a.get("href", "")
        if not href or ("/web/listing/" not in href and "/s-ad/" not in href):
            continue
        full_url = href if href.startswith("http") else f"https://www.gumtree.com.au{href}"
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        title_el = a.select_one(".user-ad-row-new-design__title-span, [class*='title']")
        price_el = a.select_one(".user-ad-price-new-design__price, [class*='price']")
        desc_el = a.select_one(".user-ad-row-new-design__description-text, [class*='description']")
        loc_el = a.select_one(".user-ad-row-new-design__location, [class*='location']")
        img_el = a.select_one("img")

        title = title_el.get_text(" ", strip=True) if title_el else (a.get("aria-label") or "")
        price_text = price_el.get_text(" ", strip=True) if price_el else ""
        price_aud = parse_aud_price(price_text)

        if not title or price_aud is None:
            continue

        loc_text = loc_el.get_text(" ", strip=True) if loc_el else loc_info["label"]
        loc_text = re.sub(r"<\s*\d+\s*km", "", loc_text).strip() or loc_info["label"]

        lid = "gt_" + hashlib.md5(full_url.encode()).hexdigest()[:12]
        results.append({
            "id": lid,
            "source": "Gumtree AU",
            "title": title,
            "description": desc_el.get_text(" ", strip=True) if desc_el else "",
            "price_aud": price_aud,
            "location": loc_text,
            "state": state.upper(),
            "url": full_url,
            "image_url": img_el.get("src", "") if img_el else "",
            "posted_at": "Live Gumtree Ad"
        })
        if len(results) >= max_results:
            break

    if not cards:
        low = html.lower()
        health.record("gumtree", query, "http", "empty" if ("no results" in low or "0 results" in low) else "layout_change",
                      note="no ad cards found")
    else:
        health.record("gumtree", query, "http", "ok", nodes=len(seen_urls), parsed=len(results),
                      skipped=len(seen_urls) - len(results))
    return results


async def scrape_gumtree_au(
    query: str,
    state: str = "NSW",
    radius_km: int = 50,
    max_results: int = 18,
    errors: Optional[List[str]] = None,
    pages: int = 1,
) -> List[Dict[str, Any]]:
    """Fetch up to `pages` result pages, stopping early when a page adds nothing new."""
    out: Dict[str, Dict[str, Any]] = {}
    for page in range(1, max(1, pages) + 1):
        items = await asyncio.to_thread(_scrape_gumtree_sync, query, state, radius_km, max_results, errors, page)
        new = [i for i in items if i["id"] not in out]
        for i in new:
            out[i["id"]] = i
        if not new:
            break
        await asyncio.sleep(0.8)  # be polite between pages
    return list(out.values())


def build_fb_marketplace_url(query: str, state: str = "NSW") -> str:
    city = FB_CITY_SLUGS.get(state.upper(), "sydney")
    encoded_q = urllib.parse.quote(query)
    return f"https://www.facebook.com/marketplace/{city}/search?query={encoded_q}&sortBy=creation_time_descend"


GONE_MARKERS = ("no longer available", "this ad has been removed", "ad has expired", "ad is no longer",
                "this listing is no longer", "sold")


def _inspect_gumtree_sync(url: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": False, "full_description": "", "gone": False}
    if not is_safe_listing_url(url, ("www.gumtree.com.au", "gumtree.com.au")):
        out["error"] = "URL is not a Gumtree listing"
        return out
    try:
        resp = cffi_requests.get(url, impersonate="safari17_0", timeout=15, proxies=PROXIES)
    except Exception as e:
        out["error"] = type(e).__name__
        return out
    if resp.status_code in (404, 410):
        out["gone"] = True
        return out
    if resp.status_code != 200:
        out["error"] = f"HTTP {resp.status_code}"
        return out
    soup = BeautifulSoup(resp.text, "html.parser")
    title_text = (soup.title.string or "").lower() if soup.title else ""
    if any(m in title_text for m in GONE_MARKERS[:4]):
        out["gone"] = True
        return out
    desc = ""
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except ValueError:
            continue
        for node in (data if isinstance(data, list) else [data]):
            if isinstance(node, dict) and node.get("description"):
                desc = str(node["description"])
                break
        if desc:
            break
    if not desc:
        meta = soup.find("meta", attrs={"property": "og:description"}) or soup.find("meta", attrs={"name": "description"})
        desc = (meta.get("content") if meta else "") or ""
    desc = re.sub(r"\s+", " ", desc).strip()
    if desc:
        out.update(ok=True, full_description=desc[:5000])
    return out


async def inspect_gumtree_item_details(url: str) -> Dict[str, Any]:
    """Fetch a Gumtree ad page: full description, or gone=True when the ad was removed/sold."""
    return await asyncio.to_thread(_inspect_gumtree_sync, url)
