"""
Facebook Marketplace AU collection, built to degrade gracefully instead of silently breaking.

How it reads Facebook
  Facebook server-renders every search result and listing page with the data as JSON inside the HTML
  (title, price, suburb, exact listed time, sold / pending flags, full description). We read that JSON
  directly instead of scraping visible text, so a cosmetic redesign cannot break us.

Strategy ladder (per search or listing page)
  1. HTTP with a real-browser TLS fingerprint (curl_cffi): ~0.6s, no browser, no account.
  2. Real Chrome via Playwright: page JSON + background API responses + visible cards as a last resort.
  Logged-in sessions (fb_login.py) prefer the browser, which can scroll past the logged-out 24 cap.

Safety nets
  * every attempt is recorded in scrape_runs (health.py) and surfaces on the dashboard
  * retries with jittered backoff, a circuit breaker that stops hammering a blocking Facebook
  * login walls, rate limits, empty results and layout changes are told apart
  * the first failing page of a run is saved to debug/ so a parser fix takes minutes
"""
import asyncio
import hashlib
import json
import logging
import random
import re
import time
import urllib.parse
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from curl_cffi import requests as cffi_requests
from playwright.async_api import async_playwright

import health

log = logging.getLogger("fb")

PROFILE_DIR = Path(os.environ.get("DEALS_DATA_DIR", str(Path(__file__).parent))) / "fb_browser_profile"
DEBUG_DIR = Path(__file__).parent / "debug"
ITEM_URL = "https://www.facebook.com/marketplace/item/{}/"

AU_STATES = ("NSW", "VIC", "QLD", "WA", "SA", "ACT", "TAS", "NT")
AU_STATE_SUFFIXES = tuple(f", {s}" for s in AU_STATES) + (" Australia",)
STATE_LABEL = {"NSW": "Sydney, NSW", "VIC": "Melbourne, VIC", "QLD": "Brisbane, QLD", "WA": "Perth, WA",
               "SA": "Adelaide, SA", "ACT": "Canberra, ACT", "TAS": "Hobart, TAS", "NT": "Darwin, NT"}
FB_CITY_SLUGS = {
    "NSW": "sydney", "VIC": "melbourne", "QLD": "brisbane", "WA": "perth", "SA": "adelaide",
    "ACT": "106270089409734",  # Canberra: numeric Marketplace location id
    "TAS": "111652435519898",  # Hobart: numeric Marketplace location id
    "ALL": "sydney",
}
IMPERSONATE = ("chrome131", "safari17_0")      # rotated across retries
HTTP_HEADERS = {"Accept-Language": "en-AU,en;q=0.9"}
JUST_LISTED_MIN = 90


# --------------------------------------------------------------------------------------
# Parsing (pure functions: tested offline against saved real pages)
# --------------------------------------------------------------------------------------
_SCRIPT_RE = re.compile(r'<script type="application/json"[^>]*>(.*?)</script>', re.S)


def _json_blobs(html: str, needle: str) -> Iterable[Any]:
    for m in _SCRIPT_RE.finditer(html):
        body = m.group(1)
        if needle in body:
            try:
                yield json.loads(body)
            except ValueError:
                continue


def _find_dicts(root: Any, key: str) -> List[Dict[str, Any]]:
    """Every dict anywhere in the tree that has `key`. Schema-agnostic on purpose: survives re-nesting."""
    out: List[Dict[str, Any]] = []
    stack = [root]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            if key in x:
                out.append(x)
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)
    return out


