"""The embedded-JSON extractor must not be defeated by trailing binary.

Camera responses like GET_SESSION_STATS (0x0935) and GET_USER_LIST (0x0947) are
binary IOCTL blobs with a JSON object somewhere inside them. The extractor used
a greedy `\\{.*\\}` match, which spans from the FIRST '{' to the LAST '}'
anywhere in the blob — so a single stray 0x7D byte among the trailing counters
and padding swallowed everything after the real object and json.loads failed,
discarding the whole response.

Because the trailing bytes are counters, whether a 0x7D appeared there varied
poll to poll. On a live camera that showed up as the Connection Mode sensor
dropping to `unknown` for exactly one poll and recovering — roughly one poll in
three, 200 times over 12 hours.

Extraction is now brace-counted and string-aware, with the old greedy match kept
as a fallback so this can only ever parse more responses than before, never
fewer.
"""

import importlib.util
import json
import os
import re
import sys

_TUTK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "custom_components", "cuboai", "tutk")

_spec = importlib.util.spec_from_file_location("cuboai_messages_under_test",
                                               os.path.join(_TUTK, "cuboai_messages.py"))
msgs = importlib.util.module_from_spec(_spec)
# Registered before exec: the module defines @dataclass types, and dataclasses
# resolves annotations through sys.modules[cls.__module__].
sys.modules[_spec.name] = msgs
_spec.loader.exec_module(msgs)


def _greedy(raw: bytes):
    """The previous implementation, for contrast."""
    m = re.search(rb"\{.*\}", raw, re.S)
    if not m:
        return None
    txt = m.group(0).decode("latin1", "replace")
    txt = re.sub(r':\s*(\d{1,3}(?:\.\d{1,3}){3})\s*([,}\]])', r': "\1"\2', txt)
    try:
        return json.loads(txt)
    except Exception:
        return None


HEADER = bytes(8)
BODY = b'{"mode":"lan","nat":2,"ip":192.168.1.124,"session_id":3}'


def test_plain_embedded_object_still_parses():
    got = msgs._extract_json(HEADER + BODY)
    assert got["mode"] == "lan"
    assert got["ip"] == "192.168.1.124", "the bare dotted-quad repair must still apply"


def test_trailing_binary_with_a_stray_close_brace():
    """The failure mode, reproduced: 0x7D among the trailing counters."""
    blob = HEADER + BODY + bytes([0x00, 0x11, 0x7D, 0x00, 0x04])
    assert _greedy(blob) is None, "the old extractor should fail here (that was the bug)"
    got = msgs._extract_json(blob)
    assert got is not None and got["mode"] == "lan"


def test_trailing_binary_without_a_stray_brace_is_unaffected():
    """The other two-thirds of polls, which always worked."""
    blob = HEADER + BODY + bytes([0x00, 0x11, 0x22, 0x00])
    assert _greedy(blob) is not None
    assert msgs._extract_json(blob)["mode"] == "lan"


def test_a_brace_inside_a_string_does_not_close_the_object():
    body = b'{"ssid":"my}wifi","mode":"lan"}'
    got = msgs._extract_json(HEADER + body + b"\x7d\x00")
    assert got == {"ssid": "my}wifi", "mode": "lan"}


def test_an_escaped_quote_inside_a_string_is_handled():
    body = rb'{"name":"a\"b}c","mode":"lan"}'
    got = msgs._extract_json(HEADER + body + b"\x00\x7d")
    assert got["mode"] == "lan"


def test_nested_objects_are_spanned_whole():
    body = b'{"a_frame":[{"frm_count":12}],"mode":"lan"}'
    got = msgs._extract_json(HEADER + body + b"\x7d")
    assert got["mode"] == "lan"
    assert got["a_frame"][0]["frm_count"] == 12


def test_first_object_wins_when_the_blob_holds_two():
    blob = HEADER + b'{"mode":"lan"}' + bytes(4) + b'{"mode":"relay"}'
    assert msgs._extract_json(blob)["mode"] == "lan"


def test_truncated_object_returns_none():
    assert msgs._extract_json(HEADER + b'{"mode":"la') is None


def test_no_json_at_all_returns_none():
    assert msgs._extract_json(bytes(64)) is None


def test_session_stats_recovers_the_mode_through_the_parser():
    """End to end: the symptom that started this was parse_session_stats
    returning a dict with no 'mode' key."""
    blob = HEADER + BODY + bytes([0x7D, 0x00, 0x03])
    parsed = msgs.parse_session_stats(blob)
    assert parsed["mode"] == "lan"
    assert parsed["ip"] == "192.168.1.124"


if __name__ == "__main__":
    for fn in sorted(k for k in dict(globals()) if k.startswith("test_")):
        globals()[fn]()
        print("ok:", fn)
