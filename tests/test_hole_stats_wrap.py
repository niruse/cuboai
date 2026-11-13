"""A recovered hole must not be counted as "recovered" again 65536 fragments later.

_hole_req_ts kept an entry for every hole ever requested: never removed when the hole filled,
and the only prune deleted the RECENT entries and kept the stale ones. When the frag-seq came
round again, an ordinary first arrival of that number counted as a recovered resend
(recovery_pct 200 % reproduced) and a new hole there was not counted as lost, so the health
figures improved with uptime. Entries are now popped on arrival and pruned to the live window.

Wrap-crossing: drives _note_cam_data through one full trip round the u16 counter.
"""

import importlib.util
import os
import struct

_TUTK = os.path.join(os.path.dirname(__file__), "..", "custom_components", "cuboai", "tutk")
_spec = importlib.util.spec_from_file_location("live_pure_hole_stats", os.path.join(_TUTK, "cuboai_pure.py"))
cp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cp)


def _av(frag):
    d = bytearray(68)
    struct.pack_into("<H", d, 46, frag & 0xFFFF)
    d[58:64] = b"\x01\x00\x00\x00\x00\x00"  # non-zero = AV fragment, not an IO frame
    return bytes(d)


def test_recovered_hole_is_popped_and_not_recounted_after_a_wrap():
    s = cp.TUTKDirectSession("127.0.0.1")
    for f in range(100):
        s._note_cam_data(_av(f))
    s._hole_req_ts[100] = 1.0  # frag 100 was lost and resend-requested
    for f in range(101, 120):
        s._note_cam_data(_av(f))
    s._note_cam_data(_av(100))  # the resend arrives
    assert s._stat_resend_recovered == 1
    assert 100 not in s._hole_req_ts

    for f in range(120, 65536 + 200):  # a loss-free trip round the counter, past 100 again
        s._note_cam_data(_av(f))
    assert s._stat_resend_recovered == 1  # was 2: the ordinary arrival of 100 counted as "recovered"
