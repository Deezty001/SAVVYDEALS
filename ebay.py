"""eBay AU listings via the official Browse API (client-credentials token, no scraping).

Enabled by EBAY_APP_ID + EBAY_CERT_ID. Searches are Buy-It-Now only (an auction's current bid is not a price
you can buy at), sorted newest first, restricted to items located in Australia.
Free quota is ~5,000 calls/day, so callers pass a bounded set of queries per pass.
"""
import asyncio
import base64
import logging
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

from config import EBAY_APP_ID, EBAY_CERT_ID

log = logging.getLogger("ebay")

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
SCOPE = "https://api.ebay.com/oauth/api_scope"
POSTCODE_STATE = {"0": "NT", "2": "NSW", "3": "VIC", "4": "QLD", "5": "SA", "6": "WA", "7": "TAS"}
_token: Dict[str, Any] = {"value": "", "exp": 0.0}

# Only the parts that make money: CPU, RAM, motherboard, SSD and combos. No cases / PSUs / coolers / GPUs.
EBAY_QUERIES = [
    # combos
    "cpu motherboard combo", "cpu motherboard ram combo", "ryzen motherboard combo", "intel motherboard combo",
    "cpu ram bundle", "motherboard ram bundle",
    # CPUs
    "ryzen 5", "ryzen 7", "ryzen 9", "ryzen 5 5600x", "ryzen 7 5800x3d", "ryzen 7 7800x3d", "ryzen 5 7600",
    "core i5", "core i7", "core i9", "core i5 12600k", "core i7 13700k",
    # RAM
    "ddr4 16gb", "ddr4 32gb", "ddr4 64gb", "ddr5 16gb", "ddr5 32gb", "ddr5 64gb",
    # motherboards
    "b550 motherboard", "x570 motherboard", "b450 motherboard", "b650 motherboard", "x670 motherboard",
    "z690 motherboard", "z790 motherboard", "b760 motherboard", "am5 motherboard", "am4 motherboard",
    # SSDs
    "nvme ssd 1tb", "nvme ssd 2tb", "nvme ssd 500gb", "samsung 990 pro", "samsung 980 pro", "sn850x", "sata ssd 1tb",
]
_rotation = {"i": 0}


def next_batch(n: int) -> List[str]:
    """Next n queries, wrapping round, so every query is refreshed every few passes within the daily quota."""
    qs = EBAY_QUERIES
    start = _rotation["i"] % len(qs)
    _rotation["i"] = start + n
    return (qs + qs)[start:start + n]


def enabled() -> bool:
    return bool(EBAY_APP_ID and EBAY_CERT_ID)


def state_from_postcode(postcode: str) -> str:
    pc = (postcode or "").strip()
    if pc[:2] in ("26", "29"):
        return "ACT"
    return POSTCODE_STATE.get(pc[:1], "")


def parse_item(it: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Map one Browse API item summary to the dict ingest_raw_listings expects."""
    try:
        price = float((it.get("price") or {}).get("value"))
    except (TypeError, ValueError):
        return None
    if (it.get("price") or {}).get("currency", "AUD") != "AUD":
        return None
    url = it.get("itemWebUrl") or ""
    m = re.search(r"/itm/(\d+)", url) or re.search(r"\|(\d+)\|", it.get("itemId") or "")
    if not m:
        return None
    loc = it.get("itemLocation") or {}
    state = state_from_postcode(loc.get("postalCode", ""))
    city = loc.get("city") or ""
    location = ", ".join(p for p in (city, state) if p) or "Australia"
    ship = ""
    for opt in it.get("shippingOptions") or []:
        cost = opt.get("shippingCost") or {}
        if cost.get("value") not in (None, ""):
            ship = "Free postage" if float(cost["value"]) == 0 else f"Postage ${float(cost['value']):.0f}"
            break
    epoch = 0
    try:
        epoch = int(datetime.fromisoformat((it.get("itemCreationDate") or "").replace("Z", "+00:00")).timestamp())
    except ValueError:
        pass
    desc = " · ".join(p for p in (it.get("condition"), ship, it.get("shortDescription")) if p)
    return {
        "id": "eb_" + m.group(1), "title": it.get("title") or "", "description": desc,
        "price_aud": price, "url": f"https://www.ebay.com.au/itm/{m.group(1)}",
        "image_url": (it.get("image") or {}).get("imageUrl") or "",
        "location": location, "state": state, "listed_epoch": epoch, "posted_at": "Live eBay listing",
    }


async def _get_token(client: httpx.AsyncClient) -> str:
    if _token["value"] and time.time() < _token["exp"] - 60:
        return _token["value"]
    basic = base64.b64encode(f"{EBAY_APP_ID}:{EBAY_CERT_ID}".encode()).decode()
    r = await client.post(TOKEN_URL, headers={"Authorization": f"Basic {basic}"},
                          data={"grant_type": "client_credentials", "scope": SCOPE})
    r.raise_for_status()
    body = r.json()
    _token.update(value=body["access_token"], exp=time.time() + int(body.get("expires_in", 7200)))
    return _token["value"]


async def scrape_ebay_au(queries: List[str], max_per_query: int = 50, errors: Optional[List[str]] = None,
                         concurrency: int = 3) -> List[Dict[str, Any]]:
    if not enabled() or not queries:
        return []
    errors = errors if errors is not None else []
    out: List[Dict[str, Any]] = []
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            token = await _get_token(client)
        except Exception as e:
            errors.append(f"eBay auth failed: {type(e).__name__}")
            return []
        headers = {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_AU"}

        async def one(q: str) -> None:
            async with sem:
                try:
                    r = await client.get(SEARCH_URL, headers=headers, params={
                        "q": q, "sort": "newlyListed", "limit": str(min(max_per_query, 200)),
                        "filter": "buyingOptions:{FIXED_PRICE},itemLocationCountry:AU,priceCurrency:AUD",
                    })
                    if r.status_code == 429:
                        errors.append("eBay rate limit reached")
                        return
                    r.raise_for_status()
                    for it in r.json().get("itemSummaries") or []:
                        row = parse_item(it)
                        if row:
                            out.append(row)
                except Exception as e:
                    errors.append(f"eBay search '{q}' failed: {type(e).__name__}")

        await asyncio.gather(*(one(q) for q in queries))
    return out
