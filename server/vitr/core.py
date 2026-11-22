"""Anemometer hub logic: radio frames from the tank hub's bridge, pairing, data.

Frames arrive over UDP from the tank hub (retencka/bridge.py), which owns the
CC1101. A PAIR_REQ is answered in the same datagram exchange (the node listens only
~0.6 s): PAIR_ACK while the user opened the pairing window, UNPAIR when the user
asked to unpair a sensor. New keys become valid only after the node proves them
with its first authenticated DATA frame (as in the tank hub).

Wind speed is computed from the raw counts on every query, so changing the
anemometer factor recomputes the whole history:
    speed [km/h] = pulses / secs * factor          (factor = km/h per 1 pulse/s
                                                    = rev_ms * 3.6 / pulses per revolution)
    gust  [km/h] = gust_pulses / gust_secs * factor
secs = window length of the node corrected by its RC clock (`clock`, learned from
the arrival times of consecutive packets).
"""

from __future__ import annotations

import logging
import struct
import threading
import time
from typing import Callable, Optional

from . import crypto
from .db import Database

log = logging.getLogger("vitr.core")

MAGIC = b"RB1"
PAIR_WINDOW_S = 600
PENDING_KEY_S = 24 * 3600
ONLINE_S = 5 * 60                 # last packet younger than this: online
STALE_S = 30 * 60                 # ... then "stale", older: offline
GUST_NODE_S = 3                   # firmware: gust over 3 node seconds
CLOCK_MIN, CLOCK_MAX, CLOCK_ALPHA = 0.85, 1.15, 0.05
MAX_NODES = 8

FLAG_BAT_ERR, FLAG_TEMP_ERR, FLAG_STUCK, FLAG_OVERFLOW = 0x01, 0x02, 0x04, 0x08
FLAG_BAT_LOW, FLAG_BOOT, FLAG_WDT, FLAG_PARTIAL = 0x10, 0x20, 0x40, 0x80
TEMP_INVALID = -128
U16_INVALID = 0xFFFF


def battery_pct(mv: Optional[int]) -> Optional[int]:
    """Rough charge of 3x AA from the rest voltage (3.0 V cut-off .. 4.6 V fresh)."""
    if not mv:
        return None
    return max(0, min(100, round((mv - 3000) / 16)))


