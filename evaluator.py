"""
Deal evaluation engine.

listing text -> matched parts -> used value (capped at new retail, trend-adjusted)
             -> net profit after fees / time / warranty / GST / travel
             -> max buy, offer, confidence, tier.
"""
import json
import re
from typing import Any, Dict, List, Optional, Tuple

import classify as clf
import pricing
import routing
from messages import build_offer_message

BUNDLE_KEYWORDS = [
    r"\bgaming\s*pc\b", r"\bfull\s*pc\b", r"\bdesktop\s*pc\b", r"\bcustom\s*pc\b", r"\bpc\s*build\b",
    r"\bbundle\b", r"\bcombo\b", r"\bmobo\s*cpu\b", r"\bcpu\s*mobo\b", r"\btower\s*only\b",
    r"\bwhole\s*setup\b", r"\bcomplete\s*system\b",
]
FULL_PC_PATTERNS = BUNDLE_KEYWORDS[:5] + [r"\btower\b", r"\bwhole\s*setup\b", r"\bcomplete\s*system\b"]

CASE_PSU_COOLER_RESIDUAL = 160.0
DDR5_SOCKETS = ("AM5", "LGA1851")

HARD_EXCLUDES = [
    "heatsink", "cooler only", "original cooler", "shroud", "backplate only",
    "waterblock", "fan only", "replacement fan", "box only", "empty box",
    "faulty", "broken", "for parts", "not working", "no display", "artifacting",
    "spares or repair", "spares and repair", "dead",
]

FAKE_PRICE_PATTERNS = [
    r"send offers", r"make an offer", r"price is for attention",
    r"not \$1(?![\d,.])", r"not \$123(?![\d,.])", r"read description for prices",
]

NOT_A_GPU_CONTEXT = re.compile(
    r"(optiplex|thinkcentre|inspiron|vostro|precision|latitude|elitedesk|prodesk|thinkpad|ideacentre|"
    r"monitor|display|tiny|micro|sff|ddr\d|mhz|mt/s|\$|\d-)\s*$", re.I)
BARE_GPU = re.compile(
    r"(?<![\w$.,/-])((?:20[678]|30[5-9]|40[6-9]|50[6-9])0)(?:\s*(ti\s*super|ti|super|s))?(?![\w.,%/])"
    r"(?!\s*(?:mhz|mt|gb|w\b|hz|cl|\$|fps|k\b))", re.I)


def _contains_term(text_lower: str, term: str) -> bool:
    term = term.strip().lower()
    if not term:
        return False
    return re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", text_lower) is not None


def price_is_fake(text_lower: str) -> bool:
    return any(re.search(p, text_lower) for p in FAKE_PRICE_PATTERNS)


def is_excluded_listing(text: str, exclude_keywords: List[str]) -> bool:
    text_lower = text.lower()
    if price_is_fake(text_lower):
        return True
    if any(_contains_term(text_lower, hw) for hw in HARD_EXCLUDES):
        return True
    return any(_contains_term(text_lower, kw) for kw in exclude_keywords)


def _load(v: Any) -> List[str]:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return []
    return v or []


def _result(tier: str, reason: str = "", excluded: bool = False, **extra: Any) -> Dict[str, Any]:
    base = {
        "excluded": excluded, "reason": reason, "matched_parts": [], "is_bundle": False,
        "estimated_value_aud": 0.0, "profit_margin_aud": 0.0, "discount_pct": 0.0,
        "deal_score": 0.0, "deal_tier": tier, "net_profit_aud": 0.0, "max_buy_aud": 0.0,
        "offer_aud": 0.0, "roi_pct": 0.0, "confidence": 0.0, "kind": "private", "flags": [],
        "strategy": "", "suburb": "", "drive_min": 0.0, "travel_cost_aud": 0.0,
        "needs_read": False, "offer_message": "",
    }
    base.update(extra)
    return base


def _bare_gpu_texts(text: str) -> List[str]:
    """Bare model numbers ('Gaming pc 3070 R7 7800X3D') as synthetic 'rtx 3070 ti' strings."""
    out = []
    for m in BARE_GPU.finditer(text):
        before = text[max(0, m.start() - 16):m.start()]
        if NOT_A_GPU_CONTEXT.search(before):
            continue
        sfx = (m.group(2) or "").lower().replace("  ", " ")
        sfx = {"s": "super"}.get(sfx, sfx)
        out.append(f"rtx {m.group(1)}" + (f" {sfx}" if sfx else ""))
    return out


def _part_view(part: Dict[str, Any], econ: pricing.Econ) -> Dict[str, Any]:
    value = pricing.part_used_value(part, econ)
    trend = float(part.get("trend_pct") or 0)
    if trend <= -5:                      # falling stock: expect to resell below today's level
        value = round(value * (1 + trend / 100.0 * 0.5), 2)
    return {
        "id": part["id"], "canonical_name": part["canonical_name"], "category": part["category"],
        "socket_or_series": part["socket_or_series"],
        "avg_used_aud": value,
        "guide_used_aud": float(part["avg_used_aud"]),
        "new_retail_aud": float(part.get("new_retail_aud") or 0),
        "trend_pct": round(trend, 1),
        "good_deal_aud": round(value * 0.8, 2), "steal_aud": round(value * 0.68, 2),
    }


