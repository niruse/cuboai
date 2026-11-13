"""A network outage must not kill the AV reader thread.

With the host's Wi-Fi switched off mid-stream, _send_nak() raised `OSError: [Errno 49] Can't
assign requested address` inside the reader thread (macOS; Linux gives ENETUNREACH/ENETDOWN).
Nothing caught it, so the thread died and the session ended at once, even for a sub-second blip;
the same unguarded sendto raised EBADF on a socket closed under the reader at shutdown. The
reader now guards its select() and its per-tick sends: errors are counted in tx_err and the loop
carries on, so a short outage resumes and a long one is left to the silence gates.

Real _av_reader over loopback; the session socket's sendto() raises during the "outage".
"""

import importlib.util
import os
import queue
import socket
import struct
import threading
import time

_TUTK = os.path.join(os.path.dirname(__file__), "..", "custom_components", "cuboai", "tutk")
_spec = importlib.util.spec_from_file_location("live_pure_send_error", os.path.join(_TUTK, "cuboai_pure.py"))
cp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cp)


def _frag(idx, frag, payload):
    d = bytearray(64 + len(payload))
    d[8:12] = b"\x08\x04\x12\x00"
    d[28] = 0x0C
    struct.pack_into("<H", d, 46, frag & 0xFFFF)
    struct.pack_into("<H", d, 52, len(payload))
    struct.pack_into("<H", d, 56, idx & 0xFFFF)
    d[58:64] = b"\x01\x00\x00\x00\x00\x00"
    d[64:] = payload
    return cp.transcode(bytes(d))


class _FlakySock:
    """The session socket, except sendto() fails while `down` is set."""

    def __init__(self, s):
        self._s, self.down = s, False

    def sendto(self, *a):
        if self.down:
            raise OSError(49, "Can't assign requested address")
        return self._s.sendto(*a)

    def __getattr__(self, k):
        return getattr(self._s, k)

    def fileno(self):
        return self._s.fileno()


def _rig(monkeypatch):
    monkeypatch.setenv("CUBOAI_IDX_SEED", "1")
    errors = []
    monkeypatch.setattr(threading, "excepthook", lambda a: errors.append(repr(a.exc_value)))
    sess = cp.TUTKDirectSession()
    sess._R, sess.session_hdr = 0x1234, bytes(16)
    cam = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    cam.bind(("127.0.0.1", 0))
    raw = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    raw.bind(("127.0.0.1", 0))
    raw.setblocking(False)
    cli = _FlakySock(raw)
    sess._sock, sess._cam = cli, cam.getsockname()
    q, stop = queue.Queue(maxsize=600), threading.Event()
    th = threading.Thread(target=sess._av_reader, args=(cli, q, stop), daemon=True)
    th.start()
    return sess, cam, raw, cli, q, stop, th, errors


def _send_aus(cam, dest, start, n):
    au = b"\x00\x00\x00\x01\x26\x01\xaf" + b"\xaa" * 40
    for i in range(start, start + n):
        cam.sendto(_frag(i, i, au), dest)
        time.sleep(0.002)


def _drain(q):
    n = 0
    while True:
        try:
            item = q.get_nowait()
        except queue.Empty:
            return n
        if item is not None and item[0] == "video":
            n += 1


def test_reader_survives_an_outage_and_resumes(monkeypatch):
    sess, cam, raw, cli, q, stop, th, errors = _rig(monkeypatch)
    try:
        _send_aus(cam, raw.getsockname(), 0, 40)
        time.sleep(0.4)
        before = _drain(q)
        cli.down = True  # outage: every send fails
        time.sleep(1.0)
        assert th.is_alive(), errors
        cli.down = False
        _send_aus(cam, raw.getsockname(), 40, 40)
        time.sleep(1.2)
        after = _drain(q)
    finally:
        stop.set()
        th.join(2)
    assert before > 0 and after > 0
    assert sess.get_stats()["tx_err"] > 0
    assert not errors


def test_socket_closed_under_the_reader_does_not_raise(monkeypatch):
    sess, cam, raw, cli, q, stop, th, errors = _rig(monkeypatch)
    time.sleep(0.2)
    raw.close()  # teardown racing the reader
    time.sleep(0.3)
    stop.set()
    th.join(2)
    assert not th.is_alive()
    assert not errors
