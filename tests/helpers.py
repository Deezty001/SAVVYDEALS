import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402
import pricing  # noqa: E402

_ctx = None


def ctx():
    """Fresh temp DB seeded from catalog_seed.json + catalog_v2 (never touches deals.db)."""
    global _ctx
    if _ctx is None:
        tmp = Path(tempfile.mkdtemp()) / "test.db"
        db.DB_PATH = tmp
        db.init_db()
        conn = db.get_conn()
        parts = [dict(r) for r in conn.execute("SELECT * FROM reference_parts")]
        conn.close()
        settings = db.get_all_settings()
        _ctx = (pricing.Econ.from_settings(settings), parts, json.loads(settings["exclude_keywords"]))
    return _ctx


def ev(title, price, desc="", location="Parramatta, NSW", state="NSW"):
    from evaluator import evaluate_listing
    econ, parts, ex = ctx()
    return evaluate_listing(title, desc, price, parts, ex, econ, location, state, "Facebook Marketplace")


def part_names(res):
    return [p["canonical_name"] for p in res["matched_parts"]]
