"""Web UI and JSON API (standard library HTTP server in its own thread).

Accounts work exactly like on the tank hub (retencka):
  * user accounts with a role: "admin" (settings, pairing, users, event history)
    or "user" (read only: dashboard and charts, own password);
  * first start: while no account exists the only way in is the name "admin"
    with the setup code from the data directory (/var/lib/vitr/setup_code,
    printed by the installer). It grants only a 15 min ticket for creating the
    real administrator account; afterwards the code stops working;
  * server-side sessions (only the SHA-256 of the cookie token is stored),
    idle 7 days / absolute 30 days, all other sessions dropped on a password
    change, a user's sessions dropped when deleted or given a new password;
  * CSRF: per-session token in the X-CSRF-Token header + Origin check + JSON only;
  * brute force: per-IP exponential back-off and a per-(name, IP) lock-out,
    unknown names cost the same scrypt as known ones;
  * the real client address is taken from CF-Connecting-IP / X-Forwarded-For
    only from a local proxy (cloudflared on loopback); Secure cookie and HSTS
    behind an HTTPS tunnel;
  * strict CSP (no inline scripts), X-Frame-Options, no-store for the API;
  * everything is written to the event history (audit), like the tank hub.
Without a session only the login / setup pages and their assets are served.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import mimetypes
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from . import __version__, auth, updates
from .core import MAX_NODES

log = logging.getLogger("vitr.web")

COOKIE = "vitr_s"
SETUP_COOKIE = "vitr_setup"
SETUP_TICKET_S = 15 * 60
MAX_BODY = 16 * 1024
SOCKET_TIMEOUT_S = 20

PUBLIC_FILES = {"/login.html", "/setup.html", "/js/login.js", "/js/theme.js", "/css/style.css",
                "/favicon.svg"}

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
       "form-action 'self'; frame-ancestors 'none'")

AUDIT_CATEGORIES = {
    "login": ["LOGIN_SUCCESS", "LOGIN_FAILED", "LOGOUT", "SETUP_LOGIN", "SETUP_FAILED", "PASSWORD_CHANGED"],
    "users": ["USER_ADDED", "USER_DELETED", "USER_ROLE_CHANGED", "PASSWORD_CHANGED"],
    "sensors": ["NODE_PAIRED", "NODE_PAIRING", "NODE_PAIRING_CANCELLED", "NODE_REMOVED",
                "NODE_UNPAIR_REQUESTED", "NODE_UNPAIR_CANCELLED", "NODE_UNPAIRED", "CALIBRATION"],
    "settings": ["SETTINGS_CHANGED", "CALIBRATION"],
    "system": ["UPDATE_REQUESTED", "UPDATE_AVAILABLE", "UPDATE_INSTALLED", "UPDATE_ROLLED_BACK",
               "UPDATE_ERROR", "UPDATE_UP_TO_DATE"],
    "problems": ["LOGIN_FAILED", "SETUP_FAILED", "UPDATE_ROLLED_BACK", "UPDATE_ERROR"],
}


def _is_loopback(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


def ensure_setup_code(data_dir: Path) -> str:
    """The first-login password (like the label password of the tank hub)."""
    path = data_dir / "setup_code"
    if not path.exists():
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        code = "".join(secrets.choice(alphabet) for _ in range(8))
        path.write_text(code[:4] + "-" + code[4:] + "\n")
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return path.read_text().strip()


def parse_node_id(raw) -> int:
    txt = str(raw or "").strip().upper().replace("0X", "")
    if not txt or len(txt) > 4 or any(c not in "0123456789ABCDEF" for c in txt):
        raise ValueError("ID čidla jsou 4 znaky 0–9/A–F (např. 3F2A).")
    return int(txt, 16)


class HttpError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code


class App:
    def __init__(self, core, db, mqtt, static_dir: str, radio_ok: callable, data_dir: Optional[str] = None):
        self.core, self.db, self.mqtt = core, db, mqtt
        self.static = Path(static_dir).resolve()
        self.data_dir = Path(data_dir or Path(db.path).parent)
        self.radio_ok = radio_ok
        self.sessions = auth.Sessions(db)
        self.login_locks = auth.LoginLocks()
        self.ip_limiter = auth.IpLimiter()
        self.setup_tickets: dict[str, float] = {}
        self.setup_lock = threading.Lock()
        self.lock = threading.Lock()

    # ------------------------------------------------------------ accounts
    def no_users(self) -> bool:
        return self.db.scalar("SELECT COUNT(*) FROM users", (), 0) == 0

    def find_user(self, name: str) -> Optional[dict]:
        key = auth.name_key(name)
        if not key:
            return None
        return next((r for r in self.db.all("SELECT * FROM users") if auth.name_key(r["username"]) == key), None)

    def setup_password(self) -> str:
        try:
            return (self.data_dir / "setup_code").read_text().strip()
        except OSError:
            return ""

    def valid_ticket(self, tok: Optional[str]) -> bool:
        with self.lock:
            exp = self.setup_tickets.get(tok or "")
        return bool(tok) and exp is not None and exp > time.time()

    def new_ticket(self) -> str:
        tok = secrets.token_urlsafe(32)
        now = time.time()
        with self.lock:
            self.setup_tickets = {k: v for k, v in self.setup_tickets.items() if v > now}
            self.setup_tickets[tok] = now + SETUP_TICKET_S
        return tok

    def last_admin(self, row: dict) -> bool:
        return row["role"] == "admin" and self.db.scalar(
            "SELECT COUNT(*) FROM users WHERE role='admin'", (), 0) <= 1

    # ---------------------------------------------------------------- data
    def mqtt_cfg(self, with_password: bool = False) -> dict:
        raw = self.db.get("mqtt", "{}")
        try:
            cfg = json.loads(raw)
        except ValueError:
            cfg = {}
        cfg.setdefault("enabled", False)
        cfg.setdefault("host", "127.0.0.1")
        cfg.setdefault("port", 1883)
        cfg.setdefault("user", "")
        cfg.setdefault("password", "")
        cfg["uid"] = self.db.get("uid")
        if not with_password:
            cfg["password_set"] = bool(cfg.pop("password"))
        return cfg

    def overview(self) -> dict:
        return {
            "version": __version__,
            "nodes": self.core.states(),
            "radio": {**self.core.stats, "bridge_ok": self.radio_ok()},
            "mqtt": self.mqtt.status,
            "now": int(time.time()),
        }

    def nodes_page(self) -> dict:
        """Settings → Čidla: sensors, pairing, sensors in range (same shape as the tank hub)."""
        pairing = self.core.pairing_status()
        return {"nodes": self.core.states(), "heard": self.core.heard_list(), "pairing": pairing,
                "radio": {**self.core.stats, "bridge_ok": self.radio_ok()}}

    def audit_page(self, q: dict) -> dict:
        def arg(k, d=""):
            return q.get(k, [d])[0]
        try:
            page = min(max(1, int(arg("page", "1"))), 1_000_000)
        except ValueError:
            page = 1
        per = 50
        where, args = [], []
        cat = arg("category")
        if cat in AUDIT_CATEGORIES:
            acts = AUDIT_CATEGORIES[cat]
            where.append(f"action IN ({','.join('?' * len(acts))})")
            args += acts
        who = arg("who")
        if who == "people":
            where.append("username NOT LIKE 'system%'")
        elif who == "system":
            where.append("username LIKE 'system%'")
        elif who:
            where.append("username = ? COLLATE NOCASE")
            args.append(who[:64])
        text = arg("q").strip()[:100]
        if text:
            where.append("(detail LIKE ? ESCAPE '\\' OR username LIKE ? ESCAPE '\\' OR ip LIKE ? ESCAPE '\\')")
            like = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            args += [like, like, like]
        for key, op in (("from", ">="), ("to", "<")):
            try:
                v = int(arg(key))
                if -2 ** 62 < v < 2 ** 62:
                    where.append(f"ts {op} ?")
                    args.append(v)
            except ValueError:
                pass
        w = ("WHERE " + " AND ".join(where)) if where else ""
        total = self.db.scalar(f"SELECT COUNT(*) FROM audit {w}", tuple(args), 0)
        rows = self.db.all(f"SELECT ts, username, ip, action, detail FROM audit {w} "
                           "ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?", tuple(args + [per, (page - 1) * per]))
        users = [r["username"] for r in self.db.all(
            "SELECT DISTINCT username FROM audit WHERE username NOT LIKE 'system%' "
            "AND username != '' ORDER BY username COLLATE NOCASE LIMIT 50")]
        return {"logs": rows, "total": total, "page": page,
                "total_pages": max(1, (total + per - 1) // per), "users": users}


def make_handler(app: App):
    class H(BaseHTTPRequestHandler):
        server_version = "vitr"
        sys_version = ""
        protocol_version = "HTTP/1.1"
        timeout = SOCKET_TIMEOUT_S

        def log_message(self, fmt, *args):     # quiet: systemd journal only for errors
            pass

        # ------------------------------------------------------- helpers
        def _from_proxy(self) -> bool:
            return _is_loopback(self.client_address[0])

        def _ip(self) -> str:
            peer = self.client_address[0]
            if self._from_proxy():
                cf = self.headers.get("CF-Connecting-IP")
                if cf:
                    return cf.strip()[:64]
                xff = self.headers.get("X-Forwarded-For")
                if xff:
                    return xff.split(",")[0].strip()[:64]
            return peer

        def _https(self) -> bool:
            return self._from_proxy() and (
                self.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower() == "https")

        def _cookie(self, name: str = COOKIE) -> Optional[str]:
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == name:
                    return v
            return None

        def _session(self) -> Optional[dict]:
            if not hasattr(self, "_sess"):
                self._sess = app.sessions.lookup(self._cookie())
            return self._sess

        def _cookie_hdr(self, name: str, tok: str, max_age: int) -> str:
            secure = "; Secure" if self._https() else ""
            return f"{name}={tok}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{secure}"

        def _send(self, code: int, body: bytes, ctype: str, headers=None, cache: str = "no-store") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("X-Robots-Tag", "noindex, nofollow")
            if self._https():
                self.send_header("Strict-Transport-Security", "max-age=31536000")
            for k, v in (headers.items() if isinstance(headers, dict) else (headers or [])):
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj, code: int = 200, headers=None) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", headers)

        def _err(self, code: int, msg: str, headers=None) -> None:
            self._json({"ok": False, "error": msg}, code, headers)

        def _redirect(self, to: str) -> None:
            self._send(302, b"", "text/plain; charset=utf-8", {"Location": to})

        def _body(self) -> Optional[dict]:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if n < 0 or n > MAX_BODY:
                self.close_connection = True     # do not read the rest
                return None
            raw = self.rfile.read(n) if n else b""
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return None                     # also blocks cross-site form posts
            try:
                obj = json.loads(raw or b"{}")
            except ValueError:
                return None
            return obj if isinstance(obj, dict) else None

        def _same_origin(self) -> bool:
            origin = self.headers.get("Origin")
            if not origin:
                return True
            host = origin.split("://", 1)[-1].rstrip("/").lower()
            allowed = {(self.headers.get("Host") or "").lower()}
            if self._from_proxy() and self.headers.get("X-Forwarded-Host"):
                allowed.add(self.headers["X-Forwarded-Host"].split(",")[0].strip().lower())
            return host in allowed

        def _need(self, role: Optional[str] = None) -> dict:
            s = self._session()
            if not s:
                raise HttpError(401, "Nejste přihlášen")
            if role == "admin" and s["role"] != "admin":
                raise HttpError(403, "Nedostatečná oprávnění")
            return s

        def _limited(self, ip: str) -> bool:
            wait = app.ip_limiter.retry_after(ip)
            if wait:
                self._err(429, f"Příliš mnoho pokusů. Zkuste to za {wait} s.", {"Retry-After": str(wait)})
                return True
            return False

        # ---------------------------------------------------------- GET
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            self.__dict__.pop("_sess", None)    # keep-alive: one handler serves many requests
            u = urlparse(self.path)
            p = u.path
            try:
                if p.startswith("/api/"):
                    return self._api_get(p, parse_qs(u.query))
                if p in ("/login", "/login.html"):
                    if self._session():
                        return self._redirect("/")
                    if app.no_users() and app.valid_ticket(self._cookie(SETUP_COOKIE)):
                        return self._redirect("/setup")
                    return self._static("/login.html")
                if p in ("/setup", "/setup.html"):
                    if not app.no_users() or not app.valid_ticket(self._cookie(SETUP_COOKIE)):
                        return self._redirect("/login")
                    return self._static("/setup.html")
                if p in ("", "/", "/index.html"):
                    if not self._session():
                        return self._redirect("/login")
                    return self._static("/index.html")
                if p in PUBLIC_FILES or self._session():
                    return self._static(p)
                return self._err(404, "Nenalezeno")
            except HttpError as e:
                return self._err(e.code, str(e))
            except (KeyError, ValueError, IndexError):
                return self._err(400, "Špatný požadavek")
            except Exception:
                log.exception("GET %s", p)
                return self._err(500, "Chyba serveru")

        def _api_get(self, p: str, q: dict) -> None:
            if p == "/api/auth/mode":
                return self._json({"first_login": app.no_users()})
            if p == "/api/me":
                s = self._need()
                return self._json({"username": s["username"], "role": s["role"], "csrf": s["csrf"],
                                   "version": __version__})
            if p == "/api/state":
                self._need()
                return self._json(app.overview())
            if p == "/api/update/status":
                self._need("admin")
                return self._json(updates.summary(str(app.data_dir), __version__))
            if p == "/api/nodes":
                self._need("admin")
                return self._json(app.nodes_page())
            if p == "/api/history":
                self._need()
                nid = int(q["id"][0])
                to = int(q.get("to", [time.time()])[0])
                frm = int(q.get("from", [to - 86400])[0])
                return self._json(app.core.history(nid, frm, to))
            if p == "/api/mqtt":
                self._need("admin")
                return self._json(app.mqtt_cfg())
            if p == "/api/users":
                self._need("admin")
                return self._json(app.db.all(
                    "SELECT username, role, created, last_login FROM users ORDER BY username COLLATE NOCASE"))
            if p == "/api/audit":
                self._need("admin")
                return self._json(app.audit_page(q))
            raise HttpError(404, "Nenalezeno")

        def _static(self, p: str) -> None:
            f = (app.static / p.lstrip("/")).resolve()
            if app.static not in f.parents or not f.is_file():
                return self._err(404, "Nenalezeno")
            ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            cache = "private, max-age=604800" if p.startswith("/vendor/") else "no-cache"
            self._send(200, f.read_bytes(), ctype, cache=cache)

        # --------------------------------------------------------- POST
        def do_POST(self):
            self.__dict__.pop("_sess", None)
            p = urlparse(self.path).path
            body = self._body()
            if body is None:
                return self._err(400, "Špatný požadavek")
            if not self._same_origin():
                return self._err(403, "Neplatný původ požadavku")
            sess = self._session()
            if sess and p not in ("/api/auth/login", "/api/auth/setup"):
                sent = self.headers.get("X-CSRF-Token", "")
                if not hmac.compare_digest(sent.encode("utf-8"), sess["csrf"].encode("utf-8")):
                    return self._err(403, "Neplatný CSRF token – obnovte stránku")
            try:
                if p == "/api/auth/login":
                    return self._login(body)
                if p == "/api/auth/setup":
                    return self._setup(body)
                if p == "/api/auth/logout":
                    if sess:
                        app.core.audit("LOGOUT", "Odhlášení", sess["username"], self._ip())
                    app.sessions.delete(self._cookie())
                    return self._json({"ok": True}, headers={"Set-Cookie": self._cookie_hdr(COOKIE, "", 0)})
                if p == "/api/me/password":
                    return self._my_password(self._need(), body)
                if p.startswith("/api/users/"):
                    return self._users(p, self._need("admin"), body)
                return self._admin(p, self._need("admin"), body)
            except HttpError as e:
                return self._err(e.code, str(e))
            except ValueError as e:
                return self._err(400, str(e))
            except (KeyError, TypeError):
                return self._err(400, "Špatný požadavek")
            except Exception:
                log.exception("POST %s", p)
                return self._err(500, "Chyba serveru")

        # ------------------------------------------------------ accounts
        def _login(self, b: dict) -> None:
            ip = self._ip()
            if self._limited(ip):
                return
            username = str(b.get("username", ""))[:64].strip()
            password = str(b.get("password", ""))[:256]
            if app.no_users():
                expected = app.setup_password()
                ok = (username.lower() == "admin" and bool(expected)
                      and hmac.compare_digest(password.strip().encode(), expected.encode()))
                if not ok:
                    app.ip_limiter.fail(ip)
                    app.core.audit("SETUP_FAILED", "Neplatné výchozí přihlášení", username or "-", ip)
                    time.sleep(0.5 + secrets.randbelow(500) / 1000)
                    return self._err(401, "Neplatné jméno nebo heslo. Při prvním přihlášení použijte jméno "
                                          "admin a kód z instalace (sudo cat /var/lib/vitr/setup_code).")
                app.ip_limiter.success(ip)
                app.core.audit("SETUP_LOGIN", "První přihlášení výchozím účtem – vytváření vlastního účtu",
                               "admin", ip)
                return self._json({"ok": True, "setup": True}, headers={
                    "Set-Cookie": self._cookie_hdr(SETUP_COOKIE, app.new_ticket(), SETUP_TICKET_S)})

            row = app.find_user(username)
            # locked (name, IP): verify against nothing (same cost), answer like a wrong password
            locked = app.login_locks.locked(username, ip)
            ok = auth.verify_password(password, row["pw_hash"] if row and not locked else None)
            if not ok or locked:
                app.ip_limiter.fail(ip)
                if not locked:
                    app.login_locks.fail(username, ip)
                app.core.audit("LOGIN_FAILED", "Neplatné jméno nebo heslo", username or "(prázdné)", ip)
                time.sleep(0.5 + secrets.randbelow(500) / 1000)
                return self._err(401, "Neplatné jméno nebo heslo.")
            app.ip_limiter.success(ip)
            app.login_locks.success(username, ip)
            app.db.execute("UPDATE users SET last_login=? WHERE username=?", (int(time.time()), row["username"]))
            app.sessions.delete(self._cookie())                 # no session fixation
            app.sessions.purge()
            token, _ = app.sessions.create(row["username"], ip, self.headers.get("User-Agent", ""))
            app.core.audit("LOGIN_SUCCESS", "Přihlášení", row["username"], ip)
            return self._json({"ok": True},
                              headers={"Set-Cookie": self._cookie_hdr(COOKIE, token, auth.SESSION_ABSOLUTE_S)})

        def _setup(self, b: dict) -> None:
            """Create the first own administrator account (needs the setup ticket)."""
            ip = self._ip()
            if not app.no_users():
                return self._err(409, "Systém už je nastaven.")
            if not app.valid_ticket(self._cookie(SETUP_COOKIE)):
                return self._err(403, "Platnost prvního přihlášení vypršela – přihlaste se znovu kódem z instalace.")
            username = auth.clean_username(str(b.get("username", "")))
            password = str(b.get("password", ""))
            if username.lower() == "admin":
                return self._err(400, "Zvolte si vlastní jméno – výchozí účet „admin“ bude zrušen.")
            problem = auth.username_problem(username) or auth.password_problem(username, password)
            if problem:
                return self._err(400, problem)
            if hmac.compare_digest(password.encode(), app.setup_password().encode()):
                return self._err(400, "Nové heslo nesmí být stejné jako kód z instalace.")
            pw_hash = auth.hash_password(password)
            with app.setup_lock:
                if not app.no_users():
                    return self._err(409, "Systém už je nastaven.")
                now = int(time.time())
                app.db.execute("INSERT INTO users(username, pw_hash, role, created, pw_changed) VALUES(?,?,?,?,?)",
                               (username, pw_hash, "admin", now, now))
                with app.lock:
                    app.setup_tickets.clear()
            app.core.audit("USER_ADDED", f"Vytvořen administrátor {username!r}; výchozí účet admin zrušen",
                           username, ip)
            token, _ = app.sessions.create(username, ip, self.headers.get("User-Agent", ""))
            # one Set-Cookie only (some proxies fold several into one header); the setup
            # ticket is already void on the server and its cookie expires on its own
            return self._json({"ok": True},
                              headers={"Set-Cookie": self._cookie_hdr(COOKIE, token, auth.SESSION_ABSOLUTE_S)})

        def _my_password(self, s: dict, b: dict) -> None:
            ip = self._ip()
            if self._limited(ip):
                return
            old, new = str(b.get("old_password", ""))[:256], str(b.get("new_password", ""))
            row = app.db.one("SELECT pw_hash FROM users WHERE username=?", (s["username"],))
            if not row or not auth.verify_password(old, row["pw_hash"]):
                app.ip_limiter.fail(ip)
                return self._err(400, "Současné heslo není správné.")
            problem = auth.password_problem(s["username"], new)
            if problem:
                return self._err(400, problem)
            app.db.execute("UPDATE users SET pw_hash=?, pw_changed=? WHERE username=?",
                           (auth.hash_password(new), int(time.time()), s["username"]))
            app.sessions.delete_user_sessions(s["username"], except_hash=s["id_hash"])
            app.core.audit("PASSWORD_CHANGED", "Změna vlastního hesla (ostatní relace odhlášeny)",
                           s["username"], ip)
            return self._json({"ok": True})

        def _users(self, p: str, s: dict, b: dict) -> None:
            me, ip = s["username"], self._ip()
            if p == "/api/users/add":
                username = auth.clean_username(str(b.get("username", "")))
                password = str(b.get("password", ""))
                role = b.get("role", "user")
                if role not in ("user", "admin"):
                    return self._err(400, "Neplatná role.")
                problem = auth.username_problem(username) or auth.password_problem(username, password)
                if problem:
                    return self._err(400, problem)
                if username.lower() == "admin" or app.find_user(username):
                    return self._err(400, "Uživatel již existuje (velikost písmen se nerozlišuje).")
                now = int(time.time())
                app.db.execute("INSERT INTO users(username, pw_hash, role, created, pw_changed) VALUES(?,?,?,?,?)",
                               (username, auth.hash_password(password), role, now, now))
                app.core.audit("USER_ADDED", f"Přidán uživatel {username!r} ({role})", me, ip)
                return self._json({"ok": True})

            row = app.find_user(str(b.get("username", "")))
            if not row:
                return self._err(404, "Uživatel nenalezen")
            if auth.name_key(row["username"]) == auth.name_key(me):
                return self._err(400, "Tuto akci nelze provést na vlastním účtu.")
            if p == "/api/users/role":
                role = b.get("role")
                if role not in ("user", "admin"):
                    return self._err(400, "Neplatná role.")
                if role != "admin" and app.last_admin(row):
                    return self._err(400, "Musí zůstat alespoň jeden administrátor.")
                app.db.execute("UPDATE users SET role=? WHERE username=?", (role, row["username"]))
                app.core.audit("USER_ROLE_CHANGED", f"{row['username']!r}: {row['role']} → {role}", me, ip)
                return self._json({"ok": True})
            if p == "/api/users/delete":
                if app.last_admin(row):
                    return self._err(400, "Musí zůstat alespoň jeden administrátor.")
                app.db.execute("DELETE FROM users WHERE username=?", (row["username"],))
                app.sessions.delete_user_sessions(row["username"])
                app.core.audit("USER_DELETED", f"Smazán uživatel {row['username']!r}", me, ip)
                return self._json({"ok": True})
            if p == "/api/users/password":
                password = str(b.get("password", ""))
                problem = auth.password_problem(row["username"], password)
                if problem:
                    return self._err(400, problem)
                app.db.execute("UPDATE users SET pw_hash=?, pw_changed=? WHERE username=?",
                               (auth.hash_password(password), int(time.time()), row["username"]))
                app.sessions.delete_user_sessions(row["username"])
                app.core.audit("PASSWORD_CHANGED",
                               f"Administrátor nastavil nové heslo uživateli {row['username']!r}", me, ip)
                return self._json({"ok": True})
            return self._err(404, "Nenalezeno")

        # --------------------------------------------------- admin actions
        def _admin(self, p: str, s: dict, b: dict) -> None:
            core, me, ip = app.core, s["username"], self._ip()
            if p in ("/api/update/check", "/api/update/install"):
                # the root updater (system/bin/vitr-update.py) is driven by files in the data
                # directory (updates.py); a check never installs, install is a separate request
                dd = str(app.data_dir)
                action, ver = "check", None
                if p == "/api/update/install":
                    action, ver = "install", b.get("version")
                    if not updates.valid_version(ver):
                        return self._err(400, "Neplatná verze.")
                    st0 = updates.read_status(dd) or {}
                    if st0.get("state") not in ("available", "error", "rolled_back") or st0.get("latest") != ver:
                        return self._err(409, "Tato verze není k dispozici – nejprve zkontrolujte aktualizace.")
                st = updates.read_status(dd)
                if updates.busy(st) and (action == "install" or st.get("state") != "checking"):
                    return self._err(409, "Aktualizace právě probíhá.")
                try:
                    updates.request(dd, action, ver)
                except OSError:
                    return self._err(500, "Žádost se nepodařilo uložit.")
                core.audit("UPDATE_REQUESTED", f"Instalace verze {ver}" if ver else "Ruční kontrola aktualizací", me, ip)
                return self._json({"ok": True, **updates.summary(dd, __version__)})
            if p == "/api/update/prefs":
                try:
                    pr = updates.write_prefs(str(app.data_dir), b.get("auto"), b.get("channel"))
                except OSError:
                    return self._err(500, "Nastavení se nepodařilo uložit.")
                core.audit("SETTINGS_CHANGED", "Aktualizace: " + ("automaticky" if pr["auto"] else "jen ručně")
                           + (", kanál beta" if pr["channel"] == "beta" else ""), me, ip)
                return self._json({"ok": True, "prefs": pr})
            if p == "/api/pairing/start":
                nid = parse_node_id(b.get("node_id"))
                if core.node(nid) is None and len(core.nodes()) >= MAX_NODES:
                    return self._err(400, f"Maximálně {MAX_NODES} anemometrů.")
                name = str(b.get("name") or "").strip()[:40]
                if core.node(nid) is None and not name:
                    name = core.archive_info(nid)["archived_name"]     # re-paired removed sensor
                    if not name:
                        return self._err(400, "Zadejte název čidla.")
                core.start_pairing(nid, name, me)
                core.audit("NODE_PAIRING", f"Čeká se na párování čidla {name} ({nid:04X})" if name
                           else f"Čeká se na párování čidla {core.label(nid)}", me, ip)
                return self._json({"ok": True, "pairing": core.pairing_status()})
            if p == "/api/pairing/cancel":
                nid = parse_node_id(b.get("node_id"))
                waiting = nid in core.pair_targets
                core.cancel_pairing(nid)
                if waiting:
                    core.audit("NODE_PAIRING_CANCELLED", f"Zrušeno čekání na párování čidla {core.label(nid)}", me, ip)
                return self._json({"ok": True, "pairing": core.pairing_status()})
            if p == "/api/nodes/remove":
                # default two-step (the sensor confirms after a battery re-insertion);
                # force: a lost or broken sensor, its key is archived for a later re-pairing
                nid = parse_node_id(b.get("node_id"))
                node = core.node(nid)
                if not node:
                    return self._err(404, "Čidlo neexistuje.")
                if b.get("force") or not node["k_mac"]:
                    core.forget_node(nid, "NODE_REMOVED", f"Odebráno čidlo {core.label(nid)} bez potvrzení čidlem", me, ip)
                    return self._json({"ok": True, "removed": True})
                core.request_unpair(nid, me, ip)
                return self._json({"ok": True, "removed": False})
            if p == "/api/nodes/unpair/cancel":
                nid = parse_node_id(b.get("node_id"))
                node = core.node(nid)
                if not node or not node["unpair"]:
                    return self._err(404, "Čidlo nečeká na odpárování.")
                core.cancel_unpair(nid, me, ip)
                return self._json({"ok": True})
            if p == "/api/node/update":
                nid = int(b["id"])
                old = core.node(nid)
                rev = b.get("rev_value")
                rev_ms = None
                if rev not in (None, ""):
                    rev_ms = float(rev) / 3.6 if b.get("rev_unit") == "kmh" else float(rev)
                ppr = b.get("ppr")
                core.update_node(nid, name=b.get("name"), rev_ms=rev_ms,
                                 ppr=None if ppr in (None, "") else int(ppr),
                                 battery_type=b.get("battery_type") or None)
                new = core.node(nid)
                if old and new:
                    changes = []
                    if old["name"] != new["name"]:
                        changes.append(f"název {old['name']!r} → {new['name']!r}")
                    if old.get("battery_type") != new.get("battery_type"):
                        kinds = {"lifes2": "lithiové", "alkaline": "alkalické"}
                        changes.append(f"baterie {kinds.get(old.get('battery_type'), '?')} → "
                                       f"{kinds.get(new.get('battery_type'), '?')}")
                    if abs(old["rev_ms"] - new["rev_ms"]) > 1e-9 or old["ppr"] != new["ppr"]:
                        changes.append(f"1 ot/s = {old['rev_ms']:g} → {new['rev_ms']:g} m/s, "
                                       f"pulzů/ot {old['ppr']} → {new['ppr']}")
                    if changes:
                        core.audit("CALIBRATION", f"{core.label(nid)}: " + "; ".join(changes), me, ip)
                app.mqtt.notify(nid)
                return self._json({"ok": True})
            if p == "/api/node/battery":
                nid = int(b["id"])
                if core.node(nid) is None:
                    return self._err(404, "Neznámé čidlo.")
                try:
                    actual = float(str(b.get("actual_v")).replace(",", "."))
                except ValueError:
                    return self._err(400, "Zadejte změřené napětí.")
                try:
                    trim = core.battery_calibrate(nid, actual)
                except (LookupError, ValueError) as e:
                    return self._err(400, str(e.args[0]))
                core.audit("CALIBRATION", f"{core.label(nid)}: korekce baterie {trim:.4f}", me, ip)
                app.mqtt.notify(nid)
                return self._json({"ok": True, "battery_trim": trim})
            if p == "/api/mqtt":
                cfg = app.mqtt_cfg(with_password=True)
                cfg.pop("uid", None)
                cfg["enabled"] = bool(b.get("enabled"))
                cfg["host"] = str(b.get("host", "")).strip()[:200]
                port = int(b.get("port") or 1883)
                if not 1 <= port <= 65535:
                    raise ValueError("Port musí být 1–65535")
                cfg["port"] = port
                cfg["user"] = str(b.get("user", ""))[:200]
                if b.get("password") is not None and b.get("password") != "":
                    cfg["password"] = str(b["password"])[:200]
                if b.get("clear_password"):
                    cfg["password"] = ""
                app.db.set("mqtt", json.dumps(cfg))
                app.mqtt.reconfigure()
                core.audit("SETTINGS_CHANGED", "Home Assistant (MQTT): " +
                           (f"zapnuto, {cfg['host']}:{cfg['port']}" if cfg["enabled"] else "vypnuto"), me, ip)
                return self._json({"ok": True})
            return self._err(404, "Nenalezeno")

    return H


def start_web(app: App, bind: str, port: int) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((bind, port), make_handler(app))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="web", daemon=True).start()
    log.info("Web on http://%s:%d", bind, port)
    return srv
