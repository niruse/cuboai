"""VAAPI settings, discovery and fallback tests."""

import json
import stat
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
import yaml

from custom_components.cuboai import config_flow as cf
from custom_components.cuboai import go2rtc as g2
from custom_components.cuboai.const import (
    DOMAIN,
    H264_ENCODERS,
    H264_KEYINT,
    OPT_H264_ENCODER,
    OPT_H264_RESOLUTION,
    OPT_VAAPI_DEVICE,
    h264_encoder,
    live_stream_name,
)

HEVC_CAMERA = "SW05TEST"
SECOND_CAMERA = "CB02TEST"
VAAPI_DEVICE_DEFAULT = "/dev/dri/renderD128"


@pytest.fixture
def manager(tmp_path, monkeypatch):
    hass = MagicMock()
    hass.data = {}

    async def run_executor(function, *args):
        return function(*args)

    hass.async_add_executor_job = AsyncMock(side_effect=run_executor)
    instance = g2.Go2RTCManager(hass)
    instance._config_path = str(tmp_path / "go2rtc.yaml")
    instance._cameras = [{"device_id": HEVC_CAMERA}, {"device_id": SECOND_CAMERA}]
    instance._options = {"h264_cameras": [HEVC_CAMERA], OPT_VAAPI_DEVICE: VAAPI_DEVICE_DEFAULT}
    monkeypatch.setattr(g2, "_FPS_FLAG", g2.FPS_MODE_PASSTHROUGH)
    return instance


def source(manager):
    return manager._streams[f"cuboai_h264_{HEVC_CAMERA}"][0]


async def configuration(manager):
    await manager._generate_config()
    return yaml.safe_load(Path(manager._config_path).read_text())


@pytest.mark.parametrize("encoder", [None, "software", "unknown", 1, []])
def test_existing_or_invalid_encoder_uses_software(encoder):
    assert h264_encoder({OPT_H264_ENCODER: encoder}) == "software"
    assert h264_encoder(None) == "software"


async def test_default_does_not_probe_or_change_software_pipeline(manager):
    with patch.object(g2, "_probe_vaapi") as probe:
        await manager._resolve_codecs()
    probe.assert_not_called()
    assert "-x264-params level=4.0:keyint=15" in source(manager)
    assert "#hardware=" not in source(manager)
    assert "ffmpeg" not in await configuration(manager)


async def test_vaapi_is_not_probed_without_a_selected_transcoding_camera(manager):
    manager._options = {OPT_H264_ENCODER: "vaapi", "h264_cameras": ["removed-camera"]}
    with patch.object(g2, "_probe_vaapi") as probe:
        await manager._resolve_codecs()
    probe.assert_not_called()
    assert not manager._vaapi_enabled
    assert not any(name.startswith("cuboai_h264_") for name in manager._streams)


@pytest.mark.parametrize("resolution,width,height", [("1080p", 1920, 1080), ("720p", 1280, 720)])
async def test_vaapi_preserves_name_size_cadence_and_single_camera_source(manager, resolution, width, height):
    manager._options.update({OPT_H264_ENCODER: "vaapi", OPT_H264_RESOLUTION: resolution})
    with patch.object(g2, "_probe_vaapi", return_value=True) as probe:
        await manager._resolve_codecs()
    probe.assert_called_once_with(VAAPI_DEVICE_DEFAULT, resolution)
    assert manager.hass.async_add_executor_job.await_count == 1
    hardware_source = source(manager)
    assert hardware_source.startswith(f"ffmpeg:cuboai_combined_{HEVC_CAMERA}#video=h264#audio=aac#timeout=20")
    assert f"#width='min({width},iw)'#height='min({height},ih)'" in hardware_source
    assert "force_original_aspect_ratio=decrease:force_divisible_by=2" in hardware_source
    assert "#hardware=vaapi#input=cuboai_vaapi#raw=-fps_mode passthrough" in hardware_source
    assert "-vf" not in hardware_source
    assert "x264" not in hardware_source
    assert live_stream_name(HEVC_CAMERA, manager._options) == f"cuboai_h264_{HEVC_CAMERA}"
    assert f"cuboai_h264_{SECOND_CAMERA}" not in manager._streams
    assert not any(name.startswith("cuboai_h264_vaapi_") for name in manager._streams)
    assert sum("cuboai_stream_video.py" in entry for entry in manager._streams[f"cuboai_combined_{HEVC_CAMERA}"]) == 1
    assert len(manager._streams[f"cuboai_h264_{HEVC_CAMERA}"]) == 1
    assert not any(
        "vaapi" in entry
        for name, entries in manager._streams.items()
        if name != f"cuboai_h264_{HEVC_CAMERA}"
        for entry in entries
    )


