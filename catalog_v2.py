"""
Catalog migration v2 (idempotent, keyed on settings.catalog_version).

* seeds new_retail_aud: lowest in-stock new price per part from the
  Savvy price engine (snapshot 2026-11-14; refresh with POST /api/prices)
* splits grouped parts so each chip / chipset / VRAM variant has its own price
* adds the cheap end (GTX 10/16, RX 500/5000, older Ryzen, Intel 8th-11th gen)

Used values for NEW rows are seed estimates (price_source='seed-estimate'); real
listings overwrite them through learn_used_values() as data accumulates.
"""
import json
import sqlite3
from typing import Any, Dict, List

VERSION = "2"

# canonical_name -> lowest new retail AUD (Savvy price engine, 2026-11-14)
RETAIL: Dict[str, float] = {
    "AMD Ryzen 7 9800X3D": 629,
    "AMD Ryzen 7 7800X3D": 529,
    "AMD Ryzen 7 9700X": 469,
    "AMD Ryzen 5 9600X": 346,
    "AMD Ryzen 5 7500F / 7600 / 7600X": 279,
    "Intel Core Ultra 7 265K / KF": 429,
    "Intel Core Ultra 5 245K / KF": 259,
    "Intel Core Ultra 9 285K": 879,
    "AMD Ryzen 9 7900X": 579,
    "AMD Ryzen 9 9950X3D": 939,
    "NVIDIA RTX 5090 32GB": 12499,
    "NVIDIA RTX 5080 16GB": 2999,
    "NVIDIA RTX 5070 12GB": 1299,
    "NVIDIA RTX 5060 8GB": 649,
    "NVIDIA RTX 4060 Ti 8GB": 599,
    "AMD Radeon RX 9070 XT 16GB": 1299,
    "AMD Radeon RX 7600 8GB": 419,
}

P = lambda *a: list(a)  # noqa: E731


def _part(name, cat, series, used, pats, excl=None):
    return dict(canonical_name=name, category=cat, socket_or_series=series, avg_used_aud=used,
                regex_patterns=pats, exclude_patterns=excl or [])


GPU_EXCL = ["laptop", "mobile"]

