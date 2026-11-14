"""
Alert dispatcher: Discord, Telegram, ntfy (phone push) and macOS desktop notifications.

Every alert carries: direct listing link, max buy, suggested offer, net profit, confidence,
suburb + drive time, and a READY-TO-SEND message. Messages are never sent for you; copy it or
tap the link on your phone and send it yourself (auto-sending gets Facebook accounts banned).
"""
import asyncio
import html
import json
import re
from typing import Any, Dict, List, Tuple

import httpx

NTFY_BASE = "https://ntfy.sh"


def _parts(deal: Dict[str, Any]) -> List[Dict[str, Any]]:
    if isinstance(deal.get("matched_parts_json"), str):
        try:
            return json.loads(deal["matched_parts_json"])
        except ValueError:
            return []
    return deal.get("matched_parts", [])


def _flags(deal: Dict[str, Any]) -> List[str]:
    f = deal.get("flags") or []
    return f.split(",") if isinstance(f, str) else f


def alert_lines(deal: Dict[str, Any], reason: str = "") -> Dict[str, str]:
    """Plain-text building blocks shared by every channel."""
    emoji = "🔥" if deal["deal_tier"] == "STEAL" else "✅" if deal["deal_tier"] == "GOOD_DEAL" else "🤝"
    head = f"{emoji} {deal['deal_tier'].replace('_', ' ')}: {deal['title'][:90]}"
    if reason:
        head = f"{reason} — {head}"
    where = deal.get("suburb") or deal.get("location") or ""
    drive = f" · ~{deal['drive_min']:.0f} min drive" if deal.get("drive_min") else ""
    money = (f"Ask ${deal['price_aud']:.0f} · Max buy ${deal.get('max_buy_aud', 0):.0f} · "
             f"Offer ${deal.get('offer_aud', 0):.0f}\n"
             f"Net profit ${deal.get('net_profit_aud', 0):.0f} ({deal.get('roi_pct', 0):.0f}% return) · "
             f"Confidence {deal.get('confidence', 0):.0f}/100")
    parts = ", ".join(f"{p['canonical_name']}" for p in _parts(deal)[:6])
    warn = [f.split(":", 1)[1] for f in _flags(deal) if f.startswith("falling:")]
    warn_line = f"⚠ Falling used price: {', '.join(warn)}" if warn else ""
    return {"head": head, "where": f"📍 {where.title()}{drive}", "money": money, "parts": parts,
            "warn": warn_line, "msg": deal.get("offer_message", ""), "url": deal["url"]}


async def send_discord_alert(webhook_url: str, deal: Dict[str, Any], reason: str = "") -> Tuple[bool, str]:
    if not webhook_url or not webhook_url.startswith("http"):
        return False, "Discord Webhook URL not configured."
    a = alert_lines(deal, reason)
    desc = f"{a['money']}\n{a['where']}\n🧩 {a['parts']}"
    if a["warn"]:
        desc += f"\n{a['warn']}"
    if a["msg"]:
        desc += f"\n\n**Message to send (copy it, don't auto-send):**\n```{a['msg']}```"
    payload = {"username": "AU PC Deal Sniper", "embeds": [{
        "title": a["head"][:250], "url": deal["url"], "description": desc[:3900],
        "color": 0x10B981 if deal["deal_tier"] == "STEAL" else 0x3B82F6,
        "footer": {"text": f"{deal['source']} · score {deal['deal_score']}/99"}}]}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(webhook_url, json=payload)
            return (resp.status_code in (200, 204)), f"Discord HTTP {resp.status_code}"
    except Exception as e:
        return False, f"Discord error: {type(e).__name__}"


async def send_telegram_alert(bot_token: str, chat_id: str, deal: Dict[str, Any], reason: str = "") -> Tuple[bool, str]:
    if not bot_token or not chat_id:
        return False, "Telegram Bot Token or Chat ID not configured."
    a = alert_lines(deal, reason)
    e = html.escape
    text = f"<b>{e(a['head'])}</b>\n\n{e(a['money'])}\n{e(a['where'])}\n🧩 {e(a['parts'])}"
    if a["warn"]:
        text += f"\n{e(a['warn'])}"
    if a["msg"]:
        text += f"\n\n<b>Tap to copy, then send yourself:</b>\n<code>{e(a['msg'])}</code>"
    text += f'\n\n🔗 <a href="{html.escape(deal["url"], quote=True)}">Open listing</a>'
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", json={
                "chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False})
            return resp.status_code == 200, f"Telegram HTTP {resp.status_code}"
    except Exception as ex:
        return False, f"Telegram error: {type(ex).__name__}"      # never echo: URL holds the token


async def send_ntfy_alert(topic: str, deal: Dict[str, Any], reason: str = "") -> Tuple[bool, str]:
    """Phone push via ntfy.sh (topic acts as the secret). Tap the notification = open the listing."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", topic or ""):
        return False, "ntfy topic not configured (8-64 chars, letters/digits/_-)."
    a = alert_lines(deal, reason)
    body = f"{a['money']}\n{a['where']}"
    if a["warn"]:
        body += f"\n{a['warn']}"
    if a["msg"]:
        body += f"\n\nMessage:\n{a['msg']}"
    headers = {"Title": a["head"][:120].encode("ascii", "ignore").decode() or "PC deal",
               "Click": deal["url"], "Priority": "high" if deal["deal_tier"] == "STEAL" else "default",
               "Tags": "moneybag", "Actions": f"view, Open listing, {deal['url']}"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{NTFY_BASE}/{topic}", content=body.encode("utf-8"), headers=headers)
            return resp.status_code == 200, f"ntfy HTTP {resp.status_code}"
    except Exception as ex:
        return False, f"ntfy error: {type(ex).__name__}"


def _osa(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')[:230]


async def send_mac_notification(deal: Dict[str, Any], reason: str = "") -> Tuple[bool, str]:
    a = alert_lines(deal, reason)
    script = (f'display notification "{_osa(a["money"].splitlines()[0])} · {_osa(a["where"])}" '
              f'with title "{_osa(a["head"])}" sound name "Glass"')
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.wait(), timeout=8)
        return proc.returncode == 0, "macOS notification"
    except Exception as ex:
        return False, f"macOS notify error: {type(ex).__name__}"
