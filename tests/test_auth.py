import unittest

from helpers import ctx  # noqa: F401  (points db at a temp database)

ctx()

import auth  # noqa: E402


class AuthTest(unittest.TestCase):
    def setUp(self):
        self._pw = auth.DASHBOARD_PASSWORD
        auth.DASHBOARD_PASSWORD = "correct horse"
        auth._fails.clear()

    def tearDown(self):
        auth.DASHBOARD_PASSWORD = self._pw
        auth._fails.clear()

    def client(self):
        from fastapi.testclient import TestClient
        import app
        return TestClient(app.app, base_url="http://127.0.0.1")

    def test_requires_login_and_accepts_password(self):
        with self.client() as c:
            self.assertEqual(c.get("/api/deals").status_code, 401)
            r = c.get("/", follow_redirects=False)
            self.assertEqual((r.status_code, r.headers["location"]), (303, "/login"))
            self.assertEqual(c.get("/login").status_code, 200)
            self.assertEqual(c.get("/healthz").status_code, 200)
            # Ingest keeps its own token check.
            self.assertEqual(c.post("/api/ingest", json={"listings": []}).status_code, 401)

            bad = c.post("/login", data={"password": "nope"}, follow_redirects=False)
            self.assertEqual(bad.headers["location"], "/login?e=1")
            self.assertEqual(c.get("/api/deals").status_code, 401)

            ok = c.post("/login", data={"password": "correct horse"}, follow_redirects=False)
            self.assertEqual(ok.headers["location"], "/")
            self.assertEqual(c.get("/api/deals").status_code, 200)

    def test_forged_or_expired_cookie_rejected(self):
        self.assertFalse(auth.valid_session("9999999999.deadbeef"))
        self.assertFalse(auth.valid_session(auth._sign(1)))
        self.assertTrue(auth.valid_session(auth._sign(9999999999)))

    def test_lockout_after_repeated_failures(self):
        with self.client() as c:
            for _ in range(auth.MAX_FAILS):
                c.post("/login", data={"password": "x"}, follow_redirects=False)
            r = c.post("/login", data={"password": "correct horse"}, follow_redirects=False)
            self.assertEqual(r.headers["location"], "/login?e=locked")

    def test_disabled_without_password(self):
        auth.DASHBOARD_PASSWORD = ""
        with self.client() as c:
            self.assertEqual(c.get("/api/deals").status_code, 200)


if __name__ == "__main__":
    unittest.main()
