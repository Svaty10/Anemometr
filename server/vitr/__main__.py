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

from . import __version__
from .core import Core
from .db import Database
from .mqtt import Mqtt
from .web import App, hash_password, start_web

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
    if not db.get("password"):
        pw = "-".join(secrets.token_hex(2) for _ in range(3))
        db.set("password", hash_password(pw))
        f = data_dir / "heslo.txt"
        f.write_text(f"Heslo pro nastavení webu anemometru: {pw}\n", encoding="utf-8")
        os.chmod(f, 0o600)
        log.warning("Initial web password written to %s", f)


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

    loop = asyncio.get_running_loop()
    udp_port = int(env("VITR_UDP_PORT", "47613"))
    await loop.create_datagram_endpoint(lambda: BridgeProtocol(core), local_addr=("127.0.0.1", udp_port))
    log.info("vitr %s: waiting for frames from the radio bridge on 127.0.0.1:%d", __version__, udp_port)

    def bridge_ok() -> bool:
        return time.time() - core.stats["last_frame"] < 1800

    app = App(core, db, mqtt, static_dir, bridge_ok)
    web = start_web(app, env("VITR_BIND", "0.0.0.0"), int(env("VITR_WEB_PORT", "8080")))

    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):     # Windows (development)
            pass
    task = asyncio.ensure_future(mqtt.run())
    await stop.wait()
    task.cancel()
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
