"""vitr web: accounts like the tank hub (setup code, roles, users, audit), CSRF, limits, headers."""

import http.client
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vitr import auth, web  # noqa: E402
from vitr.core import Core  # noqa: E402
from vitr.db import Database  # noqa: E402

STATIC = Path(__file__).resolve().parents[1] / "static"
PW = "Spravne-heslo-1"


class FakeMqtt:
    status = "vypnuto"

    def notify(self, *a):
        pass

    def reconfigure(self):
        pass

    def remove_node(self, *a):
        pass


class Client:
    """A browser: keeps cookies and the CSRF token."""

    def __init__(self, t, headers=None):
        self.t, self.cookies, self.csrf, self.headers = t, {}, "", dict(headers or {})

    def req(self, method, path, body=None, headers=None, raw=None, ctype="application/json"):
        c = http.client.HTTPConnection("127.0.0.1", self.t.port, timeout=5)
        h = {**self.headers, **(headers or {})}
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
        if data is not None:
            h.setdefault("Content-Type", ctype)
        if method == "POST" and self.csrf:
            h.setdefault("X-CSRF-Token", self.csrf)
        if self.cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        out = r.read()
        for k, v in r.getheaders():
            if k.lower() == "set-cookie":
                name, _, rest = v.partition("=")
                val = rest.split(";")[0]
                if "Max-Age=0" in v:
                    self.cookies.pop(name, None)
                else:
                    self.cookies[name] = val
        c.close()
        try:
            j = json.loads(out)
        except ValueError:
            j = None
        return r.status, dict(r.getheaders()), j

    def login(self, user, pw):
        st, h, j = self.req("POST", "/api/auth/login", {"username": user, "password": pw})
        if st == 200 and not (j or {}).get("setup"):
            self.csrf = self.req("GET", "/api/me")[2]["csrf"]
        return st, h, j


