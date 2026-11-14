"""
Ad classification, risk flags and confidence. Pure functions.

An ad is one of: private, shop, laptop, wanted, multi (several options / variants) or
from (a "from $X" teaser price). Anything but private/shop cannot be valued reliably.
"""
import re
from typing import Any, Dict, List, Optional

WANTED = re.compile(r"(?<![a-z])(wtb|wanted|want to buy|looking (?:for|to buy)|iso|in search of)(?![a-z])", re.I)
LAPTOP = re.compile(r"\b(laptop|notebook|macbook|zbook|zephyrus|razer blade|legion\s*\d|rog strix scar|predator helios|alienware m\d+)\b", re.I)
SHOP = re.compile(
    r"\b(abn|pty\s*ltd|p/l|our (?:store|shop|showroom)|visit (?:us|our)|showroom|afterpay|zip\s*pay|zippay|"
    r"we (?:offer|sell|have|ship|stock|build|specialise)|trade[- ]?ins? (?:welcome|accepted)|business hours|"
    r"australia[- ]?wide (?:shipping|delivery)|free shipping|tax invoice|gst (?:invoice|inclusive)|www\.|\.com\.au|"
    r"open (?:mon|tue|7 days)|click (?:and|&) collect|pc store|computer store|custom pc builder)\b", re.I)
MULTI = re.compile(
    r"\b(multiple (?:options|configs?|configurations|models|specs)|various (?:specs|models|options)|"
    r"choose (?:your|from)|pick (?:your|a)|select (?:your|a)|build to order|available in (?:various|multiple)|"
    r"options? (?:available|from)|several (?:options|builds)|customi[sz]e|customisable|"
    r"(?:rtx|gtx|rx)\s*\d{4}[^.\n]{0,40}\bor\b[^.\n]{0,12}(?:rtx|gtx|rx)?\s*\d{4})\b", re.I)
FROM = re.compile(r"\b(?:from|starting (?:at|from)|prices? start(?:s|ing)? (?:at|from)|as low as|up to)\s*\$\s*\d", re.I)
NEW_ITEM = re.compile(r"\b(brand new|sealed|unopened|new in box|nib|bnib)\b", re.I)
SCAM = re.compile(r"\b(deposit (?:required|first)|pay (?:via|by) (?:gift ?card|crypto|western union)|whatsapp|"
                  r"telegram me|text me on \+?\d|overseas|shipping only from|dm me for (?:price|details))\b", re.I)


def classify(title: str, description: str = "", price: float = 0.0, source: str = "") -> Dict[str, Any]:
    text = f"{title}\n{description}"
    flags: List[str] = []
    kind = "private"
    if WANTED.search(title) or WANTED.search(description[:200]):
        kind = "wanted"
    elif LAPTOP.search(text):
        kind = "laptop"
    elif FROM.search(text):
        kind = "from"
    elif MULTI.search(text):
        kind = "multi"
    elif SHOP.search(text):
        kind = "shop"
    if NEW_ITEM.search(text):
        flags.append("new_item")
    if SCAM.search(text):
        flags.append("scam_words")
    return {"kind": kind, "flags": flags}


def fingerprint(title: str, price: float) -> str:
    """Loose identity of an ad for relist / cross-post detection (not price-sensitive)."""
    t = re.sub(r"[^a-z0-9 ]", " ", title.lower())
    t = re.sub(r"\b(sale|urgent|need gone|negotiable|obo|ono|pickup|sydney|nsw|must go|cheap|like new)\b", " ", t)
    return " ".join(sorted(set(t.split())))[:120]


def confidence_score(
    kind: str,
    flags: List[str],
    *,
    core_in_title: bool,
    is_full_pc: bool,
    inferred_residual: bool,
    desc_len: int,
    ambiguous: bool,
    value_ratio: Optional[float],
) -> float:
    """
    0-100. Below ~60 a listing can never be called a steal; it is queued for a deep read instead.
    `value_ratio` is asking price / estimated value.
    """
    score = 72.0
    if kind == "shop":
        score -= 25
    elif kind in ("multi", "from"):
        score -= 70
    if core_in_title:
        score += 10
    else:
        score -= 12
    if is_full_pc:
        score -= 8
        if inferred_residual:
            score -= 10
        if desc_len < 40:
            score -= 12                      # FB search cards carry no spec text
    if ambiguous:
        score -= 18
    if "scam_words" in flags:
        score -= 30
    if value_ratio is not None and value_ratio < 0.45:
        score -= 30                          # too good: placeholder, bait or mis-match
    return max(0.0, min(100.0, round(score, 1)))