def _amount(price: Any) -> Optional[float]:
    if not isinstance(price, dict):
        return None
    try:
        v = float(str(price.get("amount")).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _age_text(minutes: float) -> str:
    if minutes < 60:
        return f"{max(1, int(minutes))} min"
    if minutes < 60 * 24:
        h = int(minutes // 60)
        return f"{h} hour{'s' if h != 1 else ''}"
    d = int(minutes // (60 * 24))
    return f"{d} day{'s' if d != 1 else ''}"


def listing_from_node(n: Dict[str, Any], state_hint: str = "NSW", now: Optional[float] = None
                      ) -> Tuple[Optional[Dict[str, Any]], str]:
    """One Facebook listing node -> our listing dict. Returns (listing, "") or (None, reason skipped)."""
    lid = str(n.get("id") or "")
    if not lid.isdigit():
        return None, "no_id"
    title = str(n.get("marketplace_listing_title") or n.get("custom_title") or "").strip()
    if not title:
        return None, "no_title"
    price = _amount(n.get("listing_price"))
    if price is None:
        return None, "no_price"
    geo = ((n.get("location") or {}).get("reverse_geocode")) or {}
    city, st = str(geo.get("city") or "").strip(), str(geo.get("state") or "").strip().upper()
    formatted = str((n.get("listing_price") or {}).get("formatted_amount") or "")
    if st and st not in AU_STATES:
        return None, "foreign"
    if not st and not formatted.startswith("AU$"):
        return None, "foreign"
    state = st or state_hint.upper()
    location = f"{city}, {st}" if city and st else (city or STATE_LABEL.get(state, "Australia"))

    now = now or time.time()
    posted = "Live FB Ad"
    ct = n.get("creation_time")
    listed_epoch = 0
    if isinstance(ct, (int, float)) and ct > 1_000_000_000:
        listed_epoch = int(ct)
        age_min = max(0.0, (now - ct) / 60)
        posted = "⚡ Just Listed" if age_min <= JUST_LISTED_MIN else f"Listed {_age_text(age_min)} ago"
    was = _amount(n.get("strikethrough_price")) or 0.0
    if was > price:
        posted = f"📉 Dropped -${int(was - price)} (Was ${int(was)})"
    photo = (((n.get("primary_listing_photo") or {}).get("image")) or {}).get("uri") or ""
    seller = n.get("marketplace_listing_seller") or {}
    url = ITEM_URL.format(lid)
    gone = bool(n.get("is_sold")) or n.get("is_live") is False
    return {
        "id": "fb_" + hashlib.md5(url.encode()).hexdigest()[:12],
        "source": "Facebook Marketplace",
        "title": title,
        "description": "",
        "price_aud": price,
        "was_price_aud": was,
        "location": location,
        "state": state,
        "url": url,
        "image_url": photo,
        "posted_at": posted,
        "listed_epoch": listed_epoch,
        "seller_id": str(seller.get("id") or ""),
        "seller_name": str(seller.get("name") or ""),
        "pending": bool(n.get("is_pending")),
        "shipping": "SHIPPING" in [str(x).upper() for x in (n.get("delivery_types") or [])],
        "gone": gone,
    }, ""


def parse_search_html(html: str, state_hint: str = "NSW", now: Optional[float] = None
                      ) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, int]]:
    """Listings embedded in a search page. Returns ({listing id: listing}, stats)."""
    return _parse_blobs(_json_blobs(html, "marketplace_listing_title"), state_hint, now)


def parse_graphql_text(text: str, state_hint: str = "NSW", now: Optional[float] = None
                       ) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, int]]:
    """Background API responses can hold several JSON documents, one per line."""
    docs = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{") and "marketplace_listing_title" in line:
            try:
                docs.append(json.loads(line))
            except ValueError:
                continue
    return _parse_blobs(docs, state_hint, now)


def _parse_blobs(blobs: Iterable[Any], state_hint: str, now: Optional[float]
                 ) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, int]]:
    out: Dict[str, Dict[str, Any]] = {}
    stats = {"nodes": 0, "parsed": 0, "skipped": 0}
    seen = set()
    for blob in blobs:
        for n in _find_dicts(blob, "marketplace_listing_title"):
            if "listing_price" not in n:
                continue                       # a title mention, not a listing node
            key = str(n.get("id"))
            if key in seen:
                continue
            seen.add(key)
            stats["nodes"] += 1
            item, why = listing_from_node(n, state_hint, now)
            if item is None:
                stats["skipped"] += 1
                stats[why] = stats.get(why, 0) + 1
                continue
            out[item["id"]] = item
            stats["parsed"] += 1
    return out, stats


