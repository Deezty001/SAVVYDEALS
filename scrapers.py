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
from playwright.async_api import BrowserContext, Page, async_playwright

log = logging.getLogger("scrapers")

# Created by fb_login.py. When present, scans reuse that logged-in session: logged-out Facebook
# caps every search at 24 results and shows a login dialog, so it cannot return "everything".
PROFILE_DIR = Path(__file__).parent / "fb_browser_profile"

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


async def _launch_profile_context(p: Any, headless: bool) -> Any:
    kwargs = dict(user_data_dir=str(PROFILE_DIR), headless=headless, locale="en-AU",
                  viewport={"width": 1440, "height": 900})
    try:
        return await p.chromium.launch_persistent_context(channel="chrome", **kwargs)
    except Exception as e:
        log.warning("Chrome channel unavailable for profile (%s); using bundled Chromium", e)
        return await p.chromium.launch_persistent_context(**kwargs)


async def _launch_browser(p: Any) -> Any:
    """Prefer the user's installed Chrome, fall back to Playwright's bundled Chromium."""
    try:
        return await p.chromium.launch(channel="chrome", headless=True)
    except Exception as chrome_err:
        log.warning("Chrome channel unavailable (%s); falling back to bundled Chromium", chrome_err)
        return await p.chromium.launch(headless=True)


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

FB_CITY_SLUGS = {
    "NSW": "sydney",
    "VIC": "melbourne",
    "QLD": "brisbane",
    "WA": "perth",
    "SA": "adelaide",
    "ACT": "106270089409734",  # Canberra, ACT numeric Facebook Marketplace location ID
    "TAS": "111652435519898",  # Hobart, TAS numeric Facebook Marketplace location ID
    "ALL": "sydney"
}

AU_STATE_SUFFIXES = (", NSW", ", VIC", ", QLD", ", WA", ", SA", ", ACT", ", TAS", ", NT", " Australia")

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


def parse_fb_cards_Price_and_was(lines: List[str]) -> Tuple[Optional[float], Optional[float]]:
    """
    Facebook Marketplace cards often have two price lines when a seller drops the price:
    Line 0: 'AU$600' (current price)
    Line 1: 'AU$750' (original strikethrough price)
    Returns (current_price_aud, original_price_aud)
    """
    prices: List[float] = []
    for ln in lines[:3]:
        if "$" in ln:
            # Handle concatenated 'AU$1,099AU$1,199' or single 'AU$600'
            matches = re.findall(r"A?\$\s*(\d[\d,]*)", ln)
            for m in matches:
                try:
                    val = float(m.replace(",", ""))
                    if val > 0:
                        prices.append(val)
                except ValueError:
                    pass
    if not prices:
        return None, None
    if len(prices) >= 2 and prices[1] > prices[0]:
        return prices[0], prices[1]
    return prices[0], None


