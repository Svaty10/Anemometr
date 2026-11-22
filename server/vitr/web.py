"""Web UI and JSON API (standard library HTTP server in its own thread).

Everything needs the admin password (the web may be published through a
Cloudflare tunnel): without a session only the login page, its script, the
stylesheet and the icon are served; "/" redirects to /login and every API call
except /api/login answers 401.

Security, modelled on the tank hub:
  * scrypt password hash, constant-time compare, passwords capped at 256 chars;
  * per-IP back-off and lock-out of failed logins (the real client address is
    taken from CF-Connecting-IP / X-Forwarded-For only when the request comes
    from a local proxy such as cloudflared on loopback);
  * session cookie HttpOnly, SameSite=Strict, Secure behind an HTTPS tunnel;
    idle and absolute expiry; a new session on login (no fixation); all
    sessions dropped on password change;
  * CSRF: JSON body required and Origin must match the host;
  * strict CSP (no inline scripts), X-Frame-Options, HSTS over HTTPS,
    no-store for the API; socket timeout against slow clients.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import mimetypes
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from . import __version__

log = logging.getLogger("vitr.web")

SESSION_IDLE_S = 7 * 86400
SESSION_ABSOLUTE_S = 30 * 86400
MAX_SESSIONS = 50
COOKIE = "vitr_s"
MAX_BODY = 16 * 1024
MAX_PASSWORD = 256
MIN_PASSWORD = 10
SOCKET_TIMEOUT_S = 20

FREE_FAILS = 3                 # then exponential back-off per IP: 2, 4, 8 … s
MAX_DELAY_S = 3600
FAIL_FORGET_S = 3600

PUBLIC_FILES = {"/login.html", "/js/login.js", "/js/theme.js", "/css/style.css", "/css/vitr.css",
                "/favicon.svg"}

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
       "form-action 'self'; frame-ancestors 'none'")

_DUMMY_HASH = None


def hash_password(pw: str, salt: Optional[bytes] = None) -> str:
    salt = salt or os.urandom(16)
    h = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return salt.hex() + "$" + h.hex()


def check_password(pw: str, stored: Optional[str]) -> bool:
    """Constant-time check; with no stored hash it still costs one scrypt."""
    global _DUMMY_HASH
    pw = str(pw)[:MAX_PASSWORD]
    if not stored:
        _DUMMY_HASH = _DUMMY_HASH or hash_password(secrets.token_hex(8))
        hash_password(pw, bytes.fromhex(_DUMMY_HASH.split("$", 1)[0]))
        return False
    try:
        salt, _ = stored.split("$", 1)
        return hmac.compare_digest(hash_password(pw, bytes.fromhex(salt)), stored)
    except (ValueError, AttributeError):
        return False


def _is_loopback(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


class LoginLimiter:
    """Failed logins per client IP: a few free tries, then exponential back-off."""

    def __init__(self, clock=time.time):
        self.clock = clock
        self._d: dict[str, tuple[int, float, float]] = {}      # ip -> (fails, until, last)
        self._lock = threading.Lock()

    def retry_after(self, ip: str) -> int:
        with self._lock:
            _, until, _ = self._d.get(ip, (0, 0.0, 0.0))
        return max(0, int(until - self.clock() + 0.999))

    def fail(self, ip: str) -> None:
        now = self.clock()
        with self._lock:
            if len(self._d) > 10000:
                self._d = {k: v for k, v in self._d.items() if now - v[2] < FAIL_FORGET_S}
            fails, _, last = self._d.get(ip, (0, 0.0, 0.0))
            if now - last > FAIL_FORGET_S:
                fails = 0
            fails += 1
            delay = 0 if fails <= FREE_FAILS else min(MAX_DELAY_S, 2 ** (fails - FREE_FAILS))
            self._d[ip] = (fails, now + delay, now)

    def success(self, ip: str) -> None:
        with self._lock:
            self._d.pop(ip, None)


class App:
    def __init__(self, core, db, mqtt, static_dir: str, radio_ok: callable, clock=time.time):
        self.core, self.db, self.mqtt = core, db, mqtt
        self.static = Path(static_dir).resolve()
        self.radio_ok = radio_ok
        self.clock = clock
        self.sessions: dict[str, tuple[float, float]] = {}    # token -> (created, last seen)
        self.lock = threading.Lock()
        self.limiter = LoginLimiter(clock)

    # ------------------------------------------------------------ sessions
    def new_session(self, old: Optional[str] = None) -> str:
        tok = secrets.token_urlsafe(32)
        now = self.clock()
        with self.lock:
            self.sessions.pop(old or "", None)
            self.sessions = {k: v for k, v in self.sessions.items() if self._alive(v, now)}
            while len(self.sessions) >= MAX_SESSIONS:             # drop the oldest
                self.sessions.pop(min(self.sessions, key=lambda k: self.sessions[k][1]))
            self.sessions[tok] = (now, now)
        return tok

    @staticmethod
    def _alive(v: tuple[float, float], now: float) -> bool:
        return now - v[0] < SESSION_ABSOLUTE_S and now - v[1] < SESSION_IDLE_S

    def valid(self, tok: Optional[str]) -> bool:
        if not tok:
            return False
        now = self.clock()
        with self.lock:
            v = self.sessions.get(tok)
            if not v or not self._alive(v, now):
                self.sessions.pop(tok, None)
                return False
            self.sessions[tok] = (v[0], now)
            return True

    def drop(self, tok: Optional[str]) -> None:
        with self.lock:
            self.sessions.pop(tok or "", None)

    def drop_all_except(self, tok: Optional[str]) -> None:
        with self.lock:
            self.sessions = {k: v for k, v in self.sessions.items() if k == tok}

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
            "pairing_left": self.core.pairing_left(),
            "heard": self.core.heard_unknown(),
            "radio": {**self.core.stats, "bridge_ok": self.radio_ok()},
            "mqtt": self.mqtt.status,
            "now": int(time.time()),
        }


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

        def _cookie(self) -> Optional[str]:
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == COOKIE:
                    return v
            return None

        def _authed(self) -> bool:
            return app.valid(self._cookie())

        def _set_cookie(self, tok: str, max_age: int) -> str:
            secure = "; Secure" if self._https() else ""
            return f"{COOKIE}={tok}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{secure}"

        def _send(self, code: int, body: bytes, ctype: str, headers: Optional[dict] = None,
                  cache: str = "no-store") -> None:
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
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj, code: int = 200, headers: Optional[dict] = None) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", headers)

        def _err(self, code: int, msg: str, headers: Optional[dict] = None) -> None:
            self._json({"error": msg}, code, headers)

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
                return True                     # same-origin fetch of old browsers / curl
            host = origin.split("://", 1)[-1].rstrip("/").lower()
            allowed = {(self.headers.get("Host") or "").lower()}
            if self._from_proxy() and self.headers.get("X-Forwarded-Host"):
                allowed.add(self.headers["X-Forwarded-Host"].split(",")[0].strip().lower())
            return host in allowed

        # ---------------------------------------------------------- GET
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            u = urlparse(self.path)
            p = u.path
            try:
                if p.startswith("/api/"):
                    if not self._authed():
                        return self._err(401, "Přihlaste se")
                    if p == "/api/state":
                        return self._json(app.overview())
                    if p == "/api/me":
                        return self._json({"admin": True})
                    if p == "/api/history":
                        q = parse_qs(u.query)
                        nid = int(q["id"][0])
                        to = int(q.get("to", [time.time()])[0])
                        frm = int(q.get("from", [to - 86400])[0])
                        return self._json(app.core.history(nid, frm, to))
                    if p == "/api/mqtt":
                        return self._json(app.mqtt_cfg())
                    return self._err(404, "Nenalezeno")
                if p in ("/login", "/login.html"):
                    if self._authed():
                        return self._redirect("/")
                    return self._static("/login.html")
                if p in ("", "/", "/index.html"):
                    if not self._authed():
                        return self._redirect("/login")
                    return self._static("/index.html")
                if p in PUBLIC_FILES or self._authed():
                    return self._static(p)
                return self._err(404, "Nenalezeno")
            except (KeyError, ValueError, IndexError):
                return self._err(400, "Špatný požadavek")
            except Exception:
                log.exception("GET %s", p)
                return self._err(500, "Chyba serveru")

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
            p = urlparse(self.path).path
            body = self._body()
            if body is None:
                return self._err(400, "Špatný požadavek")
            if not self._same_origin():
                return self._err(403, "Neplatný původ požadavku")
            try:
                if p == "/api/login":
                    return self._login(body)
                if p == "/api/logout":
                    app.drop(self._cookie())
                    return self._json({"ok": True}, headers={"Set-Cookie": self._set_cookie("", 0)})
                if not self._authed():
                    return self._err(401, "Přihlaste se")
                return self._admin(p, body)
            except ValueError as e:
                return self._err(400, str(e))
            except (KeyError, TypeError):
                return self._err(400, "Špatný požadavek")
            except Exception:
                log.exception("POST %s", p)
                return self._err(500, "Chyba serveru")

        def _login(self, body: dict) -> None:
            ip = self._ip()
            wait = app.limiter.retry_after(ip)
            if wait:
                return self._err(429, f"Příliš mnoho pokusů. Zkuste to za {wait} s.",
                                 {"Retry-After": str(wait)})
            if check_password(str(body.get("password", "")), app.db.get("password")):
                app.limiter.success(ip)
                tok = app.new_session(self._cookie())
                log.info("login ok from %s", ip)
                return self._json({"ok": True},
                                  headers={"Set-Cookie": self._set_cookie(tok, SESSION_ABSOLUTE_S)})
            app.limiter.fail(ip)
            log.warning("login failed from %s", ip)
            time.sleep(0.5 + secrets.randbelow(500) / 1000)
            return self._err(401, "Špatné heslo")

        def _admin(self, p: str, b: dict) -> None:
            core = app.core
            if p == "/api/pair/start":
                core.start_pairing()
                return self._json({"ok": True, "left": core.pairing_left()})
            if p == "/api/pair/stop":
                core.stop_pairing()
                return self._json({"ok": True})
            if p == "/api/node/update":
                nid = int(b["id"])
                rev = b.get("rev_value")
                rev_ms = None
                if rev not in (None, ""):
                    rev_ms = float(rev) / 3.6 if b.get("rev_unit") == "kmh" else float(rev)
                ppr = b.get("ppr")
                core.update_node(nid, name=b.get("name"), rev_ms=rev_ms,
                                 ppr=None if ppr in (None, "") else int(ppr))
                app.mqtt.notify(nid)
                return self._json({"ok": True})
            if p == "/api/node/unpair":
                core.request_unpair(int(b["id"]), bool(b.get("on", True)))
                return self._json({"ok": True})
            if p == "/api/node/forget":
                nid = int(b["id"])
                core.forget(nid)
                if b.get("history"):
                    core.delete_history(nid)
                app.mqtt.remove_node(f"{nid:04X}")
                return self._json({"ok": True})
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
                return self._json({"ok": True})
            if p == "/api/password":
                ip = self._ip()
                wait = app.limiter.retry_after(ip)
                if wait:
                    return self._err(429, f"Příliš mnoho pokusů. Zkuste to za {wait} s.",
                                     {"Retry-After": str(wait)})
                if not check_password(str(b.get("old", "")), app.db.get("password")):
                    app.limiter.fail(ip)
                    return self._err(403, "Současné heslo nesouhlasí")
                new = str(b.get("new", ""))
                if len(new) < MIN_PASSWORD:
                    return self._err(400, f"Nové heslo musí mít aspoň {MIN_PASSWORD} znaků")
                if len(new) > MAX_PASSWORD:
                    return self._err(400, f"Nové heslo může mít nejvýš {MAX_PASSWORD} znaků")
                app.db.set("password", hash_password(new))
                app.drop_all_except(self._cookie())        # other devices must log in again
                pw_file = Path(app.db.path).parent / "heslo.txt"
                if pw_file.exists():
                    try:
                        pw_file.unlink()                   # the initial password is no longer valid
                    except OSError:
                        pass
                log.info("password changed from %s", ip)
                return self._json({"ok": True})
            return self._err(404, "Nenalezeno")

    return H


def start_web(app: App, bind: str, port: int) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((bind, port), make_handler(app))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="web", daemon=True).start()
    log.info("Web on http://%s:%d", bind, port)
    return srv
