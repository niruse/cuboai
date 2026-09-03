"""The client fingerprint must not collapse to a shared constant on a NIC-less host.

`_AV_MID` (the 6-byte client fingerprint in the probe and AV/DATA plaintext) is
derived from the host NIC MAC. When every lookup fails — a network-isolated
container with no NIC, which is a plausible shape for a Home Assistant add-on —
the old code returned a hard-coded `000000000000`, so every such host presented
the same fingerprint to its camera.

The camera does not validate this value, so this is hardening rather than a
crash fix; a random fallback simply keeps distinct hosts distinct.
"""

import importlib.util
import os

_TUTK = os.path.join(os.path.dirname(__file__), "..", "custom_components", "cuboai", "tutk")

_spec = importlib.util.spec_from_file_location("live_pure_avmid", os.path.join(_TUTK, "cuboai_pure.py"))
cp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cp)


def test_normal_path_still_derives_from_the_nic_mac():
    """On a host that has a NIC, nothing about this changes."""
    mid = cp.compute_av_mid()
    assert isinstance(mid, bytes) and len(mid) == 6
    assert mid == cp.compute_av_mid(), "the MAC-derived fingerprint must be stable"


def test_fallback_is_random_not_a_shared_constant(monkeypatch=None):
    saved = (cp._local_mac_via_getifaddrs, cp._local_mac_via_sysfs, cp._local_mac_via_uuid)
    try:
        cp._local_mac_via_getifaddrs = lambda: None
        cp._local_mac_via_sysfs = lambda: None
        cp._local_mac_via_uuid = lambda: None
        a = cp.compute_av_mid()
        b = cp.compute_av_mid()
        assert len(a) == len(b) == 6
        assert a != bytes(6), "the fallback must not be the old all-zero constant"
        assert a != b, "two NIC-less hosts must not present the same fingerprint"
    finally:
        (cp._local_mac_via_getifaddrs, cp._local_mac_via_sysfs, cp._local_mac_via_uuid) = saved


def test_no_all_zero_constant_remains():
    src = open(os.path.join(_TUTK, "cuboai_pure.py"), encoding="utf-8").read()
    assert "_AVMID_FALLBACK" not in src


if __name__ == "__main__":
    for fn in sorted(k for k in dict(globals()) if k.startswith("test_")):
        globals()[fn]()
        print("ok:", fn)
