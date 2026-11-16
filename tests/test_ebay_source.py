import unittest

from helpers import ctx  # noqa: F401  (points db at a temp database)

import ebay  # noqa: E402

ITEM = {
    "itemId": "v1|123456789012|0", "title": "AMD Ryzen 7 5800X3D CPU",
    "price": {"value": "310.00", "currency": "AUD"},
    "image": {"imageUrl": "https://i.ebayimg.com/images/g/abc/s-l225.jpg"},
    "itemWebUrl": "https://www.ebay.com.au/itm/123456789012?hash=item1&var=0",
    "itemLocation": {"postalCode": "20**", "country": "AU", "city": "Sydney"},
    "condition": "Used", "itemCreationDate": "2026-11-16T01:00:00.000Z",
    "shippingOptions": [{"shippingCost": {"value": "0.00", "currency": "AUD"}}],
}


class EbayParseTest(unittest.TestCase):
    def test_parse_item(self):
        r = ebay.parse_item(ITEM)
        self.assertEqual(r["id"], "eb_123456789012")
        self.assertEqual(r["url"], "https://www.ebay.com.au/itm/123456789012")
        self.assertEqual(r["price_aud"], 310.0)
        self.assertEqual(r["state"], "NSW")
        self.assertEqual(r["location"], "Sydney, NSW")
        self.assertIn("Free postage", r["description"])
        self.assertGreater(r["listed_epoch"], 0)

    def test_rejects_foreign_currency_and_bad_price(self):
        self.assertIsNone(ebay.parse_item({**ITEM, "price": {"value": "5", "currency": "USD"}}))
        self.assertIsNone(ebay.parse_item({**ITEM, "price": {}}))

    def test_state_from_postcode(self):
        self.assertEqual(ebay.state_from_postcode("2600"), "ACT")
        self.assertEqual(ebay.state_from_postcode("3***"), "VIC")
        self.assertEqual(ebay.state_from_postcode("6000"), "WA")
        self.assertEqual(ebay.state_from_postcode(""), "")

    def test_batches_cover_only_wanted_parts_and_wrap(self):
        seen = set()
        for _ in range(10):
            seen.update(ebay.next_batch(14))
        self.assertEqual(seen, set(ebay.EBAY_QUERIES))
        for bad in ("case", "psu", "power supply", "cooler", "rtx", "gpu"):
            self.assertFalse(any(bad in q for q in ebay.EBAY_QUERIES), bad)


if __name__ == "__main__":
    unittest.main()
