"""vitr web: everything behind the password, login limits, cookies, CSRF, headers."""

import http.client
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vitr import web  # noqa: E402
from vitr.core import Core  # noqa: E402
from vitr.db import Database  # noqa: E402

STATIC = Path(__file__).resolve().parents[1] / "static"


class FakeMqtt:
    status = {"state": "off"}

    def notify(self, *a):
        pass

    def reconfigure(self):
        pass

    def remove_node(self, *a):
        pass


class WebTest(unittest.TestCase):
    def setUp(self):
        web.time.sleep = lambda s: None                 # no slow-down in tests
        self.dir = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.dir, "t.db"))
        self.db.set("password", web.hash_password("spravne-heslo-1"))
        Path(self.dir, "heslo.txt").write_text("x")
        self.app = web.App(Core(self.db), self.db, FakeMqtt(), str(STATIC), lambda: True)
        self.srv = web.start_web(self.app, "127.0.0.1", 0)
        self.port = self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def req(self, method, path, body=None, cookie=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
        if cookie:
            h["Cookie"] = f"{web.COOKIE}={cookie}"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            j = json.loads(raw)
        except ValueError:
            j = None
        return r.status, dict(r.getheaders()), j

    def login(self, pw="spravne-heslo-1", headers=None):
        st, h, _ = self.req("POST", "/api/login", {"password": pw}, headers=headers)
        tok = None
        if "Set-Cookie" in h:
            tok = h["Set-Cookie"].split(";")[0].split("=", 1)[1]
        return st, h, tok

    def test_nothing_without_login(self):
        for p in ("/api/state", "/api/me", "/api/history?id=1", "/api/mqtt"):
            self.assertEqual(self.req("GET", p)[0], 401, p)
        st, h, _ = self.req("GET", "/")
        self.assertEqual((st, h["Location"]), (302, "/login"))
        self.assertEqual(self.req("GET", "/index.html")[0], 302)
        self.assertEqual(self.req("GET", "/js/app.js")[0], 404)
        self.assertEqual(self.req("GET", "/vendor/chart.umd.min.js")[0], 404)
        self.assertEqual(self.req("POST", "/api/pair/start", {})[0], 401)
        self.assertEqual(self.req("GET", "/login")[0], 200)
        self.assertEqual(self.req("GET", "/js/login.js")[0], 200)
        self.assertEqual(self.req("GET", "/css/style.css")[0], 200)

    def test_login_and_use(self):
        st, h, tok = self.login()
        self.assertEqual(st, 200)
        self.assertIn("HttpOnly", h["Set-Cookie"])
        self.assertIn("SameSite=Strict", h["Set-Cookie"])
        self.assertNotIn("Secure", h["Set-Cookie"])           # plain http on the LAN
        self.assertEqual(self.req("GET", "/api/state", cookie=tok)[0], 200)
        self.assertEqual(self.req("GET", "/", cookie=tok)[0], 200)
        self.assertEqual(self.req("GET", "/js/app.js", cookie=tok)[0], 200)
        self.assertEqual(self.req("GET", "/login", cookie=tok)[0], 302)
        self.req("POST", "/api/logout", {}, cookie=tok)
        self.assertEqual(self.req("GET", "/api/state", cookie=tok)[0], 401)

    def test_wrong_password_and_backoff(self):
        for _ in range(web.FREE_FAILS):
            self.assertEqual(self.login("spatne")[0], 401)
        self.assertEqual(self.login("spatne")[0], 401)        # starts the back-off
        st, h, _ = self.login()                                # even the right one waits
        self.assertEqual(st, 429)
        self.assertIn("Retry-After", h)
        # another client (behind cloudflared) is not affected
        st, _, _ = self.login(headers={"CF-Connecting-IP": "203.0.113.9"})
        self.assertEqual(st, 200)

    def test_https_tunnel_cookie_and_hsts(self):
        st, h, _ = self.login(headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(st, 200)
        self.assertIn("Secure", h["Set-Cookie"])
        self.assertIn("Strict-Transport-Security", h)

    def test_cross_origin_post_rejected(self):
        _, _, tok = self.login()
        st, _, _ = self.req("POST", "/api/pair/start", {}, cookie=tok,
                            headers={"Origin": "https://evil.example"})
        self.assertEqual(st, 403)
        st, _, _ = self.req("POST", "/api/pair/start", {}, cookie=tok,
                            headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(st, 200)
        st, _, _ = self.req("POST", "/api/pair/stop", {}, cookie=tok,
                            headers={"Origin": "https://vitr.example", "X-Forwarded-Host": "vitr.example"})
        self.assertEqual(st, 200)
        # a form post (not JSON) is refused
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", "/api/pair/start", body=b"a=1", headers={
            "Content-Type": "application/x-www-form-urlencoded", "Cookie": f"{web.COOKIE}={tok}"})
        self.assertEqual(c.getresponse().status, 400)
        c.close()

    def test_security_headers(self):
        _, h, _ = self.req("GET", "/login")
        self.assertIn("script-src 'self'", h["Content-Security-Policy"])
        self.assertEqual(h["X-Frame-Options"], "DENY")
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")

    def test_password_change_drops_other_sessions(self):
        _, _, a = self.login()
        _, _, b = self.login()
        st, _, j = self.req("POST", "/api/password", {"old": "spravne-heslo-1", "new": "kratke"}, cookie=a)
        self.assertEqual(st, 400)
        st, _, _ = self.req("POST", "/api/password", {"old": "spravne-heslo-1", "new": "nove-heslo-123"}, cookie=a)
        self.assertEqual(st, 200)
        self.assertEqual(self.req("GET", "/api/state", cookie=a)[0], 200)
        self.assertEqual(self.req("GET", "/api/state", cookie=b)[0], 401)
        self.assertFalse(Path(self.dir, "heslo.txt").exists())
        self.assertEqual(self.login("spravne-heslo-1")[0], 401)
        self.assertEqual(self.login("nove-heslo-123")[0], 200)

    def test_path_traversal_and_bad_length(self):
        _, _, tok = self.login()
        self.assertEqual(self.req("GET", "/../vitr/web.py", cookie=tok)[0], 404)
        self.assertEqual(self.req("GET", "/%2e%2e/vitr/web.py", cookie=tok)[0], 404)
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.putrequest("POST", "/api/login")
        c.putheader("Content-Type", "application/json")
        c.putheader("Content-Length", "abc")
        c.endheaders()
        self.assertEqual(c.getresponse().status, 400)
        c.close()

    def test_session_expiry(self):
        t = [1000.0]
        self.app.clock = lambda: t[0]
        _, _, tok = self.login()
        t[0] += web.SESSION_IDLE_S + 1
        self.assertEqual(self.req("GET", "/api/state", cookie=tok)[0], 401)


if __name__ == "__main__":
    unittest.main()