async def test_one_preflight_per_manager_not_per_camera(manager):
    manager._options.update({OPT_H264_ENCODER: "vaapi", "h264_cameras": [HEVC_CAMERA, SECOND_CAMERA]})
    with patch.object(g2, "_probe_vaapi", return_value=True) as probe:
        await manager._resolve_codecs()
    probe.assert_called_once()
    assert all(
        "#hardware=vaapi" in manager._streams[f"cuboai_h264_{camera}"][0] for camera in (HEVC_CAMERA, SECOND_CAMERA)
    )


async def test_vaapi_keeps_the_older_ffmpeg_cadence_flag(manager, monkeypatch):
    manager._options[OPT_H264_ENCODER] = "vaapi"
    monkeypatch.setattr(g2, "_FPS_FLAG", g2.VSYNC_PASSTHROUGH)
    with patch.object(g2, "_probe_vaapi", return_value=True):
        await manager._resolve_codecs()
    assert "#raw=-vsync passthrough" in source(manager)


@pytest.mark.parametrize("failure", [False, OSError("executor failed")])
async def test_failed_preflight_falls_back_without_hardware_arguments(manager, failure, caplog):
    manager._options[OPT_H264_ENCODER] = "vaapi"
    with patch.object(
        g2, "_probe_vaapi", side_effect=failure if isinstance(failure, Exception) else None, return_value=False
    ):
        await manager._resolve_codecs()
    assert not manager._vaapi_enabled
    assert "-x264-params" in source(manager)
    assert "#hardware=" not in source(manager)
    assert "ffmpeg" not in await configuration(manager)
    assert "using software encoding" in caplog.text
    assert manager._options[OPT_H264_ENCODER] == "vaapi"


async def test_failed_later_probe_resets_a_previous_hardware_selection(manager):
    manager._options[OPT_H264_ENCODER] = "vaapi"
    with patch.object(g2, "_probe_vaapi", side_effect=[True, False]):
        await manager._resolve_codecs()
        assert manager._vaapi_enabled
        await manager._resolve_codecs()
    assert not manager._vaapi_enabled
    assert "#hardware=" not in source(manager)
    assert "ffmpeg" not in await configuration(manager)


async def test_device_is_scoped_to_hardware_input_and_nvr_credentials_survive(manager):
    manager._options.update(
        {
            OPT_H264_ENCODER: "vaapi",
            OPT_VAAPI_DEVICE: "/dev/dri/renderD129",
            "nvr_enabled": True,
            "nvr_username": "recorder",
            "nvr_password": "private-test",
        }
    )
    with patch.object(g2, "_probe_vaapi", return_value=True):
        await manager._resolve_codecs()
    config = await configuration(manager)
    hardware = config["ffmpeg"]
    assert "global" not in hardware
    assert hardware["cuboai_vaapi"].startswith("-vaapi_device /dev/dri/renderD129 ")
    assert "-timeout {timeout}" in hardware["cuboai_vaapi"]
    assert hardware["cuboai_vaapi"].endswith("-i {input}")
    assert f"-g {H264_KEYINT}" in hardware["h264/vaapi"]
    assert "-level:v 4.0" in hardware["h264/vaapi"]
    assert "-bf 0" in hardware["h264/vaapi"]
    assert config["rtsp"]["username"] == "recorder"
    assert config["rtsp"]["password"] == "private-test"


