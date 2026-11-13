"""The streaming path must range-check the numeric CUBOAI_* knobs, not only the CLI.

`_clamp_env_knobs` lived in cuboai_validate and only that CLI called it. The go2rtc
exec entry point (cuboai_stream_video.apply_env_profile) skipped it, so a bad knob
(CUBOAI_GAP_DEPTH_CAP=0, a non-numeric interval, ...) reached get_session()
unchecked. CUBOAI_VERBOSE_INTERVAL was also parsed with a bare float() that raised
on garbage.

The clamp now lives in the streamer module, apply_env_profile calls it, and the
validate CLI imports the same function.
"""

import importlib.util
import os

import pytest

_TUTK = os.path.join(os.path.dirname(__file__), "..", "custom_components", "cuboai", "tutk")


def _load(name, fname):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_TUTK, fname))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sv = _load("live_stream_video_env_clamp", "cuboai_stream_video.py")


@pytest.fixture
def clean_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("CUBOAI_"):
            monkeypatch.delenv(k)
    return monkeypatch


def test_streamer_profile_clamps_out_of_range(clean_env, capsys):
    clean_env.setenv("CUBOAI_GAP_DEPTH_CAP", "0")
    clean_env.setenv("CUBOAI_STALL_S", "999")
    sv.apply_env_profile(False)
    assert os.environ["CUBOAI_GAP_DEPTH_CAP"] == "1"
    assert float(os.environ["CUBOAI_STALL_S"]) == 60
    assert "clamped" in capsys.readouterr().err


@pytest.mark.parametrize("knob,default", [("CUBOAI_VERBOSE_INTERVAL", 5.0), ("CUBOAI_LIST_PACE_S", 0.5)])
def test_streamer_profile_replaces_garbage(clean_env, knob, default):
    clean_env.setenv(knob, "fast")
    sv.apply_env_profile(False)
    assert float(os.environ[knob]) == default


def test_raw_profile_clamps_too(clean_env):
    clean_env.setenv("CUBOAI_GAP_DEPTH_CAP", "-5")
    sv.apply_env_profile(True)
    assert os.environ["CUBOAI_GAP_DEPTH_CAP"] == "1"


def test_valid_values_untouched(clean_env, capsys):
    clean_env.setenv("CUBOAI_GAP_DEPTH_CAP", "150")
    clean_env.setenv("CUBOAI_VERBOSE_INTERVAL", "2.5")
    sv.apply_env_profile(False)
    assert os.environ["CUBOAI_GAP_DEPTH_CAP"] == "150"
    assert os.environ["CUBOAI_VERBOSE_INTERVAL"] == "2.5"
    assert "Warning" not in capsys.readouterr().err


def test_verbose_interval_parse_tolerates_garbage(clean_env):
    clean_env.setenv("CUBOAI_VERBOSE_INTERVAL", "not-a-number")
    assert sv._env_float("CUBOAI_VERBOSE_INTERVAL", 5.0) == 5.0


def test_validate_cli_uses_the_same_clamp():
    src = open(os.path.join(_TUTK, "cuboai_validate.py"), encoding="utf-8").read()
    assert "def _clamp_env_knobs" not in src
    assert "from cuboai_stream_video import apply_env_profile, _clamp_env_knobs" in src
