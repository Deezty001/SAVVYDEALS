"""Alert qualification and delivery (Discord, Telegram, ntfy, macOS). Message drafts are never auto-sent."""
from typing import Any, Dict, List

from db import get_all_settings
import pricing
from db import get_conn
from notifier import send_discord_alert, send_mac_notification, send_ntfy_alert, send_telegram_alert


def qualifies_for_alert(deal: Dict[str, Any], econ: pricing.Econ) -> bool:
    """Only high-confidence deals that meet the profit AND return targets."""
    flags = deal.get("flags") or ""
    return (deal["deal_tier"] in ("STEAL", "GOOD_DEAL") and deal["confidence"] >= 65
            and deal["net_profit_aud"] >= econ.min_net_profit and deal["roi_pct"] >= econ.min_roi_pct
            and "interstate_pc" not in flags and not deal.get("needs_read") and not deal.get("gone_at"))


async def dispatch_auto_alerts(saved_ids: List[str]) -> int:
    """
    Alert on qualifying deals: new ones, and ones that already existed but a price drop has
    just made qualify (re-alert). Channels: Discord, Telegram, ntfy phone push, macOS.
    """
    s = get_all_settings()
    tier = s.get("auto_notify_tier", "GOOD_DEAL")
    econ = pricing.Econ.from_settings(s)
    has_discord = bool(s.get("discord_webhook_url"))
    has_telegram = bool(s.get("telegram_bot_token") and s.get("telegram_chat_id"))
    has_ntfy = bool(s.get("ntfy_topic"))
    has_mac = s.get("mac_notify", "1") == "1"
    if tier == "OFF" or not saved_ids or not (has_discord or has_telegram or has_ntfy or has_mac):
        return 0
    allowed = ("STEAL",) if tier == "STEAL" else ("STEAL", "GOOD_DEAL")

    conn = get_conn()
    marks = ",".join("?" * len(saved_ids))
    rows = [dict(r) for r in conn.execute(
        f"SELECT * FROM listings WHERE id IN ({marks}) AND status != 'HIDDEN' ORDER BY net_profit_aud DESC", saved_ids)]
    conn.close()

    sent = 0
    for deal in rows:
        if deal["deal_tier"] not in allowed or not qualifies_for_alert(deal, econ):
            continue
        reason = ""
        if deal["notified"]:
            # already alerted: only again if the price has dropped meaningfully since that alert
            if not (deal["alerted_price"] and deal["price_aud"] <= deal["alerted_price"] * 0.95):
                continue
            reason = f"📉 PRICE DROP ${deal['alerted_price']:.0f} → ${deal['price_aud']:.0f}"
        if sent >= 10:  # never flood the channel after a big first scan
            break
        ok_any = False
        if has_discord:
            ok_any |= (await send_discord_alert(s["discord_webhook_url"], deal, reason))[0]
        if has_telegram:
            ok_any |= (await send_telegram_alert(s["telegram_bot_token"], s["telegram_chat_id"], deal, reason))[0]
        if has_ntfy:
            ok_any |= (await send_ntfy_alert(s["ntfy_topic"], deal, reason))[0]
        if has_mac:
            ok_any |= (await send_mac_notification(deal, reason))[0]
        if ok_any:
            c = get_conn()
            c.execute("UPDATE listings SET notified = 1, alerted_price = ? WHERE id = ?", (deal["price_aud"], deal["id"]))
            c.commit()
            c.close()
            sent += 1
    return sent


