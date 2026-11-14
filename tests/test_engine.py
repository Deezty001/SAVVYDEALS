"""Run: .venv/bin/python -m unittest discover -s tests -v"""
import json
import unittest
from pathlib import Path

from helpers import ctx, ev, part_names  # noqa: F401
import classify
import pricing
import routing
from evaluator import evaluate_listing

REAL = json.loads((Path(__file__).parent / "real_listings.json").read_text())
DEALS = ("STEAL", "GOOD_DEAL", "OFFER")


class RealListings(unittest.TestCase):
    """Every real listing in the database, checked against invariants the engine must never break."""

    @classmethod
    def setUpClass(cls):
        econ, parts, ex = ctx()
        cls.econ = econ
        cls.results = [(r, evaluate_listing(r["title"], r["description"] or "", r["price_aud"], parts, ex, econ,
                                            r["location"], r["state"], r["source"])) for r in REAL]

    def test_low_confidence_is_never_a_deal(self):
        for r, e in self.results:
            if e["deal_tier"] in DEALS:
                self.assertGreaterEqual(e["confidence"], 60, r["title"])

    def test_deals_meet_targets(self):
        for r, e in self.results:
            if e["deal_tier"] in ("STEAL", "GOOD_DEAL"):
                self.assertGreaterEqual(e["net_profit_aud"], self.econ.min_net_profit, r["title"])
                self.assertGreaterEqual(e["roi_pct"], self.econ.min_roi_pct, r["title"])
                self.assertLessEqual(r["price_aud"], e["max_buy_aud"] + 0.5, r["title"])

    def test_used_value_never_exceeds_retail_cap(self):
        for r, e in self.results:
            for p in e["matched_parts"]:
                if p.get("new_retail_aud"):
                    self.assertLessEqual(p["avg_used_aud"], p["new_retail_aud"] * self.econ.used_cap_pct / 100 + 0.01,
                                         f"{r['title']} / {p['canonical_name']}")

    def test_known_false_steals_are_gone(self):
        """The old guide called these steals; they are priced at or above what the part is worth."""
        must_not_be_deal = [
            ("9800x3D", 500), ("AMD Ryzen 7 9800X3D CPU", 485), ("Ryzen 7 9800x3d", 600),
            ("Ryzen 7 7800X3D - High End Gaming CPU/Processor", 400), ("AMD Ryzen7 7800X3D CPU", 420),
            ("AMAZING CONDITION AMD RYZEN 7800X3D", 380),
            ("New Custom Build Desktop for Gaming or Office From $600", 100),
        ]
        by_key = {(r["title"], r["price_aud"]): e for r, e in self.results}
        for key in must_not_be_deal:
            e = by_key.get((key[0], float(key[1])))
            if e is not None:
                self.assertNotIn(e["deal_tier"], ("STEAL", "GOOD_DEAL", "OFFER"), key)

    def test_every_deal_has_offer_and_message(self):
        for r, e in self.results:
            if e["deal_tier"] in DEALS:
                self.assertGreater(e["offer_aud"], 0, r["title"])
                self.assertTrue(e["offer_message"], r["title"])
                self.assertLessEqual(e["offer_aud"], r["price_aud"] + 0.01, r["title"])


class Matching(unittest.TestCase):
    def test_bare_gpu_model_number(self):
        r = ev("Gaming pc 3070 R7 7800X3D", 1100)
        self.assertIn("NVIDIA RTX 3070 8GB", part_names(r))

    def test_ram_speed_not_a_gpu(self):
        r = ev("Ryzen 7 7800X3D + 32GB DDR5 6000 CL30 3600 combo", 700)
        self.assertFalse(any("RTX" in n for n in part_names(r)))

    def test_dell_optiplex_3070_not_a_gpu(self):
        r = ev("Dell OptiPlex 3070 SFF desktop i5", 250)
        self.assertEqual(r["deal_tier"], "UNMATCHED")

    def test_cpu_variants_split(self):
        self.assertIn("AMD Ryzen 9 7900X", part_names(ev("Ryzen 9 7900X cpu", 400)))
        n = part_names(ev("Ryzen 9 7950X3D cpu", 600))
        self.assertIn("AMD Ryzen 9 7950X3D", n)
        self.assertNotIn("AMD Ryzen 9 7900X", n)

    def test_chipset_split(self):
        self.assertIn("AM4 Motherboard B550", part_names(ev("Gigabyte B550 Aorus Elite motherboard", 120)))
        self.assertIn("AM5 Motherboard X670 / X670E", part_names(ev("ASUS X670E Hero motherboard", 350)))

    def test_vram_split(self):
        self.assertIn("NVIDIA RTX 3080 12GB", part_names(ev("RTX 3080 12GB graphics card", 560)))
        self.assertIn("NVIDIA RTX 3080 10GB", part_names(ev("RTX 3080 10GB graphics card", 500)))

    def test_cheap_end(self):
        self.assertIn("NVIDIA GTX 1660 / 1660 Super / Ti", part_names(ev("GTX 1660 super 6gb", 100)))
        self.assertIn("Intel Core i5-10400 / 10600K", part_names(ev("Intel i5 10400f cpu", 70)))
        self.assertIn("AMD Radeon RX 570 / 580 / 590", part_names(ev("RX 580 8gb", 60)))

    def test_pc_infers_missing_parts(self):
        r = ev("Gaming PC RTX 3070 Ryzen 7 5800X", 800)
        names = part_names(r)
        self.assertTrue(any("Motherboard (assumed)" in n for n in names))
        self.assertTrue(any("RAM (assumed)" in n for n in names))
        self.assertIn("flags", r)