@pytest.fixture
def vaapi_environment(monkeypatch):
    monkeypatch.setattr(g2.sys, "platform", "linux")
    monkeypatch.setattr(g2.os, "access", lambda *args: True)


@pytest.mark.parametrize("device", ["/dev/dri/renderD128;touch /tmp/test", "/dev/dri/renderD128\n", "", None])
def test_unsafe_or_invalid_render_device_never_reaches_ffmpeg(device, vaapi_environment):
    with patch.object(g2.subprocess, "run") as run:
        assert not g2._probe_vaapi(device, "1080p")
    run.assert_not_called()


def test_non_linux_host_does_not_probe(monkeypatch, vaapi_environment):
    monkeypatch.setattr(g2.sys, "platform", "darwin")
    with patch.object(g2.subprocess, "run") as run:
        assert not g2._probe_vaapi(VAAPI_DEVICE_DEFAULT, "1080p")
    run.assert_not_called()


def test_missing_or_inaccessible_device_does_not_probe(monkeypatch, vaapi_environment):
    monkeypatch.setattr(g2.os, "access", lambda *args: False)
    with patch.object(g2.subprocess, "run") as run:
        assert not g2._probe_vaapi(VAAPI_DEVICE_DEFAULT, "1080p")
    run.assert_not_called()


@pytest.mark.parametrize(
    "failure", [FileNotFoundError(), subprocess.TimeoutExpired("ffmpeg", 10), subprocess.SubprocessError()]
)
def test_missing_ffmpeg_or_probe_timeout_is_a_clean_failure(vaapi_environment, failure):
    with patch.object(g2.subprocess, "run", side_effect=failure):
        assert not g2._probe_vaapi(VAAPI_DEVICE_DEFAULT, "1080p")


@pytest.mark.parametrize("returncode,ready", [(0, True), (1, False)])
def test_preflight_requires_real_encoding_success_and_is_bounded(vaapi_environment, returncode, ready):
    with patch.object(g2.subprocess, "run", return_value=MagicMock(returncode=returncode)) as run:
        assert g2._probe_vaapi(VAAPI_DEVICE_DEFAULT, "720p") is ready
    arguments = run.call_args.args[0]
    assert isinstance(arguments, list)
    assert arguments[arguments.index("-vaapi_device") + 1] == VAAPI_DEVICE_DEFAULT
    assert arguments[arguments.index("-c:v") + 1] == "h264_vaapi"
    assert arguments[arguments.index("-frames:v") + 1] == "1"
    assert arguments[arguments.index("-i") + 1] == "color=size=128x128:rate=1"
    assert "min(1280,iw)" in arguments[arguments.index("-vf") + 1]
    assert "force_original_aspect_ratio=decrease" in arguments[arguments.index("-vf") + 1]
    assert run.call_args.kwargs["timeout"] == 10
    assert not run.call_args.kwargs.get("shell", False)


def options_flow(saved=None, devices=None):
    flow = cf.CuboAIOptionsFlowHandler()
    flow.hass = MagicMock()
    flow.hass.data = {DOMAIN: {}}
    available = [VAAPI_DEVICE_DEFAULT, "/dev/dri/renderD129"] if devices is None else devices

    async def run_executor(function, *args):
        return available if function is g2.vaapi_devices else function(*args)

    flow.hass.async_add_executor_job = AsyncMock(side_effect=run_executor)
    flow.hass.config_entries.async_entries = lambda domain: []
    flow.config_entry = MagicMock(entry_id="entryA", options=saved or {}, data={"cameras": []})
    flow.async_show_form = lambda **kwargs: kwargs
    flow.async_create_entry = lambda **kwargs: kwargs
    return flow


