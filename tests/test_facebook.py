"""Facebook collection, tested offline against real saved pages (tests/fixtures) and a fake transport."""
import asyncio
import copy
import hashlib
import json
import unittest
from pathlib import Path

from helpers import ctx  # noqa: F401  (patches db to a temp file)
import fb
import health

FIX = Path(__file__).parent / "fixtures"
SEARCH = (FIX / "fb_search.html").read_text()
ITEM = (FIX / "fb_item.html").read_text()
GONE = (FIX / "fb_item_gone.html").read_text()
EMPTY = (FIX / "fb_search_empty.html").read_text()
LOGIN = "<html><body><form id=\"login_form\"></form></body></html>"


class Parsing(unittest.TestCase):
    def test_search_page_yields_structured_listings(self):
        items, st = fb.parse_search_html(SEARCH, "NSW")
        self.assertEqual(st["nodes"], 24)
        self.assertGreaterEqual(st["parsed"], 20)
        for it in items.values():
            self.assertGreater(it["price_aud"], 0)
            self.assertTrue(it["title"])
            self.assertRegex(it["url"], r"^https://www\.facebook\.com/marketplace/item/\d+/$")
            self.assertIn(it["state"], fb.AU_STATES)
            self.assertTrue(it["id"].startswith("fb_"))
            self.assertGreater(it["listed_epoch"], 1_000_000_000)

    def test_ids_match_the_old_scheme_so_stored_listings_stay_linked(self):
        items, _ = fb.parse_search_html(SEARCH)
        it = next(iter(items.values()))
        self.assertEqual(it["id"], "fb_" + hashlib.md5(it["url"].encode()).hexdigest()[:12])

    def test_item_page_gives_full_description(self):
        info = fb.parse_item_html(ITEM, "1598595994915335")
        self.assertTrue(info["ok"])
        self.assertIn("RTX", info["full_description"])
        self.assertFalse(info["gone"])
        self.assertTrue(info["listed_time"].startswith("Listed"))

    def test_removed_listing_is_detected_only_on_facebooks_own_unavailable_page(self):
        self.assertTrue(fb.parse_item_html(GONE, "1111")["gone"])
        # an unreadable page must NOT be treated as removed (would hide live listings after a layout change)
        info = fb.parse_item_html("<html><body>something new</body></html>", "1111")
        self.assertFalse(info["gone"])
        self.assertFalse(info["ok"])

    def test_sold_flag_marks_gone(self):
        blob = _first_blob(SEARCH)
        node = fb._find_dicts(blob, "marketplace_listing_title")[0]
        node = copy.deepcopy(node)
        node["is_sold"] = True
        item, _ = fb.listing_from_node(node)
        self.assertTrue(item["gone"])

    def test_foreign_and_bad_nodes_are_skipped_not_crashed(self):
        node = copy.deepcopy(fb._find_dicts(_first_blob(SEARCH), "marketplace_listing_title")[0])
        us = copy.deepcopy(node)
        us["location"]["reverse_geocode"]["state"] = "CA"
        self.assertEqual(fb.listing_from_node(us)[1], "foreign")
        free = copy.deepcopy(node)
        free["listing_price"]["amount"] = "0.00"
        self.assertEqual(fb.listing_from_node(free)[1], "no_price")
        nid = copy.deepcopy(node)
        nid["id"] = "abc"
        self.assertEqual(fb.listing_from_node(nid)[1], "no_id")

    def test_price_drop_uses_strikethrough_price(self):
        node = copy.deepcopy(fb._find_dicts(_first_blob(SEARCH), "marketplace_listing_title")[0])
        node["listing_price"]["amount"] = "400.00"
        node["strikethrough_price"] = {"amount": "500.00"}
        item, _ = fb.listing_from_node(node)
        self.assertIn("Dropped -$100", item["posted_at"])

    def test_survives_renesting(self):
        """Wrapping every node in new levels (a typical Facebook refactor) must not break extraction."""
        blob = _first_blob(SEARCH)
        wrapped = {"data": {"new_layer": [{"wrapper": n} for n in fb._find_dicts(blob, "marketplace_listing_title")]}}
        html = '<script type="application/json">' + json.dumps(wrapped) + "</script>"
        items, st = fb.parse_search_html(html)
        self.assertEqual(st["nodes"], 24)

    def test_graphql_multi_document_response(self):
        node = fb._find_dicts(_first_blob(SEARCH), "marketplace_listing_title")[0]
        text = json.dumps({"data": {"x": node}}) + "\n" + json.dumps({"label": "other"})
        items, st = fb.parse_graphql_text(text)
        self.assertEqual(st["parsed"], 1)

    def test_card_text_fallback(self):
        cards = [{"href": "/marketplace/item/999/?ref=x", "text": "AU$450\nRTX 3070 Gigabyte\nSydney, NSW", "img_src": ""}]
        out = fb.cards_to_listings(cards, "NSW")
        self.assertEqual(out["999"]["price_aud"], 450.0)
        self.assertEqual(out["999"]["location"], "Sydney, NSW")


