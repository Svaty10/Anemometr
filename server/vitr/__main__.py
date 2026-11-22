"""Entry point: python3 -m vitr

Environment (systemd EnvironmentFile=/etc/default/vitr):
  VITR_DATA       data directory                                  (/var/lib/vitr)
  VITR_STATIC     static files directory                          (<package>/../static)
  VITR_WEB_PORT   web UI port                                     (8080)
  VITR_BIND       bind address of the web                         (0.0.0.0)
  VITR_UDP_PORT   port the tank hub's radio bridge sends to       (47613, 127.0.0.1 only)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import signal
import time
from pathlib import Path

from . import __version__, updates
from .core import Core
from .db import Database
from .mqtt import Mqtt
from .web import App, ensure_setup_code, start_web

log = logging.getLogger("vitr")


def env(name: str, default: str) -> str:
    return os.environ.get(name, default)


class BridgeProtocol(asyncio.DatagramProtocol):
    def __init__(self, core: Core):
        self.core = core
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if addr[0] != "127.0.0.1":
            return
        try:
            reply = self.core.on_datagram(data)
        except Exception:
            log.exception("frame failed")
            return
        if reply is not None:
            self.transport.sendto(reply, addr)


def first_start(db: Database, data_dir: Path) -> None:
    if not db.get("uid"):
        db.set("uid", secrets.token_hex(3))
    ensure_setup_code(data_dir)                       # first login: admin + this code
    old = data_dir / "heslo.txt"                      # single password of 1.0 (pre-accounts)
    if old.exists():
        old.unlink()


async def main() -> None:
    data_dir = Path(env("VITR_DATA", "/var/lib/vitr"))
    data_dir.mkdir(parents=True, exist_ok=True)
    static_dir = env("VITR_STATIC", str(Path(__file__).resolve().parent.parent / "static"))
    db = Database(str(data_dir / "vitr.db"))
    first_start(db, data_dir)
    core = Core(db)

    def mqtt_settings() -> dict:
        try:
            cfg = json.loads(db.get("mqtt", "{}"))
        except ValueError:
            cfg = {}
        cfg["uid"] = db.get("uid")
        return cfg

    mqtt = Mqtt(core, mqtt_settings)
    core.listeners.append(mqtt.notify)
    core.removed_listeners.append(lambda nid: mqtt.remove_node(f"{nid:04X}"))

    loop = asyncio.get_running_loop()
    udp_port = int(env("VITR_UDP_PORT", "47613"))
    await loop.create_datagram_endpoint(lambda: BridgeProtocol(core), local_addr=("127.0.0.1", udp_port))
    log.info("vitr %s: waiting for frames from the radio bridge on 127.0.0.1:%d", __version__, udp_port)

    def bridge_ok() -> bool:
        return time.time() - core.stats["last_frame"] < 1800

    app = App(core, db, mqtt, static_dir, bridge_ok, str(data_dir))
    web = start_web(app, env("VITR_BIND", "0.0.0.0"), int(env("VITR_WEB_PORT", "8080")))

    def health() -> None:
        try:
            updates.write_health(str(data_dir), __version__, bridge_ok())
        except OSError:
            log.exception("health.json not written")

    async def housekeeping() -> None:
        """health.json for the updater, audit of update results, finished unpairings."""
        while True:
            health()
            try:
                ev = updates.audit_event(updates.read_status(str(data_dir)), db.get("update_audit_key"))
                if ev is not None:
                    key, action, detail = ev
                    db.set("update_audit_key", key)
                    core.audit(action, detail)
                with core.lock:
                    core.unpair_tick()
            except Exception:
                log.exception("housekeeping failed")
            await asyncio.sleep(30)

    health()                                      # the updater waits for this after a restart
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):     # Windows (development)
            pass
    task = asyncio.ensure_future(mqtt.run())
    hk = asyncio.ensure_future(housekeeping())
    await stop.wait()
    task.cancel()
    hk.cancel()
    web.shutdown()
    db.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("VITR_LOGLEVEL", "INFO"),
        format="%(levelname)s %(name)s: %(message)s" if os.environ.get("INVOCATION_ID")
        else "%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
