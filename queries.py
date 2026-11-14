"""
Builds the exhaustive search-query list for a deep scan straight from the price catalog,
so every part we know how to value gets searched (including every chipset / model variant),
plus generic bundle and category queries to catch listings with vague titles.
"""
import re
from typing import Any, Dict, List

GENERIC_QUERIES = [
    # Full PCs & bundles
    "gaming pc", "gaming pc rtx", "gaming computer", "custom gaming pc", "pc bundle", "desktop pc",
    "ryzen gaming pc", "intel gaming pc", "i7 gaming pc", "i5 gaming pc", "streaming pc",
    "cpu motherboard combo", "cpu motherboard ram combo", "am4 combo", "am5 combo", "lga1700 combo",
    "pc parts", "computer parts", "pc tower", "pc upgrade",
    # Broad component categories
    "graphics card", "gpu", "nvidia graphics card", "amd graphics card", "radeon", "geforce",
    "ryzen cpu", "intel cpu", "processor", "motherboard", "ddr4 ram", "ddr5 ram",
    "nvme ssd", "am4 motherboard", "am5 motherboard", "lga1700 motherboard", "lga1851 motherboard",
]


def _dedupe(items: List[str]) -> List[str]:
    seen, out = set(), []
    for q in items:
        q = re.sub(r"\s+", " ", q).strip().lower()
        if q and q not in seen:
            seen.add(q)
            out.append(q)
    return out


def queries_for_part(part: Dict[str, Any]) -> List[str]:
    name = part["canonical_name"]
    cat = part["category"]
    out: List[str] = []

    if cat == "GPU":
        brand = "rtx" if "RTX" in name else "rx"
        nums = re.findall(r"\b(\d{4})\b", name)
        suffixes = [sfx for sfx in ("Ti", "Super", "XT", "XTX", "GRE") if re.search(rf"\b{sfx}\b", name, re.I)]
        for n in nums:
            out.append(f"{brand} {n}")
            out += [f"{brand} {n} {sfx}" for sfx in suffixes]
    elif cat == "CPU":
        if "Ryzen" in name:
            tier = re.search(r"Ryzen (\d)", name)
            for tok in re.findall(r"\b\d{4}(?:X3D|X|F|G)?\b", name):
                out.append(f"ryzen {tier.group(1)} {tok}" if tier else f"ryzen {tok}")
                if "X3D" in tok:
                    out.append(tok)  # "5800x3d" is distinctive enough alone; bare "5600" is not
        elif "Ultra" in name:
            tier = re.search(r"Ultra (\d)", name)
            for tok in re.findall(r"\b\d{3}K\b", name):
                out.append(f"ultra {tier.group(1)} {tok}" if tier else f"core ultra {tok}")
        else:
            tier = re.search(r"\b(i\d)-", name)
            for tok in re.findall(r"\b\d{5}[A-Z]{0,2}\b", name):
                out.append(f"{tier.group(1)} {tok}" if tier else tok)
    elif cat == "Motherboard":
        inner = re.search(r"\(([^)]+)\)", name)
        if inner:
            out += [c.strip() for c in inner.group(1).split("/")]  # chipset alone, e.g. "b650"
    elif cat == "RAM":
        m = re.search(r"(\d+)GB\s+(DDR\d)", name)
        if m:
            out.append(f"{m.group(2)} {m.group(1)}gb")
    elif cat == "Storage":
        m = re.search(r"(\d)TB", name)
        if m:
            out.append(f"{m.group(1)}tb nvme")
    return out


def build_deep_queries(reference_parts: List[Dict[str, Any]], extra: List[str] = ()) -> List[str]:
    queries: List[str] = []
    for part in reference_parts:
        queries += queries_for_part(part)
    queries += GENERIC_QUERIES
    queries += list(extra)
    return _dedupe(queries)
