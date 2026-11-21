"""Web UI and JSON API (standard library HTTP server in its own thread).

Reading (dashboard, charts) needs no login – it is the home network. Changes
(pairing, settings, MQTT, password) need the admin password: a session cookie
(HttpOnly, SameSite=Strict) after POST /api/login.
"""

from __future__ import annotations

import hashlib
import hmac
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

SESSION_S = 30 * 86400
COOKIE = "vitr_s"
MAX_BODY = 16 * 1024


def hash_password(pw: str, salt: Optional[bytes] = None) -> str:
    salt = salt or os.urandom(16)
    h = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return salt.hex() + "$" + h.hex()


def check_password(pw: str, stored: str) -> bool:
    try:
        salt, _ = stored.split("$", 1)
        return hmac.compare_digest(hash_password(pw, bytes.fromhex(salt)), stored)
    except (ValueError, AttributeError):
        return False


class App:
    def __init__(self, core, db, mqtt, static_dir: str, radio_ok: callable):
        self.core, self.db, self.mqtt = core, db, mqtt
        self.static = Path(static_dir).resolve()
        self.radio_ok = radio_ok
        self.sessions: dict[str, float] = {}
        self.lock = threading.Lock()

    # ------------------------------------------------------------ sessions
    def new_session(self) -> str:
        tok = secrets.token_urlsafe(32)
        with self.lock:
            now = time.time()
            self.sessions = {k: v for k, v in self.sessions.items() if v > now}
            self.sessions[tok] = now + SESSION_S
        return tok

    def valid(self, tok: Optional[str]) -> bool:
        if not tok:
            return False
        with self.lock:
            exp = self.sessions.get(tok)
            return bool(exp and exp > time.time())

    def drop(self, tok: Optional[str]) -> None:
        with self.lock:
            self.sessions.pop(tok or "", None)

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
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):     # quiet: systemd journal only for errors
            pass

        # ------------------------------------------------------- helpers
        def _cookie(self) -> Optional[str]:
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == COOKIE:
                    return v
            return None

        def _send(self, code: int, body: bytes, ctype: str, headers: Optional[dict] = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj, code: int = 200, headers: Optional[dict] = None) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", headers)

        def _err(self, code: int, msg: str) -> None:
            self._json({"error": msg}, code)

        def _body(self) -> Optional[dict]:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return None
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return None                     # also blocks cross-site form posts
            try:
                obj = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return None
            return obj if isinstance(obj, dict) else None

        # ---------------------------------------------------------- GET
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            u = urlparse(self.path)
            p = u.path
            try:
                if p == "/api/state":
                    return self._json(app.overview())
                if p == "/api/me":
                    return self._json({"admin": app.valid(self._cookie())})
                if p == "/api/history":
                    q = parse_qs(u.query)
                    nid = int(q["id"][0])
                    to = int(q.get("to", [time.time()])[0])
                    frm = int(q.get("from", [to - 86400])[0])
                    return self._json(app.core.history(nid, frm, to))
                if p == "/api/mqtt":
                    if not app.valid(self._cookie()):
                        return self._err(401, "Přihlaste se")
                    return self._json(app.mqtt_cfg())
                return self._static(p)
            except (KeyError, ValueError):
                return self._err(400, "Špatný požadavek")
            except Exception:
                log.exception("GET %s", p)
                return self._err(500, "Chyba serveru")

        def _static(self, p: str) -> None:
            if p in ("", "/"):
                p = "/index.html"
            f = (app.static / p.lstrip("/")).resolve()
            if app.static not in f.parents or not f.is_file():
                return self._err(404, "Nenalezeno")
            ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self._send(200, f.read_bytes(), ctype)

        # --------------------------------------------------------- POST
        def do_POST(self):
            p = urlparse(self.path).path
            body = self._body()
            if body is None:
                return self._err(400, "Špatný požadavek")
            try:
                if p == "/api/login":
                    stored = app.db.get("password")
                    if stored and check_password(str(body.get("password", "")), stored):
                        tok = app.new_session()
                        return self._json({"ok": True}, headers={
                            "Set-Cookie": f"{COOKIE}={tok}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_S}"})
                    time.sleep(1.0)              # slow down guessing
                    return self._err(403, "Špatné heslo")
                if p == "/api/logout":
                    app.drop(self._cookie())
                    return self._json({"ok": True}, headers={
                        "Set-Cookie": f"{COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"})
                if not app.valid(self._cookie()):
                    return self._err(401, "Přihlaste se")
                return self._admin(p, body)
            except ValueError as e:
                return self._err(400, str(e))
            except (KeyError, TypeError) as e:
                return self._err(400, f"Špatný požadavek ({e})")
            except Exception:
                log.exception("POST %s", p)
                return self._err(500, "Chyba serveru")

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
                cfg["port"] = int(b.get("port") or 1883)
                cfg["user"] = str(b.get("user", ""))[:200]
                if b.get("password") is not None and b.get("password") != "":
                    cfg["password"] = str(b["password"])[:200]
                if b.get("clear_password"):
                    cfg["password"] = ""
                app.db.set("mqtt", json.dumps(cfg))
                app.mqtt.reconfigure()
                return self._json({"ok": True})
            if p == "/api/password":
                if not check_password(str(b.get("old", "")), app.db.get("password", "")):
                    return self._err(403, "Současné heslo nesouhlasí")
                new = str(b.get("new", ""))
                if len(new) < 8:
                    return self._err(400, "Nové heslo musí mít aspoň 8 znaků")
                app.db.set("password", hash_password(new))
                return self._json({"ok": True})
            return self._err(404, "Nenalezeno")

    return H


def start_web(app: App, bind: str, port: int) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((bind, port), make_handler(app))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="web", daemon=True).start()
    log.info("Web on http://%s:%d", bind, port)
    return srv
