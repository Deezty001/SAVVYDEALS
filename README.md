# AU PC Part & Bundle Deal Sniper (Gumtree AU & Facebook Marketplace AU)

A local web dashboard, valuation engine, and notification dispatcher built for finding underpriced computer components, CPU/Motherboard/RAM combos, and full gaming PCs across **Gumtree Australia** and **Facebook Marketplace Australia**.

## Supported Hardware & Sockets
- **GPUs**: NVIDIA RTX 3000 (`3060`–`3090`), RTX 4000 (`4060`–`4090`), RTX 5000 (`5070`–`5090`), AMD Radeon RX 7000 (`7700 XT`–`7900 XTX`), RX 9000 (`9070`, `9070 XT`)
- **CPUs & Platforms**: AMD `AM4` (`5600X`, `5700X3D`, `5800X3D`), `AM5` Ryzen 7000 & 9000 (`7600X`, `7800X3D`, `9600X`, `9700X`, `9800X3D`, `9950X3D`), Intel `LGA1700` (`13600K`–`14900K`), Intel `LGA1851` (`Core Ultra 5 245K`, `265K`, `285K`)
- **Combos & Full Gaming PCs**: Automatically extracts multiple parts from a single listing (e.g., GPU + CPU + Motherboard + DDR4/DDR5 RAM + NVMe SSD + residual Case/PSU value) and calculates the **part-out profit margin in AUD**.

## Quick Start

```bash
cd /Users/adamkarim/Documents/au-pc-deals
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8050 --reload
```

Open `http://127.0.0.1:8050` in your browser.

## Collecting listings

1. **Scan now** (top right) queries Facebook Marketplace and Gumtree for your region. Problems (login wall, blocked requests, Chrome missing) are shown in a banner instead of failing silently. Type a part in the search box and press Enter to scan just that query.
2. **Facebook Playwright scanner** (most reliable, uses a persistent logged-in profile):
   ```bash
   .venv/bin/playwright install chromium
   .venv/bin/python fb_playwright_scanner.py --state NSW
   ```
   It reads the ingest token from `deals.db` automatically.
3. **Bookmarklet**: Settings → Facebook bookmarklet. Facebook may block it.

## Deep scan (everything)

**Deep scan** (top bar) runs in the background: ~160 queries generated from the price catalog (every GPU, CPU, chipset, RAM and SSD variant plus generic PC / bundle / category searches), newest-first, scrolling deep, with Gumtree pages 1-3. Results appear as each batch finishes; you can cancel. Choosing "All Australia" sweeps every state.

**Facebook caps logged-out searches at 24 results.** For real depth, log in once:
```bash
.venv/bin/python fb_login.py
```
Scans then reuse that session automatically (delete `fb_browser_profile/` to go back to logged-out). Use a throwaway account.

## How deals are valued

- **Used value** = guide blended with value learned from your own collected listings (`POST /api/learn`, also nightly), then **capped at 85% of new retail**. Push fresh retail prices with `POST /api/prices` (header `X-Ingest-Token`, body `{"prices":[{"canonical_name":"AMD Ryzen 7 9800X3D","new_retail_aud":629}]}`); every listing is re-valued.
- **Net profit** = resale value minus selling fees (6%), warranty reserve (3%), your time, GST on the margin and the cost of the drive. Targets: $50 net and 25% return (settings). Each listing shows **max buy** and a **suggested offer**.
- Tiers: `STEAL`, `GOOD_DEAL` (meet targets), `OFFER` (works with up to 15% off), `FAIR`, `OVERPRICED`, `SUSPECT` (too cheap to be real). Low-confidence listings (shops, "from $X", multi-option, vague PCs) can never be a deal until a **deep read** of the full ad raises confidence.
- Full PCs are valued as the better of selling whole or parting out; missing parts are inferred from the platform. Interstate parts get a shipping cost; interstate PCs are never alerted.
- **Trips**: `GET /api/trips` groups qualifying pickups by suburb into drive routes with time and cost saved. Drive times are offline estimates (about +/-25%), so check the real route.
- **Trends**: `GET /api/trends` lists parts whose used price is falling. Falling parts are valued lower and flagged.

## Alerts

