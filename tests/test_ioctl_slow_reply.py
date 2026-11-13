"""A slow IOCTL answer on a LIVE session must not tear the session down.

GET_LIGHTWEIGHT_STATUS (0x0964) takes the camera 1.1-3.3+ s to answer (other GETs 30-260 ms;
measured on firmware 3.0.1426) while its ACKs and keepalives keep arriving. _ioctl_once gave up
at a hard 3.0 s and ioctl() then disconnected and reconnected, discarding the answer that was
about to arrive and restarting the camera's wait, up to three times (seen live: 3 reconnects,
then TimeoutError). The coordinator reads the firmware version through this call.

While the camera is demonstrably alive the request now waits up to CUBOAI_IOCTL_ALIVE_WAIT
seconds (default 8) on the same session, and an unanswered request on a live session raises
without disconnecting. A silent session keeps the 3.0 s + reconnect behaviour.

Real ioctl() against a loopback "camera" that sends ACK chatter and answers a request D seconds
after first seeing it from that socket (a reconnect restarts the wait, as on the real camera).
"""

import importlib.util
import os
import socket
import struct
import threading
import time

_TUTK = os.path.join(os.path.dirname(__file__), "..", "custom_components", "cuboai", "tutk")
_spec = importlib.util.spec_from_file_location("live_pure_ioctl_slow", os.path.join(_TUTK, "cuboai_pure.py"))
cp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cp)


def _frame(sub, io=None, body=b""):
    d = bytearray(68 + len(body) if io is not None else 52)
    d[8:12] = b"\x08\x04\x12\x00"
    d[28] = sub
    if io is not None:
        struct.pack_into("<H", d, 52, len(body) + 4)
        struct.pack_into("<H", d, 64, io)
        d[68:] = body
    return cp.transcode(bytes(d))


class _Camera:
    def __init__(self, delay, chatter=True, answer=True):
        self.s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.s.bind(("127.0.0.1", 0))
        self.s.settimeout(0.05)
        self.delay, self.chatter, self.answer = delay, chatter, answer
        self.first, self.done, self.peer, self.stop = {}, set(), None, False
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        last_chat = 0.0
        while not self.stop:
            try:
                raw, addr = self.s.recvfrom(4096)
                d = cp.inv_transcode(raw)
                if len(d) >= 66 and d[28] == 0x0C:
                    self.peer = addr
                    self.first.setdefault(addr, (time.time(), struct.unpack("<H", d[64:66])[0]))
            except Exception:
                pass
            now = time.time()
            if self.chatter and self.peer and now - last_chat > 0.2:
                last_chat = now
                try:
                    self.s.sendto(_frame(0x09), self.peer)
                except OSError:
                    pass
            for addr, (t, io) in list(self.first.items()):
                if self.answer and addr not in self.done and now - t >= self.delay:
                    self.done.add(addr)
                    try:
                        self.s.sendto(_frame(0x0C, io=io | 1, body=b"\x07" * 40), addr)
                    except OSError:
                        pass


def _run(delay, chatter=True, answer=True):
    cam = _Camera(delay, chatter, answer)
    sess = cp.TUTKDirectSession("127.0.0.1")
    reconnects = [0]

    def fake_connect(*a, **k):
        reconnects[0] += 1
        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        c.bind(("127.0.0.1", 0))
        c.setblocking(False)
        sess._sock, sess.session_hdr, sess._R, sess._cam = c, bytes(16), 0x1234, cam.s.getsockname()
        return True

    sess.connect = fake_connect
    fake_connect()
    reconnects[0] = 0
    t0 = time.time()
    try:
        result, err = sess.ioctl(0x0964, b"\0" * 8), None
    except TimeoutError as e:
        result, err = None, e
    finally:
        cam.stop = True
    return result, err, time.time() - t0, reconnects[0], sess


def test_fast_answer_no_reconnect():
    result, err, _dt, reconnects, _s = _run(0.05)
    assert result is not None and err is None and reconnects == 0


def test_slow_answer_on_a_live_session_is_waited_for():
    result, err, dt, reconnects, sess = _run(3.4)
    assert result is not None and err is None
    assert reconnects == 0
    assert dt < 6.0


def test_live_but_unanswered_keeps_the_session(monkeypatch):
    monkeypatch.setenv("CUBOAI_IOCTL_ALIVE_WAIT", "4")
    result, err, dt, reconnects, sess = _run(99, answer=False)
    assert result is None and "session alive" in str(err)
    assert reconnects == 0 and sess._sock is not None
    assert 3.5 < dt < 6.0
    assert sess.get_stats()["ioctl_unanswered_alive"] == 1


def test_silent_session_still_reconnects():
    result, err, _dt, reconnects, _s = _run(0.05, chatter=False, answer=False)
    assert result is None and err is not None and reconnects >= 1