async def _fetch_single_fb_query(
    ctx: BrowserContext,
    sem: asyncio.Semaphore,
    query: str,
    city_slug: str,
    state: str,
    sort_by_newest: bool,
    max_per_query: int,
    errors: Optional[List[str]] = None,
    max_scrolls: int = 0,
    price_min: Optional[int] = None,
    price_max: Optional[int] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    encoded_q = urllib.parse.quote(query.strip())
    url = f"https://www.facebook.com/marketplace/{city_slug}/search?query={encoded_q}"
    if sort_by_newest:
        url += "&sortBy=creation_time_descend"
    if price_min is not None:
        url += f"&minPrice={int(price_min)}"
    if price_max is not None:
        url += f"&maxPrice={int(price_max)}"

    extracted: List[Dict[str, Any]] = []
    async with sem:
        page: Optional[Page] = None
        try:
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=18000)
            await page.wait_for_timeout(2200)

            if "/login" in page.url or "checkpoint" in page.url:
                if errors is not None:
                    errors.append(f"Facebook showed a login wall for '{query}'")
                return extracted

            EXTRACT_JS = """() => Array.from(document.querySelectorAll('a[href*="/marketplace/item/"]')).map(a => {
                const img = a.querySelector('img');
                return {
                    href: a.getAttribute('href') || '',
                    text: a.innerText || '',
                    img_src: img ? (img.getAttribute('src') || '') : ''
                };
            })"""

            # Marketplace is infinite-scroll: keep scrolling and collecting until no new cards
            # appear for 2 rounds in a row, or we hit the scroll / card caps.
            collected: Dict[str, Dict[str, Any]] = {}
            stale_rounds = 0
            for _ in range(max(1, max_scrolls) + 1):
                for c in await page.evaluate(EXTRACT_JS):
                    key = (c.get("href") or "").split("?")[0]
                    if key and key not in collected:
                        collected[key] = c
                if len(collected) >= max_per_query:
                    break
                before = len(collected)
                await page.mouse.wheel(0, 3000)
                await page.wait_for_timeout(1300)
                for c in await page.evaluate(EXTRACT_JS):
                    key = (c.get("href") or "").split("?")[0]
                    if key and key not in collected:
                        collected[key] = c
                stale_rounds = stale_rounds + 1 if len(collected) == before else 0
                if stale_rounds >= 2:
                    break
            cards_data = list(collected.values())
            if meta is not None:
                meta["raw"] = len(cards_data)
            if max_scrolls > 0 and len(cards_data) <= 24 and errors is not None:
                has_login_dialog = await page.evaluate(
                    "[...document.querySelectorAll('[role=dialog]')].some(d => /log in/i.test(d.innerText))")
                if has_login_dialog:
                    msg = ("Facebook limits logged-out searches to 24 results. "
                           "Run fb_login.py once to log in for full results.")
                    if msg not in errors:
                        errors.append(msg)

            for c in cards_data[:max_per_query]:
                href = c.get("href", "")
                if not href:
                    continue
                clean_path = href.split("?")[0]
                full_url = clean_path if clean_path.startswith("http") else f"https://www.facebook.com{clean_path}"

                lines = [ln.strip() for ln in c.get("text", "").splitlines() if ln.strip()]
                if len(lines) < 2:
                    continue

                price_aud, was_price_aud = parse_fb_cards_Price_and_was(lines)
                if price_aud is None:
                    continue

                title_candidates: List[str] = []
                loc_str = ""
                posted_badge = "Live FB Ad"
                has_au_dollar = any("AU$" in ln for ln in lines[:3])

                for ln in lines:
                    if ln.lower() == "just listed":
                        posted_badge = "⚡ Just Listed"
                    elif any(st in ln for st in AU_STATE_SUFFIXES):
                        loc_str = ln
                    elif "$" not in ln:
                        title_candidates.append(ln)

                # Strict Australia-only guard: skip any foreign listing (e.g. CA, US)
                if not loc_str and not has_au_dollar:
                    continue
                if not loc_str:
                    loc_str = GUMTREE_LOCATIONS.get(state.upper(), GUMTREE_LOCATIONS["NSW"])["label"]

                if not title_candidates:
                    continue

                title = max(title_candidates, key=len)
                desc_parts = [l for l in lines if l != title]
                if was_price_aud and was_price_aud > price_aud:
                    drop_amt = int(was_price_aud - price_aud)
                    posted_badge = f"📉 Dropped -${drop_amt} (Was ${int(was_price_aud)})"

                lid = "fb_" + hashlib.md5(full_url.encode()).hexdigest()[:12]
                extracted.append({
                    "id": lid,
                    "source": "Facebook Marketplace",
                    "title": title,
                    "description": " • ".join(desc_parts),
                    "price_aud": price_aud,
                    "was_price_aud": was_price_aud or 0.0,
                    "location": loc_str,
                    "state": state.upper(),
                    "url": full_url,
                    "image_url": c.get("img_src", ""),
                    "posted_at": posted_badge
                })
        except Exception as e:
            log.warning("FB query %r failed: %s", query, e)
            if errors is not None:
                errors.append(f"Facebook query '{query}' failed: {type(e).__name__}")
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass
    return extracted


