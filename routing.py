"""
Suburb extraction, drive-time estimate and pickup trip grouping. Offline: no map API.

Drive time = straight-line distance x 1.35 road factor at a speed that depends on distance
(slow inner-city, faster on motorways). It is an estimate good to roughly +/-25%, which is
enough to rank pickups and cost them; check the real route before you leave.
"""
import math
import re
from typing import Any, Dict, List, Optional, Tuple

# suburb (lowercase) -> (lat, lon). Greater Sydney + Wollongong / Newcastle / Central Coast hubs.
SUBURBS: Dict[str, Tuple[float, float]] = {
    "sydney": (-33.8688, 151.2093), "sydney cbd": (-33.8688, 151.2093), "haymarket": (-33.8800, 151.2030),
    "ultimo": (-33.8800, 151.1960), "pyrmont": (-33.8710, 151.1950), "surry hills": (-33.8840, 151.2110),
    "redfern": (-33.8930, 151.2040), "newtown": (-33.8980, 151.1790), "glebe": (-33.8800, 151.1860),
    "bondi": (-33.8915, 151.2767), "bondi junction": (-33.8920, 151.2480), "randwick": (-33.9140, 151.2420),
    "kensington": (-33.9100, 151.2200), "maroubra": (-33.9500, 151.2420), "coogee": (-33.9210, 151.2580),
    "mascot": (-33.9250, 151.1930), "botany": (-33.9460, 151.1960), "alexandria": (-33.9090, 151.1950),
    "marrickville": (-33.9110, 151.1550), "dulwich hill": (-33.9040, 151.1390), "ashfield": (-33.8890, 151.1250),
    "burwood": (-33.8770, 151.1040), "strathfield": (-33.8730, 151.0940), "homebush": (-33.8650, 151.0850),
    "auburn": (-33.8490, 151.0330), "lidcombe": (-33.8650, 151.0450), "granville": (-33.8330, 151.0100),
    "parramatta": (-33.8150, 151.0010), "westmead": (-33.8030, 150.9880), "harris park": (-33.8200, 151.0100),
    "north parramatta": (-33.7960, 151.0040), "epping": (-33.7720, 151.0820), "ryde": (-33.8150, 151.1040),
    "eastwood": (-33.7910, 151.0810), "macquarie park": (-33.7760, 151.1260), "chatswood": (-33.7960, 151.1830),
    "lane cove": (-33.8140, 151.1690), "artarmon": (-33.8090, 151.1850), "st leonards": (-33.8230, 151.1950),
    "north sydney": (-33.8400, 151.2070), "mosman": (-33.8290, 151.2440), "manly": (-33.7970, 151.2880),
    "dee why": (-33.7500, 151.2860), "brookvale": (-33.7640, 151.2700), "mona vale": (-33.6780, 151.3030),
    "hornsby": (-33.7040, 151.0990), "pennant hills": (-33.7390, 151.0720), "castle hill": (-33.7300, 151.0050),
    "baulkham hills": (-33.7600, 150.9900), "kellyville": (-33.7110, 150.9560), "rouse hill": (-33.6890, 150.9220),
    "blacktown": (-33.7710, 150.9060), "seven hills": (-33.7740, 150.9370), "mount druitt": (-33.7680, 150.8200),
    "st marys": (-33.7650, 150.7740), "penrith": (-33.7510, 150.6940), "emu plains": (-33.7480, 150.6700),
    "richmond": (-33.5990, 150.7510), "windsor": (-33.6090, 150.8150), "liverpool": (-33.9200, 150.9230),
    "fairfield": (-33.8720, 150.9560), "cabramatta": (-33.8940, 150.9370), "bankstown": (-33.9190, 151.0350),
    "padstow": (-33.9520, 151.0330), "revesby": (-33.9500, 151.0160), "hurstville": (-33.9670, 151.1030),
    "kogarah": (-33.9630, 151.1340), "rockdale": (-33.9530, 151.1380), "sutherland": (-34.0310, 151.0580),
    "cronulla": (-34.0550, 151.1520), "miranda": (-34.0340, 151.1010), "caringbah": (-34.0420, 151.1220),
    "campbelltown": (-34.0650, 150.8140), "narellan": (-34.0410, 150.7360), "camden": (-34.0540, 150.6950),
    "leppington": (-33.9590, 150.8000), "ingleburn": (-34.0000, 150.8650), "casula": (-33.9500, 150.9060),
    "kingsgrove": (-33.9330, 151.0990), "belmore": (-33.9170, 151.0890), "canterbury": (-33.9110, 151.1190),
    "lakemba": (-33.9200, 151.0760), "five dock": (-33.8680, 151.1330), "drummoyne": (-33.8530, 151.1530),
    "leichhardt": (-33.8830, 151.1560), "balmain": (-33.8590, 151.1800), "rhodes": (-33.8310, 151.0870),
    "meadowbank": (-33.8160, 151.0890), "carlingford": (-33.7760, 151.0480), "winston hills": (-33.7770, 150.9810),
    "wentworthville": (-33.8070, 150.9710), "merrylands": (-33.8360, 150.9930), "guildford": (-33.8540, 151.0030),
    "greystanes": (-33.8250, 150.9400), "prestons": (-33.9400, 150.8730), "bella vista": (-33.7350, 150.9570),
    "wollongong": (-34.4250, 150.8930), "newcastle": (-32.9280, 151.7760), "gosford": (-33.4250, 151.3420),
}

