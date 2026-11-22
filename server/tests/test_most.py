"""The anemometer bridge attached to the tank hub at run time (system/retencka-most)."""

import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path

MOST = Path(__file__).resolve().parents[2] / "system" / "retencka-most"

# a stand-in for the tank hub's radio: same names as retencka.radio.Receiver
FAKE_RADIO = textwrap.dedent('''
    class Receiver:
        def __init__(self, on_frame, responder=None, bus=0, dev=0, speed_hz=5_000_000,
                     gdo0_gpio=25, mock=False):
            self.on_frame = on_frame
            self.responder = responder
            self.spi = object()
            self.sent = []
            self.rx = 0
        def _enter_rx(self):
            self.rx += 1
        def _transmit(self, frame):
            self.sent.append(frame)
        def receive(self, payload):              # what _poll_fifo does after RX
            self._enter_rx()
            self.on_frame(payload, -71.5, 40)
''')

SCRIPT = textwrap.dedent('''
    import sys
    from retencka.radio import Receiver
    got = []
    r = Receiver(lambda p, rssi, lqi: got.append(p))
    r.receive(bytes([0x11, 1, 2, 3]))            # tank frame
    r.receive(bytes([0x13, 9, 9]))               # anemometer data
    r.receive(bytes([0x14, 7, 7]))               # anemometer pairing request
    print("patched", getattr(Receiver, "_vitr_most", False))
    print("tank", [p.hex() for p in got])
    print("sent", [p.hex() for p in r.sent])
''')


class MostTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        pkg = Path(self.tmp.name) / "retencka"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "radio.py").write_text(FAKE_RADIO)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(5)
        self.port = self.sock.getsockname()[1]
        self.received = []
        self.reply = b"RB1" + bytes([0x15, 1, 2])

    def tearDown(self):
        self.sock.close()
        self.tmp.cleanup()

    def _serve(self, n):
        for _ in range(n):
            try:
                data, addr = self.sock.recvfrom(256)
            except OSError:
                return
            self.received.append(data)
            if data[3] == 1:
                self.sock.sendto(self.reply, addr)

    def _run(self, with_most=True, n=2):
        t = threading.Thread(target=self._serve, args=(n,), daemon=True)
        t.start()
        env = dict(os.environ, VITR_MOST_PORT=str(self.port))
        paths = ([str(MOST)] if with_most else []) + [self.tmp.name]
        env["PYTHONPATH"] = os.pathsep.join(paths)
        out = subprocess.run([sys.executable, "-c", SCRIPT], env=env, capture_output=True,
                             text=True, timeout=30, cwd=self.tmp.name)
        self.assertEqual(out.returncode, 0, out.stderr)
        t.join(5)
        return out.stdout.splitlines()

    def test_forwards_anemometer_frames_only(self):
        lines = self._run()
        self.assertEqual(lines[0], "patched True")
        self.assertEqual(lines[1], "tank ['11010203']")          # anemometer frames never reach the tank hub
        self.assertEqual(lines[2], "sent ['150102']")            # PAIR_ACK transmitted
        self.assertEqual(len(self.received), 2)
        self.assertEqual(self.received[0][:4], b"RB1\x00")
        self.assertEqual(self.received[0][7:], bytes([0x13, 9, 9]))
        self.assertEqual(self.received[1][:4], b"RB1\x01")

    def test_rejects_foreign_answer_type(self):
        self.reply = b"RB1" + bytes([0x01, 0xAA])                # a tank frame type: never sent
        lines = self._run()
        self.assertEqual(lines[2], "sent []")

    def test_without_service_tank_hub_unchanged(self):
        self.sock.close()                                       # vitr not running
        t_env = dict(os.environ, VITR_MOST_PORT=str(self.port),
                     PYTHONPATH=os.pathsep.join([str(MOST), self.tmp.name]))
        out = subprocess.run([sys.executable, "-c", SCRIPT], env=t_env, capture_output=True,
                             text=True, timeout=30, cwd=self.tmp.name)
        self.assertEqual(out.returncode, 0, out.stderr)
        lines = out.stdout.splitlines()
        self.assertEqual(lines[1], "tank ['11010203']")
        self.assertEqual(lines[2], "sent []")

    def test_changed_receiver_left_alone(self):
        (Path(self.tmp.name) / "retencka" / "radio.py").write_text(
            FAKE_RADIO.replace("def _transmit", "def _send_frame").replace("self._transmit", "self._send_frame"))
        script = SCRIPT.replace("r.sent", "r.sent")
        env = dict(os.environ, VITR_MOST_PORT=str(self.port),
                   PYTHONPATH=os.pathsep.join([str(MOST), self.tmp.name]))
        out = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True,
                             text=True, timeout=30, cwd=self.tmp.name)
        self.assertEqual(out.returncode, 0, out.stderr)
        lines = out.stdout.splitlines()
        self.assertEqual(lines[0], "patched False")
        self.assertEqual(lines[1], "tank ['11010203', '130909', '140707']")   # as without the bridge


if __name__ == "__main__":
    unittest.main()