@pytest.mark.parametrize(
    "saved,encoder,device",
    [
        ({}, "software", VAAPI_DEVICE_DEFAULT),
        ({OPT_H264_ENCODER: "vaapi", OPT_VAAPI_DEVICE: "/dev/dri/renderD129"}, "vaapi", "/dev/dri/renderD129"),
    ],
)
async def test_configure_offers_and_remembers_encoder_and_device(saved, encoder, device):
    flow = options_flow(saved)
    result = await flow.async_step_init()
    schema = result["data_schema"].schema
    keys = {str(key): key for key in schema}
    assert keys[OPT_H264_ENCODER].default() == encoder
    assert keys[OPT_VAAPI_DEVICE].default() == device
    assert schema[keys[OPT_H264_ENCODER]].container == H264_ENCODERS
    validator = schema[keys[OPT_VAAPI_DEVICE]]
    assert validator.container == [VAAPI_DEVICE_DEFAULT, "/dev/dri/renderD129"]
    assert validator("/dev/dri/renderD129") == "/dev/dri/renderD129"
    with pytest.raises(vol.Invalid):
        validator("/dev/dri/renderD128#raw=-other")
    with pytest.raises(vol.Invalid):
        validator("/dev/dri/renderD999")


@pytest.mark.parametrize("saved", [{}, {OPT_H264_ENCODER: "vaapi", OPT_VAAPI_DEVICE: VAAPI_DEVICE_DEFAULT}])
async def test_no_gpu_disables_vaapi_and_hides_device_without_changing_saved_options(saved):
    flow = options_flow(saved.copy(), devices=[])
    result = await flow.async_step_init()
    schema = result["data_schema"].schema
    keys = {str(key): key for key in schema}
    assert keys[OPT_H264_ENCODER].default() == "software"
    assert schema[keys[OPT_H264_ENCODER]].container == {"software": "Software (CPU)"}
    assert OPT_VAAPI_DEVICE not in keys
    assert flow.config_entry.options == saved
    flow.hass.async_add_executor_job.assert_awaited_once_with(g2.vaapi_devices)


async def test_first_detected_device_is_not_assumed_to_be_render128():
    flow = options_flow(devices=["/dev/dri/renderD130"])
    result = await flow.async_step_init()
    keys = {str(key): key for key in result["data_schema"].schema}
    assert keys[OPT_VAAPI_DEVICE].default() == "/dev/dri/renderD130"
    assert keys[OPT_H264_ENCODER].default() == "software"


async def test_missing_saved_device_does_not_implicitly_enable_a_different_gpu():
    saved = {OPT_H264_ENCODER: "vaapi", OPT_VAAPI_DEVICE: "/dev/dri/renderD129"}
    flow = options_flow(saved, devices=[VAAPI_DEVICE_DEFAULT])
    result = await flow.async_step_init()
    keys = {str(key): key for key in result["data_schema"].schema}
    assert keys[OPT_H264_ENCODER].default() == "software"
    assert flow.config_entry.options == saved


@pytest.mark.parametrize(
    "devices,device,field,error",
    [
        ([], VAAPI_DEVICE_DEFAULT, OPT_H264_ENCODER, "vaapi_unavailable"),
        ([VAAPI_DEVICE_DEFAULT], "/dev/dri/renderD129", OPT_VAAPI_DEVICE, "vaapi_device_unavailable"),
        ([VAAPI_DEVICE_DEFAULT], None, OPT_VAAPI_DEVICE, "vaapi_device_unavailable"),
    ],
)
async def test_submission_rechecks_gpu_access(devices, device, field, error):
    flow = options_flow(devices=devices)
    result = await flow.async_step_init({OPT_H264_ENCODER: "vaapi", OPT_VAAPI_DEVICE: device})
    assert result["errors"][field] == error
    assert "data" not in result