def parse_item_html(html: str, item_id: str, now: Optional[float] = None) -> Dict[str, Any]:
    """Full description and state of one listing page."""
    res: Dict[str, Any] = {"ok": False, "full_description": "", "listed_time": "", "condition": "",
                           "suburb": "", "gone": False, "pending": False, "shipping": False}
    node = None
    for blob in _json_blobs(html, "redacted_description"):
        for n in _find_dicts(blob, "redacted_description"):
            if str(n.get("id")) == str(item_id) or node is None:
                node = n
                if str(n.get("id")) == str(item_id):
                    break
    if node is not None:
        desc = ((node.get("redacted_description") or {}).get("text")) or ""
        desc = re.sub(r"\s*\n\s*", " | ", desc.strip())
        res["full_description"] = desc[:4000]
        res["ok"] = bool(desc)
        res["pending"] = bool(node.get("is_pending"))
        res["gone"] = bool(node.get("is_sold")) or node.get("is_live") is False
        res["shipping"] = bool(node.get("is_shipping_offered"))
        loc = node.get("location_text")
        res["suburb"] = str((loc.get("text") if isinstance(loc, dict) else loc) or "")
        ct = node.get("creation_time")
        if isinstance(ct, (int, float)) and ct > 1_000_000_000:
            age = _age_text(max(0.0, ((now or time.time()) - ct) / 60))
            res["listed_time"] = f"Listed {age} ago" + (f" in {res['suburb']}" if res["suburb"] else "")
        return res
    # No listing data. Only call it removed when Facebook's own "not available" page is present;
    # anything else is an unknown layout and must NOT hide a live listing.
    if "CometErrorRoot" in html and "isn't available" in html:
        res["gone"] = True
    return res


def classify_page(html: str, http_status: int, final_url: str, nodes: int) -> str:
    """ok | empty | login_wall | rate_limited | blocked | layout_change"""
    if http_status == 429:
        return "rate_limited"
    if http_status in (401, 403) or http_status >= 500:
        return "blocked"
    low_url = final_url.lower()
    if "/login" in low_url or "checkpoint" in low_url or "/recover" in low_url:
        return "login_wall"
    if nodes > 0:
        return "ok"
    if '"marketplace_search"' in html and "feed_units" in html:
        return "empty"                     # normal page, genuinely zero results
    if 'id="login_form"' in html or 'name="login"' in html:
        return "login_wall"
    return "layout_change"


# --------------------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------------------
def _http_get_sync(url: str, impersonate: str) -> Tuple[int, str, str]:
    r = cffi_requests.get(url, impersonate=impersonate, timeout=20, headers=HTTP_HEADERS)
    return r.status_code, r.text, str(r.url)


async def http_get(url: str, attempt: int = 0) -> Tuple[int, str, str]:
    """Swappable in tests."""
    return await asyncio.to_thread(_http_get_sync, url, IMPERSONATE[attempt % len(IMPERSONATE)])


def search_url(query: str, city_slug: str, newest: bool = False, pmin: Optional[int] = None,
               pmax: Optional[int] = None, category: str = "") -> str:
    """A text search, or (category set, query empty) a browse of a whole marketplace category."""
    params = []
    if category:
        url = f"https://www.facebook.com/marketplace/{city_slug}/{category}"
    else:
        url = f"https://www.facebook.com/marketplace/{city_slug}/search"
        params.append(f"query={urllib.parse.quote(query.strip())}")
    if newest:
        params.append("sortBy=creation_time_descend")
    if pmin is not None:
        params.append(f"minPrice={int(pmin)}")
    if pmax is not None:
        params.append(f"maxPrice={int(pmax)}")
    return url + ("?" + "&".join(params) if params else "")


