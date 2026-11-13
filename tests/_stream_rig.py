"""Shared synthetic-camera rig for the live-reader stream tests (no real camera data)."""

import importlib.util
import os
import queue
import socket
import struct
import threading
import time

_TUTK = os.path.join(os.path.dirname(__file__), "..", "custom_components", "cuboai", "tutk")


def load_pure(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_TUTK, "cuboai_pure.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def plain_frag(idx, frag, payload, group_size=20):
    """One decoded camera AV fragment in the standard layout, with the group header the camera
    uses for FEC: [36:38] group number, [38] position in the group, [39] group size."""
    d = bytearray(64 + len(payload))
    d[8:12] = b"\x08\x04\x12\x00"
    d[28] = 0x0C
    struct.pack_into("<H", d, 36, (frag // group_size) & 0xFFFF)
    d[38] = frag % group_size
    d[39] = group_size
    struct.pack_into("<H", d, 46, frag & 0xFFFF)
    struct.pack_into("<H", d, 52, len(payload))
    struct.pack_into("<H", d, 56, idx & 0xFFFF)
    d[58:64] = b"\x01\x00\x00\x00\x00\x00"
    d[64:] = payload
    return bytes(d)


def extended(plain, clock=0x1234):
    """The same fragment in the EXTENDED layout: byte[29] bit 0x08 and 8 extra bytes at [36:44]."""
    d = bytearray(plain[:36] + struct.pack("<HHHH", clock, 30, 1, 0) + plain[36:])
    d[29] |= 0x08
    return bytes(d)


def au_parts(i, nf=7):
    """Fragment payloads of access unit i: an Annex-B start + nf-1 continuation chunks."""
    return [b"\x00\x00\x00\x01\x02\x01" + bytes([i % 256]) * 200] + [
        bytes([0x40 + k, i % 256]) * 100 for k in range(1, nf)
    ]


class Reader:
    """The REAL _av_reader on loopback; feed it decoded frames, collect video AUs."""

    def __init__(self, cp, env, monkeypatch):
        monkeypatch.setenv("CUBOAI_IDX_SEED", "1")
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        self.cp = cp
        self.sess = cp.TUTKDirectSession()
        self.sess._R, self.sess.session_hdr = 0x1234, bytes(16)
        self.cam = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.cam.bind(("127.0.0.1", 0))
        self.cli = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.cli.bind(("127.0.0.1", 0))
        self.cli.setblocking(False)
        self.cli.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
        self.sess._sock, self.sess._cam = self.cli, self.cam.getsockname()
        self.q, self.stop = queue.Queue(maxsize=100000), threading.Event()
        self.th = threading.Thread(target=self.sess._av_reader, args=(self.cli, self.q, self.stop), daemon=True)
        self.th.start()
        self.n = 0

    def send(self, dec):
        self.cam.sendto(self.cp.transcode(dec), self.cli.getsockname())
        self.n += 1
        if self.n % 8 == 0:
            time.sleep(0.003)

    def finish(self, settle=1.5):
        time.sleep(settle)
        self.stop.set()
        self.th.join(3)
        aus = []
        while True:
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                break
            if item is not None and item[0] == "video":
                aus.append(item[1])
        self.cam.close()
        self.cli.close()
        return aus, self.sess.get_stats()
