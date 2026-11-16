import hashlib
import unittest

from helpers import ctx  # noqa: F401  (points db at a temp database)

import ebay_notify  # noqa: E402


class EbayNotifyTest(unittest.TestCase):
    def setUp(self):
        self._saved = (ebay_notify.EBAY_VERIFICATION_TOKEN, ebay_notify.PUBLIC_URL)
        ebay_notify.EBAY_VERIFICATION_TOKEN = "t" * 40
        ebay_notify.PUBLIC_URL = "https://deals.example.com"
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(ebay_notify.router)
        self.client = TestClient(app)

    def tearDown(self):
        ebay_notify.EBAY_VERIFICATION_TOKEN, ebay_notify.PUBLIC_URL = self._saved

    def test_challenge_hash(self):
        r = self.client.get("/ebay/account-deletion", params={"challenge_code": "abc"})
        want = hashlib.sha256(("abc" + "t" * 40 + "https://deals.example.com/ebay/account-deletion").encode()).hexdigest()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"challengeResponse": want})

    def test_notification_acknowledged(self):
        self.assertEqual(self.client.post("/ebay/account-deletion", json={"metadata": {}}).status_code, 204)

    def test_disabled_without_token(self):
        ebay_notify.EBAY_VERIFICATION_TOKEN = ""
        self.assertEqual(self.client.get("/ebay/account-deletion", params={"challenge_code": "x"}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