class WebTest(unittest.TestCase):
    def setUp(self):
        web.time.sleep = lambda s: None                 # no slow-down in tests
        self.dir = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.dir, "t.db"))
        self.code = web.ensure_setup_code(Path(self.dir))
        self.core = Core(self.db)
        self.app = web.App(self.core, self.db, FakeMqtt(), str(STATIC), lambda: True, self.dir)
        self.srv = web.start_web(self.app, "127.0.0.1", 0)
        self.port = self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def admin(self, name="Matěj"):
        """First start: admin + setup code -> own administrator account."""
        c = Client(self)
        st, _, j = c.login("admin", self.code)
        self.assertEqual((st, j.get("setup")), (200, True))
        st, _, j = c.req("POST", "/api/auth/setup", {"username": name, "password": PW})
        self.assertEqual(st, 200, j)
        c.csrf = c.req("GET", "/api/me")[2]["csrf"]
        return c

    def add_user(self, adm, name, role="user"):
        st, _, j = adm.req("POST", "/api/users/add", {"username": name, "password": PW, "role": role})
        self.assertEqual(st, 200, j)

    # ------------------------------------------------------------ access
    def test_nothing_without_login(self):
        c = Client(self)
        for p in ("/api/state", "/api/me", "/api/history?id=1", "/api/mqtt", "/api/users", "/api/audit"):
            self.assertEqual(c.req("GET", p)[0], 401, p)
        st, h, _ = c.req("GET", "/")
        self.assertEqual((st, h["Location"]), (302, "/login"))
        self.assertEqual(c.req("GET", "/js/app.js")[0], 404)
        self.assertEqual(c.req("POST", "/api/pairing/start", {"node_id": "1234", "name": "Test"})[0], 401)
        self.assertEqual(c.req("GET", "/setup")[0], 302)            # no ticket
        for p in ("/login", "/js/login.js", "/css/style.css"):
            self.assertEqual(c.req("GET", p)[0], 200, p)
        self.assertEqual(c.req("GET", "/api/auth/mode")[2], {"first_login": True})

    def test_first_start_flow(self):
        c = Client(self)
        self.assertEqual(c.login("admin", "spatny-kod")[0], 401)
        self.assertEqual(c.login("Matěj", self.code)[0], 401)       # only "admin" during setup
        st, _, j = c.login("admin", self.code)
        self.assertTrue(j["setup"])
        self.assertEqual(c.req("GET", "/api/state")[0], 401)        # the ticket is no session
        self.assertEqual(c.req("GET", "/setup")[0], 200)
        st, _, j = c.req("POST", "/api/auth/setup", {"username": "admin", "password": PW})
        self.assertEqual(st, 400)
        st, _, j = c.req("POST", "/api/auth/setup", {"username": "Matěj", "password": "kratke"})
        self.assertEqual(st, 400)
        st, _, j = c.req("POST", "/api/auth/setup", {"username": "Matěj", "password": PW})
        self.assertEqual(st, 200)
        c.csrf = c.req("GET", "/api/me")[2]["csrf"]
        me = c.req("GET", "/api/me")[2]
        self.assertEqual((me["username"], me["role"]), ("Matěj", "admin"))
        # the setup code no longer works, setup is closed
        self.assertEqual(Client(self).login("admin", self.code)[0], 401)
        self.assertEqual(c.req("POST", "/api/auth/setup", {"username": "X2", "password": PW})[0], 409)
        acts = [r["action"] for r in self.db.all("SELECT action FROM audit ORDER BY id")]
        self.assertIn("SETUP_LOGIN", acts)
        self.assertIn("USER_ADDED", acts)

    def test_roles(self):
        adm = self.admin()
        self.add_user(adm, "Petr")
        u = Client(self)
        self.assertEqual(u.login("PETR", PW)[0], 200)              # name in any case
        self.assertEqual(u.req("GET", "/api/me")[2]["role"], "user")
        self.assertEqual(u.req("GET", "/api/state")[0], 200)
        self.assertEqual(u.req("GET", "/api/history?id=1")[0], 200)
        self.assertEqual(u.req("GET", "/")[0], 200)
        for p in ("/api/mqtt", "/api/users", "/api/audit"):
            self.assertEqual(u.req("GET", p)[0], 403, p)
        for p in ("/api/pairing/start", "/api/nodes/remove", "/api/mqtt", "/api/users/add"):
            self.assertEqual(u.req("POST", p, {"id": 1})[0], 403, p)
        # promote -> admin rights
        self.assertEqual(adm.req("POST", "/api/users/role", {"username": "petr", "role": "admin"})[0], 200)
        self.assertEqual(u.req("POST", "/api/pairing/start", {"node_id": "1234", "name": "Test"})[0], 200)

    def test_user_management(self):
        adm = self.admin()
        self.assertEqual(adm.req("POST", "/api/users/add", {"username": "matěj", "password": PW})[0], 400)
        self.assertEqual(adm.req("POST", "/api/users/add", {"username": "a b", "password": PW})[0], 400)
        self.add_user(adm, "Petr")
        u = Client(self)
        u.login("Petr", PW)
        # own account: no role change / delete
        self.assertEqual(adm.req("POST", "/api/users/delete", {"username": "Matěj"})[0], 400)
        # new password from the admin logs the user out
        self.assertEqual(adm.req("POST", "/api/users/password", {"username": "Petr", "password": "Jine-heslo-22"})[0], 200)
        self.assertEqual(u.req("GET", "/api/state")[0], 401)
        self.assertEqual(Client(self).login("Petr", "Jine-heslo-22")[0], 200)
        # delete
        self.assertEqual(adm.req("POST", "/api/users/delete", {"username": "petr"})[0], 200)
        self.assertEqual(Client(self).login("Petr", "Jine-heslo-22")[0], 401)
        users = adm.req("GET", "/api/users")[2]
        self.assertEqual([x["username"] for x in users], ["Matěj"])
        self.assertNotIn("pw_hash", users[0])

    def test_last_admin_stays(self):
        adm = self.admin()
        self.add_user(adm, "Petr", "admin")
        p = Client(self)
        p.login("Petr", PW)
        self.assertEqual(p.req("POST", "/api/users/delete", {"username": "Matěj"})[0], 200)
        # Petr is now the only admin; nobody can demote him (own account) - and an only
        # admin cannot be removed by anyone
        self.assertEqual(p.req("POST", "/api/users/role", {"username": "Petr", "role": "user"})[0], 400)

    def test_my_password(self):
        adm = self.admin()
        other = Client(self)
        other.login("Matěj", PW)
        st, _, _ = adm.req("POST", "/api/me/password", {"old_password": "spatne", "new_password": "Nove-heslo-123"})
        self.assertEqual(st, 400)
        st, _, _ = adm.req("POST", "/api/me/password", {"old_password": PW, "new_password": "Nove-heslo-123"})
        self.assertEqual(st, 200)
        self.assertEqual(adm.req("GET", "/api/state")[0], 200)     # this device stays in
        self.assertEqual(other.req("GET", "/api/state")[0], 401)   # others are logged out
        self.assertEqual(Client(self).login("Matěj", "Nove-heslo-123")[0], 200)

    # ---------------------------------------------------------- security
    def test_csrf_and_origin(self):
        adm = self.admin()
        tok = adm.csrf
        adm.csrf = ""
        self.assertEqual(adm.req("POST", "/api/pairing/start", {"node_id": "1234", "name": "Test"})[0], 403)                 # no token
        self.assertEqual(adm.req("POST", "/api/pairing/start", {"node_id": "1234", "name": "Test"}, {"X-CSRF-Token": "x"})[0], 403)
        adm.csrf = tok
        self.assertEqual(adm.req("POST", "/api/pairing/start", {"node_id": "1234", "name": "Test"}, {"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(adm.req("POST", "/api/pairing/start", {"node_id": "1234", "name": "Test"}, {"Origin": f"http://127.0.0.1:{self.port}"})[0], 200)
        self.assertEqual(adm.req("POST", "/api/pairing/cancel", {"node_id": "1234"}, {"Origin": "https://vitr.example",
                                                                 "X-Forwarded-Host": "vitr.example"})[0], 200)
        self.assertEqual(adm.req("POST", "/api/pair/start", raw=b"a=1",
                                 ctype="application/x-www-form-urlencoded")[0], 400)

    def test_login_limits(self):
        adm = self.admin()
        a = Client(self)
        for _ in range(3):
            self.assertEqual(a.login("Matěj", "spatne")[0], 401)
        self.assertEqual(a.login("Matěj", "spatne")[0], 401)       # starts the back-off
        self.assertEqual(a.login("Matěj", PW)[0], 429)
        # another visitor behind cloudflared is not affected
        b = Client(self, {"CF-Connecting-IP": "203.0.113.9"})
        self.assertEqual(b.login("Matěj", PW)[0], 200)
        # lock-out per (name, IP): 5 failures lock the name for that IP only
        self.app.ip_limiter = auth.IpLimiter(free_attempts=100)
        c = Client(self, {"CF-Connecting-IP": "198.51.100.7"})
        for _ in range(auth.USER_MAX_FAILS):
            c.login("Matěj", "spatne")
        self.assertEqual(c.login("Matěj", PW)[0], 401)             # same answer as a wrong password
        self.assertEqual(Client(self, {"CF-Connecting-IP": "198.51.100.8"}).login("Matěj", PW)[0], 200)
        fails = self.db.scalar("SELECT COUNT(*) FROM audit WHERE action='LOGIN_FAILED'")
        self.assertGreaterEqual(fails, 9)
        self.assertTrue(adm.req("GET", "/api/audit?category=problems")[2]["total"] >= 9)

    def test_https_tunnel_cookie_and_hsts(self):
        self.admin()
        c = Client(self, {"X-Forwarded-Proto": "https"})
        st, h, _ = c.login("Matěj", PW)
        self.assertEqual(st, 200)
        self.assertIn("Secure", h["Set-Cookie"])
        self.assertIn("HttpOnly", h["Set-Cookie"])
        self.assertIn("SameSite=Strict", h["Set-Cookie"])
        self.assertIn("Strict-Transport-Security", h)
        st, h, _ = Client(self).login("Matěj", PW)
        self.assertNotIn("Secure", h["Set-Cookie"])                # plain http on the LAN

    def test_sessions_stored_hashed_and_logout(self):
        adm = self.admin()
        tok = adm.cookies[web.COOKIE]
        self.assertIsNone(self.db.one("SELECT 1 FROM sessions WHERE id_hash=?", (tok,)))
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM sessions"), 1)
        adm.req("POST", "/api/auth/logout", {})
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM sessions"), 0)
        adm.cookies[web.COOKIE] = tok
        self.assertEqual(adm.req("GET", "/api/state")[0], 401)

    def test_keep_alive_connection(self):
        """Browsers reuse one connection: login then data on the same socket."""
        self.admin()
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)

        def go(method, path, body=None, cookie=""):
            h = {"Content-Type": "application/json"}
            if cookie:
                h["Cookie"] = cookie
            c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
            r = c.getresponse()
            r.read()
            return r
        self.assertEqual(go("GET", "/api/me").status, 401)
        r = go("POST", "/api/auth/login", {"username": "Matěj", "password": PW})
        ck = r.getheader("Set-Cookie").split(";")[0]
        self.assertEqual(go("GET", "/api/me", cookie=ck).status, 200)
        self.assertEqual(go("GET", "/", cookie=ck).status, 200)
        self.assertEqual(go("GET", "/api/me").status, 401)
        c.close()

    def test_security_headers_and_paths(self):
        adm = self.admin()
        _, h, _ = adm.req("GET", "/login")
        self.assertIn("script-src 'self'", h["Content-Security-Policy"])
        self.assertEqual(h["X-Frame-Options"], "DENY")
        self.assertEqual(adm.req("GET", "/../vitr/web.py")[0], 404)
        self.assertEqual(adm.req("GET", "/%2e%2e/vitr/web.py")[0], 404)
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.putrequest("POST", "/api/auth/login")
        c.putheader("Content-Type", "application/json")
        c.putheader("Content-Length", "abc")
        c.endheaders()
        self.assertEqual(c.getresponse().status, 400)
        c.close()

    def test_audit_of_admin_actions(self):
        adm = self.admin()
        adm.req("POST", "/api/pairing/start", {"node_id": "1234", "name": "Test"})
        adm.req("POST", "/api/mqtt", {"enabled": True, "host": "10.0.0.2", "port": 1883})
        d = adm.req("GET", "/api/audit?who=Matěj".replace("ě", "%C4%9B"))[2]
        acts = {x["action"] for x in d["logs"]}
        self.assertTrue({"NODE_PAIRING", "SETTINGS_CHANGED", "USER_ADDED"} <= acts, acts)
        self.assertEqual(adm.req("GET", "/api/audit?category=settings")[2]["total"], 1)

    def test_pairing_api_like_tank_hub(self):
        adm = self.admin()
        st, _, d = adm.req("GET", "/api/nodes")
        self.assertEqual(st, 200)
        self.assertEqual(set(d), {"nodes", "heard", "pairing", "radio"})
        self.assertEqual(adm.req("POST", "/api/pairing/start", {"node_id": "12G4", "name": "x"})[0], 400)
        self.assertEqual(adm.req("POST", "/api/pairing/start", {"node_id": "abcd"})[0], 400)     # name missing
        st, _, d = adm.req("POST", "/api/pairing/start", {"node_id": "abcd", "name": "Střecha"})
        self.assertEqual([t["id"] for t in d["pairing"]["targets"]], ["ABCD"])
        st, _, d = adm.req("POST", "/api/pairing/cancel", {"node_id": "ABCD"})
        self.assertEqual(d["pairing"]["targets"], [])
        self.assertEqual(adm.req("POST", "/api/nodes/remove", {"node_id": "ABCD"})[0], 404)
        self.assertEqual(adm.req("POST", "/api/nodes/unpair/cancel", {"node_id": "ABCD"})[0], 404)
        self.add_user(adm, "Host")
        u = Client(self)
        u.login("Host", PW)
        self.assertEqual(u.req("GET", "/api/nodes")[0], 403)


if __name__ == "__main__":
    unittest.main()
