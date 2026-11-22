"""Radio bridge for the anemometers, attached to the installed tank hub (retencka) at run time.

Only one process can own the CC1101, and the tank hub's code stays untouched (its
repository knows nothing about anemometers). The vitr installer adds a systemd
drop-in to retencka.service that puts this directory on PYTHONPATH; the local
``sitecustomize`` calls :func:`install`, which wraps ``retencka.radio.Receiver``
once that module is imported:

  * frames of the anemometer (types 0x13..0x16) go over UDP to the vitr service on
    127.0.0.1 and never reach the tank hub's own logic,
  * for a PAIR_REQ (0x14) the service's answer (PAIR_ACK 0x15 / UNPAIR 0x16) is
    transmitted right away – the node listens only ~0.6 s,
  * anything else (all tank frames) passes through unchanged.

Datagram to the service:   b"RB1" | want_reply u8 | rssi*10 i16 | lqi u8 | frame
Answer (PAIR_REQ only):    b"RB1" | frame        (empty frame = no answer)

Nothing is decrypted here, and only answer types of the anemometer can be
transmitted, so the service can never make the hub send tank frames. Any failure
leaves the tank hub running exactly as without the bridge. Removing the drop-in
(sudo rm /etc/systemd/system/retencka.service.d/vitr-most.conf) restores it fully.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import logging
import os
import socket
import struct
import sys
import time
from typing import Optional

log = logging.getLogger("retencka.vitr-most")


def _say(msg: str) -> None:
    """Start-up message straight to the journal (the tank hub sets up logging later)."""
    print(msg, file=sys.stderr, flush=True)

MAGIC = b"RB1"
FORWARD_TYPES = frozenset((0x13, 0x14, 0x15, 0x16))
REQUEST_TYPES = frozenset((0x14,))          # answered right away by the service
ANSWER_TYPES = frozenset((0x15, 0x16))      # the only frames the service may send
MAX_FRAME = 61
DEFAULT_PORT = 47613
TARGET = "retencka.radio"


class Bridge:
    def __init__(self, port: int = DEFAULT_PORT, host: str = "127.0.0.1", reply_timeout: float = 0.3):
        self.addr = (host, port)
        self.reply_timeout = reply_timeout
        self.forwarded = 0
        self.answers = 0
        self.errors = 0
        self.last_forward = 0.0

    @staticmethod
    def handles(payload: bytes) -> bool:
        return bool(payload) and payload[0] in FORWARD_TYPES

    def forward(self, payload: bytes, rssi: float, lqi: int) -> Optional[bytes]:
        """Send the frame to the service; for a request wait briefly for its answer."""
        want = payload[0] in REQUEST_TYPES
        t0 = time.monotonic()
        msg = (MAGIC + bytes([1 if want else 0])
               + struct.pack("<hB", max(-32768, min(32767, int(round(rssi * 10)))), lqi & 0xFF)
               + payload)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(self.reply_timeout)
                s.connect(self.addr)                  # fresh port: a late answer can never be mixed up
                s.send(msg)
                self.forwarded += 1
                self.last_forward = time.time()
                if not want:
                    return None
                data = s.recv(128)
        except (socket.timeout, OSError) as e:
            if want:
                self.errors += 1                      # service not running or too slow
                log.warning("vitr-most: no answer from vitr to the pairing request of %02X%02X (%s)",
                            payload[2], payload[1], "timeout" if isinstance(e, socket.timeout) else e)
            return None
        frame = data[len(MAGIC):] if data[:len(MAGIC)] == MAGIC else b""
        if not frame:
            return None
        if len(frame) > MAX_FRAME or frame[0] not in ANSWER_TYPES:
            log.warning("vitr-most: rejected answer type 0x%02X (%d B)", frame[0], len(frame))
            self.errors += 1
            return None
        self.answers += 1
        log.warning("vitr-most: answer 0x%02X for %02X%02X from vitr after %.0f ms",
                    frame[0], payload[2], payload[1], (time.monotonic() - t0) * 1000)
        return frame


def patch_receiver(module, bridge: Bridge) -> bool:
    """Wrap Receiver of the loaded retencka.radio. False (and nothing changed) when the
    tank hub's radio no longer looks as expected."""
    cls = getattr(module, "Receiver", None)
    if cls is None or getattr(cls, "_vitr_most", False):
        return False
    if not all(callable(getattr(cls, n, None)) for n in ("__init__", "_transmit", "_enter_rx")):
        _say("vitr-most: retencka.radio.Receiver has changed – anemometer bridge NOT active")
        return False
    orig_init = cls.__init__

    def __init__(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        inner = getattr(self, "on_frame", None)
        if not callable(inner):
            log.warning("vitr-most: Receiver.on_frame missing – anemometer bridge NOT active")
            return

        def on_frame(payload, rssi, lqi):
            if not bridge.handles(payload):
                return inner(payload, rssi, lqi)          # tank frames: unchanged
            try:
                reply = bridge.forward(payload, rssi, lqi)
                if reply and getattr(self, "spi", None) is not None:
                    self._transmit(reply)                 # radio thread, RX was just re-entered
                    self._enter_rx()
            except Exception:
                log.exception("vitr-most: forwarding failed")
            return None

        self.on_frame = on_frame
        self.vitr_bridge = bridge

    cls.__init__ = __init__
    cls._vitr_most = True
    _say(f"vitr-most: anemometer frames (0x13-0x16) go to vitr on 127.0.0.1:{bridge.addr[1]}")
    return True


class _Finder(importlib.abc.MetaPathFinder):
    """Patches retencka.radio right after the tank hub imports it."""

    def __init__(self, bridge: Bridge):
        self.bridge = bridge

    def find_spec(self, name, path, target=None):
        if name != TARGET:
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        loader = getattr(spec, "loader", None) if spec else None
        if loader is None or not hasattr(loader, "exec_module"):
            return spec
        orig_exec = loader.exec_module
        bridge = self.bridge

        def exec_module(module):
            orig_exec(module)
            try:
                patch_receiver(module, bridge)
            except Exception:
                _say("vitr-most: patching failed – anemometer bridge NOT active")

        loader.exec_module = exec_module
        return spec


def install() -> None:
    """Called from sitecustomize. Never raises."""
    try:
        port = int(os.environ.get("VITR_MOST_PORT", str(DEFAULT_PORT)))
        if port <= 0 or any(isinstance(f, _Finder) for f in sys.meta_path):
            return
        bridge = Bridge(port)
        mod = sys.modules.get(TARGET)
        if mod is not None:
            patch_receiver(mod, bridge)
        else:
            sys.meta_path.insert(0, _Finder(bridge))
    except Exception:                                     # pragma: no cover
        pass
