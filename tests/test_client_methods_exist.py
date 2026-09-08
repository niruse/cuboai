"""Every `client.<method>()` the integration calls must exist on CuboAIClient.

`coordinator.py` called `client.get_lullaby_schedule()`, which was never defined
on `CuboAIClient`. Every poll raised AttributeError, the surrounding
`except Exception` swallowed it, and the old hardcoded `lullaby_volume = 50`
fallback made the result indistinguishable from a real reading — so the lullaby
volume Home Assistant displayed was never once the camera's, and nothing
anywhere said so.

A missing method is invisible in this codebase because almost every camera read
is wrapped in a broad except. This test re-derives both sets from the source, so
a typo or a getter that was planned but never written fails here instead of
silently degrading to a fallback for months.
"""

import ast
import os
import re

_CC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "custom_components", "cuboai")


def _client_methods():
    tree = ast.parse(open(os.path.join(_CC, "tutk", "cuboai_messages.py"), encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "CuboAIClient":
            return {f.name for f in node.body if isinstance(f, ast.FunctionDef)}
    raise AssertionError("CuboAIClient not found")


def _called():
    calls = {}
    for name in sorted(os.listdir(_CC)):
        if not name.endswith(".py"):
            continue
        text = open(os.path.join(_CC, name), encoding="utf-8").read()
        for m in re.finditer(r"\bclient\.(\w+)\s*\(", text):
            calls.setdefault(m.group(1), set()).add(name)
    return calls


def test_every_called_client_method_is_defined():
    have = _client_methods()
    missing = {k: sorted(v) for k, v in _called().items() if k not in have}
    assert not missing, (
        "these CuboAIClient methods are called but never defined, so every call raises "
        "AttributeError and is swallowed by the surrounding except: "
        + ", ".join(f"client.{k}() in {', '.join(v)}" for k, v in sorted(missing.items()))
    )


def test_the_call_set_is_not_trivially_empty():
    """Guard the guard — if the pattern stops matching, the check above passes vacuously."""
    calls = _called()
    assert len(calls) > 10, f"only {len(calls)} client calls matched — the pattern has drifted"
    assert "get_lullaby_schedule" in calls
    assert "get_hw_control" in calls


def test_get_lullaby_schedule_returns_the_volume_and_timer():
    """The specific method that was missing, exercised end to end off a synthetic response."""
    import importlib.util
    import struct
    import sys
    from unittest.mock import MagicMock

    spec = importlib.util.spec_from_file_location(
        "cuboai_messages_client_test", os.path.join(_CC, "tutk", "cuboai_messages.py")
    )
    msgs = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = msgs
    spec.loader.exec_module(msgs)

    raw = bytearray(144)
    struct.pack_into("<I", raw, 8, 1800)     # timer_mode: 30 minutes
    struct.pack_into("<I", raw, 12, 42)      # volume
    transport = MagicMock()
    transport.ioctl.return_value = (msgs.IOTYPE_USER_GET_LULLABY_SCHEDULE_RESP, bytes(raw))

    sched = msgs.CuboAIClient(transport).get_lullaby_schedule()
    assert sched["volume"] == 42
    assert sched["timer_mode"] == 1800
    assert sched["timer"] == "30 min"


def test_get_lullaby_schedule_rejects_a_mismatched_response_type():
    import importlib.util
    import sys
    from unittest.mock import MagicMock

    spec = importlib.util.spec_from_file_location(
        "cuboai_messages_client_test2", os.path.join(_CC, "tutk", "cuboai_messages.py")
    )
    msgs = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = msgs
    spec.loader.exec_module(msgs)

    transport = MagicMock()
    transport.ioctl.return_value = (9999, bytes(144))
    try:
        msgs.CuboAIClient(transport).get_lullaby_schedule()
    except ValueError:
        return
    raise AssertionError("a mismatched response type should raise")


if __name__ == "__main__":
    for fn in sorted(k for k in dict(globals()) if k.startswith("test_")):
        globals()[fn]()
        print("ok:", fn)