async def scrape_facebook_marketplace_au(
    queries: List[str],
    state: str = "NSW",
    max_per_query: int = 24,
    include_newest_pass: bool = True,
    concurrency: int = 5,
    errors: Optional[List[str]] = None,
    max_scrolls: int = 0,
    newest_only: bool = False,
) -> List[Dict[str, Any]]:
    """
    High-speed parallel Facebook Marketplace AU scraper.
    Uses up to `concurrency` parallel tabs inside an isolated incognito browser context
    (0% connection to user's personal Facebook account) and fetches both Best Match
    and Newest Listed (`sortBy=creation_time_descend`) so every relevant listing is captured.
    """
    city_slug = FB_CITY_SLUGS.get(state.upper(), "sydney")
    deduped: Dict[str, Dict[str, Any]] = {}
    sem = asyncio.Semaphore(concurrency)

    try:
        async with async_playwright() as p:
            browser = None
            if PROFILE_DIR.exists():
                ctx = await _launch_profile_context(p, headless=True)
            else:
                browser = await _launch_browser(p)
                ctx = await browser.new_context(locale="en-AU", viewport={"width": 1440, "height": 900})

            tasks = []
            for q in queries:
                if newest_only:
                    tasks.append(_fetch_single_fb_query(ctx, sem, q, city_slug, state, True, max_per_query, errors, max_scrolls))
                    continue
                tasks.append(_fetch_single_fb_query(ctx, sem, q, city_slug, state, False, max_per_query, errors, max_scrolls))
                # Newest-first pass is what finds fresh listings; run it for every query when asked.
                if include_newest_pass:
                    tasks.append(_fetch_single_fb_query(ctx, sem, q, city_slug, state, True, max_per_query, errors, max_scrolls))

            batches = await asyncio.gather(*tasks, return_exceptions=True)
            for batch in batches:
                if isinstance(batch, list):
                    for item in batch:
                        uid = item["id"]
                        if uid not in deduped or "Just Listed" in item.get("posted_at", "") or "Dropped" in item.get("posted_at", ""):
                            deduped[uid] = item

            await (browser.close() if browser else ctx.close())
    except Exception as e:
        log.error("Facebook scrape aborted: %s", e)
        if errors is not None:
            errors.append(f"Facebook scan could not start: {type(e).__name__}: {e}")

    if not deduped and errors is not None and not errors:
        errors.append("Facebook returned no listings (possible block or layout change)")
    return list(deduped.values())


# Logged-out Facebook returns at most ~24 cards per search URL. Price filters (minPrice/maxPrice)
# DO work logged out, so a search that hits the cap is split into narrower price bands until every
# band comes back under the cap. Unioning the bands enumerates the full result set.
FB_CAP_THRESHOLD = 22
MIN_RELEVANT_TO_SPLIT = 3  # a capped band with fewer relevant cards than this is treated as exhausted


def is_relevant_to_query(query: str, item: Dict[str, Any]) -> bool:
    """Facebook pads results with loosely related items. A card is 'relevant' if it contains the
    query's model token (e.g. '3080', 'b650', '5800x3d'); queries with no model token need all words."""
    text = f"{item.get('title', '')} {item.get('description', '')}".lower()
    tokens = [t for t in re.split(r"[\s\-]+", query.lower()) if t]
    model = [t for t in tokens if re.search(r"\d", t) and len(t) >= 3]
    needed = model or [t for t in tokens if len(t) >= 2]
    return all(t in text for t in needed) if not model else any(t in text for t in model)


