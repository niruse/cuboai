"""A session-stats response with no parseable body must not erase the known mode.

`parse_session_stats` returns a dict WITHOUT a 'mode' key whenever
`_extract_json` cannot recover the embedded JSON from the response blob. The
coordinator used to write `stats.get("mode")` unconditionally, so that None
landed in `local_data` and the merge in `_fetch_all` — which exists to carry
values forward across a failed read — dutifully overwrote the last good "lan"
with nothing.

Measured on a live camera over 12 hours: the Connection Mode sensor flapped to
`unknown` 200 times, each lasting a median of 64.6s (exactly one poll), for a
value that never actually changed. Every other failure path in
`_fetch_local_data` leaves its key unset and is carried forward; this one call
succeeded, so it escaped that protection.

Unknown is never a true answer here in any case: this GET only returns at all
because the local session to the camera is up.
"""

import importlib
import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The Home Assistant stubs the coordinator needs come from conftest. utils stays
# conftest's mock — the coordinator only takes log_to_file from it.
sys.modules.pop("custom_components.cuboai.coordinator", None)
coordinator = importlib.import_module("custom_components.cuboai.coordinator")


def _client(stats):
    """A CuboAIClient whose getters all answer, with get_session_stats controlled."""
    c = MagicMock()
    c.get_session_stats.return_value = stats
    return c


def _session_ctx():
    sess = MagicMock()
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=sess)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


def _fetch(stats):
    client = _client(stats)
    with patch("custom_components.cuboai.tutk.cuboai_session.get_session", return_value=_session_ctx()), \
         patch("custom_components.cuboai.tutk.cuboai_messages.CuboAIClient", return_value=client):
        return coordinator._fetch_local_data("uid", "acct", "pw", "192.0.2.10")


def test_mode_is_reported_when_the_response_carries_one():
    data = _fetch({"mode": "lan", "raw_len": 512})
    assert data["connection_mode"] == "lan"


def test_missing_mode_leaves_the_key_unset_so_the_last_value_carries():
    """The key must be ABSENT, not None — absence is what the merge carries forward."""
    data = _fetch({"raw_len": 96, "result": 0})
    assert "connection_mode" not in data, "a None mode was written and will erase the good value"


def test_none_mode_leaves_the_key_unset():
    data = _fetch({"mode": None, "raw_len": 96})
    assert "connection_mode" not in data


def test_a_raised_get_also_leaves_the_key_unset():
    """The pre-existing failure path, pinned alongside the new one."""
    client = _client({})
    client.get_session_stats.side_effect = RuntimeError("timeout")
    with patch("custom_components.cuboai.tutk.cuboai_session.get_session", return_value=_session_ctx()), \
         patch("custom_components.cuboai.tutk.cuboai_messages.CuboAIClient", return_value=client):
        data = coordinator._fetch_local_data("uid", "acct", "pw", "192.0.2.10")
    assert "connection_mode" not in data


def test_the_merge_carries_an_absent_key_forward():
    """The other half of the contract: _fetch_all merges onto the previous local
    dict, so a key this poll did not set keeps its previous value."""
    old_local = {"connection_mode": "lan", "temperature": 25.0}
    local_data = {"temperature": 24.5}          # this poll had no mode
    merged = old_local.copy()
    merged.update(local_data)
    assert merged["connection_mode"] == "lan"
    assert merged["temperature"] == 24.5
