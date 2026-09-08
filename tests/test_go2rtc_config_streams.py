"""go2rtc.yaml is regenerated, and its stream set must be exactly ours.

`_generate_config` builds `config` from scratch and `_write` truncates the file,
so the written stream set is whatever `self._streams` holds — nothing is carried
over from the previous file.

The code used to claim otherwise: a comment described the file as "merged into,
not replaced", and a loop pruned stale `cuboai_*` entries from
`config["streams"]`. That key was always empty when the loop ran, so it could
never do anything. These tests pin the behaviour that is actually wanted —
regeneration with no leftovers — so a future attempt to reintroduce merging has
to confront the stale-entry question deliberately.
"""

import importlib.util
import os
import sys
from types import ModuleType
from unittest.mock import MagicMock

import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_go2rtc():
    for name in ("homeassistant.components", "homeassistant.helpers.aiohttp_client"):
        sys.modules.setdefault(name, ModuleType(name))
    path = os.path.join(_ROOT, "custom_components", "cuboai", "go2rtc.py")
    spec = importlib.util.spec_from_file_location("custom_components.cuboai.go2rtc", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


go2rtc = _load_go2rtc()


class _Hass:
    """Just enough hass for _generate_config: it only uses the executor."""

    def __init__(self):
        self.data = {}

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


def _manager(tmp_path, streams, options=None):
    m = object.__new__(go2rtc.Go2RTCManager)
    m.hass = _Hass()
    m._config_path = str(tmp_path / "go2rtc.yaml")
    m._options = options or {}
    m._streams = streams
    m._rtsp_port = 8557
    m._webrtc_port = 8556
    m._api_port = 1985
    m._LOGGER = MagicMock()
    return m


async def _write_config(tmp_path, streams, options=None):
    m = _manager(tmp_path, streams, options)
    await m._generate_config()
    with open(m._config_path) as f:
        return yaml.safe_load(f)


async def test_written_streams_are_exactly_the_generated_set(tmp_path):
    streams = {"cuboai_combined_A": "exec:one", "cuboai_dvr_A": "exec:two"}
    cfg = await _write_config(tmp_path, streams)
    assert cfg["streams"] == streams


async def test_a_stream_we_no_longer_generate_does_not_survive(tmp_path):
    """The regression the removed loop was aiming at, asserted end to end."""
    first = await _write_config(tmp_path, {"cuboai_combined_A": "exec:one", "cuboai_old_A": "exec:stale"})
    assert "cuboai_old_A" in first["streams"]

    second = await _write_config(tmp_path, {"cuboai_combined_A": "exec:one"})
    assert "cuboai_old_A" not in second["streams"], "a dropped stream survived regeneration"
    assert second["streams"] == {"cuboai_combined_A": "exec:one"}


async def test_ports_are_written_from_the_resolved_values(tmp_path):
    cfg = await _write_config(tmp_path, {})
    assert cfg["api"]["listen"] == ":1985"
    assert cfg["rtsp"]["listen"] == ":8557"
    assert cfg["webrtc"]["listen"] == ":8556"


async def test_nvr_credentials_are_written_only_when_both_are_set(tmp_path):
    cfg = await _write_config(tmp_path, {}, {"nvr_enabled": True, "nvr_password": "s3cret"})
    assert cfg["rtsp"]["username"] == "cuboai"
    assert cfg["rtsp"]["password"] == "s3cret"

    cfg = await _write_config(tmp_path, {}, {"nvr_enabled": True})
    assert "password" not in cfg["rtsp"]
