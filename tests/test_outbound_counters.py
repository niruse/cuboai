"""get_stats() counts what the session SENDS, not only what it receives.

Every inbound counter had no send-side twin, so a send-side fault looked exactly like a
healthy session: an IOCTL that only got through on its third retransmit, an ACK that never
left, a keepalive reply that failed. get_stats() now reports tx_ack, tx_nak, tx_ioctl,
tx_ioctl_retx, tx_keepalive and tx_err; the streamer's health line shows ioctl-retx and
txerr next to the other recoveries when they are non-zero.

Real _ioctl_once against a loopback "camera" that ignores the first request and answers the
retransmit.
"""

import importlib.util
import os
import socket
import struct
import threading

_TUTK = os.path.join(os.path.dirname(__file__), "..", "custom_components", "cuboai", "tutk")
_spec = importlib.util.spec_from_file_location("live_pure_outbound", os.path.join(_TUTK, "cuboai_pure.py"))
cp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cp)

TX_KEYS = ("tx_ack", "tx_nak", "tx_ioctl", "tx_ioctl_retx", "tx_keepalive", "tx_err")


def _response(io, body=b"\x07" * 40):
    d = bytearray(68 + len(body))
    d[8:12] = b"\x08\x04\x12\x00"
    d[28] = 0x0C
    struct.pack_into("<H", d, 52, len(body) + 4)
    struct.pack_into("<H", d, 64, io)
    d[68:] = body
    return cp.transcode(bytes(d))


class _Camera:
    """Answers an IOCTL only on the Nth time it sees it."""

    def __init__(self, answer_on):
        self.s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.s.bind(("127.0.0.1", 0))
        self.s.settimeout(0.05)
        self.answer_on, self.seen, self.stop = answer_on, 0, False
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while not self.stop:
            try:
                raw, addr = self.s.recvfrom(4096)
            except OSError:
                continue
            d = cp.inv_transcode(raw)
            if len(d) >= 66 and d[28] == 0x0C:
                self.seen += 1
                if self.seen == self.answer_on:
                    self.s.sendto(_response(struct.unpack("<H", d[64:66])[0] | 1), addr)


def _session(cam):
    sess = cp.TUTKDirectSession("127.0.0.1")
    c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    c.bind(("127.0.0.1", 0))
    c.setblocking(False)
    sess._sock, sess._R, sess.session_hdr, sess._cam = c, 0x1234, bytes(16), cam.s.getsockname()
    return sess


def test_counters_exist_and_start_at_zero():
    st = cp.TUTKDirectSession("127.0.0.1").get_stats()
    assert all(st[k] == 0 for k in TX_KEYS)


def test_retransmitted_ioctl_is_counted():
    cam = _Camera(answer_on=2)
    sess = _session(cam)
    try:
        resp_type, body = sess._ioctl_once(0x0964, b"\0" * 8, timeout=3.0)
    finally:
        cam.stop = True
    st = sess.get_stats()
    assert resp_type == 0x0965 and body
    assert st["tx_ioctl"] == 2 and st["tx_ioctl_retx"] == 1
    assert st["tx_ack"] >= 1  # the response was acknowledged
    assert st["tx_err"] == 0


def test_first_time_answer_has_no_retransmit():
    cam = _Camera(answer_on=1)
    sess = _session(cam)
    try:
        sess._ioctl_once(0x0964, b"\0" * 8, timeout=3.0)
    finally:
        cam.stop = True
    st = sess.get_stats()
    assert st["tx_ioctl"] == 1 and st["tx_ioctl_retx"] == 0


def test_health_line_reports_send_side_recoveries():
    src = open(os.path.join(_TUTK, "cuboai_stream_video.py")).read()
    assert "cur.get('tx_ioctl_retx', 0)" in src and "cur.get('tx_err', 0)" in src