# Fallback hubs for other capitals when a suburb isn't in the table.
CITY_HUBS: Dict[str, Tuple[float, float]] = {
    "NSW": (-33.8688, 151.2093), "VIC": (-37.8136, 144.9631), "QLD": (-27.4698, 153.0251),
    "WA": (-31.9505, 115.8605), "SA": (-34.9285, 138.6007), "TAS": (-42.8821, 147.3272),
    "ACT": (-35.2809, 149.1300), "NT": (-12.4634, 130.8456),
}

ROAD_FACTOR = 1.35


def extract_suburb(location: str) -> str:
    """'Parramatta, NSW' / 'Parramatta NSW 2150' / 'Sydney 2000' -> 'parramatta'."""
    if not location:
        return ""
    t = re.sub(r"\b(nsw|vic|qld|wa|sa|tas|act|nt)\b|\b\d{4}\b|\baustralia\b", " ", location.lower())
    t = re.sub(r"[,;|()\-]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if t in SUBURBS:
        return t
    # longest known suburb contained in the text ("greater parramatta area")
    best = ""
    for name in SUBURBS:
        if re.search(rf"\b{re.escape(name)}\b", t) and len(name) > len(best):
            best = name
    return best or t


def coords(suburb: str, state: str = "NSW") -> Tuple[Optional[Tuple[float, float]], bool]:
    """Returns (lat/lon, exact). Unknown suburbs fall back to the state's hub (exact=False)."""
    s = (suburb or "").lower().strip()
    if s in SUBURBS:
        return SUBURBS[s], True
    return CITY_HUBS.get((state or "NSW").upper()), False


def _haversine_km(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def drive(a: Tuple[float, float], b: Tuple[float, float]) -> Tuple[float, float]:
    """(road km, minutes) one way."""
    km = _haversine_km(a, b) * ROAD_FACTOR
    speed = 28 if km < 8 else 38 if km < 25 else 52           # km/h blended with traffic
    return round(km, 1), round(km / speed * 60 + 4, 0)        # +4 min parking / handover


def trip_cost(minutes_round_trip: float, km_round_trip: float, hourly: float, per_km: float, handover_min: float = 10) -> float:
    return round((minutes_round_trip + handover_min) / 60 * hourly + km_round_trip * per_km, 2)


def pickup_info(location: str, state: str, home_suburb: str, hourly: float, per_km: float) -> Dict[str, Any]:
    """Suburb, one-way drive minutes and the round-trip cost to buy this single item."""
    suburb = extract_suburb(location)
    home, _ = coords(extract_suburb(home_suburb) or home_suburb, "NSW")
    there, exact = coords(suburb, state)
    if not home or not there:
        return {"suburb": suburb, "drive_min": 0.0, "km": 0.0, "cost": 0.0, "exact": False}
    km, minutes = drive(home, there)
    return {"suburb": suburb, "drive_min": minutes, "km": km,
            "cost": trip_cost(minutes * 2, km * 2, hourly, per_km), "exact": exact}


def plan_trips(stops: List[Dict[str, Any]], home_suburb: str, hourly: float, per_km: float,
               max_hop_min: float = 25.0) -> List[Dict[str, Any]]:
    """
    Group pickups into trips. Greedy: start from the stop with the most profit, keep adding
    the nearest remaining stop while it is within `max_hop_min` of the current route end.
    Each stop needs: id, title, suburb, state, net_profit_aud, price_aud.
    Reports the saving versus doing every pickup as its own round trip.
    """
    home, _ = coords(extract_suburb(home_suburb) or home_suburb, "NSW")
    pts = []
    for s in stops:
        c, exact = coords(s.get("suburb", ""), s.get("state", "NSW"))
        if c:
            pts.append({**s, "_c": c, "_exact": exact})
    remaining = sorted(pts, key=lambda s: -float(s.get("net_profit_aud", 0)))
    trips: List[Dict[str, Any]] = []
    while remaining and home:
        route = [remaining.pop(0)]
        while remaining:
            end = route[-1]["_c"]
            nxt = min(remaining, key=lambda s: drive(end, s["_c"])[1])
            if drive(end, nxt["_c"])[1] > max_hop_min:
                break
            route.append(nxt)
            remaining.remove(nxt)
        km = minutes = 0.0
        cur = home
        for s in route:
            k, m = drive(cur, s["_c"])
            km, minutes, cur = km + k, minutes + m, s["_c"]
        k, m = drive(cur, home)
        km, minutes = km + k, minutes + m
        handover = 10 * len(route)
        cost = round((minutes + handover) / 60 * hourly + km * per_km, 2)
        separate = 0.0
        for s in route:
            k, m = drive(home, s["_c"])
            separate += trip_cost(m * 2, k * 2, hourly, per_km)
        trips.append({
            "stops": [{"id": s["id"], "title": s["title"], "suburb": s.get("suburb") or "unknown",
                       "price_aud": s.get("price_aud"), "net_profit_aud": s.get("net_profit_aud"),
                       "approx_location": not s["_exact"]} for s in route],
            "suburbs": [s.get("suburb") or "unknown" for s in route],
            "total_km": round(km, 1), "total_min": round(minutes + handover),
            "trip_cost_aud": cost, "separate_cost_aud": round(separate, 2),
            "saving_aud": round(separate - cost, 2),
            "net_after_travel_aud": round(sum(float(s.get("net_profit_aud", 0)) for s in route) - cost, 2),
        })
    return trips