def save_debug(tag: str, html: str) -> Optional[str]:
    """Keep the last few failing pages so a parser fix is quick. Never raises."""
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        path = DEBUG_DIR / f"fb_{tag}_{int(time.time())}.html"
        path.write_text(html[:2_000_000])
        for old in sorted(DEBUG_DIR.glob("fb_*.html"))[:-8]:
            old.unlink()
        return str(path)
    except Exception:
        return None


async def _launch_profile_context(p: Any, headless: bool) -> Any:
    kwargs = dict(user_data_dir=str(PROFILE_DIR), headless=headless, locale="en-AU",
                  viewport={"width": 1440, "height": 900})
    try:
        return await p.chromium.launch_persistent_context(channel="chrome", **kwargs)
    except Exception as e:
        log.warning("Chrome channel unavailable for profile (%s); using bundled Chromium", e)
        return await p.chromium.launch_persistent_context(**kwargs)


async def _launch_browser(p: Any) -> Any:
    try:
        return await p.chromium.launch(channel="chrome", headless=True)
    except Exception as e:
        log.warning("Chrome channel unavailable (%s); falling back to bundled Chromium", e)
        return await p.chromium.launch(headless=True)


_EXTRACT_CARDS_JS = """() => Array.from(document.querySelectorAll('a[href*="/marketplace/item/"]')).map(a => {
    const img = a.querySelector('img');
    return {href: a.getAttribute('href') || '', text: a.innerText || '',
            img_src: img ? (img.getAttribute('src') || '') : ''};
})"""


def parse_aud(price_str: str) -> Optional[float]:
    m = re.search(r"(?:A?\$)?\s*(\d+(?:\.\d{1,2})?)", (price_str or "").replace(",", ""))
    return float(m.group(1)) if m else None