async def test_software_can_be_saved_without_a_gpu_and_preserves_saved_device():
    flow = options_flow({OPT_VAAPI_DEVICE: VAAPI_DEVICE_DEFAULT}, devices=[])
    result = await flow.async_step_init({OPT_H264_ENCODER: "software", "nvr_password": "keep-private"})
    assert result["data"][OPT_H264_ENCODER] == "software"
    assert result["data"][OPT_VAAPI_DEVICE] == VAAPI_DEVICE_DEFAULT
    assert result["data"]["nvr_password"] == "keep-private"


def test_discovery_filters_permissions_names_and_non_devices_and_sorts_numerically(monkeypatch):
    monkeypatch.setattr(g2.sys, "platform", "linux")
    candidates = [
        "/dev/dri/renderD130",
        "/dev/dri/renderD129",
        "/dev/dri/renderD128",
        "/dev/dri/renderD99",
        "/dev/dri/renderD131",
        "/dev/dri/renderDx",
        "/tmp/renderD128",
    ]
    monkeypatch.setattr(g2.glob, "glob", lambda pattern: candidates)

    original_stat = g2.os.stat

    def device_stat(device, *args, **kwargs):
        if device not in candidates:
            return original_stat(device, *args, **kwargs)
        if device.endswith("131"):
            raise FileNotFoundError
        return MagicMock(st_mode=stat.S_IFREG if device.endswith("129") else stat.S_IFCHR)

    monkeypatch.setattr(g2.os, "stat", device_stat)
    monkeypatch.setattr(g2.os, "access", lambda device, mode: not device.endswith("128"))
    assert g2.vaapi_devices() == ["/dev/dri/renderD99", "/dev/dri/renderD130"]


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_discovery_on_non_linux_never_accesses_devices(monkeypatch, platform):
    monkeypatch.setattr(g2.sys, "platform", platform)
    with patch.object(g2.glob, "glob") as scan:
        assert g2.vaapi_devices() == []
    scan.assert_not_called()


@pytest.mark.parametrize("failure", [FileNotFoundError, PermissionError])
def test_discovery_fails_closed_when_directory_is_unavailable(monkeypatch, failure):
    monkeypatch.setattr(g2.sys, "platform", "linux")
    with patch.object(g2.glob, "glob", side_effect=failure):
        assert g2.vaapi_devices() == []


async def test_saved_vaapi_without_device_falls_back_without_guessing_a_path(manager):
    manager._options[OPT_H264_ENCODER] = "vaapi"
    manager._options.pop(OPT_VAAPI_DEVICE)
    with patch.object(g2, "_probe_vaapi", return_value=False) as probe:
        await manager._resolve_codecs()
    probe.assert_called_once_with("", "1080p")
    assert not manager._vaapi_enabled
    assert "x264" in source(manager)


async def test_options_save_keeps_credentials_and_camera_transcode_selection():
    flow = options_flow()
    values = {
        OPT_H264_ENCODER: "vaapi",
        OPT_VAAPI_DEVICE: VAAPI_DEVICE_DEFAULT,
        "h264_cameras": [HEVC_CAMERA],
        "nvr_enabled": True,
        "nvr_username": "recorder",
        "nvr_password": "private-test",
    }
    result = await flow.async_step_init(values.copy())
    for key, value in values.items():
        assert result["data"][key] == value


def test_encoder_fields_have_labels_and_help_in_both_english_resources():
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            assert key not in result, f"Duplicate translation key: {key}"
            result[key] = value
        return result

    for filename in ("strings.json", "translations/en.json"):
        content = json.loads((Path("custom_components/cuboai") / filename).read_text(), object_pairs_hook=unique_keys)
        fields = content["options"]["step"]["init"]
        for option in (OPT_H264_ENCODER, OPT_VAAPI_DEVICE):
            assert fields["data"][option]
            assert fields["data_description"][option]
        for error in ("vaapi_unavailable", "vaapi_device_unavailable", "rtsp_port_in_use"):
            assert content["options"]["error"][error]