async def scrape_facebook_exhaustive(
    queries: List[str],
    state: str = "NSW",
    concurrency: int = 6,
    max_pages_per_query: int = 25,
    errors: Optional[List[str]] = None,
    stats: Optional[Dict[str, int]] = None,
) -> List[Dict[str, Any]]:
    city_slug = FB_CITY_SLUGS.get(state.upper(), "sydney")
    deduped: Dict[str, Dict[str, Any]] = {}
    sem = asyncio.Semaphore(concurrency)
    stats = stats if stats is not None else {}
    stats.setdefault("pages", 0)
    stats.setdefault("capped_queries", 0)

    async def fetch_band(ctx: Any, query: str, lo: int, hi: Optional[int]) -> Tuple[List[Dict[str, Any]], int]:
        meta: Dict[str, Any] = {}
        items = await _fetch_single_fb_query(
            ctx, sem, query, city_slug, state, False, 200, errors, 0,
            price_min=lo if lo > 0 else None, price_max=hi, meta=meta)
        stats["pages"] += 1
        for it in items:
            deduped[it["id"]] = it
        return items, meta.get("raw", 0)

    async def sweep(ctx: Any, query: str) -> None:
        pages = 0
        stack: List[Tuple[int, Optional[int]]] = [(0, None)]
        while stack:
            if pages >= max_pages_per_query:
                stats["capped_queries"] += 1
                if errors is not None:
                    errors.append(f"'{query}' has more listings than the per-query page limit ({max_pages_per_query})")
                return
            wave = [stack.pop() for _ in range(min(3, len(stack), max_pages_per_query - pages))]
            results = await asyncio.gather(*(fetch_band(ctx, query, lo, hi) for lo, hi in wave))
            pages += len(wave)
            if errors and any("login wall" in e for e in errors):
                return
            for (lo, hi), (items, raw) in zip(wave, results):
                if raw < FB_CAP_THRESHOLD or not items:
                    continue  # under the cap: this band is fully enumerated
                if sum(1 for i in items if is_relevant_to_query(query, i)) < MIN_RELEVANT_TO_SPLIT:
                    continue  # mostly padding: the relevant listings in this band are exhausted
                if hi is not None and lo >= hi:
                    continue  # single exact price, cannot split further
                prices = sorted(int(i["price_aud"]) for i in items)
                med = prices[len(prices) // 2]
                if med > lo:
                    stack.append((lo, med - 1))
                stack.append((med, med))  # exact-median price, so duplicates at one price are not lost
                if hi is None or med < hi:
                    stack.append((med + 1, hi))

    try:
        async with async_playwright() as p:
            browser = None
            if PROFILE_DIR.exists():
                ctx = await _launch_profile_context(p, headless=True)
            else:
                browser = await _launch_browser(p)
                ctx = await browser.new_context(locale="en-AU", viewport={"width": 1440, "height": 900})
            await asyncio.gather(*(sweep(ctx, q) for q in queries), return_exceptions=True)
            await (browser.close() if browser else ctx.close())
    except Exception as e:
        log.error("Exhaustive Facebook scrape aborted: %s", e)
        if errors is not None:
            errors.append(f"Facebook scan could not start: {type(e).__name__}: {e}")
    return list(deduped.values())


async def inspect_facebook_item_details(url: str) -> Dict[str, Any]:
    """
    Opens a single Facebook Marketplace listing page (`/marketplace/item/<id>/`) in an isolated
    headless Chrome tab and extracts the full seller description, condition, listed time, and suburb.
    Essential for Full Gaming PCs & Bundles where the card title only says 'Gaming PC'
    and the actual GPU/CPU/RAM/SSD specs are inside the description.
    """
    result: Dict[str, Any] = {
        "ok": False,
        "full_description": "",
        "listed_time": "",
        "condition": "",
        "suburb": "",
        "gone": False,
    }
    if not is_safe_listing_url(url, ("www.facebook.com", "facebook.com", "m.facebook.com")):
        result["error"] = "URL is not a Facebook Marketplace listing"
        return result
    try:
        async with async_playwright() as p:
            browser = await _launch_browser(p)
            ctx = await browser.new_context(locale="en-AU", viewport={"width": 1366, "height": 900})
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(2400)

            # Click 'See more' if present so full spec list expands
            try:
                see_more = page.locator("text=See more").first
                if await see_more.count() > 0:
                    await see_more.click(timeout=1500)
                    await page.wait_for_timeout(400)
            except Exception:
                pass

            body_text = await page.locator("body").inner_text()
            lines = [ln.strip() for ln in body_text.splitlines() if ln.strip()]
            low = body_text.lower()
            if ("isn't available" in low or "no longer available" in low or "this listing has been removed" in low
                    or re.search(r"\bsold\b\s*[·•-]", low[:600])):
                result["gone"] = True

            listed_line = ""
            details_lines: List[str] = []
            in_details = False
            condition_str = ""

            for i, ln in enumerate(lines):
                if ln.startswith("Listed ") and " in " in ln:
                    listed_line = ln
                elif ln == "Details":
                    in_details = True
                    continue
                elif in_details:
                    if ln in ("Seller information", "Seller details", "Similar sponsored items", "Today's picks"):
                        break
                    if ln == "Condition" and i + 1 < len(lines):
                        condition_str = lines[i + 1]
                        continue
                    if ln not in ("See more", "Show more", "Tags", "Location is approximate"):
                        details_lines.append(ln)

            if details_lines:
                result["ok"] = True
                result["full_description"] = " | ".join(details_lines[:35])
                result["listed_time"] = listed_line
                result["condition"] = condition_str

            await browser.close()
    except Exception as e:
        log.warning("Deep inspect failed for %s: %s", url, e)
        result["error"] = f"{type(e).__name__}"

    return result


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
        resp = cffi_requests.get(url, impersonate="safari17_0", timeout=15)
        if resp.status_code != 200:
            log.warning("Gumtree returned HTTP %s for %r", resp.status_code, query)
            if errors is not None:
                errors.append(f"Gumtree HTTP {resp.status_code} for '{query}'")
            return results
        html = resp.text
    except Exception as e:
        log.warning("Gumtree request failed for %r: %s", query, e)
        if errors is not None:
            errors.append(f"Gumtree request failed for '{query}': {type(e).__name__}")
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
        resp = cffi_requests.get(url, impersonate="safari17_0", timeout=15)
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
