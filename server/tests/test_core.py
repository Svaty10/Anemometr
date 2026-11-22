"""vitr core: pairing over the bridge, data, speeds, unpairing (no hardware)."""

import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vitr import crypto  # noqa: E402
from vitr.core import Core, MAGIC  # noqa: E402
from vitr.db import Database  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


class Clock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeNode:
    """Behaves like the firmware: PAIR_REQ, then DATA frames."""

    def __init__(self, nid=0x1234):
        self.nid = nid
        self.priv = bytes(range(7, 39))
        self.pub = crypto.x25519_base(self.priv)
        self.keys = None
        self.ctr = 100

    def pair_req(self):
        return crypto.build_pair_req(self.nid, self.pub, self.keys is not None)

    def take_ack(self, frame):
        assert frame and frame[0] == crypto.PKT_PAIR_ACK
        self.keys = crypto.derive_keys(crypto.x25519(self.priv, frame[3:35]))

    def data(self, pulses, gust, window=60, vbat=4500, temp=12, flags=0):
        tel = crypto.build_telemetry(flags, vbat, pulses, gust, window, temp)
        self.ctr += 1
        return crypto.build_data(self.keys[0], self.keys[1], self.nid, self.ctr, tel)


def dgram(frame, rssi=-70.0, want=None):
    want = frame[0] == crypto.PKT_PAIR_REQ if want is None else want
    return MAGIC + bytes([1 if want else 0]) + struct.pack("<hB", int(rssi * 10), 30) + frame


class CoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.dir, "t.db"))
        self.clock = Clock()
        self.core = Core(self.db, self.clock)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def pair(self, node, name=""):
        self.core.start_pairing(node.nid, name)
        ans = self.core.on_datagram(dgram(node.pair_req()))
        self.assertEqual(ans[:3], MAGIC)
        node.take_ack(ans[3:])
        self.core.on_datagram(dgram(node.data(0, 0)))       # first DATA proves the key

    def test_no_pairing_without_confirmed_id(self):
        n, other = FakeNode(), FakeNode(0x7777)
        self.assertEqual(self.core.on_datagram(dgram(n.pair_req())), MAGIC)   # empty answer
        self.core.start_pairing(other.nid, "Jiný")
        self.assertEqual(self.core.on_datagram(dgram(n.pair_req())), MAGIC)   # another ID confirmed
        self.assertEqual(self.core.nodes(), [])
        st = self.core.pairing_status()
        self.assertEqual([q["id"] for q in st["requests"]], ["1234"])
        self.assertEqual(st["requests"][0]["known"], False)
        self.assertEqual([t["id"] for t in st["targets"]], ["7777"])
        self.clock.t += 16 * 60                                               # confirmation expired
        self.assertEqual(self.core.pairing_status()["targets"], [])

    def test_pairing_with_name_and_pending(self):
        n = FakeNode()
        self.core.start_pairing(n.nid, "Střecha")
        ans = self.core.on_datagram(dgram(n.pair_req()))
        n.take_ack(ans[3:])
        self.assertEqual(self.core.pairing_status()["targets"][0]["answered"], True)
        self.assertEqual(self.core.nodes(), [])                               # not before the first data
        self.core.cancel_pairing(n.nid)
        self.assertEqual(self.core.pairing_status()["pending"], [])
        self.core.on_datagram(dgram(n.data(0, 0)))
        self.assertEqual(self.core.nodes(), [])                               # cancelled key: refused
        # confirmed again: the sensor holds the cancelled key -> UNPAIR with it, then a new key
        self.core.start_pairing(n.nid, "Střecha")
        ans = self.core.on_datagram(dgram(n.pair_req()))
        self.assertTrue(crypto.check_unpair(n.keys[1], n.nid, n.pub, ans[3:]))
        n.keys = None
        self.pair(n, "Střecha")
        self.assertEqual(self.core.nodes()[0]["name"], "Střecha")

    def test_pair_and_measure(self):
        n = FakeNode()
        self.pair(n)
        nodes = self.core.nodes()
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["name"], "Anemometr 1")
        self.clock.t += 60
        self.core.on_datagram(dgram(n.data(pulses=300, gust=24)))     # 5 Hz, gust 8 Hz
        st = self.core.state(n.nid)
        self.assertEqual(st["status"], "online")
        self.assertAlmostEqual(st["speed_kmh"], 12.0, delta=0.2)     # 5 Hz * 2.4
        self.assertAlmostEqual(st["gust_kmh"], 19.2, delta=0.3)
        self.assertEqual(st["battery_v"], 4.5)
        self.assertNotIn("temperature", st)              # no thermometer in the anemometer

    def test_battery_like_tank_hub(self):
        """Percent by battery type, voltmeter correction, signal in % (as in the tank hub)."""
        n = FakeNode()
        self.pair(n)
        self.clock.t += 60
        self.core.on_datagram(dgram(n.data(pulses=10, gust=1)))     # 4.5 V = 1.5 V/cell
        st = self.core.state(n.nid)
        self.assertEqual(st["battery_type"], "alkaline")
        self.assertEqual(st["battery_pct"], 95)
        self.core.update_node(n.nid, battery_type="lifes2")
        self.assertEqual(self.core.state(n.nid)["battery_pct"], 58)  # flat Li-FeS2 curve
        with self.assertRaises(ValueError):
            self.core.update_node(n.nid, battery_type="nimh")
        self.assertEqual(self.core.battery_calibrate(n.nid, 4.41), 0.98)
        st = self.core.state(n.nid)
        self.assertEqual(st["battery_v"], 4.41)
        with self.assertRaises(ValueError):                 # more than +-20 %
            self.core.battery_calibrate(n.nid, 3.0)
        h = self.core.history(n.nid, int(self.clock.t) - 3600, int(self.clock.t))
        self.assertEqual(h["vbat"][-1], 4.41)
        self.assertIsNotNone(h["bat_pct"][-1])
        self.assertEqual(h["first_ts"], h["t"][0] * 1000)
        from vitr.core import signal_pct
        self.assertEqual([signal_pct(x) for x in (-120, -110, -80, -50, -40)], [0, 0, 50, 100, 100])

    def test_factor_changes_history(self):
        n = FakeNode()
        self.pair(n)
        self.clock.t += 60
        self.core.on_datagram(dgram(n.data(600, 40)))
        # 1 revolution/s = 2.4 m/s, 2 pulses per revolution: 10 Hz = 5 rev/s = 12 m/s
        self.core.update_node(n.nid, rev_ms=2.4, ppr=2)
        st = self.core.state(n.nid)
        self.assertAlmostEqual(st["speed_kmh"], 12 * 3.6, delta=0.2)
        self.assertEqual((st["rev_ms"], st["ppr"]), (2.4, 2))
        with self.assertRaises(ValueError):
            self.core.update_node(n.nid, rev_ms=0)

    def test_replay_and_forgery_dropped(self):
        n = FakeNode()
        self.pair(n)
        f = n.data(100, 5)
        self.core.on_datagram(dgram(f))
        self.core.on_datagram(dgram(f))                    # the repeated important packet
        self.assertEqual(self.core.stats["replay"], 1)
        bad = bytearray(n.data(100, 5))
        bad[10] ^= 1
        self.core.on_datagram(dgram(bytes(bad)))
        self.assertEqual(self.core.stats["auth_fail"], 1)
        cnt = self.db.one("SELECT COUNT(*) c FROM samples")["c"]
        self.assertEqual(cnt, 2)                           # first proof + one data

    def test_tank_frames_ignored(self):
        self.assertIsNone(self.core.on_datagram(dgram(bytes([0x03]) + bytes(22))))
        self.assertEqual(self.core.nodes(), [])

    def test_clock_learning(self):
        n = FakeNode()
        self.pair(n)
        for _ in range(200):                               # node clock 4 % slow: 60 node s = 62.4 s
            self.clock.t += 62.4
            self.core.on_datagram(dgram(n.data(300, 20)))
        clock = self.core.node(n.nid)["clock"]
        self.assertAlmostEqual(clock, 1.04, delta=0.005)
        self.assertAlmostEqual(self.core.state(n.nid)["speed_kmh"], 300 / 62.4 * 2.4, delta=0.1)

    def test_unpair_flow(self):
        n = FakeNode()
        self.pair(n)
        # battery re-inserted while not asked: a paired node gets nothing
        self.assertEqual(self.core.on_datagram(dgram(n.pair_req())), MAGIC)
        self.core.request_unpair(n.nid)
        ans = self.core.on_datagram(dgram(n.pair_req()))
        unpair = ans[3:]
        self.assertTrue(crypto.check_unpair(n.keys[1], n.nid, n.pub, unpair))
        n.keys = None                                      # the node forgets its key
        self.core.on_datagram(dgram(n.pair_req()))         # unpaired request = confirmation
        self.assertEqual(len(self.core.nodes()), 1)        # ... settles first
        self.clock.t += 25
        self.core.pairing_status()
        self.assertEqual(self.core.nodes(), [])
        self.assertTrue(self.core.archive_info(n.nid)["archived"])

    def test_unpair_cancel_and_repair_known(self):
        n = FakeNode()
        self.pair(n, "A")
        old = n.keys
        self.core.request_unpair(n.nid)
        self.core.cancel_unpair(n.nid)
        self.assertEqual(self.core.on_datagram(dgram(n.pair_req())), MAGIC)
        # re-pair a known sensor: UNPAIR with its key, then a new key
        self.core.start_pairing(n.nid, "")
        ans = self.core.on_datagram(dgram(n.pair_req()))
        self.assertTrue(crypto.check_unpair(old[1], n.nid, n.pub, ans[3:]))
        n.keys = None
        ans = self.core.on_datagram(dgram(n.pair_req()))
        n.take_ack(ans[3:])
        self.core.on_datagram(dgram(n.data(5, 1)))
        self.assertNotEqual(n.keys, old)
        self.assertEqual(bytes(self.core.node(n.nid)["k_mac"]), n.keys[1])
        self.assertEqual(self.core.node(n.nid)["name"], "A")

    def test_removed_sensor_repaired_from_archive(self):
        n = FakeNode()
        self.pair(n, "Garáž")
        self.core.forget_node(n.nid, "NODE_REMOVED", "test")
        self.assertEqual(self.core.nodes(), [])
        self.clock.t += 60
        self.core.on_datagram(dgram(n.data(10, 1)))                           # still sends with the old key
        h = {x["id"]: x for x in self.core.heard_list()}["1234"]
        self.assertTrue(h["archived"] and h["key_match"] and not h["added"])
        self.assertEqual(h["archived_name"], "Garáž")
        self.core.start_pairing(n.nid, "")                                     # "Spárovat znovu"
        ans = self.core.on_datagram(dgram(n.pair_req()))
        self.assertTrue(crypto.check_unpair(n.keys[1], n.nid, n.pub, ans[3:]))
        n.keys = None
        ans = self.core.on_datagram(dgram(n.pair_req()))
        n.take_ack(ans[3:])
        self.core.on_datagram(dgram(n.data(10, 1)))
        self.assertEqual(self.core.nodes()[0]["name"], "Garáž")
        self.assertFalse(self.core.archive_info(n.nid)["archived"])

    def test_foreign_paired_sensor_gets_nothing(self):
        n = FakeNode()
        n.keys = (bytes(16), bytes(16))                    # paired to another hub
        self.core.start_pairing(n.nid, "Soused")
        self.assertEqual(self.core.on_datagram(dgram(n.pair_req())), MAGIC)

    def test_two_anemometers(self):
        a, b = FakeNode(0x1111), FakeNode(0x2222)
        self.pair(a)
        self.pair(b)
        self.assertEqual([x["name"] for x in self.core.nodes()], ["Anemometr 1", "Anemometr 2"])

    def test_history_buckets(self):
        n = FakeNode()
        self.pair(n)
        for i in range(24 * 60):
            self.clock.t += 60
            self.core.on_datagram(dgram(n.data(60 * (i % 10), 30)))
        h = self.core.history(n.nid, int(self.clock.t - 86400), int(self.clock.t))
        self.assertEqual(h["bucket"], 300)
        self.assertLessEqual(len(h["t"]), 500)
        h = self.core.history(n.nid, int(self.clock.t - 7 * 86400), int(self.clock.t))
        self.assertGreater(h["bucket"], 300)

    @unittest.skipUnless(shutil.which("gcc"), "needs a C compiler")
    def test_firmware_packet_interop(self):
        """A DATA frame built by the firmware's C code (proto.c, ANM labels) opens here."""
        out = os.path.join(self.dir, "st.exe")
        u = ROOT / "uzel"
        subprocess.run(["gcc", "-O2", f"-I{u / 'include'}", str(u / "tools" / "crypto_selftest.c"),
                        str(u / "src" / "crypto.c"), str(u / "src" / "proto.c"), "-o", out], check=True)
        shared = bytes(range(1, 33))
        tel = crypto.build_telemetry(0x20, 4321, 777, 55, 61, -5, 10, 0x10, 9)
        r = subprocess.run([out, "data", shared.hex(), "BEEF", "42", tel.hex()],
                           check=True, capture_output=True, text=True)
        frame = bytes.fromhex(r.stdout.strip())
        self.assertEqual(frame[0], crypto.PKT_DATA)
        keys = crypto.derive_keys(shared)
        self.assertEqual(crypto.open_data(*keys, frame), (0x20, 4321, 777, 55, 61, -5, 10, 0x10, 9))
        self.assertEqual(crypto.build_data(*keys, 0xBEEF, 42, tel), frame)
        # UNPAIR built here is accepted by the firmware's check (proto_check_unpair)
        pub = crypto.x25519_base(bytes(range(9, 41)))
        r = subprocess.run([out, "unpair", keys[1].hex(), "BEEF", pub.hex()],
                           check=True, capture_output=True, text=True)
        self.assertEqual(bytes.fromhex(r.stdout.strip()), crypto.build_unpair(keys[1], 0xBEEF, pub))


if __name__ == "__main__":
    unittest.main()
