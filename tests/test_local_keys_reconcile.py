"""Every value an entity reads must be one the poll actually writes.

The integration passes camera state around as a plain dict at
`coordinator.data["cameras"][id]["local"]`. Nothing checks that the key an
entity reads is the key the coordinator writes, so a typo or a never-implemented
poll is invisible: the entity just returns its fallback forever.

Two of these were live at once:

  * `status_led_on` — the Status LED switch read it; the poll writes
    `status_light_on`. The switch never reflected the camera and sat at False.
  * `brightness` — the Night Light Brightness number and the light entity read
    it; nothing anywhere wrote it, because no code called GET_LIGHT_STYLE. The
    number reported a hardcoded 100 forever.
  * `sleep_safety_raw` — the Sleep Safety sensor exposed it as an attribute;
    nothing wrote it, so the attribute was always None.

This test re-derives the read set and the written set from the source and fails
on any key that is read but never written.
"""

import os
import re

_CC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "custom_components", "cuboai")

_READ_PATTERNS = (
    r'get\(\s*"local"\s*,\s*\{\}\s*\)\s*\.get\(\s*"(\w+)"',
    r'\(\s*cam\.get\("local"\)\s*or\s*\{\}\s*\)\.get\(\s*"(\w+)"',
    r'\blocal\.get\(\s*"(\w+)"',
)
#: Only the coordinator's poll counts as a real write. An optimistic update after
#: a SET does put the key in the dict, but it never reconciles with the camera —
#: which is exactly how `status_led_on` hid: the switch wrote it on every toggle
#: and read it back, while the poll wrote `status_light_on` that nothing read.
_POLL_WRITE_PATTERN = r'\bdata\[\s*"(\w+)"\s*\]\s*='


def _sources():
    for name in sorted(os.listdir(_CC)):
        if name.endswith(".py"):
            yield name, open(os.path.join(_CC, name), encoding="utf-8").read()


def _collect(patterns):
    found = {}
    for name, text in _sources():
        for pat in patterns:
            for m in re.finditer(pat, text):
                found.setdefault(m.group(1), set()).add(name)
    return found


def test_no_entity_reads_a_key_the_poll_never_writes():
    reads = _collect(_READ_PATTERNS)
    polled = _collect((_POLL_WRITE_PATTERN,))
    orphans = {k: sorted(v) for k, v in reads.items()
               if k not in polled}
    assert not orphans, (
        "these keys are read by an entity but never written by the coordinator "
        "poll, so the entity never reconciles with the camera: "
        + ", ".join(f"{k} (read in {', '.join(v)})" for k, v in sorted(orphans.items()))
    )


def test_the_read_set_is_not_trivially_empty():
    """Guard the guard: if the patterns stop matching, the test above passes vacuously."""
    reads = _collect(_READ_PATTERNS)
    assert len(reads) > 30, f"only {len(reads)} local keys matched — the patterns have drifted"
    for expected in ("temperature", "humidity", "brightness", "status_light_on"):
        assert expected in reads, f"{expected} should be read by some entity"


if __name__ == "__main__":
    for fn in sorted(k for k in dict(globals()) if k.startswith("test_")):
        globals()[fn]()
        print("ok:", fn)