# old canonical_name -> replacement parts
SPLITS: Dict[str, List[Dict[str, Any]]] = {
    "AMD Ryzen 9 7900X / 7900X3D / 7950X / 7950X3D": [
        _part("AMD Ryzen 9 7900X", "CPU", "AM5", 400, [r"\b7900x\b"], ["radeon", "rx"]),
        _part("AMD Ryzen 9 7900X3D", "CPU", "AM5", 470, [r"\b7900x3d\b"], ["radeon", "rx"]),
        _part("AMD Ryzen 9 7950X", "CPU", "AM5", 480, [r"\b7950x\b"], ["radeon", "rx"]),
        _part("AMD Ryzen 9 7950X3D", "CPU", "AM5", 650, [r"\b7950x3d\b"], ["radeon", "rx"]),
    ],
    "AMD Ryzen 9 9900X / 9950X / 9950X3D": [
        _part("AMD Ryzen 9 9900X", "CPU", "AM5", 450, [r"\b9900x\b"]),
        _part("AMD Ryzen 9 9950X", "CPU", "AM5", 600, [r"\b9950x\b"]),
        _part("AMD Ryzen 9 9950X3D", "CPU", "AM5", 780, [r"\b9950x3d\b"]),
        _part("AMD Ryzen 9 9900X3D", "CPU", "AM5", 650, [r"\b9900x3d\b"]),
    ],
    "NVIDIA RTX 3080 10GB/12GB": [
        _part("NVIDIA RTX 3080 10GB", "GPU", "RTX 3000", 520,
              [r"\brtx\s*3080\b", r"\b3080\s*10\s*gb\b"],
              [r"\b3080\s*ti\b", r"\b3080ti\b", r"\b3080\s*12\s*gb\b"] + GPU_EXCL),
        _part("NVIDIA RTX 3080 12GB", "GPU", "RTX 3000", 600, [r"\b3080\s*12\s*gb\b"],
              [r"\b3080\s*ti\b", r"\b3080ti\b"] + GPU_EXCL),
    ],
    "NVIDIA RTX 4060 Ti (8GB/16GB)": [
        _part("NVIDIA RTX 4060 Ti 8GB", "GPU", "RTX 4000", 440,
              [r"\brtx\s*4060\s*ti\b", r"\b4060\s*ti\b", r"\b4060ti\b"], [r"\b16\s*gb\b"] + GPU_EXCL),
        _part("NVIDIA RTX 4060 Ti 16GB", "GPU", "RTX 4000", 520,
              [r"\b4060\s*ti\s*16\s*gb\b", r"\b16\s*gb\s*4060\s*ti\b"], GPU_EXCL),
    ],
    "NVIDIA RTX 5060 / 5060 Ti": [
        _part("NVIDIA RTX 5060 8GB", "GPU", "RTX 5000", 470, [r"\brtx\s*5060\b(?!\s*ti)"], [r"\b5060\s*ti\b", "laptop"]),
        _part("NVIDIA RTX 5060 Ti", "GPU", "RTX 5000", 620, [r"\b5060\s*ti\b"], ["laptop"]),
    ],
    "NVIDIA RTX 4080 / 4080 Super 16GB": [
        _part("NVIDIA RTX 4080 16GB", "GPU", "RTX 4000", 1250, [r"\brtx\s*4080\b(?!\s*s)"], [r"4080\s*super", r"4080s\b"] + GPU_EXCL),
        _part("NVIDIA RTX 4080 Super 16GB", "GPU", "RTX 4000", 1350, [r"\b4080\s*super\b", r"\b4080s\b"], GPU_EXCL),
    ],
    "AMD Radeon RX 7600 / 7600 XT": [
        _part("AMD Radeon RX 7600 8GB", "GPU", "RX 7000", 290, [r"\brx\s*7600\b(?!\s*xt)"], [r"7600\s*xt", "laptop"]),
        _part("AMD Radeon RX 7600 XT 16GB", "GPU", "RX 7000", 380, [r"\b7600\s*xt\b"], ["laptop"]),
    ],
    "AMD Radeon RX 6600 / 6600 XT / 6650 XT": [
        _part("AMD Radeon RX 6600", "GPU", "RX 6000", 195, [r"\brx\s*6600\b(?!\s*xt)"], [r"6600\s*xt", "laptop"]),
        _part("AMD Radeon RX 6600 XT / 6650 XT", "GPU", "RX 6000", 250, [r"\brx\s*66[05]0\s*xt\b", r"\b6600\s*xt\b", r"\b6650\s*xt\b"], ["laptop"]),
    ],
    "NVIDIA RTX 2060 / 2060 Super": [
        _part("NVIDIA RTX 2060", "GPU", "RTX 2000", 165, [r"\brtx\s*2060\b(?!\s*s)"], [r"2060\s*super", "laptop", "mobile"]),
        _part("NVIDIA RTX 2060 Super", "GPU", "RTX 2000", 200, [r"\b2060\s*super\b", r"\b2060s\b"], ["laptop", "mobile"]),
    ],
    "AM4 Motherboard (B450 / B550 / X570)": [
        _part("AM4 Motherboard B450", "Motherboard", "AM4", 95, [r"\bb450[a-z0-9-]*\b"]),
        _part("AM4 Motherboard B550", "Motherboard", "AM4", 125, [r"\bb550[a-z0-9-]*\b"]),
        _part("AM4 Motherboard X570", "Motherboard", "AM4", 165, [r"\bx570[a-z0-9-]*\b"]),
    ],
    "AM5 Motherboard (B650 / X670 / B850 / X870)": [
        _part("AM5 Motherboard B650 / B650E", "Motherboard", "AM5", 190, [r"\bb650e?\b"]),
        _part("AM5 Motherboard X670 / X670E", "Motherboard", "AM5", 290, [r"\bx670e?\b"]),
        _part("AM5 Motherboard B850", "Motherboard", "AM5", 240, [r"\bb850\b"]),
        _part("AM5 Motherboard X870 / X870E", "Motherboard", "AM5", 340, [r"\bx870e?\b"]),
    ],
    "LGA1700 Motherboard (B660 / B760 / Z690 / Z790)": [
        _part("LGA1700 Motherboard B660", "Motherboard", "LGA1700", 115, [r"\bb660\b"]),
        _part("LGA1700 Motherboard B760", "Motherboard", "LGA1700", 150, [r"\bb760\b"]),
        _part("LGA1700 Motherboard Z690", "Motherboard", "LGA1700", 170, [r"\bz690\b"]),
        _part("LGA1700 Motherboard Z790", "Motherboard", "LGA1700", 230, [r"\bz790\b"]),
    ],
}

