"""
Ready-to-send seller message. Deliberately NEVER sent automatically: automated messaging on
Facebook is the quickest way to get the account banned. You copy it (or tap the link on your
phone), glance at it, and send it yourself.
"""
import re


def _short_item(title: str) -> str:
    t = re.sub(r"[$\d,]+\s*(?:obo|ono|neg\w*)?\s*$", "", title, flags=re.I).strip(" -|,.")
    t = re.sub(r"\s+", " ", t)
    return t[:70] or "item"


def build_offer_message(title: str, ask: float, offer: float, suburb: str, max_buy: float, tier: str,
                        pickup_hint: str = "I can pick up today or tomorrow with cash") -> str:
    item = _short_item(title)
    where = f" I'm able to come to {suburb.title()}." if suburb else ""
    if offer >= ask:      # already at/below our ceiling: ask if it's available and secure it
        return f"Hi, is the {item} still available? I'll take it at ${ask:.0f}. {pickup_hint}.{where}"
    return (f"Hi, is the {item} still available? Would you take ${offer:.0f}? "
            f"{pickup_hint}.{where} Thanks!")