def _inferred(label: str, value: float, series: str = "Inferred") -> Dict[str, Any]:
    return {"id": 0, "canonical_name": label, "category": "System", "socket_or_series": series,
            "avg_used_aud": round(value, 2), "good_deal_aud": round(value * 0.8, 2),
            "steal_aud": round(value * 0.68, 2), "inferred": True}


def evaluate_listing(
    title: str,
    description: str,
    price_aud: float,
    reference_parts: List[Dict[str, Any]],
    exclude_keywords: List[str],
    econ: Optional[pricing.Econ] = None,
    location: str = "",
    state: str = "NSW",
    source: str = "",
) -> Dict[str, Any]:
    econ = econ or pricing.Econ()
    full_text = f"{title} {description}".lower()
    title_lower = title.lower()

    if price_aud <= 25 or price_aud in (111, 123, 1234, 9999):
        return _result("IGNORED", "Placeholder or invalid price", excluded=True)
    if is_excluded_listing(full_text, exclude_keywords):
        return _result("IGNORED", "Matched exclusion keyword (laptop/faulty/box only/WTB)", excluded=True)

    cls = clf.classify(title, description, price_aud, source)
    kind, flags = cls["kind"], list(cls["flags"])
    if kind in ("wanted", "laptop"):
        return _result("IGNORED", f"{kind} ad", excluded=True, kind=kind)

    # ---- match ---------------------------------------------------------------------------
    candidates: Dict[str, List[Tuple[bool, Dict[str, Any]]]] = {}

    def consider(part: Dict[str, Any], haystack: str, title_hay: str) -> None:
        pats = _load(part["regex_patterns"])
        exs = _load(part.get("exclude_patterns", "[]"))
        if any(re.search(ep, haystack, re.I) for ep in exs):
            return
        if not any(re.search(p, haystack, re.I) for p in pats):
            return
        in_title = any(re.search(p, title_hay, re.I) for p in pats)
        candidates.setdefault(part["category"], []).append((in_title, part))

    for part in reference_parts:
        consider(part, full_text, title_lower)

    if "GPU" not in candidates:                       # bare '3070' style model numbers
        for synthetic in _bare_gpu_texts(full_text):
            for part in reference_parts:
                if part["category"] == "GPU":
                    consider(part, synthetic, synthetic)
            if "GPU" in candidates:
                flags.append("bare_model_number")
                break

    matched: Dict[str, Dict[str, Any]] = {}
    ambiguous = False
    core_in_title = False
    for cat, options in candidates.items():
        title_hits = [p for t, p in options if t]
        pool = title_hits or [p for _, p in options]
        if len(title_hits) != 1 and len({p["id"] for p in pool}) > 1:
            ambiguous = True
        pick = min(pool, key=lambda p: pricing.part_used_value(p, econ))   # cheapest: never inflate
        matched[cat] = pick
        if title_hits and cat in ("GPU", "CPU", "Motherboard"):
            core_in_title = True
    parts = [_part_view(p, econ) for p in matched.values()]

    looks_like_pc = any(re.search(p, full_text) for p in BUNDLE_KEYWORDS)
    has_core = any(p["category"] in ("GPU", "CPU", "Motherboard") for p in parts)
    standalone = (bool(parts) and all(p["category"] in ("RAM", "Storage") for p in parts)
                  and not looks_like_pc and price_aud <= 600)
    if not parts or not (has_core or standalone):
        return _result("UNMATCHED", "No core catalog parts (GPU/CPU/Mobo) matched", kind=kind)

    is_full_pc = any(re.search(p, full_text) for p in FULL_PC_PATTERNS)
    is_bundle = len(parts) > 1 or looks_like_pc

    # ---- infer what the ad doesn't say, from the platform ----------------------------------
    inferred_any = False
    if is_full_pc:
        cats = {p["category"] for p in parts}
        parts.append(_inferred("Case + PSU + Cooler (assumed)", CASE_PSU_COOLER_RESIDUAL, "Full PC"))
        if "CPU" not in cats:
            gpu_val = next((p["avg_used_aud"] for p in parts if p["category"] == "GPU"), 0)
            cpu, mobo, ram = (240, 190, 75) if gpu_val >= 600 else (140, 135, 45)
            parts.append(_inferred("CPU (assumed from GPU tier)", cpu))
            parts.append(_inferred("Motherboard (assumed)", mobo))
            parts.append(_inferred("RAM (assumed)", ram))
            inferred_any = True
        else:
            cpu_series = next(p["socket_or_series"] for p in parts if p["category"] == "CPU")
            if "Motherboard" not in cats:
                same = [pricing.part_used_value(r, econ) for r in reference_parts
                        if r["category"] == "Motherboard" and r["socket_or_series"] == cpu_series]
                parts.append(_inferred(f"{cpu_series} Motherboard (assumed)", min(same) if same else 130))
                inferred_any = True
            if "RAM" not in cats:
                parts.append(_inferred("RAM (assumed)", 100 if cpu_series in DDR5_SOCKETS else 45))
                inferred_any = True
        if "Storage" not in cats:
            parts.append(_inferred("SSD (assumed)", 40))
            inferred_any = True

    real_sum = sum(p["avg_used_aud"] for p in parts if not p.get("inferred"))
    inferred_sum = sum(p["avg_used_aud"] for p in parts if p.get("inferred"))
    gross_value = real_sum + inferred_sum
    n_real = sum(1 for p in parts if not p.get("inferred"))

    # ---- location, travel ------------------------------------------------------------------
    home = econ.home_state
    interstate = bool(state) and state.upper() not in (home, "ALL", "")
    pick = routing.pickup_info(location, state if state and state != "ALL" else home,
                               econ.home_suburb, econ.hourly_value, econ.cost_per_km)
    shipping = travel = 0.0
    if interstate:
        if is_full_pc:
            flags.append("interstate_pc")
        else:
            shipping = econ.ship_part_aud
            flags.append("interstate_part")
    else:
        travel = pick["cost"]
        if not pick["exact"]:
            flags.append("suburb_approx")

    # ---- value & profit --------------------------------------------------------------------
    if is_full_pc:
        v = pricing.value_pc(real_sum, inferred_sum, n_real + len([p for p in parts if p.get("inferred")]) // 2, econ)
        strategy, sale_value, time_cost = v["strategy"], v["sale_value"], v["time_cost"]
    elif n_real > 1:
        strategy, sale_value, time_cost = "part-out", gross_value * 0.97, econ.time_cost_part * n_real
    else:
        strategy, sale_value, time_cost = "resell", gross_value, econ.time_cost_part

    net = pricing.net_profit(sale_value, price_aud, econ, time_cost, shipping, travel)
    roi = pricing.roi_pct(net, price_aud, shipping)
    max_buy = pricing.max_buy_price(sale_value, econ, time_cost, shipping, travel)
    ratio = price_aud / gross_value if gross_value > 0 else None

    falling = [p["canonical_name"] for p in parts if p.get("trend_pct", 0) <= -5]
    for name in falling:
        flags.append(f"falling:{name}")

    conf = clf.confidence_score(
        kind, flags, core_in_title=core_in_title, is_full_pc=is_full_pc,
        inferred_residual=inferred_any, desc_len=len(description or ""), ambiguous=ambiguous,
        value_ratio=ratio)

    # ---- tier ------------------------------------------------------------------------------
    meets = net >= econ.min_net_profit and roi >= econ.min_roi_pct
    offer_ok = (not meets) and max_buy > 0 and max_buy >= price_aud * 0.85
    if ratio is not None and ratio < 0.45 and gross_value >= 150:
        tier = "SUSPECT"
        flags.append("too_cheap_probable_scam_or_mismatch")
    elif meets and net >= 2 * econ.min_net_profit and roi >= 2 * econ.min_roi_pct:
        tier = "STEAL"
    elif meets:
        tier = "GOOD_DEAL"
    elif offer_ok:
        tier = "OFFER"
    elif net >= 0:
        tier = "FAIR"
    else:
        tier = "OVERPRICED"

    needs_read = False
    if tier in ("STEAL", "GOOD_DEAL", "OFFER") and (conf < 60 or kind in ("multi", "from")):
        needs_read = True
        flags.append("low_confidence")
        tier = "FAIR"                       # low confidence can never be a deal until verified
    if "interstate_pc" in flags and tier in ("STEAL", "GOOD_DEAL", "OFFER"):
        tier = "FAIR"

    discount_pct = round((gross_value - price_aud) / gross_value * 100, 1) if gross_value > 0 else 0.0
    score = round(max(1.0, min(99.0, 50 + roi * 0.35 + min(max(net, -300), 600) / 14 + (conf - 60) * 0.15)), 1)
    if tier in ("SUSPECT", "OVERPRICED"):
        score = min(score, 25.0)

    offer = pricing.suggested_offer(price_aud, max_buy)
    msg = ""
    if tier in ("STEAL", "GOOD_DEAL", "OFFER") and offer > 0:
        msg = build_offer_message(title, price_aud, offer, pick["suburb"], max_buy, tier)

    return {
        "excluded": False, "reason": "", "matched_parts": parts, "is_bundle": is_bundle,
        "estimated_value_aud": round(gross_value, 2), "profit_margin_aud": round(net, 2),
        "discount_pct": discount_pct, "deal_score": score, "deal_tier": tier,
        "net_profit_aud": round(net, 2), "max_buy_aud": max_buy, "offer_aud": offer,
        "roi_pct": round(roi, 1), "confidence": conf, "kind": kind, "flags": flags,
        "strategy": strategy, "suburb": pick["suburb"], "drive_min": pick["drive_min"],
        "travel_cost_aud": round(travel, 2), "needs_read": needs_read, "offer_message": msg,
    }
