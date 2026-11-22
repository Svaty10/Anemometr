"""Anemometer hub logic: radio frames from the tank hub's bridge, pairing, data.

Frames arrive over UDP from the tank hub (bridge attached to it by system/retencka-most),
which owns the CC1101. A PAIR_REQ is answered in the same datagram exchange (the node
listens only ~0.6 s). Pairing works exactly as in the tank hub:

  * an unpaired sensor gets a PAIR_ACK only after the user confirmed its ID here
    (start_pairing: ID from the label + name, valid PAIR_TARGET_S) – never by itself;
  * a sensor that holds a key accepts nothing but an UNPAIR signed with that key. It is
    sent only when the user asked for it here (unpair, or re-pair = unpair + pair), and
    the sensor asks only after its battery was re-inserted (hub login + physical access);
    the hub tries its current key, then pending and archived keys of the ID;
  * new keys become valid only after the sensor proves them with its first DATA frame;
  * keys of a removed sensor are archived, so a later pairing of its ID releases it.

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
PAIR_TARGET_S = 15 * 60           # user's confirmation of an ID: new PAIR_ACKs this long
PENDING_KEY_S = 24 * 3600         # key of a PAIR_ACK accepted on the first DATA this long
PAIR_REQUESTS_MAX = 64            # PAIR_REQ is unauthenticated: bounded request log
PAIR_PUBS_MAX = 8
PENDING_MAX = 4                   # handed-out keys per ID (strongest signal kept)
ARCHIVE_MAX = 8                   # archived keys per ID
UNPAIR_SETTLE_S = 20              # no paired request this long after the confirmation -> removed
HEARD_MAX = 20
ONLINE_S = 5 * 60                 # last packet younger than this: online
STALE_S = 30 * 60                 # ... then "stale", older: offline
GUST_NODE_S = 3                   # firmware: gust over 3 node seconds
CLOCK_MIN, CLOCK_MAX, CLOCK_ALPHA = 0.85, 1.15, 0.05
MAX_NODES = 8

FLAG_BAT_ERR, FLAG_TEMP_ERR, FLAG_STUCK, FLAG_OVERFLOW = 0x01, 0x02, 0x04, 0x08
FLAG_BAT_LOW, FLAG_BOOT, FLAG_WDT, FLAG_PARTIAL = 0x10, 0x20, 0x40, 0x80
TEMP_INVALID = -128
U16_INVALID = 0xFFFF


# Battery (3x AA), the same curves as the tank hub (retencka/model.py): cell voltage at a
# very light load vs. remaining capacity. Li-FeS2 stays flat for most of the life and
# falls steeply at the end, an alkaline cell falls steadily. 1.0 V/cell = empty (cut-off).
_LIFES2_CELL = [
    (1.00, 0.0), (1.10, 1.0), (1.20, 3.0), (1.30, 7.0), (1.38, 13.0), (1.42, 22.0),
    (1.45, 35.0), (1.48, 50.0), (1.52, 65.0), (1.57, 80.0), (1.63, 92.0), (1.70, 98.0),
    (1.78, 100.0),
]
_ALKALINE_CELL = [
    (1.00, 0.0), (1.05, 2.0), (1.10, 5.0), (1.15, 10.0), (1.20, 20.0), (1.25, 35.0),
    (1.30, 50.0), (1.35, 65.0), (1.40, 78.0), (1.45, 88.0), (1.50, 95.0), (1.55, 99.0),
    (1.60, 100.0),
]
BATTERY_CURVES = {"lifes2": _LIFES2_CELL, "alkaline": _ALKALINE_CELL}
BATTERY_LOW_PCT = 5.0
BATTERY_TRIM_RANGE = (0.8, 1.2)      # correction at most +-20 % (as in the tank hub)


def battery_percent(vbat_v: float, kind: str = "alkaline", cells: int = 3) -> float:
    v = vbat_v / cells
    pts = BATTERY_CURVES.get(kind, _ALKALINE_CELL)
    if v <= pts[0][0]:
        return 0.0
    if v >= pts[-1][0]:
        return 100.0
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= v <= x1:
            return y0 + (v - x0) * (y1 - y0) / (x1 - x0)
    return 0.0


def signal_pct(dbm: Optional[float]) -> Optional[int]:
    """Signal strength in %, linear between -110 dBm (CC1101 sensitivity) and -50 dBm (as in the tank hub)."""
    if dbm is None:
        return None
    return round(min(100.0, max(0.0, (dbm + 110) / 60 * 100)))


class Core:
    def __init__(self, db: Database, clock: Callable[[], float] = time.time):
        self.db = db
        self.now = clock
        self.lock = threading.RLock()
        self.pair_requests: dict[int, dict] = {}   # node -> {ts, rssi, count, node_paired, pubs}
        self.pair_targets: dict[int, dict] = {}    # node -> {priv, pub, expires, name, user}
        self._unpair_sent: dict[int, float] = {}   # node -> last UNPAIR sent
        self._unpair_confirm: dict[int, float] = {}  # node -> unpaired request after the UNPAIR
        self._unpair_rot: dict[int, int] = {}      # node -> next UNPAIR key candidate
        self._last_rx: dict[int, tuple[float, int]] = {}   # node -> (arrival, ctr)
        self._deferred: list[Callable[[], object]] = []    # database work after a PAIR_REQ answer
        self.heard: dict[int, dict] = {}           # every ID heard recently (for the web)
        self.listeners: list[Callable[[int], None]] = []   # called with a node id on new data
        self.removed_listeners: list[Callable[[int], None]] = []   # ... when a sensor was removed
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
        t0 = time.perf_counter()
        reply = self.on_frame(frame, rssi10 / 10.0, lqi)
        if want:
            if reply:
                log.info("Answer 0x%02X to %04X in %.0f ms", reply[0], struct.unpack_from("<H", reply, 1)[0],
                         (time.perf_counter() - t0) * 1000)
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
                req = crypto.parse_pair_req(frame)
                if req is None:
                    return None
                # the sensor listens only ~0.6 s: answer from memory, the bookkeeping
                # (database) runs after the answer was sent (after_reply), as in the tank hub
                reply = self._respond(*req)
                ts = self.now()
                self._deferred.append(lambda: (self._note_request(*req, rssi, ts), self.unpair_tick()))
                return reply
            if frame[0] == crypto.PKT_DATA:
                self.after_reply()
                self._data(frame, rssi)
            return None

    def after_reply(self) -> None:
        """Database work of answered PAIR_REQs (called by the bridge listener right after
        sending the answer; everything that reads pairing state calls it first)."""
        with self.lock:
            while self._deferred:
                fn = self._deferred.pop(0)
                try:
                    fn()
                except Exception:
                    log.exception("deferred pairing work failed")

    def _heard(self, nid: int, rssi: float, kind: str, ts: Optional[float] = None) -> dict:
        h = self.heard.pop(nid, {"count": 0})
        h.update(ts=self.now() if ts is None else ts, rssi=rssi, count=h["count"] + 1, kind=kind)
        self.heard[nid] = h                               # re-insert: least recently heard first
        while len(self.heard) > HEARD_MAX:
            del self.heard[next(iter(self.heard))]
        return h

    # ---------------------------------------------------------- pairing
    def _respond(self, nid: int, node_pub: bytes, node_paired: bool) -> Optional[bytes]:
        """Answer to a PAIR_REQ (see the module docstring)."""
        now = self.now()
        t = self.pair_targets.get(nid)
        if t is not None and t["expires"] < now:
            t = None
        node = self.node(nid)
        unpairing = bool(node and node["unpair"])
        if node_paired:
            if not unpairing and t is None:
                return None                               # battery swap etc.: nothing to do
            cands = self._unpair_candidates(nid, node)
            if not cands:
                return None                               # paired to a hub we know nothing of
            i = self._unpair_rot.get(nid, 0)
            self._unpair_rot[nid] = i + 1
            self._unpair_sent[nid] = now                  # it still holds a key: an earlier
            self._unpair_confirm.pop(nid, None)           # "unpaired" request did not count
            log.info("Sending UNPAIR to %04X", nid)
            return crypto.build_unpair(cands[i % len(cands)], nid, node_pub)
        if unpairing or t is None:
            return None                                   # not confirmed by the user
        if node is None and len(self.nodes()) >= MAX_NODES:
            return None
        if t["priv"] is None:
            t["priv"], t["pub"] = crypto.x25519_keypair()
        keys = crypto.derive_keys(crypto.x25519(t["priv"], node_pub))
        if keys is None:
            return None
        name, user = t.get("name") or "", t.get("user") or "system"

        def persist():
            if not self.db.one("SELECT id FROM key_archive WHERE node_id=? AND k_mac=? AND reason='pending'",
                               (nid, keys[1])):       # a retry gives the same key
                self.db.execute("INSERT INTO key_archive(node_id, k_enc, k_mac, ts, reason, name, username) "
                                "VALUES(?,?,?,?,'pending',?,?)", (nid, keys[0], keys[1], int(now), name, user))
                self._trim_pending(nid)
        self._deferred.append(persist)
        return crypto.build_pair_ack(nid, t["pub"])

    def _note_request(self, nid: int, node_pub: bytes, node_paired: bool, rssi: float,
                      ts: Optional[float] = None) -> None:
        ts = self.now() if ts is None else ts
        self._heard(nid, rssi, "pair", ts)
        pr = self.pair_requests.pop(nid, None) or {"count": 0, "pubs": set()}
        if len(pr["pubs"]) < PAIR_PUBS_MAX:
            pr["pubs"].add(node_pub.hex())
        pr.update(ts=ts, rssi=rssi, count=pr["count"] + 1, node_paired=node_paired)
        self.pair_requests[nid] = pr
        while len(self.pair_requests) > PAIR_REQUESTS_MAX:
            del self.pair_requests[next(iter(self.pair_requests))]
        node = self.node(nid)
        if node and node["unpair"] and not node_paired and nid in self._unpair_sent:
            # the sensor says it has forgotten its key. The request is not authenticated:
            # it counts only after an UNPAIR was sent, and the sensor is removed only when
            # it stays unpaired for UNPAIR_SETTLE_S (unpair_tick)
            self._unpair_confirm.setdefault(nid, ts)

    def _unpair_candidates(self, nid: int, node: Optional[dict]) -> list[bytes]:
        """MAC keys an UNPAIR may be signed with: current key, then keys seen in DATA
        frames, then the newest pending / archived ones."""
        out = []
        if node and node["k_mac"]:
            out.append(bytes(node["k_mac"]))
        for r in self.db.all("SELECT k_mac FROM key_archive WHERE node_id=? "
                             "ORDER BY seen_at IS NOT NULL DESC, IFNULL(seen_at, 0) DESC, ts DESC, id DESC", (nid,)):
            k = bytes(r["k_mac"])
            if k not in out:
                out.append(k)
        return out

    def _trim_pending(self, nid: int) -> None:
        ids = [r["id"] for r in self.db.all("SELECT id FROM key_archive WHERE node_id=? AND reason='pending' "
                                            "ORDER BY ts DESC, id DESC", (nid,))]
        for old in ids[PENDING_MAX:]:
            self.db.execute("UPDATE key_archive SET reason='expired' WHERE id=?", (old,))
        self._trim_archive(nid)

    def _trim_archive(self, nid: int) -> None:
        ids = [r["id"] for r in self.db.all(
            "SELECT id FROM key_archive WHERE node_id=? AND reason<>'pending' "
            "ORDER BY seen_at IS NOT NULL DESC, IFNULL(seen_at, 0) DESC, ts DESC, id DESC", (nid,))]
        for old in ids[ARCHIVE_MAX:]:
            self.db.execute("DELETE FROM key_archive WHERE id=?", (old,))

    def _pending(self, nid: int) -> list[dict]:
        self.db.execute("UPDATE key_archive SET reason='expired' WHERE reason='pending' AND ts<?",
                        (int(self.now() - PENDING_KEY_S),))
        return self.db.all("SELECT * FROM key_archive WHERE node_id=? AND reason='pending' ORDER BY id", (nid,))

    def _drop_pending(self, nid: int, reason: str) -> None:
        self.db.execute("UPDATE key_archive SET reason=? WHERE node_id=? AND reason='pending'", (reason, nid))
        self._trim_archive(nid)

    def _match_archive(self, nid: int, frame: bytes) -> bool:
        """An unaccepted DATA frame signed with an archived key: the sensor still holds
        it, so the web offers re-pairing (UNPAIR with that key)."""
        for r in self.db.all("SELECT id, k_enc, k_mac FROM key_archive WHERE node_id=? AND reason<>'pending'", (nid,)):
            if crypto.open_data(bytes(r["k_enc"]), bytes(r["k_mac"]), frame) is not None:
                self.db.execute("UPDATE key_archive SET seen_at=? WHERE id=?", (int(self.now()), r["id"]))
                return True
        return False

    def archive_info(self, nid: int) -> dict:
        rows = self.db.all("SELECT name, seen_at FROM key_archive WHERE node_id=? ORDER BY id", (nid,))
        return {"archived": bool(rows), "key_match": any(r["seen_at"] for r in rows),
                "archived_name": next((r["name"] for r in reversed(rows) if r["name"]), "")}

    def start_pairing(self, nid: int, name: str, user: str = "system") -> None:
        """The user confirmed this ID: answer its next requests (UNPAIR with any key the
        hub holds for it if the sensor is still paired, then PAIR_ACK)."""
        with self.lock:
            self.after_reply()
            node = self.node(nid)
            if node and node["unpair"]:
                self.db.execute("UPDATE nodes SET unpair=0 WHERE id=?", (nid,))   # re-pair instead of removing
            priv, pub = crypto.x25519_keypair()          # not inside the sensor's 0.6 s window
            self.pair_targets[nid] = {"priv": priv, "pub": pub, "expires": self.now() + PAIR_TARGET_S,
                                      "name": name, "user": user}
            self._clear_unpair(nid)

    def cancel_pairing(self, nid: int) -> None:
        with self.lock:
            self.after_reply()
            self.pair_targets.pop(nid, None)
            self._drop_pending(nid, "cancelled")

    def _clear_unpair(self, nid: int) -> None:
        self._unpair_sent.pop(nid, None)
        self._unpair_confirm.pop(nid, None)
        self._unpair_rot.pop(nid, None)

    def unpair_tick(self) -> None:
        """Finish unpairings the sensor confirmed and did not take back."""
        now = self.now()
        for nid in [k for k, ts in self._unpair_confirm.items() if now - ts >= UNPAIR_SETTLE_S]:
            node = self.node(nid)
            self._unpair_confirm.pop(nid, None)
            if node is None or not node["unpair"]:
                continue
            self.forget_node(nid, "NODE_UNPAIRED", f"Čidlo {self.label(nid)} odpárováno – potvrzeno čidlem")

    def request_unpair(self, nid: int, user: str = "system", ip: str = "") -> None:
        """Step 1 of unpairing: stop accepting data, wait for the sensor to confirm
        after its battery was re-inserted."""
        with self.lock:
            self.cancel_pairing(nid)
            self._clear_unpair(nid)
            self.db.execute("UPDATE nodes SET unpair=1 WHERE id=?", (nid,))
            self.audit("NODE_UNPAIR_REQUESTED",
                       f"Odpárování čidla {self.label(nid)} – čeká na vložení baterie do čidla", user, ip)

    def cancel_unpair(self, nid: int, user: str = "system", ip: str = "") -> None:
        with self.lock:
            self.db.execute("UPDATE nodes SET unpair=0 WHERE id=?", (nid,))
            self._clear_unpair(nid)
            self.audit("NODE_UNPAIR_CANCELLED", f"Odpárování čidla {self.label(nid)} zrušeno", user, ip)

    def forget_node(self, nid: int, action: str, detail: str, user: str = "system", ip: str = "") -> None:
        """Delete the sensor; its measured data stay. Its key goes to the archive: if the
        sensor still holds it, pairing its ID again releases it with a signed UNPAIR."""
        with self.lock:
            self.after_reply()
            node = self.node(nid) or {}
            reason = "unpaired" if action == "NODE_UNPAIRED" else "removed"
            self.pair_targets.pop(nid, None)
            self._drop_pending(nid, reason)
            self._clear_unpair(nid)
            if node.get("k_mac"):
                if not self.db.one("SELECT id FROM key_archive WHERE node_id=? AND k_mac=?", (nid, node["k_mac"])):
                    self.db.execute("INSERT INTO key_archive(node_id, k_enc, k_mac, ts, reason, name, username) "
                                    "VALUES(?,?,?,?,?,?,?)", (nid, node["k_enc"], node["k_mac"], int(self.now()),
                                                               reason, node.get("name") or "", user or "system"))
                self._trim_archive(nid)
            self.db.execute("DELETE FROM nodes WHERE id=?", (nid,))
            self._last_rx.pop(nid, None)
            self.audit(action, detail + "; naměřená data zůstávají", user, ip)
            log.info("Node %04X removed (%s)", nid, action)
            for cb in list(self.removed_listeners):
                try:
                    cb(nid)
                except Exception:
                    log.exception("listener failed")

    def _commit(self, nid: int, entry: dict) -> bool:
        """The first DATA frame proved a key from a confirmed PAIR_ACK: it becomes the
        sensor's key; older pending and archived keys of the ID are dropped."""
        node = self.node(nid)
        if node is None and len(self.nodes()) >= MAX_NODES:
            log.warning("Node %04X proved its pairing key, but the hub is full (%d sensors)", nid, MAX_NODES)
            return False
        t = self.pair_targets.pop(nid, {})
        self.pair_requests.pop(nid, None)
        self._clear_unpair(nid)
        old_name = self.archive_info(nid)["archived_name"]
        now = int(self.now())
        if node is None:
            n = len(self.nodes()) + 1
            name = t.get("name") or entry.get("name") or old_name or f"Anemometr {n}"
            pos = (self.db.scalar("SELECT MAX(position) FROM nodes", default=0) or 0) + 1
            self.db.execute("INSERT INTO nodes(id, name, k_enc, k_mac, paired_at, position) VALUES(?, ?, ?, ?, ?, ?)",
                            (nid, name[:40], entry["k_enc"], entry["k_mac"], now, pos))
        else:
            self.db.execute("UPDATE nodes SET k_enc=?, k_mac=?, unpair=0, last_ctr=-1, paired_at=? WHERE id=?",
                            (entry["k_enc"], entry["k_mac"], now, nid))
        self.db.execute("DELETE FROM key_archive WHERE node_id=?", (nid,))
        self._last_rx.pop(nid, None)
        if nid in self.heard:
            self.heard[nid].pop("key_match", None)
        self.audit("NODE_PAIRED", f"Čidlo {self.label(nid)} {'spárováno' if node is None else 'znovu spárováno (nový klíč)'}",
                   t.get("user") or entry.get("username") or "system")
        log.info("Node %04X paired", nid)
        return True

    def pairing_status(self) -> dict:
        """For the web, the same shape as the tank hub's."""
        with self.lock:
            self.after_reply()
            now = self.now()
            self.unpair_tick()
            for k in [k for k, v in self.pair_targets.items() if v["expires"] < now]:
                self.pair_targets.pop(k, None)
            self.db.execute("UPDATE key_archive SET reason='expired' WHERE reason='pending' AND ts<?",
                            (int(now - PENDING_KEY_S),))
            pend: dict[int, list] = {}
            for r in self.db.all("SELECT node_id, name, ts FROM key_archive WHERE reason='pending' ORDER BY id"):
                pend.setdefault(r["node_id"], []).append(r)
            archived = {r["node_id"] for r in self.db.all("SELECT DISTINCT node_id FROM key_archive")}
            known = {n["id"] for n in self.nodes()}
            reqs = [{"id": f"{k:04X}", "rssi": v["rssi"], "age_s": int(now - v["ts"]), "count": v["count"],
                     "pub_count": len(v["pubs"]), "known": k in known, "node_paired": v["node_paired"],
                     "target": k in self.pair_targets, "answered": k in pend, "archived": k in archived}
                    for k, v in sorted(self.pair_requests.items(), key=lambda kv: -kv[1]["ts"])
                    if now - v["ts"] < 120]
            targets = [{"id": f"{k:04X}", "name": v["name"], "expires_in": int(v["expires"] - now),
                        "answered": k in pend, "rssi": (self.pair_requests.get(k) or {}).get("rssi"),
                        "pub_count": max(len((self.pair_requests.get(k) or {}).get("pubs", ())), len(pend.get(k, ())))}
                       for k, v in self.pair_targets.items()]
            pending = [{"id": f"{k:04X}", "name": v[-1]["name"] or "",
                        "expires_in": int(max(e["ts"] for e in v) + PENDING_KEY_S - now),
                        "rssi": (self.pair_requests.get(k) or {}).get("rssi"),
                        "pub_count": max(len((self.pair_requests.get(k) or {}).get("pubs", ())), len(v))}
                       for k, v in pend.items() if k not in self.pair_targets]
            return {"requests": reqs, "targets": targets, "pending": pending}

    def heard_list(self) -> list[dict]:
        self.after_reply()
        now = self.now()
        known = {n["id"] for n in self.nodes()}
        out = []
        for nid, h in reversed(list(self.heard.items())):
            arch = self.archive_info(nid)
            out.append({"id": f"{nid:04X}", "rssi": h["rssi"], "age_s": int(now - h["ts"]), "count": h["count"],
                        "added": nid in known, "kind": h["kind"], "archived": arch["archived"],
                        "key_match": bool(h.get("key_match")), "archived_name": arch["archived_name"]})
        return out

    # ------------------------------------------------------------- data
    def _data(self, frame: bytes, rssi: float) -> None:
        hdr = crypto.data_header(frame)
        if hdr is None:
            return
        nid, ctr = hdr
        heard = self._heard(nid, rssi, "data")
        node = self.node(nid)
        if node and node["unpair"]:
            return                                        # being unpaired: data no longer accepted
        fields = None
        if node and node["k_enc"]:
            fields = crypto.open_data(node["k_enc"], node["k_mac"], frame)
        if fields is None:
            for e in self._pending(nid):
                f = crypto.open_data(bytes(e["k_enc"]), bytes(e["k_mac"]), frame)
                if f is not None:
                    if self._commit(nid, e):
                        node = self.node(nid)
                        fields = f
                    break
        if fields is None:
            if node is not None:
                self.stats["auth_fail"] += 1
            heard["key_match"] = self._match_archive(nid, frame)
            return                                        # unknown node or forged / damaged frame
        if ctr <= node["last_ctr"]:
            self.stats["replay"] += 1                     # repeated copy or replay
            return
        self.db.execute("UPDATE nodes SET last_ctr=? WHERE id=?", (ctr, nid))
        flags, vbat, pulses, gust, window_s, _temp, fw, diag, sag = fields
        now = self.now()
        clock = self._learn_clock(node, ctr, now, window_s)
        secs = max(1.0, window_s * clock)
        self.db.execute(
            "INSERT INTO samples(node_id, ts, secs, pulses, gust_pulses, gust_secs, vbat_mv, temp_c, rssi, flags) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (nid, int(now), secs, pulses, gust, GUST_NODE_S * clock,
             None if vbat == U16_INVALID else vbat, None,       # no thermometer: chip temperature not stored
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
                    ppr: Optional[int] = None, battery_type: Optional[str] = None) -> None:
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
        if battery_type is not None:
            if battery_type not in BATTERY_CURVES:
                raise ValueError("neznámý typ baterie")
            self.db.execute("UPDATE nodes SET battery_type=? WHERE id=?", (battery_type, nid))

    def battery_calibrate(self, nid: int, actual_v: float) -> float:
        """Correction of the battery measurement from a voltmeter reading (as in the tank hub):
        trim = measured / reported voltage of the last packet."""
        last = self.db.one("SELECT vbat_mv FROM samples WHERE node_id=? AND vbat_mv IS NOT NULL "
                           "ORDER BY ts DESC LIMIT 1", (nid,))
        if not last or not last["vbat_mv"]:
            raise LookupError("Chybí měření baterie.")
        trim = actual_v * 1000.0 / last["vbat_mv"]
        if not BATTERY_TRIM_RANGE[0] <= trim <= BATTERY_TRIM_RANGE[1]:
            raise ValueError("Korekce mimo povolený rozsah ±20 % – zkontrolujte měření.")
        trim = round(trim, 4)
        self.db.execute("UPDATE nodes SET battery_trim=? WHERE id=?", (trim, nid))
        return trim

    def battery(self, node: dict, vbat_mv: Optional[float]) -> tuple[Optional[float], Optional[float]]:
        """(voltage after the correction, percent by the battery type) of a raw reading."""
        if vbat_mv is None:
            return None, None
        v = vbat_mv / 1000.0 * float(node.get("battery_trim") or 1.0)
        return v, battery_percent(v, node.get("battery_type") or "alkaline")

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
            "battery_type": node.get("battery_type") or "alkaline", "battery_trim": node.get("battery_trim") or 1.0,
            "clock": round(node["clock"], 4), "unpair": bool(node["unpair"]), "paired": bool(node["k_mac"]),
            "fw": node.get("fw"), "status": "offline", "last_seen": None,
            "speed_kmh": None, "gust_kmh": None, "avg10_kmh": None, "avg1h_kmh": None,
            "gust_today_kmh": None, "gust_today_ts": None, "loss_pct": None, "link_pct": None,
            "battery_v": None, "battery_pct": None, "battery_low": False,
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
            "rssi": last["rssi"],
            "battery_low": bool(last["flags"] & FLAG_BAT_LOW),
            "stuck": bool(last["flags"] & FLAG_STUCK),
        })
        bat = self.db.one("SELECT vbat_mv FROM samples WHERE node_id=? AND vbat_mv IS NOT NULL "
                          "ORDER BY ts DESC LIMIT 1", (nid,))
        if bat:
            v, pct = self.battery(node, bat["vbat_mv"])
            out["battery_v"] = round(v, 3)
            out["battery_pct"] = round(pct)
            out["battery_low"] = out["battery_low"] or pct < BATTERY_LOW_PCT
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
            out["link_pct"] = signal_pct(out["rssi"])
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
                "SELECT ts t, pulses p, secs s, gust_pulses * 1.0 / gust_secs g, vbat_mv v, rssi r "
                "FROM samples WHERE node_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (nid, frm, to))
        else:
            rows = self.db.all(
                "SELECT (ts / ?) * ? + ? / 2 t, SUM(pulses) p, SUM(secs) s, "
                "MAX(gust_pulses * 1.0 / gust_secs) g, AVG(vbat_mv) v, AVG(rssi) r "
                "FROM samples WHERE node_id=? AND ts BETWEEN ? AND ? GROUP BY ts / ? ORDER BY t",
                (bucket, bucket, bucket, nid, frm, to, bucket))
        bats = [self.battery(node or {}, r["v"]) for r in rows]
        # sensor power-ups (battery inserted) and watchdog restarts: marks in the battery chart
        boots = self.db.all("SELECT ts, flags FROM samples WHERE node_id=? AND ts BETWEEN ? AND ? "
                            "AND (flags & ?) != 0 ORDER BY ts LIMIT 200", (nid, frm, to, FLAG_BOOT | FLAG_WDT))
        return {
            "bucket": bucket,
            "bucket_s": bucket or 60,
            "first_ts": self._first_ts(nid),
            "t": [r["t"] for r in rows],
            "speed": [round(r["p"] / r["s"] * f, 1) if r["s"] else None for r in rows],
            "gust": [round(r["g"] * f, 1) if r["g"] is not None else None for r in rows],
            "vbat": [None if v is None else round(v, 3) for v, _ in bats],
            "bat_pct": [None if p is None else round(p, 1) for _, p in bats],
            "rssi": [round(r["r"], 1) if r["r"] is not None else None for r in rows],
            "boots": [{"t": b["ts"] * 1000, "wdt": not b["flags"] & FLAG_BOOT} for b in boots],
        }

    def _first_ts(self, nid: int) -> Optional[int]:
        """First stored measurement in ms (the charts cannot scroll further back)."""
        t = self.db.scalar("SELECT MIN(ts) FROM samples WHERE node_id=?", (nid,))
        return None if t is None else int(t) * 1000