ADDITIONS: List[Dict[str, Any]] = [
    _part("NVIDIA GTX 1060 6GB", "GPU", "GTX 1000", 90, [r"\bgtx\s*1060\b"], GPU_EXCL + ["3gb"]),
    _part("NVIDIA GTX 1070 / 1070 Ti", "GPU", "GTX 1000", 130, [r"\bgtx\s*1070(?:\s*ti)?\b"], GPU_EXCL),
    _part("NVIDIA GTX 1080 / 1080 Ti", "GPU", "GTX 1000", 175, [r"\bgtx\s*1080(?:\s*ti)?\b"], GPU_EXCL),
    _part("NVIDIA GTX 1650 / 1650 Super", "GPU", "GTX 1600", 105, [r"\bgtx\s*1650(?:\s*super)?\b"], GPU_EXCL),
    _part("NVIDIA GTX 1660 / 1660 Super / Ti", "GPU", "GTX 1600", 135, [r"\bgtx\s*1660(?:\s*(?:super|ti))?\b"], GPU_EXCL),
    _part("AMD Radeon RX 570 / 580 / 590", "GPU", "RX 500", 80, [r"\brx\s*5[789]0\b"], GPU_EXCL),
    _part("AMD Radeon RX 5600 XT / 5700 / 5700 XT", "GPU", "RX 5000", 160, [r"\brx\s*5[67]00(?:\s*xt)?\b"], GPU_EXCL),
    _part("AMD Ryzen 5 2600 / 2600X", "CPU", "AM4", 55, [r"\b2600x?\b"], ["rtx", "gtx", "radeon", "rx\\s*2600"]),
    _part("AMD Ryzen 5 5500 / 5600G", "CPU", "AM4", 95, [r"\bryzen\s*5\s*(?:5500|5600g)\b"]),
    _part("AMD Ryzen 7 3700X / 3800X", "CPU", "AM4", 110, [r"\b3700x\b", r"\b3800x\b"]),
    _part("Intel Core i5-8400 / 8500 / 8600K", "CPU", "LGA1151", 55, [r"\bi5[ -]?8(?:400|500|600k?)f?\b"]),
    _part("Intel Core i5-9400F / 9600K", "CPU", "LGA1151", 60, [r"\bi5[ -]?9(?:400|600k?)f?\b"]),
    _part("Intel Core i5-10400 / 10600K", "CPU", "LGA1200", 85, [r"\bi5[ -]?10(?:400|600k?)f?\b"]),
    _part("Intel Core i5-11400 / 11600K", "CPU", "LGA1200", 100, [r"\bi5[ -]?11(?:400|600k?)f?\b"]),
    _part("Intel Core i7-8700 / 8700K", "CPU", "LGA1151", 90, [r"\bi7[ -]?8700k?f?\b"]),
    _part("Intel Core i7-9700 / 9700K", "CPU", "LGA1151", 100, [r"\bi7[ -]?9700k?f?\b"]),
    _part("Intel Core i7-10700 / 10700K", "CPU", "LGA1200", 120, [r"\bi7[ -]?10700k?f?\b"]),
    _part("Intel Core i7-11700 / 11700K", "CPU", "LGA1200", 135, [r"\bi7[ -]?11700k?f?\b"]),
    _part("Intel Core i3-12100F", "CPU", "LGA1700", 75, [r"\bi3[ -]?12100f?\b"]),
]


def _insert(cur: sqlite3.Cursor, p: Dict[str, Any], now: str) -> None:
    cur.execute("""
        INSERT OR IGNORE INTO reference_parts
        (canonical_name, category, socket_or_series, avg_used_aud, good_deal_aud, steal_aud,
         regex_patterns, exclude_patterns, updated_at, price_source)
        VALUES (?,?,?,?,?,?,?,?,?,'seed-estimate')
    """, (p["canonical_name"], p["category"], p["socket_or_series"], p["avg_used_aud"],
          round(p["avg_used_aud"] * 0.8, 2), round(p["avg_used_aud"] * 0.68, 2),
          json.dumps(p["regex_patterns"]), json.dumps(p["exclude_patterns"]), now))


def migrate(conn: sqlite3.Connection, now: str) -> bool:
    cur = conn.cursor()
    row = cur.execute("SELECT value FROM settings WHERE key='catalog_version'").fetchone()
    if row and row[0] == VERSION:
        return False
    for old, news in SPLITS.items():
        if cur.execute("SELECT 1 FROM reference_parts WHERE canonical_name=?", (old,)).fetchone():
            cur.execute("DELETE FROM reference_parts WHERE canonical_name=?", (old,))
            for p in news:
                _insert(cur, p, now)
    for p in ADDITIONS:
        _insert(cur, p, now)
    for name, retail in RETAIL.items():
        cur.execute("UPDATE reference_parts SET new_retail_aud=?, price_source=CASE WHEN price_source='seed' "
                    "THEN 'savvy-retail' ELSE price_source END WHERE canonical_name=?", (retail, name))
    cur.execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('catalog_version',?)", (VERSION,))
    conn.commit()
    return True