class Classify(unittest.TestCase):
    def test_page_states(self):
        self.assertEqual(fb.classify_page(SEARCH, 200, "https://www.facebook.com/marketplace/x", 24), "ok")
        self.assertEqual(fb.classify_page(EMPTY, 200, "https://www.facebook.com/marketplace/x", 0), "empty")
        self.assertEqual(fb.classify_page(LOGIN, 200, "https://www.facebook.com/marketplace/x", 0), "login_wall")
        self.assertEqual(fb.classify_page("", 200, "https://www.facebook.com/login/?next=x", 0), "login_wall")
        self.assertEqual(fb.classify_page("", 429, "u", 0), "rate_limited")
        self.assertEqual(fb.classify_page("", 403, "u", 0), "blocked")
        self.assertEqual(fb.classify_page("<html>redesigned</html>", 200, "https://www.facebook.com/marketplace/x", 0),
                         "layout_change")


class Transport(unittest.TestCase):
    """The strategy ladder with a fake network."""

    def setUp(self):
        ctx()
        self._orig = fb.http_get
        self.calls = []

    def tearDown(self):
        fb.http_get = self._orig

    def _fake(self, pages):
        async def fake(url, attempt=0):
            self.calls.append(url)
            code, html, final = pages[min(len(self.calls) - 1, len(pages) - 1)]
            return code, html, final or url
        fb.http_get = fake

    def _run(self, coro):
        return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)

    async def _search(self, **kw):
        errors = []
        async with fb.FbRun("NSW", errors, concurrency=1, prefer_browser=False) as run:
            run.http_disabled = kw.get("http_disabled", False)
            items = await run.search("rtx 3070", newest=True)
            return items, errors, run

    def test_success_path_uses_http_only(self):
        self._fake([(200, SEARCH, None)])
        items, errors, run = self._run(self._search())
        self.assertGreaterEqual(len(items), 20)
        self.assertEqual(errors, [])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(run.stats["browser"], 0)

    def test_empty_result_is_success_not_failure(self):
        self._fake([(200, EMPTY, None)])
        items, errors, _ = self._run(self._search())
        self.assertEqual((items, errors), ([], []))

    def test_transient_failure_is_retried(self):
        self._fake([(500, "", None), (200, SEARCH, None)])
        orig_sleep = asyncio.sleep

        async def fast(_s, *a, **k):
            await orig_sleep(0)
        asyncio.sleep = fast
        try:
            items, errors, _ = self._run(self._search())
        finally:
            asyncio.sleep = orig_sleep
        self.assertGreaterEqual(len(items), 20)
        self.assertEqual(len(self.calls), 2)

    def test_login_wall_reports_the_message_the_scheduler_backs_off_on(self):
        self._fake([(200, LOGIN, None)])

        async def no_browser(self_, url, scrolls):
            return {}, "login_wall", {}, ""
        orig = fb.FbRun._browser_search
        fb.FbRun._browser_search = no_browser
        try:
            items, errors, run = self._run(self._search())
        finally:
            fb.FbRun._browser_search = orig
        self.assertEqual(items, [])
        self.assertTrue(any("login wall" in e for e in errors))
        self.assertTrue(run.login_walled)

    def test_item_inspect_over_http(self):
        self._fake([(200, ITEM, None)])
        info = self._run(fb.inspect_facebook_item_details("https://www.facebook.com/marketplace/item/1598595994915335/?ref=a"))
        self.assertTrue(info["ok"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0], "https://www.facebook.com/marketplace/item/1598595994915335/")

    def test_item_inspect_rejects_other_urls(self):
        self.assertFalse(self._run(fb.inspect_facebook_item_details("https://evil.example/marketplace/item/1/"))["ok"])

    def test_item_gone_over_http(self):
        self._fake([(200, GONE, None)])
        info = self._run(fb.inspect_facebook_item_details("https://www.facebook.com/marketplace/item/1111/"))
        self.assertTrue(info["gone"])


class Health(unittest.TestCase):
    def run_(self, status, strategy="http", parsed=20, nodes=24, note=""):
        return {"status": status, "strategy": strategy, "parsed": parsed, "nodes": nodes, "note": note}

    def test_assessment(self):
        a = health.assess
        self.assertEqual(a([], "http")["status"], "idle")
        self.assertEqual(a([self.run_("ok")] * 6, "http")["status"], "healthy")
        self.assertEqual(a([self.run_("login_wall")] * 4, "http")["status"], "down")
        self.assertEqual(a([self.run_("ok", "browser")] * 4, "http")["status"], "degraded")
        mixed = [self.run_("ok"), self.run_("blocked"), self.run_("blocked"), self.run_("ok")]
        self.assertEqual(a(mixed, "http")["status"], "degraded")
        self.assertEqual(a([self.run_("ok", parsed=5, nodes=24)] * 3, "http")["status"], "degraded")
        self.assertEqual(a([self.run_("ok", note="dom_fallback")] * 3, "http")["status"], "degraded")

    def test_recording_and_summary(self):
        ctx()
        health.record("facebook", "rtx 3070", "http", "ok", 24, 24)
        s = health.summary()["facebook"]
        self.assertIn(s["status"], ("healthy", "degraded", "down"))
        self.assertGreaterEqual(s["attempts_last_hour"], 1)

    def test_alert_only_on_transitions(self):
        health._last_alert.clear()
        health._last_state.clear()
        self.assertIsNone(health.alert_due("facebook", "healthy"))
        self.assertIn("DOWN", health.alert_due("facebook", "down"))
        self.assertIsNone(health.alert_due("facebook", "down"))
        self.assertIn("recovered", health.alert_due("facebook", "healthy"))


def _first_blob(html):
    return next(fb._json_blobs(html, "marketplace_listing_title"))


if __name__ == "__main__":
    unittest.main()