class Classify(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(classify.classify("WTB rtx 3080", "")["kind"], "wanted")
        self.assertEqual(classify.classify("Gaming PC from $600", "")["kind"], "from")
        self.assertEqual(classify.classify("Custom PCs", "multiple options available, choose your GPU")["kind"], "multi")
        self.assertEqual(classify.classify("RTX 3070", "ABN 123. Visit our store. Afterpay available")["kind"], "shop")
        self.assertEqual(classify.classify("RTX 3070 used", "works great, selling to upgrade")["kind"], "private")

    def test_from_price_never_a_deal(self):
        r = ev("Gaming PC RTX 4070 from $900", 900)
        self.assertNotIn(r["deal_tier"], DEALS)

    def test_too_cheap_is_suspect(self):
        r = ev("RTX 4090 graphics card", 200)
        self.assertEqual(r["deal_tier"], "SUSPECT")

    def test_relist_fingerprint(self):
        a = classify.fingerprint("RTX 3070 URGENT must go", 300)
        b = classify.fingerprint("rtx 3070 - need gone", 300)
        self.assertEqual(a, b)


class Profit(unittest.TestCase):
    def test_max_buy_is_the_breakeven_for_targets(self):
        e = pricing.Econ()
        mb = pricing.max_buy_price(500, e, 20)
        self.assertTrue(pricing.meets_targets(500, mb, e, 20))
        self.assertFalse(pricing.meets_targets(500, mb + 2, e, 20))

    def test_gst_and_fees_reduce_profit(self):
        e = pricing.Econ()
        naive = 500 - 300
        self.assertLess(pricing.net_profit(500, 300, e, 20), naive - 50)

    def test_travel_cost_reduces_max_buy(self):
        e = pricing.Econ()
        self.assertLess(pricing.max_buy_price(500, e, 20, 0, 40), pricing.max_buy_price(500, e, 20, 0, 0))

    def test_cap_at_new_retail(self):
        e = pricing.Econ()
        p = {"avg_used_aud": 690, "new_retail_aud": 629}
        self.assertAlmostEqual(pricing.part_used_value(p, e), 629 * 0.85, 1)

    def test_learned_value_blends_in(self):
        e = pricing.Econ()
        p = {"avg_used_aud": 400, "learned_used_aud": 300, "learned_n": 40}
        self.assertLess(pricing.part_used_value(p, e), 330)

    def test_offer_candidate_tier(self):
        mb = ev("RTX 3070 graphics card", 400)["max_buy_aud"]
        r = ev("RTX 3070 graphics card", round(mb * 1.10))          # needs ~10% off: worth an offer
        self.assertEqual(r["deal_tier"], "OFFER")
        self.assertLessEqual(r["offer_aud"], mb)
        self.assertTrue(r["offer_message"])
        self.assertEqual(ev("RTX 3070 graphics card", round(mb * 1.4))["deal_tier"], "OVERPRICED")
        self.assertIn(ev("RTX 3070 graphics card", round(mb * 0.95))["deal_tier"], ("GOOD_DEAL", "STEAL"))

    def test_part_out_vs_whole(self):
        e = pricing.Econ()
        v = pricing.value_pc(1000, 200, 3, e)
        self.assertIn(v["strategy"], ("part-out", "sell-whole"))

    def test_falling_part_haircut(self):
        econ, parts, ex = ctx()
        base = ev("RTX 3070 graphics card", 300)["matched_parts"][0]["avg_used_aud"]
        falling = [dict(p, trend_pct=-20.0) if p["canonical_name"] == "NVIDIA RTX 3070 8GB" else p for p in parts]
        r = evaluate_listing("RTX 3070 graphics card", "", 300, falling, ex, econ, "Parramatta, NSW", "NSW", "")
        self.assertLess(r["matched_parts"][0]["avg_used_aud"], base)
        self.assertTrue(any(f.startswith("falling:") for f in r["flags"]))


class Routing(unittest.TestCase):
    def test_suburb_extraction(self):
        self.assertEqual(routing.extract_suburb("Parramatta, NSW"), "parramatta")
        self.assertEqual(routing.extract_suburb("Bondi Junction NSW 2022"), "bondi junction")

    def test_drive_time_ordering(self):
        near = routing.pickup_info("Newtown, NSW", "NSW", "Sydney CBD", 30, 0.35)
        far = routing.pickup_info("Penrith, NSW", "NSW", "Sydney CBD", 30, 0.35)
        self.assertLess(near["drive_min"], far["drive_min"])
        self.assertLess(near["cost"], far["cost"])
        self.assertEqual(far["suburb"], "penrith")

    def test_trip_groups_nearby_pickups(self):
        stops = [
            {"id": "a", "title": "A", "suburb": "parramatta", "state": "NSW", "net_profit_aud": 120, "price_aud": 300},
            {"id": "b", "title": "B", "suburb": "westmead", "state": "NSW", "net_profit_aud": 90, "price_aud": 200},
            {"id": "c", "title": "C", "suburb": "cronulla", "state": "NSW", "net_profit_aud": 80, "price_aud": 200},
        ]
        trips = routing.plan_trips(stops, "Sydney CBD", 30, 0.35)
        self.assertEqual(len(trips), 2)
        grouped = next(t for t in trips if len(t["stops"]) == 2)
        self.assertEqual(set(grouped["suburbs"]), {"parramatta", "westmead"})
        self.assertGreater(grouped["saving_aud"], 0)


class Messages(unittest.TestCase):
    def test_offer_message_contains_offer_and_suburb(self):
        r = ev("RTX 3070 Gigabyte graphics card", 500, location="Newtown, NSW")
        from messages import build_offer_message
        m = build_offer_message("RTX 3070 Gigabyte graphics card", 500, 400, "newtown", 410, "OFFER")
        self.assertIn("$400", m)
        self.assertIn("Newtown", m)
        self.assertNotIn("http", m)


class Store(unittest.TestCase):
    """Shared evaluate-and-store path, skip-if-unchanged re-valuation, SQL-side filtering."""

    @classmethod
    def setUpClass(cls):
        ctx()
        import db
        import engine_ops
        cls.db, cls.ops = db, engine_ops
        import ingest
        ingest.ingest_raw_listings([
            {"id": "t1", "title": "RTX 3070 graphics card", "price_aud": 380, "url": "https://www.gumtree.com.au/s-ad/x/1",
             "location": "Newtown, NSW", "state": "NSW"},
            {"id": "t2", "title": "Ryzen 7 5800X cpu", "price_aud": 150, "url": "https://www.facebook.com/marketplace/item/2/",
             "location": "Parramatta, NSW", "state": "NSW"},
        ])

    def test_unchanged_listings_are_skipped(self):
        self.ops.reevaluate_all()
        r = self.ops.reevaluate_all()
        self.assertEqual(r["revalued"], 0)
        self.assertGreaterEqual(r["skipped"], 2)

    def test_price_change_invalidates_skip(self):
        self.ops.reevaluate_all()
        conn = self.db.get_conn()
        conn.execute("UPDATE reference_parts SET new_retail_aud=new_retail_aud+1 WHERE canonical_name LIKE '%3070%'")
        conn.execute("UPDATE reference_parts SET avg_used_aud=avg_used_aud+1 WHERE canonical_name LIKE '%3070%'")
        conn.commit()
        conn.close()
        self.assertGreaterEqual(self.ops.reevaluate_all()["revalued"], 2)

    def test_relist_flag_survives_revaluation(self):
        conn = self.db.get_conn()
        conn.execute("UPDATE listings SET relist_count=2 WHERE id='t1'")
        conn.commit()
        conn.close()
        self.ops.reevaluate_all(force=True)
        conn = self.db.get_conn()
        flags = conn.execute("SELECT flags FROM listings WHERE id='t1'").fetchone()[0]
        conn.close()
        self.assertIn("relist_or_crosspost:2", flags)

    def test_deals_endpoint_filters_in_sql(self):
        from fastapi.testclient import TestClient
        import app
        with TestClient(app.app, base_url="http://127.0.0.1") as c:
            allr = c.get("/api/deals", params={"state": "ALL"}).json()
            self.assertGreaterEqual(allr["total_matching"], 2)
            gt = c.get("/api/deals", params={"state": "ALL", "source": "Gumtree AU"}).json()
            self.assertTrue(all(d["source"] == "Gumtree AU" for d in gt["deals"]))
            s = c.get("/api/deals", params={"state": "ALL", "search": "ryzen"}).json()
            self.assertEqual([d["id"] for d in s["deals"]], ["t2"])
            self.assertEqual(c.get("/api/deals", params={"state": "ALL", "search": "50%"}).json()["total_matching"], 0)
            lim = c.get("/api/deals", params={"state": "ALL", "limit": 1, "sort_by": "price_desc"}).json()
            self.assertEqual(len(lim["deals"]), 1)
            self.assertEqual(lim["deals"][0]["id"], "t1")
            self.assertEqual(lim["total_matching"], allr["total_matching"])


if __name__ == "__main__":
    unittest.main()
