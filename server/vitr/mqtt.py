"""Home Assistant over MQTT (MQTT Discovery) – one device per anemometer.

Minimal MQTT 3.1.1 client on the event loop (QoS 0, standard library only), same
approach as the tank hub. Topics (<base> = vitr/<uid>, <node> = sensor ID in hex):
  <base>/status                   online / offline (last will)            retained
  <base>/<node>/state             JSON state                              retained
  homeassistant/sensor/vitr_<uid>_<node>/<object>/config                  retained
  homeassistant/status            HA (re)started -> publish everything again
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from typing import Optional

from . import __version__

log = logging.getLogger("vitr.mqtt")

DISCOVERY = "homeassistant"
KEEPALIVE_S = 60
PING_S = 30
REFRESH_S = 60

SENSORS = [
    # object, name, extra config
    ("rychlost", "Rychlost větru", {"value_template": "{{ value_json.speed_kmh }}", "unit_of_measurement": "km/h",
                                    "device_class": "wind_speed", "state_class": "measurement"}),
    ("naraz", "Náraz větru", {"value_template": "{{ value_json.gust_kmh }}", "unit_of_measurement": "km/h",
                              "device_class": "wind_speed", "state_class": "measurement"}),
    ("prumer10", "Průměr 10 min", {"value_template": "{{ value_json.avg10_kmh }}", "unit_of_measurement": "km/h",
                                   "device_class": "wind_speed", "state_class": "measurement"}),
    ("naraz_dnes", "Nejsilnější náraz dnes", {"value_template": "{{ value_json.gust_today_kmh }}",
                                              "unit_of_measurement": "km/h", "device_class": "wind_speed"}),
    ("baterie", "Baterie", {"value_template": "{{ value_json.battery_pct }}", "unit_of_measurement": "%",
                            "device_class": "battery", "state_class": "measurement"}),
    ("napeti", "Napětí baterie", {"value_template": "{{ value_json.battery_v }}", "unit_of_measurement": "V",
                                  "device_class": "voltage", "state_class": "measurement",
                                  "entity_category": "diagnostic"}),
    ("teplota", "Teplota čidla", {"value_template": "{{ value_json.temperature }}", "unit_of_measurement": "°C",
                                  "device_class": "temperature", "state_class": "measurement",
                                  "entity_category": "diagnostic"}),
    ("signal", "Signál", {"value_template": "{{ value_json.rssi }}", "unit_of_measurement": "dBm",
                          "device_class": "signal_strength", "state_class": "measurement",
                          "entity_category": "diagnostic"}),
    ("naposledy", "Poslední měření", {"value_template": "{{ value_json.last_seen_iso }}",
                                      "device_class": "timestamp", "entity_category": "diagnostic"}),
]


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b, n = n % 128, n // 128
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _str(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack(">H", len(b)) + b


def _packet(first: int, body: bytes) -> bytes:
    return bytes([first]) + _varint(len(body)) + body


def connect_packet(client_id: str, username: str, password: str, will_topic: str, will_msg: bytes) -> bytes:
    flags = 0x02 | 0x04 | 0x20
    payload = _str(client_id) + _str(will_topic) + struct.pack(">H", len(will_msg)) + will_msg
    if username:
        flags |= 0x80
        payload += _str(username)
        if password:
            flags |= 0x40
            payload += _str(password)
    return _packet(0x10, _str("MQTT") + bytes([4, flags]) + struct.pack(">H", KEEPALIVE_S) + payload)


def publish_packet(topic: str, payload: bytes, retain: bool = False) -> bytes:
    return _packet(0x30 | (1 if retain else 0), _str(topic) + payload)


def subscribe_packet(pid: int, topics: list[str]) -> bytes:
    return _packet(0x82, struct.pack(">H", pid) + b"".join(_str(t) + b"\x00" for t in topics))


PINGREQ = b"\xc0\x00"


async def read_packet(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    first = (await reader.readexactly(1))[0]
    n, mult = 0, 1
    for _ in range(4):
        b = (await reader.readexactly(1))[0]
        n += (b & 0x7F) * mult
        if not b & 0x80:
            break
        mult *= 128
    return first, (await reader.readexactly(n) if n else b"")


def discovery(uid: str, st: dict) -> list[tuple[str, dict]]:
    """(topic, config) of every entity of one anemometer."""
    node = st["hex"]
    base = f"vitr/{uid}"
    dev = {"identifiers": [f"vitr_{uid}_{node}"], "name": st["name"] or f"Anemometr {node}",
           "manufacturer": "vlastní výroba", "model": "Anemometr ATtiny1624 + CC1101",
           "sw_version": f"vitr {__version__}" + (f", fw {st['fw'] // 10}.{st['fw'] % 10}" if st.get("fw") else "")}
    out = []
    for obj, name, extra in SENSORS:
        cfg = {"name": name, "unique_id": f"vitr_{uid}_{node}_{obj}", "object_id": f"vitr_{node.lower()}_{obj}",
               "state_topic": f"{base}/{node}/state", "availability_topic": f"{base}/status",
               "device": dev, **extra}
        out.append((f"{DISCOVERY}/sensor/vitr_{uid}_{node}/{obj}/config", cfg))
    return out


def state_payload(st: dict) -> bytes:
    d = dict(st)
    d["last_seen_iso"] = (time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(st["last_seen"]))
                          if st.get("last_seen") else None)
    return json.dumps(d, ensure_ascii=False).encode("utf-8")


class Mqtt:
    """Keeps a connection to the broker; publishes discovery and states."""

    def __init__(self, core, settings: callable):
        self.core = core
        self.settings = settings              # -> dict(enabled, host, port, user, password)
        self.uid = settings().get("uid") or "0000"
        self.status = "vypnuto"
        self._writer: Optional[asyncio.StreamWriter] = None
        self._dirty: set[int] = set()
        self._wake = asyncio.Event()
        self._restart = asyncio.Event()
        self._published: set[str] = set()     # discovery topics sent in this connection
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def notify(self, nid: int) -> None:
        """Called from the core (any thread) when a node has new data."""
        self._dirty.add(nid)
        if self._loop:
            self._loop.call_soon_threadsafe(self._wake.set)

    def reconfigure(self) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self._restart.set)

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        backoff = 2.0
        while True:
            cfg = self.settings()
            self._restart.clear()
            if not cfg.get("enabled") or not cfg.get("host"):
                self.status = "vypnuto"
                await self._restart.wait()
                continue
            try:
                await self._session(cfg)
                backoff = 2.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.status = f"chyba: {e.__class__.__name__}: {e}"[:200]
                log.warning("MQTT: %s", e)
            finally:
                if self._writer:
                    self._writer.close()
                    self._writer = None
            try:
                await asyncio.wait_for(self._restart.wait(), backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 120.0)

    async def _session(self, cfg: dict) -> None:
        base = f"vitr/{self.uid}"
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(cfg["host"], int(cfg.get("port") or 1883)), 10)
        self._writer = writer
        writer.write(connect_packet(f"vitr-{self.uid}", cfg.get("user") or "", cfg.get("password") or "",
                                    f"{base}/status", b"offline"))
        await writer.drain()
        first, body = await asyncio.wait_for(read_packet(reader), 10)
        if first >> 4 != 2 or len(body) < 2 or body[1] != 0:
            raise ConnectionError(f"broker odmítl spojení (kód {body[1] if len(body) > 1 else '?'})")
        self.status = "připojeno"
        log.info("MQTT connected to %s", cfg["host"])
        writer.write(publish_packet(f"{base}/status", b"online", retain=True))
        writer.write(subscribe_packet(1, [f"{DISCOVERY}/status"]))
        self._published.clear()
        self._publish_all()
        await writer.drain()

        reader_task = asyncio.ensure_future(self._read_loop(reader))
        try:
            next_ping = time.monotonic() + PING_S
            next_refresh = time.monotonic() + REFRESH_S
            while not reader_task.done() and not self._restart.is_set():
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), 1.0)
                except asyncio.TimeoutError:
                    pass
                now = time.monotonic()
                if self._dirty:
                    dirty, self._dirty = self._dirty, set()
                    for nid in dirty:
                        self._publish_node(nid)
                if now >= next_refresh:
                    self._publish_all()
                    next_refresh = now + REFRESH_S
                if now >= next_ping:
                    writer.write(PINGREQ)
                    next_ping = now + PING_S
                await writer.drain()
            if reader_task.done() and reader_task.exception():
                raise reader_task.exception()
        finally:
            reader_task.cancel()

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        while True:
            first, body = await read_packet(reader)
            if first >> 4 == 3:                              # PUBLISH: homeassistant/status
                tl = struct.unpack(">H", body[:2])[0]
                if body[2 + tl:].strip() == b"online":
                    self._published.clear()
                    self._publish_all()

    def _publish_all(self) -> None:
        for n in self.core.nodes():
            self._publish_node(n["id"])

    def _publish_node(self, nid: int) -> None:
        if not self._writer:
            return
        st = self.core.state(nid)
        if st is None:
            return
        for topic, cfg in discovery(self.uid, st):
            if topic not in self._published:
                self._writer.write(publish_packet(topic, json.dumps(cfg, ensure_ascii=False).encode(), retain=True))
                self._published.add(topic)
        self._writer.write(publish_packet(f"vitr/{self.uid}/{st['hex']}/state", state_payload(st), retain=True))

    def remove_node(self, hexid: str) -> None:
        """Delete the entities of a removed anemometer in Home Assistant (any thread)."""
        if self._loop:
            self._loop.call_soon_threadsafe(self._remove, hexid)

    def _remove(self, hexid: str) -> None:
        if not self._writer:
            return
        for obj, _, _ in SENSORS:
            topic = f"{DISCOVERY}/sensor/vitr_{self.uid}_{hexid}/{obj}/config"
            self._writer.write(publish_packet(topic, b"", retain=True))
            self._published.discard(topic)