def cards_to_listings(cards: List[Dict[str, Any]], state: str) -> Dict[str, Dict[str, Any]]:
    """LAST-RESORT parser: visible card text. Only used for cards the structured data did not cover."""
    out: Dict[str, Dict[str, Any]] = {}
    for c in cards:
        href = (c.get("href") or "").split("?")[0]
        m = re.search(r"/marketplace/item/(\d+)", href)
        if not m:
            continue
        lines = [ln.strip() for ln in (c.get("text") or "").splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        prices: List[float] = []
        for ln in lines[:3]:
            for v in re.findall(r"A?\$\s*(\d[\d,]*)", ln):
                try:
                    prices.append(float(v.replace(",", "")))
                except ValueError:
                    pass
        prices = [p for p in prices if p > 0]
        if not prices:
            continue
        price, was = prices[0], (prices[1] if len(prices) > 1 and prices[1] > prices[0] else 0.0)
        title_c: List[str] = []
        loc = ""
        posted = "Live FB Ad"
        for ln in lines:
            if ln.lower() == "just listed":
                posted = "⚡ Just Listed"
            elif any(s in ln for s in AU_STATE_SUFFIXES):
                loc = ln
            elif "$" not in ln:
                title_c.append(ln)
        if not title_c or (not loc and not any("AU$" in ln for ln in lines[:3])):
            continue
        title = max(title_c, key=len)
        if was:
            posted = f"📉 Dropped -${int(was - price)} (Was ${int(was)})"
        url = ITEM_URL.format(m.group(1))
        out[m.group(1)] = {
            "id": "fb_" + hashlib.md5(url.encode()).hexdigest()[:12], "source": "Facebook Marketplace",
            "title": title, "description": "", "price_aud": price, "was_price_aud": was,
            "location": loc or STATE_LABEL.get(state.upper(), "Australia"), "state": state.upper(),
            "url": url, "image_url": c.get("img_src", ""), "posted_at": posted, "gone": False,
        }
    return out


# --------------------------------------------------------------------------------------
# A scan run: shared browser, retries, circuit breaker, health
# --------------------------------------------------------------------------------------
class FbRun:
    def __init__(self, state: str = "NSW", errors: Optional[List[str]] = None, concurrency: int = 3,
                 prefer_browser: Optional[bool] = None):
        self.state = state.upper()
        self.city = FB_CITY_SLUGS.get(self.state, "sydney")
        self.errors = errors if errors is not None else []
        self.sem = asyncio.Semaphore(concurrency)
        self.prefer_browser = PROFILE_DIR.exists() if prefer_browser is None else prefer_browser
        self.http_fail_streak = 0
        self.http_disabled = False
        self.login_walled = False
        self.stats = {"searches": 0, "ok": 0, "failed": 0, "http": 0, "browser": 0}
        self._pw: Any = None
        self._browser: Any = None
        self._ctx: Any = None
        self._ctx_lock = asyncio.Lock()
        self._debug_saved = 0

    async def __aenter__(self) -> "FbRun":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        for closer in (self._ctx, self._browser):
            try:
                if closer:
                    await closer.close()
            except Exception:
                pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._ctx = self._browser = self._pw = None

    def _err(self, msg: str) -> None:
        if msg not in self.errors:
            self.errors.append(msg)

    async def _context(self) -> Any:
        async with self._ctx_lock:
            if self._ctx is None:
                self._pw = await async_playwright().start()
                if PROFILE_DIR.exists():
                    self._ctx = await _launch_profile_context(self._pw, headless=True)
                else:
                    self._browser = await _launch_browser(self._pw)
                    self._ctx = await self._browser.new_context(locale="en-AU", viewport={"width": 1440, "height": 900})
            return self._ctx

    # ---- search -------------------------------------------------------------------------
    async def search(self, query: str, newest: bool = False, pmin: Optional[int] = None, pmax: Optional[int] = None,
                     max_scrolls: int = 0, max_results: int = 200, meta: Optional[Dict[str, Any]] = None,
                     category: str = "") -> List[Dict[str, Any]]:
        url = search_url(query, self.city, newest, pmin, pmax, category)
        query = query or f"[{category}]"
        self.stats["searches"] += 1
        async with self.sem:
            await asyncio.sleep(random.uniform(0.15, 0.7))      # jitter: never a perfectly regular cadence
            order = ["browser", "http"] if self.prefer_browser else ["http", "browser"]
            status = "error"
            for strat in order:
                if strat == "http" and self.http_disabled:
                    continue
                if strat == "browser" and self.login_walled and not self.prefer_browser:
                    continue
                for attempt in range(3 if strat == "http" else 2):
                    t0 = time.time()
                    try:
                        if strat == "http":
                            items, status, st, html = await self._http_search(url, attempt)
                        else:
                            items, status, st, html = await self._browser_search(url, max_scrolls)
                    except Exception as e:
                        items, status, st, html = {}, f"error:{type(e).__name__}", {}, ""
                    note = st.get("note", "")
                    health.record("facebook", query, strat, status.split(":")[0], st.get("nodes", 0),
                                  st.get("parsed", 0), st.get("skipped", 0), int((time.time() - t0) * 1000), note)
                    self.stats[strat] += 1
                    if status in ("ok", "empty"):
                        if strat == "http":
                            self.http_fail_streak = 0
                        self.stats["ok"] += 1
                        if meta is not None:
                            meta["raw"] = st.get("nodes", 0)
                        return list(items.values())[:max_results]
                    if strat == "http":
                        self.http_fail_streak += 1
                        if self.http_fail_streak >= 4:
                            self.http_disabled = True
                            self._err("Facebook is rejecting plain requests; switched to the browser for this scan")
                    if status in ("layout_change", "blocked", "rate_limited") and html and self._debug_saved < 2:
                        self._debug_saved += 1
                        path = save_debug(status, html)
                        self._err(f"Facebook returned an unreadable page ({status}); saved to {path}")
                    if status == "login_wall":
                        break                                   # retrying the same method cannot help
                    await asyncio.sleep(min(8.0, 1.2 * (2 ** attempt)) + random.uniform(0, 0.6))
            self.stats["failed"] += 1
            if status == "login_wall":
                self.login_walled = True
                self._err(f"Facebook showed a login wall for '{query}'")
            else:
                self._err(f"Facebook query '{query}' failed: {status}")
            return []

    async def _http_search(self, url: str, attempt: int) -> Tuple[Dict[str, Dict[str, Any]], str, Dict[str, Any], str]:
        code, html, final = await http_get(url, attempt)
        items, st = parse_search_html(html, self.state)
        status = classify_page(html, code, final, st["nodes"])
        return items, status, st, html

    async def _browser_search(self, url: str, max_scrolls: int
                              ) -> Tuple[Dict[str, Dict[str, Any]], str, Dict[str, Any], str]:
        ctx = await self._context()
        page = await ctx.new_page()
        api_texts: List[str] = []

        async def on_response(resp: Any) -> None:
            try:
                if "/api/graphql" in resp.url:
                    api_texts.append(await resp.text())
            except Exception:
                pass

        page.on("response", lambda r: asyncio.ensure_future(on_response(r)))
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(2200)
            final = page.url
            if "/login" in final or "checkpoint" in final:
                return {}, "login_wall", {}, ""
            html = await page.content()
            items, st = parse_search_html(html, self.state)
            cards = await page.evaluate(_EXTRACT_CARDS_JS)
            stale = 0
            for _ in range(max_scrolls):
                before = len(items) + len(cards)
                await page.mouse.wheel(0, 3000)
                await page.wait_for_timeout(1300)
                for txt in api_texts:
                    more, mst = parse_graphql_text(txt, self.state)
                    for k, v in more.items():
                        if k not in items:
                            items[k] = v
                            st["nodes"] += 1
                            st["parsed"] += 1
                api_texts.clear()
                cards = await page.evaluate(_EXTRACT_CARDS_JS)
                stale = stale + 1 if len(items) + len(cards) == before else 0
                if stale >= 2:
                    break
            note = ""
            fallback = cards_to_listings(cards, self.state)
            have_urls = {v["url"] for v in items.values()}
            extra = {v["id"]: v for v in fallback.values() if v["url"] not in have_urls}
            if extra:
                items.update(extra)
                st["nodes"] += len(extra)
                st["parsed"] += len(extra)
                note = "dom_fallback"
            status = classify_page(html, 200, final, st["nodes"])
            if status == "layout_change" and cards:
                status = "ok"
            if max_scrolls > 0 and st["nodes"] <= 24:
                if await page.evaluate("[...document.querySelectorAll('[role=dialog]')].some(d => /log in/i.test(d.innerText))"):
                    self._err("Facebook limits logged-out searches to 24 results. Run fb_login.py once to log in for full results.")
            st["note"] = note
            return items, status, st, html
        finally:
            try:
                await page.close()
            except Exception:
                pass


# --------------------------------------------------------------------------------------
# Public API (signatures kept for app code and scripts)
# --------------------------------------------------------------------------------------
async def scrape_facebook_marketplace_au(
    queries: List[str], state: str = "NSW", max_per_query: int = 24, include_newest_pass: bool = True,
    concurrency: int = 5, errors: Optional[List[str]] = None, max_scrolls: int = 0, newest_only: bool = False,
) -> List[Dict[str, Any]]:
    deduped: Dict[str, Dict[str, Any]] = {}
    try:
        async with FbRun(state, errors, concurrency=min(concurrency, 4)) as run:
            jobs = []
            for q in queries:
                if newest_only:
                    jobs.append(run.search(q, True, None, None, max_scrolls, max_per_query))
                else:
                    jobs.append(run.search(q, False, None, None, max_scrolls, max_per_query))
                    if include_newest_pass:
                        jobs.append(run.search(q, True, None, None, max_scrolls, max_per_query))
            for batch in await asyncio.gather(*jobs, return_exceptions=True):
                if isinstance(batch, list):
                    for item in batch:
                        old = deduped.get(item["id"])
                        if old is None or "Just Listed" in item["posted_at"] or "Dropped" in item["posted_at"]:
                            deduped[item["id"]] = item
    except Exception as e:
        log.error("Facebook scrape aborted: %s", e)
        if errors is not None:
            errors.append(f"Facebook scan could not start: {type(e).__name__}: {e}")
    if not deduped and errors is not None and not errors:
        errors.append("Facebook returned no listings (possible block or layout change)")
    return list(deduped.values())


# Logged-out Facebook returns at most ~24 cards per search URL. Price filters work logged out, so a
# capped search is split into narrower price bands until each comes back under the cap.
FB_CAP_THRESHOLD = 22
MIN_RELEVANT_TO_SPLIT = 3


# Whole-category browses catch listings whose titles our searches never match ("Computer for sale").
FB_CATEGORIES = ("computers",)
FB_BROAD_QUERIES = ["gaming pc", "graphics card", "gpu", "rtx", "radeon", "geforce", "ryzen", "motherboard",
                    "pc parts", "pc bundle", "computer parts", "desktop pc"]
_PC_WORDS = re.compile(r"rtx|gtx|\brx\b|ryzen|intel|\bi[3579]\b|gpu|\bpc\b|motherboard|\bram\b|graphics|cpu|ssd|nvme|geforce|"
                       r"radeon|gaming|desktop|computer|\bam[45]\b|b[56]\d0|x[67]\d0|z[67-9]\d0|ddr[45]|tower|build", re.I)


def is_pc_related(item: Dict[str, Any]) -> bool:
    return bool(_PC_WORDS.search(f"{item.get('title', '')} {item.get('description', '')}"))


def is_relevant_to_query(query: str, item: Dict[str, Any]) -> bool:
    if query.startswith("["):                      # a category browse, labelled "[computers]"
        return is_pc_related(item)
    text = f"{item.get('title', '')} {item.get('description', '')}".lower()
    tokens = [t for t in re.split(r"[\s\-]+", query.lower()) if t]
    model = [t for t in tokens if re.search(r"\d", t) and len(t) >= 3]
    needed = model or [t for t in tokens if len(t) >= 2]
    return all(t in text for t in needed) if not model else any(t in text for t in model)


async def scrape_facebook_exhaustive(
    queries: List[str], state: str = "NSW", concurrency: int = 6, max_pages_per_query: int = 25,
    errors: Optional[List[str]] = None, stats: Optional[Dict[str, int]] = None,
    categories: Tuple[str, ...] = (),
) -> List[Dict[str, Any]]:
    deduped: Dict[str, Dict[str, Any]] = {}
    stats = stats if stats is not None else {}
    stats.setdefault("pages", 0)
    stats.setdefault("capped_queries", 0)
    errors = errors if errors is not None else []

    async with FbRun(state, errors, concurrency=min(concurrency, 4)) as run:

        async def fetch_band(query: str, lo: int, hi: Optional[int]) -> Tuple[List[Dict[str, Any]], int]:
            meta: Dict[str, Any] = {}
            cat = query[1:-1] if query.startswith("[") else ""
            items = await run.search("" if cat else query, True, lo if lo > 0 else None, hi, 0, 200, meta, cat)
            stats["pages"] += 1
            for it in items:
                deduped[it["id"]] = it
            return items, meta.get("raw", 0)

        async def sweep(query: str) -> None:
            pages = 0
            stack: List[Tuple[int, Optional[int]]] = [(0, None)]
            while stack:
                if pages >= max_pages_per_query:
                    stats["capped_queries"] += 1
                    errors.append(f"'{query}' has more listings than the per-query page limit ({max_pages_per_query})")
                    return
                wave = [stack.pop() for _ in range(min(3, len(stack), max_pages_per_query - pages))]
                results = await asyncio.gather(*(fetch_band(query, lo, hi) for lo, hi in wave))
                pages += len(wave)
                if run.login_walled:
                    return
                for (lo, hi), (items, raw) in zip(wave, results):
                    if raw < FB_CAP_THRESHOLD or not items:
                        continue
                    if sum(1 for i in items if is_relevant_to_query(query, i)) < MIN_RELEVANT_TO_SPLIT:
                        continue
                    if hi is not None and lo >= hi:
                        continue
                    prices = sorted(int(i["price_aud"]) for i in items)
                    med = prices[len(prices) // 2]
                    if med > lo:
                        stack.append((lo, med - 1))
                    stack.append((med, med))
                    if hi is None or med < hi:
                        stack.append((med + 1, hi))

        await asyncio.gather(*(sweep(q) for q in list(queries) + [f"[{c}]" for c in categories]),
                             return_exceptions=True)
    return list(deduped.values())


async def _inspect_browser(url: str) -> Dict[str, Any]:
    """Fallback: read the rendered page text (old method). Fragile by nature; used only if HTTP fails."""
    res: Dict[str, Any] = {"ok": False, "full_description": "", "listed_time": "", "condition": "", "suburb": "",
                           "gone": False}
    async with async_playwright() as p:
        browser = await _launch_browser(p)
        try:
            ctx = await browser.new_context(locale="en-AU", viewport={"width": 1366, "height": 900})
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(2400)
            html = await page.content()
            m = re.search(r"/marketplace/item/(\d+)", url)
            parsed = parse_item_html(html, m.group(1) if m else "")
            if parsed["ok"] or parsed["gone"]:
                return parsed
            try:
                see_more = page.locator("text=See more").first
                if await see_more.count() > 0:
                    await see_more.click(timeout=1500)
                    await page.wait_for_timeout(400)
            except Exception:
                pass
            lines = [ln.strip() for ln in (await page.locator("body").inner_text()).splitlines() if ln.strip()]
            details: List[str] = []
            in_details = False
            for i, ln in enumerate(lines):
                if ln.startswith("Listed ") and " in " in ln:
                    res["listed_time"] = ln
                elif ln == "Details":
                    in_details = True
                elif in_details:
                    if ln in ("Seller information", "Seller details", "Similar sponsored items", "Today's picks"):
                        break
                    if ln not in ("See more", "Show more", "Tags", "Location is approximate", "Condition"):
                        details.append(ln)
            if details:
                res["ok"] = True
                res["full_description"] = " | ".join(details[:35])
        finally:
            await browser.close()
    return res


async def inspect_facebook_item_details(url: str) -> Dict[str, Any]:
    """Full description, listed time and sold/removed state of one listing."""
    m = re.fullmatch(r"https://(?:www\.|m\.)?facebook\.com/marketplace/item/(\d+)/?(?:\?.*)?", url or "")
    if not m:
        return {"ok": False, "gone": False, "full_description": "", "listed_time": "", "condition": "",
                "suburb": "", "error": "URL is not a Facebook Marketplace listing"}
    item_id = m.group(1)
    canonical = ITEM_URL.format(item_id)
    status, html = "error", ""
    for attempt in range(3):
        t0 = time.time()
        try:
            code, html, final = await http_get(canonical, attempt)
            info = parse_item_html(html, item_id)
            if info["ok"] or info["gone"]:
                health.record("facebook", f"item {item_id}", "http", "ok", 1, 1, 0, int((time.time() - t0) * 1000),
                              "gone" if info["gone"] else "")
                return info
            status = classify_page(html, code, final, 0)
            if status == "empty":
                status = "layout_change"
        except Exception as e:
            status = f"error:{type(e).__name__}"
        health.record("facebook", f"item {item_id}", "http", status.split(":")[0], 0, 0, 0, int((time.time() - t0) * 1000))
        if status == "layout_change" and attempt == 0:
            save_debug("item_layout_change", html)
        if status == "login_wall":
            break
        await asyncio.sleep(1.0 * (attempt + 1) + random.uniform(0, 0.5))
    try:
        info = await _inspect_browser(canonical)
        health.record("facebook", f"item {item_id}", "browser", "ok" if (info["ok"] or info["gone"]) else status.split(":")[0],
                      1, int(info["ok"]), 0, 0)
        return info
    except Exception as e:
        return {"ok": False, "gone": False, "full_description": "", "listed_time": "", "condition": "", "suburb": "",
                "error": type(e).__name__}
