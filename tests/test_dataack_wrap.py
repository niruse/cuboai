"""The cumulative data-ACK must survive the u16 message-index wrap.

The camera's reliable IO/control message-index (wire [56:58]) is a u16 that
wraps 65535 -> 0. The host's cumulative ACK `_data_ack` (wire [40:42]) is
advanced by contiguity over those indices, but it is an unbounded Python int.

Once the index wraps, `(self._data_ack + 1) in self._cam_msgs` can never be
true again, so `_data_ack` FREEZES at 65535: the D field of every subsequent
host->cam ACK pins at 0xFFFF and stops crediting the camera's send window.
At the observed IO-message rate that is reachable in a long-lived session, and
nothing about the wire looks malformed while it happens.

`tutk/playback_engine/cuboai_pure.py` already carries the fix; this pins it in
the live copy. Same bug class as the `_idx_modular` fix that sits beside it.
"""

import importlib.util
import os
import struct

_TUTK = os.path.join(os.path.dirname(__file__), "..", "custom_components", "cuboai", "tutk")

_spec = importlib.util.spec_from_file_location("live_pure_dataack", os.path.join(_TUTK, "cuboai_pure.py"))
cp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cp)

WRAP = 1 << 16


def _io_frame(idx):
    """Minimal 68-byte camera DATA frame classified as a reliable IO/control response:
    dec[56:58] = message-index (LE u16); dec[58:64] == 0 is the _is_io_frame discriminator."""
    dec = bytearray(68)
    struct.pack_into("<H", dec, 56, idx & 0xFFFF)
    return bytes(dec)


def _fresh(wrap):
    """A bare session carrying only the attributes _note_cam_data's IO path touches."""
    s = object.__new__(cp.TUTKDirectSession)  # bypass __init__: no handshake, no socket
    s._dataack_wrap = bool(wrap)
    s._cam_msgs = set()
    s._data_ack = 0
    s._got_first = False
    return s


def _feed(s, start_idx, count):
    for k in range(count):
        s._note_cam_data(_io_frame((start_idx + k) % WRAP))


def test_wrap_on_crosses():
    s = _fresh(wrap=True)
    s._data_ack = 65529
    _feed(s, 65530, 11)  # idx 65530..65535 then 0..4 — crosses the wrap
    assert s._data_ack == 65540, f"WRAP ON failed to cross the wrap: _data_ack={s._data_ack}"
    assert s._data_ack & 0xFFFF == 4, "wire [40:42] should have wrapped to 0x0004"


def test_wrap_off_freezes():
    """Reproduces the bug, so the test proves the fix is what moves the needle."""
    s = _fresh(wrap=False)
    s._data_ack = 65529
    _feed(s, 65530, 11)
    assert s._data_ack == 65535, f"WRAP OFF should FREEZE at 65535, got {s._data_ack}"
    assert s._data_ack & 0xFFFF == 0xFFFF


def test_prewrap_is_byte_identical():
    """Below the wrap the fix must change nothing at all — same _data_ack sequence
    and the same bytes out of build_data_ack."""
    on, off = _fresh(wrap=True), _fresh(wrap=False)
    seq_on, seq_off = [], []
    for i in range(1, 400):
        on._note_cam_data(_io_frame(i))
        off._note_cam_data(_io_frame(i))
        seq_on.append(on._data_ack)
        seq_off.append(off._data_ack)
    assert seq_on == seq_off, "PRE-WRAP divergence between ON and OFF _data_ack sequences"
    assert on._data_ack == 399
    kw = {"R": 0x1234, "seq": 7, "relseq": 3, "ackord": 1, "C": 100, "D": 110, "sack": None}
    assert cp.build_data_ack(data_ack=on._data_ack, **kw) == cp.build_data_ack(data_ack=off._data_ack, **kw)


def test_consumed_indices_do_not_grow_without_bound():
    """The ON path discards consumed entries, so _cam_msgs stays small on a
    contiguous stream instead of retaining every index for the session's life."""
    s = _fresh(wrap=True)
    _feed(s, 1, 5000)
    assert s._data_ack == 5000
    assert len(s._cam_msgs) <= 1, f"_cam_msgs grew to {len(s._cam_msgs)} on a contiguous feed"


if __name__ == "__main__":
    for fn in sorted(k for k in dict(globals()) if k.startswith("test_")):
        globals()[fn]()
        print("ok:", fn)