Discord, Telegram, ntfy (phone push: set an unguessable topic, install the ntfy app) and macOS notifications. Each alert has the listing link, max buy, offer, net profit, confidence, suburb and drive time, and a **ready-to-send message**. Messages are **never sent automatically**: automated messaging gets Facebook accounts banned. Copy it and send it yourself. Alerts only fire for high-confidence deals meeting your targets, and again if a price drop makes a listing qualify.

## Background running

Turn on `scheduler_enabled` (Settings API). It runs a newest-first pass every ~10 minutes over a rotating slice of part searches, deep-reads promising low-confidence ads, detects removed ads, backs off when Facebook shows a login wall, and runs a deep scan + price learning nightly at 2am while the server is running.

## Facebook collection (`fb.py`)
Facebook embeds each search result and listing as JSON inside the page. We read that directly over plain HTTP with a
browser TLS fingerprint (about 0.6s per search, no browser, no account), so redesigns of the visible page do not break it.
Fallbacks, in order: real Chrome reading the same JSON and background API calls, then visible card text.
Every attempt is logged (`scrape_runs`) and shown under **Scan health**; a down or recovered source also sends an alert.
When a page cannot be read, the first one per run is saved in `debug/` for a quick parser fix.
Coverage: each background pass runs 12 broad searches (gaming pc, graphics card, gpu, ...) plus a rotating slice of 30 part
searches, newest first, so every part is re-checked about hourly and anything new surfaces within minutes. The nightly deep scan splits every
search by price band and also sweeps the whole Computers category, which catches listings with vague titles. **Scan health** shows how long
it takes us to find listings posted in the last 24 hours.
Logged out, Facebook caps a search at 24 results; the deep scan splits by price band to get past that. `fb_login.py`
(optional) logs in once so the browser path can scroll further. Sold and removed listings are retired automatically.

## Code layout
- `app.py`: FastAPI wiring, security middleware, deal / catalog / settings / engine routes.
- `scanning.py`: live and deep scans, automatic deep reads, the background scheduler (its own router).
- `fb.py`, `health.py`: Facebook collection (HTTP first, browser fallback) and scrape health tracking.
- `ingest.py`, `alerts.py`, `models.py`, `config.py`: storing scraped listings, alert delivery, request models, constants.
- `evaluator.py`, `pricing.py`, `classify.py`, `routing.py`, `messages.py`: the valuation engine (pure functions).
- `engine_ops.py`: database side of the engine: cached context, `apply_eval`, skip-if-unchanged re-valuation, learned prices, trends.
- Bump `ENGINE_VERSION` in `evaluator.py` when valuation logic changes; stored listings are re-valued once on next start.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```
`tests/real_listings.json` holds real listings, including the ones the old guide wrongly called steals.

## Hosted deployment (Coolify)

The Dockerfile builds a slim image (no browser; Facebook is read over HTTP). Set these environment variables:

- `DEALS_PUBLIC_URL`: e.g. `https://deals.savvycomputers.com.au` (allowed host + origin, bookmarklet endpoint).
- `DEALS_PASSWORD`: dashboard login. Required anywhere public; unset means no login (local use).
- Mount a persistent volume at `/data` (holds `deals.db`). `/healthz` is the health check.

To push from a home machine to the hosted instance (e.g. if Facebook blocks the VPS IP):
```bash
DEALS_API_BASE=https://deals.savvycomputers.com.au DEALS_INGEST_TOKEN=... .venv/bin/python fb_playwright_scanner.py --state NSW
```

Automate it with `scripts/push_to_hosted.sh` (reads `~/.config/deals-push.env`, outside the repo) and the launchd job in `scripts/com.savvy.dealspush.plist`, which pushes every 15 minutes.

## Security notes

- Locally the server binds to `127.0.0.1`; hosted, it sits behind Traefik with `DEALS_PASSWORD` login. The Host header is checked and cross-origin browser requests to the API are rejected.
- `/api/ingest` requires the secret `X-Ingest-Token` (stored in `deals.db`).
- Discord / Telegram secrets are write-only in the UI and `deals.db` is chmod 600. Don't share or commit `deals.db`.
- Listing URLs are limited to https Facebook / Gumtree hosts; everything shown in the dashboard is escaped and a strict Content-Security-Policy is applied.
- Scraping Facebook Marketplace is against Facebook's terms; the persistent-profile scanner can get that account restricted. Use a throwaway account.
