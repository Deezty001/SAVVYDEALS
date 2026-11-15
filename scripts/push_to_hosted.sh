#!/bin/sh
# Scan Facebook from this machine (home IP, not blocked) and push listings to the hosted dashboard.
# Config lives outside the repo in ~/.config/deals-push.env:
#   DEALS_API_BASE=https://deals.savvycomputers.com.au
#   DEALS_INGEST_TOKEN=<token from the hosted app: Settings, or /api/ingest-config while logged in>
#   DEALS_STATE=NSW            (optional, default NSW)
set -eu
cd "$(dirname "$0")/.."
. "${DEALS_PUSH_ENV:-$HOME/.config/deals-push.env}"
export DEALS_API_BASE DEALS_INGEST_TOKEN
exec .venv/bin/python fb_playwright_scanner.py --state "${DEALS_STATE:-NSW}" --headless