class Core:
    def __init__(self, db: Database, clock: Callable[[], float] = time.time):
        self.db = db
        self.now = clock
        self.lock = threading.RLock()
        self.pair_until = 0.0
        self._hub_priv: Optional[bytes] = None
        self._hub_pub: Optional[bytes] = None
        self._pending: dict[int, list[dict]] = {}
        self._unpair_sent: dict[int, float] = {}
        self._last_rx: dict[int, tuple[float, int]] = {}   # node -> (arrival, ctr)
        self.heard: dict[int, dict] = {}                   # unknown nodes heard (for the web)
        self.listeners: list[Callable[[int], None]] = []   # called with a node id on new data
        self.stats = {"frames": 0, "data": 0, "auth_fail": 0, "replay": 0, "pair_req": 0,
                      "last_frame": 0.0}

    def audit(self, action: str, detail: str = "", user: str = "system", ip: str = "") -> None:
        """One line of the event history (Historie událostí), like the tank hub."""
        try:
            self.db.execute("INSERT INTO audit(ts, username, ip, action, detail) VALUES(?,?,?,?,?)",
                            (int(self.now()), (user or "")[:64], (ip or "")[:64], action, (detail or "")[:500]))
        except Exception:
            log.exception("audit write failed")

    def label(self, nid: int) -> str:
        n = self.node(nid)
        return f"{n['name']} ({nid:04X})" if n else f"{nid:04X}"

    # ============================================================ radio
    def on_datagram(self, data: bytes) -> Optional[bytes]:
        """One datagram from the bridge. Returns the answer datagram or None."""
        if len(data) < 8 or data[:3] != MAGIC:
            return None
        want = data[3] == 1
        rssi10, lqi = struct.unpack_from("<hB", data, 4)
        frame = bytes(data[7:])
        reply = self.on_frame(frame, rssi10 / 10.0, lqi)
        if want:
            return MAGIC + (reply or b"")
        return None

    def on_frame(self, frame: bytes, rssi: float, lqi: int) -> Optional[bytes]:
        with self.lock:
            self.stats["frames"] += 1
            self.stats["last_frame"] = self.now()
            if not frame:
                return None
            if frame[0] == crypto.PKT_PAIR_REQ:
                self.stats["pair_req"] += 1
                return self._pair_request(frame, rssi)
            if frame[0] == crypto.PKT_DATA:
                self._data(frame, rssi)
            return None

    # ---------------------------------------------------------- pairing
    def start_pairing(self) -> None:
        with self.lock:
            self.pair_until = self.now() + PAIR_WINDOW_S
            if self._hub_priv is None:
                self._hub_priv, self._hub_pub = crypto.x25519_keypair()
            log.info("Pairing window open for %d s", PAIR_WINDOW_S)

    def stop_pairing(self) -> None:
        with self.lock:
            self.pair_until = 0.0

    def pairing_left(self) -> int:
        return max(0, int(self.pair_until - self.now()))

    def _pair_request(self, frame: bytes, rssi: float) -> Optional[bytes]:
        req = crypto.parse_pair_req(frame)
        if req is None:
            return None
        nid, node_pub, node_paired = req
        now = self.now()
        node = self.node(nid)
        self.heard[nid] = {"ts": now, "rssi": rssi, "paired": node_paired}
        if node_paired:
            # a sensor that holds a key accepts nothing but an UNPAIR signed with it;
            # sent only when the user asked to unpair this sensor here
            if node and node["unpair"] and node["k_mac"]:
                self._unpair_sent[nid] = now
                log.info("Sending UNPAIR to %04X", nid)
                return crypto.build_unpair(node["k_mac"], nid, node_pub)
            return None
        # the sensor holds no key
        if node and node["unpair"] and nid in self._unpair_sent:
            label = self.label(nid)
            self.forget(nid)                              # it confirmed the unpairing
            log.info("Node %04X confirmed unpairing", nid)
            self.audit("NODE_UNPAIRED", f"Čidlo {label} potvrdilo odpárování")
            node = None
        if now >= self.pair_until or self._hub_priv is None:
            return None                                   # only while the user waits for it
        if node is None and len(self.nodes()) >= MAX_NODES:
            return None
        keys = crypto.derive_keys(crypto.x25519(self._hub_priv, node_pub))
        if keys is None:
            return None
        lst = self._pending.setdefault(nid, [])
        if not any(e["k_mac"] == keys[1] for e in lst):
            lst.append({"k_enc": keys[0], "k_mac": keys[1], "ts": now})
        return crypto.build_pair_ack(nid, self._hub_pub)

    def _commit(self, nid: int, entry: dict) -> None:
        node = self.node(nid)
        now = int(self.now())
        if node is None:
            n = len(self.nodes()) + 1
            self.db.execute("INSERT INTO nodes(id, name, k_enc, k_mac, paired_at, position) "
                            "VALUES(?, ?, ?, ?, ?, ?)",
                            (nid, f"Anemometr {n}", entry["k_enc"], entry["k_mac"], now, n))
        else:
            self.db.execute("UPDATE nodes SET k_enc=?, k_mac=?, unpair=0, last_ctr=-1, paired_at=? "
                            "WHERE id=?", (entry["k_enc"], entry["k_mac"], now, nid))
        self._pending.pop(nid, None)
        self._unpair_sent.pop(nid, None)
        self._last_rx.pop(nid, None)
        self.heard.pop(nid, None)
        log.info("Node %04X paired", nid)
        self.audit("NODE_PAIRED", f"Spárováno čidlo {self.label(nid)}")

    def request_unpair(self, nid: int, on: bool = True) -> None:
        with self.lock:
            self.db.execute("UPDATE nodes SET unpair=? WHERE id=?", (1 if on else 0, nid))
            self._unpair_sent.pop(nid, None)

    def forget(self, nid: int) -> None:
        """Remove the sensor (keys and settings); its history stays for a re-pairing."""
        with self.lock:
            self.db.execute("DELETE FROM nodes WHERE id=?", (nid,))
            self._pending.pop(nid, None)
            self._unpair_sent.pop(nid, None)
            self._last_rx.pop(nid, None)

    def delete_history(self, nid: int) -> None:
        self.db.execute("DELETE FROM samples WHERE node_id=?", (nid,))

    # ------------------------------------------------------------- data
    def _data(self, frame: bytes, rssi: float) -> None:
        hdr = crypto.data_header(frame)
        if hdr is None:
            return
        nid, ctr = hdr
        node = self.node(nid)
        fields = None
        if node and node["k_enc"] and not node["unpair"]:
            fields = crypto.open_data(node["k_enc"], node["k_mac"], frame)
        if fields is None:
            now = self.now()
            for e in [e for e in self._pending.get(nid, []) if now - e["ts"] < PENDING_KEY_S]:
                f = crypto.open_data(e["k_enc"], e["k_mac"], frame)
                if f is not None:
                    self._commit(nid, e)
                    node = self.node(nid)
                    fields = f
                    break
        if fields is None:
            self.stats["auth_fail"] += 1
            if node is None:
                self.heard[nid] = {"ts": self.now(), "rssi": rssi, "paired": True}
            return
        if ctr <= node["last_ctr"]:
            self.stats["replay"] += 1                     # repeated copy or replay
            return
        self.db.execute("UPDATE nodes SET last_ctr=? WHERE id=?", (ctr, nid))
        flags, vbat, pulses, gust, window_s, temp_c, fw, diag, sag = fields
        now = self.now()
        clock = self._learn_clock(node, ctr, now, window_s)
        secs = max(1.0, window_s * clock)
        self.db.execute(
            "INSERT INTO samples(node_id, ts, secs, pulses, gust_pulses, gust_secs, vbat_mv, temp_c, rssi, flags) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (nid, int(now), secs, pulses, gust, GUST_NODE_S * clock,
             None if vbat == U16_INVALID else vbat, None if temp_c == TEMP_INVALID else temp_c,
             rssi, flags))
        self.db.execute("UPDATE nodes SET fw=?, diag=?, sag=? WHERE id=?", (fw, diag, sag, nid))
        self.stats["data"] += 1
        for cb in list(self.listeners):
            try:
                cb(nid)
            except Exception:
                log.exception("listener failed")

    def _learn_clock(self, node: dict, ctr: int, now: float, window_s: int) -> float:
        """Real seconds per node second from consecutive packets: the window ends with
        the transmission, so the time between two arrivals is the window length."""
        nid = node["id"]
        clock = node["clock"] or 1.0
        prev = self._last_rx.get(nid)
        self._last_rx[nid] = (now, ctr)
        if prev and ctr == prev[1] + 1 and window_s >= 10:
            ratio = (now - prev[0]) / window_s
            if 0.7 < ratio < 1.3:                         # a lost packet or a restart in between: skip
                clock = min(CLOCK_MAX, max(CLOCK_MIN, clock + CLOCK_ALPHA * (ratio - clock)))
                self.db.execute("UPDATE nodes SET clock=? WHERE id=?", (clock, nid))
        return clock

    # ========================================================== queries
    def node(self, nid: int) -> Optional[dict]:
        return self.db.one("SELECT * FROM nodes WHERE id=?", (nid,))

    def nodes(self) -> list[dict]:
        return self.db.all("SELECT * FROM nodes ORDER BY position, id")

    def update_node(self, nid: int, name: Optional[str] = None, rev_ms: Optional[float] = None,
                    ppr: Optional[int] = None) -> None:
        """Name and calibration: wind speed [m/s] at one revolution per second and reed
        pulses per revolution. The stored factor (km/h per 1 pulse/s) follows from them."""
        node = self.node(nid)
        if node is None:
            raise KeyError(nid)
        if name is not None:
            self.db.execute("UPDATE nodes SET name=? WHERE id=?", (name.strip()[:40], nid))
        if rev_ms is not None or ppr is not None:
            rev = node["rev_ms"] if rev_ms is None else float(rev_ms)
            p = node["ppr"] if ppr is None else int(ppr)
            if not 0.05 <= rev <= 20:
                raise ValueError("1 otáčka/s musí odpovídat 0,05–20 m/s")
            if not 1 <= p <= 16:
                raise ValueError("pulzů na otáčku 1–16")
            self.db.execute("UPDATE nodes SET rev_ms=?, ppr=?, factor=? WHERE id=?",
                            (rev, p, rev * 3.6 / p, nid))

    def state(self, nid: int) -> Optional[dict]:
        node = self.node(nid)
        if node is None:
            return None
        now = self.now()
        last = self.db.one("SELECT * FROM samples WHERE node_id=? ORDER BY ts DESC LIMIT 1", (nid,))
        f = node["factor"]
        out = {
            "id": nid, "hex": f"{nid:04X}", "name": node["name"], "factor": f,
            "rev_ms": node["rev_ms"], "ppr": node["ppr"],
            "clock": round(node["clock"], 4), "unpair": bool(node["unpair"]),
            "fw": node.get("fw"), "status": "offline", "last_seen": None,
            "speed_kmh": None, "gust_kmh": None, "avg10_kmh": None, "avg1h_kmh": None,
            "gust_today_kmh": None, "gust_today_ts": None, "loss_pct": None, "link_pct": None,
            "battery_v": None, "battery_pct": None, "battery_low": False, "temperature": None,
            "rssi": None, "stuck": False,
        }
        if last is None:
            return out
        age = now - last["ts"]
        out.update({
            "status": "online" if age < ONLINE_S else ("stale" if age < STALE_S else "offline"),
            "last_seen": last["ts"],
            "speed_kmh": round(last["pulses"] / last["secs"] * f, 1),
            "gust_kmh": round(last["gust_pulses"] / last["gust_secs"] * f, 1),
            "temperature": last["temp_c"], "rssi": last["rssi"],
            "battery_low": bool(last["flags"] & FLAG_BAT_LOW),
            "stuck": bool(last["flags"] & FLAG_STUCK),
        })
        bat = self.db.one("SELECT vbat_mv FROM samples WHERE node_id=? AND vbat_mv IS NOT NULL "
                          "ORDER BY ts DESC LIMIT 1", (nid,))
        if bat:
            out["battery_v"] = round(bat["vbat_mv"] / 1000, 2)
            out["battery_pct"] = battery_pct(bat["vbat_mv"])
        for key, span in (("avg10_kmh", 600), ("avg1h_kmh", 3600)):
            a = self.db.one("SELECT SUM(pulses) p, SUM(secs) s FROM samples WHERE node_id=? AND ts>?",
                            (nid, now - span))
            out[key] = round(a["p"] / a["s"] * f, 1) if a and a["s"] else None
        lt = time.localtime(now)
        midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        g = self.db.one("SELECT gust_pulses / gust_secs g, ts FROM samples WHERE node_id=? AND ts>=? "
                        "ORDER BY g DESC LIMIT 1", (nid, midnight))
        if g and g["g"] is not None:
            out["gust_today_kmh"] = round(g["g"] * f, 1)
            out["gust_today_ts"] = g["ts"]
        # lost packets in 24 h from the frame counter vs. the expected one per window
        d = self.db.one("SELECT COUNT(*) n, SUM(secs) s, MIN(ts) t0 FROM samples WHERE node_id=? AND ts>?",
                        (nid, now - 86400))
        if d and d["n"] and d["n"] > 5 and d["t0"]:
            expected = (now - d["t0"]) / (d["s"] / d["n"])
            out["loss_pct"] = round(max(0.0, 100 * (1 - d["n"] / max(expected, d["n"]))), 1)
        if out["rssi"] is not None:
            out["link_pct"] = max(0, min(100, round((out["rssi"] + 110) * 2)))   # -110 dBm 0 %, -60 dBm 100 %
        return out

    def states(self) -> list[dict]:
        return [self.state(n["id"]) for n in self.nodes()]

    def history(self, nid: int, frm: int, to: int) -> dict:
        """Speed, gust, battery and signal between frm and to, at most ~500 points."""
        node = self.node(nid)
        f = node["factor"] if node else 2.4
        span = max(1, to - frm)
        bucket = 0
        for b in (0, 300, 900, 1800, 3600, 3 * 3600, 6 * 3600, 86400):
            bucket = b
            if b and span / b <= 500 or (not b and span <= 500 * 60):
                break
        if bucket == 0:
            rows = self.db.all(
                "SELECT ts t, pulses p, secs s, gust_pulses * 1.0 / gust_secs g, vbat_mv v, temp_c c, rssi r "
                "FROM samples WHERE node_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (nid, frm, to))
        else:
            rows = self.db.all(
                "SELECT (ts / ?) * ? + ? / 2 t, SUM(pulses) p, SUM(secs) s, "
                "MAX(gust_pulses * 1.0 / gust_secs) g, AVG(vbat_mv) v, AVG(temp_c) c, AVG(rssi) r "
                "FROM samples WHERE node_id=? AND ts BETWEEN ? AND ? GROUP BY ts / ? ORDER BY t",
                (bucket, bucket, bucket, nid, frm, to, bucket))
        return {
            "bucket": bucket,
            "t": [r["t"] for r in rows],
            "speed": [round(r["p"] / r["s"] * f, 1) if r["s"] else None for r in rows],
            "gust": [round(r["g"] * f, 1) if r["g"] is not None else None for r in rows],
            "battery": [round(r["v"] / 1000, 2) if r["v"] else None for r in rows],
            "temp": [round(r["c"], 1) if r["c"] is not None else None for r in rows],
            "rssi": [round(r["r"], 1) if r["r"] is not None else None for r in rows],
        }

    def heard_unknown(self) -> list[dict]:
        now = self.now()
        known = {n["id"] for n in self.nodes()}
        return [{"id": k, "hex": f"{k:04X}", "age": int(now - v["ts"]), "rssi": v["rssi"],
                 "paired": v["paired"]}
                for k, v in sorted(self.heard.items()) if k not in known and now - v["ts"] < 3600]
