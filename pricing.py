"""
Profit model. Pure functions, no I/O.

Replaces "guide value minus asking price" with what you actually keep after selling fees,
warranty reserve, your time and GST on the margin (margin scheme: GST = margin / 11).
"""
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class Econ:
    fee_pct: float = 6.0            # blended selling fees (FB free locally, eBay ~13%)
    warranty_pct: float = 3.0       # reserve for returns / DOA on resold stock
    time_cost_part: float = 20.0    # your time per part sold
    time_cost_pc: float = 45.0      # your time to test + sell one whole PC
    gst_registered: bool = True
    min_net_profit: float = 50.0
    min_roi_pct: float = 25.0
    used_cap_pct: float = 85.0      # used value never exceeds this % of new retail
    home_state: str = "NSW"
    ship_part_aud: float = 25.0     # buying a part interstate
    ship_pc_aud: float = 0.0        # interstate PCs are not alerted at all
    home_suburb: str = "Sydney CBD"
    hourly_value: float = 30.0      # what an hour of your driving is worth
    cost_per_km: float = 0.35       # fuel + wear

    @classmethod
    def from_settings(cls, s: Dict[str, str]) -> "Econ":
        def f(key: str, default: float) -> float:
            try:
                return float(s.get(key, default))
            except (TypeError, ValueError):
                return default
        return cls(
            fee_pct=f("fee_pct", cls.fee_pct),
            warranty_pct=f("warranty_pct", cls.warranty_pct),
            time_cost_part=f("time_cost_part", cls.time_cost_part),
            time_cost_pc=f("time_cost_pc", cls.time_cost_pc),
            gst_registered=str(s.get("gst_registered", "1")) not in ("0", "false", "False", ""),
            min_net_profit=f("min_profit_aud", cls.min_net_profit),
            min_roi_pct=f("min_roi_pct", cls.min_roi_pct),
            used_cap_pct=f("used_cap_pct", cls.used_cap_pct),
            home_state=(s.get("state") or "NSW").upper(),
            ship_part_aud=f("ship_part_aud", cls.ship_part_aud),
            home_suburb=s.get("home_suburb") or "Sydney CBD",
            hourly_value=f("hourly_value_aud", cls.hourly_value),
            cost_per_km=f("cost_per_km_aud", cls.cost_per_km),
        )


def part_used_value(part: Dict[str, Any], econ: Econ) -> float:
    """
    Best estimate of what this part resells for used.
    Blend the hand-entered guide with value learned from real listings, then cap at a
    percentage of today's new retail so a guide that has drifted above retail cannot
    produce fake steals.
    """
    guide = float(part.get("avg_used_aud") or 0)
    n = int(part.get("learned_n") or 0)
    learned = float(part.get("learned_used_aud") or 0)
    if n >= 5 and learned > 0:
        w = n / (n + 8.0)                       # 5 listings -> 38% learned, 30 -> 79%
        value = w * learned + (1 - w) * guide
    else:
        value = guide
    retail = float(part.get("new_retail_aud") or 0)
    if retail > 0:
        value = min(value, retail * econ.used_cap_pct / 100.0)
    return round(value, 2)


def net_profit(sale_value: float, price: float, econ: Econ, time_cost: float, shipping: float = 0.0, travel: float = 0.0) -> float:
    fees = sale_value * econ.fee_pct / 100.0
    warranty = sale_value * econ.warranty_pct / 100.0
    margin = max(0.0, sale_value - price - shipping)
    gst = margin / 11.0 if econ.gst_registered else 0.0
    return sale_value - fees - warranty - gst - time_cost - price - shipping - travel


def roi_pct(net: float, price: float, shipping: float = 0.0) -> float:
    spend = price + shipping
    return (net / spend * 100.0) if spend > 0 else 0.0


def meets_targets(sale_value: float, price: float, econ: Econ, time_cost: float, shipping: float = 0.0, travel: float = 0.0) -> bool:
    net = net_profit(sale_value, price, econ, time_cost, shipping, travel)
    return net >= econ.min_net_profit and roi_pct(net, price, shipping) >= econ.min_roi_pct


def max_buy_price(sale_value: float, econ: Econ, time_cost: float, shipping: float = 0.0, travel: float = 0.0) -> float:
    """Highest asking price at which both the profit and return targets are still met."""
    if sale_value <= 0 or not meets_targets(sale_value, 0.01, econ, time_cost, shipping, travel):
        return 0.0
    lo, hi = 0.01, sale_value
    for _ in range(50):                          # both targets fall as price rises, so bisect
        mid = (lo + hi) / 2
        if meets_targets(sale_value, mid, econ, time_cost, shipping, travel):
            lo = mid
        else:
            hi = mid
    return int(lo * 100) / 100.0     # floor to the cent so the result always meets targets


def suggested_offer(price: float, max_buy: float) -> float:
    """What to offer: the max buy if the ask is above it, otherwise ~8% under the ask."""
    if max_buy <= 0:
        return 0.0
    target = max_buy if price > max_buy else min(max_buy, price * 0.92)
    return float(int(target // 5 * 5))           # round down to a natural $5 offer


def value_pc(parts_sum: float, residual: float, n_parts: int, econ: Econ) -> Dict[str, Any]:
    """
    Full PC: compare selling it whole against parting it out and use whichever nets more.
    Whole sale loses ~7% to the 'PC buyer' discount; parting out loses 3% to haggling
    and costs extra time per part sold.
    """
    whole_value = (parts_sum + residual) * 0.93
    whole_time = econ.time_cost_pc
    part_value = parts_sum * 0.97 + residual * 0.5        # cases/PSUs sell poorly alone
    part_time = econ.time_cost_part * max(1, n_parts) + 15.0
    # Compare on value net of time at a nominal $0 purchase price (ordering only).
    whole_net = net_profit(whole_value, 0.0, econ, whole_time)
    part_net = net_profit(part_value, 0.0, econ, part_time)
    if part_net > whole_net:
        return {"strategy": "part-out", "sale_value": round(part_value, 2), "time_cost": part_time}
    return {"strategy": "sell-whole", "sale_value": round(whole_value, 2), "time_cost": whole_time}
