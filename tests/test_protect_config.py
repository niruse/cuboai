"""UniFi Protect settings: the first-run step, Configure, validation, labels,
and what the diagnostics download reveals about them. Every test names the
mutation it kills."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.cuboai import config_flow as cf
from custom_components.cuboai.const import (
    DESIRED_ONVIF_PORT,
    DOMAIN,
    OPT_PROTECT_CAMERAS,
    OPT_PROTECT_ENABLED,
    OPT_PROTECT_PASSWORD,
    OPT_PROTECT_PORT,
    OPT_PROTECT_USERNAME,
)

DEV_A, DEV_B = "CB02AAAA00000001", "CB02BBBB00000002"
CAMS = [{"device_id": DEV_A, "baby_name": "A"}, {"device_id": DEV_B, "baby_name": "B"}]
KEYS = (OPT_PROTECT_ENABLED, OPT_PROTECT_CAMERAS, OPT_PROTECT_USERNAME, OPT_PROTECT_PASSWORD, OPT_PROTECT_PORT)


def _hass(other_entries=()):
    hass = MagicMock()
    hass.data = {DOMAIN: {}}

    async def _run(func, *args):
        return func(*args)

    hass.async_add_executor_job = AsyncMock(side_effect=_run)
    hass.config_entries.async_entries = lambda domain: list(other_entries)
    return hass


def _schema_keys(schema: dict) -> dict:
    return {str(k): k for k in schema}


# =============================================================================
# The fields
# =============================================================================


@pytest.mark.asyncio
async def test_configure_offers_every_protect_field_switched_off():
    """Off by default: it opens a port and answers discovery on the LAN.
    Kill: a field removed from Configure, or the default flipped on."""
    hass = _hass()
    entry = MagicMock(entry_id="entryA", options={}, data={"cameras": CAMS, "all_cameras": CAMS})
    flow = cf.CuboAIOptionsFlowHandler()
    flow.hass, flow.config_entry = hass, entry
    flow.async_show_form = lambda **kw: kw
    with patch.object(cf, "setup_file_logger", MagicMock()):
        result = await flow.async_step_init()

    keys = _schema_keys(result["data_schema"].schema)
    assert all(k in keys for k in KEYS), sorted(set(KEYS) - set(keys))
    assert keys[OPT_PROTECT_ENABLED].default() is False
    assert keys[OPT_PROTECT_PORT].default() == DESIRED_ONVIF_PORT == 8899
    assert keys[OPT_PROTECT_CAMERAS].default() == [DEV_A]


def test_first_run_offers_the_switch_and_credentials_only():
    """The picker and the port belong in Configure. Kill: full=False ignored."""
    keys = _schema_keys(cf._protect_schema({}, CAMS, full=False))
    assert set(keys) == {OPT_PROTECT_ENABLED, OPT_PROTECT_USERNAME, OPT_PROTECT_PASSWORD}


def test_the_password_field_can_be_cleared():
    """Like nvr_password: suggested_value, not default, or a cleared field
    could never be saved. Kill: default= used for the password."""
    key = _schema_keys(cf._protect_schema({OPT_PROTECT_PASSWORD: "x"}, CAMS, full=True))[OPT_PROTECT_PASSWORD]
    assert key.description == {"suggested_value": "x"}
    import voluptuous as vol

    assert key.default is vol.UNDEFINED, "a default would re-insert the old password when the field is cleared"


# =============================================================================
# Validation
# =============================================================================


async def _errors(hass, user_input, entry_id="entryA", previous=None, cameras=CAMS):
    return (await cf._protect_plan(hass, previous or {}, user_input, cameras, entry_id))[0]


def _on(**kw):
    return {OPT_PROTECT_ENABLED: True, OPT_PROTECT_PASSWORD: "pw", OPT_PROTECT_PORT: 8899, "rtsp_port": 8557, **kw}


@pytest.mark.asyncio
async def test_switched_off_needs_nothing():
    """Kill: validation running while the feature is off."""
    assert await _errors(_hass(), {OPT_PROTECT_ENABLED: False}, "entryA") == {}


@pytest.mark.asyncio
async def test_a_password_is_required():
    """Protect will not adopt without one. Kill: the check removed."""
    with patch("custom_components.cuboai.go2rtc._port_bindable", return_value=True):
        errors = await _errors(_hass(), _on(**{OPT_PROTECT_PASSWORD: "  "}), "entryA")
    assert errors == {OPT_PROTECT_PASSWORD: "unifi_password_required"}


@pytest.mark.asyncio
async def test_a_taken_port_is_refused():
    """Kill: the bindability check removed."""
    with patch("custom_components.cuboai.go2rtc._port_bindable", return_value=False):
        errors = await _errors(_hass(), _on(), "entryA")
    assert errors == {OPT_PROTECT_PORT: "onvif_port_in_use"}


@pytest.mark.asyncio
async def test_the_rtsp_port_cannot_double_as_the_protect_port():
    """Nothing is bound yet on first run, so only a direct comparison catches
    it. Kill: the rtsp_port comparison removed."""
    with patch("custom_components.cuboai.go2rtc._port_bindable", return_value=True):
        errors = await _errors(_hass(), _on(**{OPT_PROTECT_PORT: 8557}), "entryA")
    assert errors == {OPT_PROTECT_PORT: "onvif_port_in_use"}


@pytest.mark.asyncio
async def test_our_own_running_port_is_not_treated_as_taken():
    """Re-saving Configure while the server holds 8899 must work. Kill: the
    own-port exemption removed."""
    hass = _hass()
    hass.data[DOMAIN]["_ports_by_entry"] = {"entryA": {"onvif": {DEV_A: 8899}}}
    with patch("custom_components.cuboai.go2rtc._port_bindable", return_value=False):
        assert await _errors(hass, _on(), "entryA") == {}


@pytest.mark.asyncio
async def test_only_one_account_per_host_may_expose_cameras():
    """The base port and the host's real MAC are per host: a second account's
    primary camera would be merged into the first. Kill: the check removed."""
    other = SimpleNamespace(entry_id="entryB", options={OPT_PROTECT_ENABLED: True})
    with patch("custom_components.cuboai.go2rtc._port_bindable", return_value=True):
        errors = await _errors(_hass([other]), _on(), "entryA")
    assert errors == {"base": "unifi_protect_other_entry"}


@pytest.mark.asyncio
async def test_first_run_rejects_a_missing_password_and_keeps_the_form():
    """Kill: async_step_config creating the entry without validating."""
    flow = cf.CuboAIConfigFlow()
    flow.hass = _hass()
    flow._auth_data = {"username": "u", "cameras": CAMS}
    flow.async_show_form = lambda **kw: kw
    flow.async_create_entry = MagicMock()
    user_input = {"download_images": True, "rtsp_port": 8557, OPT_PROTECT_ENABLED: True, OPT_PROTECT_PASSWORD: ""}
    with (
        patch.object(cf, "setup_file_logger", MagicMock()),
        patch("custom_components.cuboai.go2rtc._port_bindable", return_value=True),
    ):
        result = await flow.async_step_config(user_input)

    flow.async_create_entry.assert_not_called()
    assert result["errors"] == {OPT_PROTECT_PASSWORD: "unifi_password_required"}


# =============================================================================
# Labels
# =============================================================================


def test_every_protect_field_and_error_is_labelled_in_both_files():
    """An unlabelled option renders as its raw key. Kill: any label removed."""
    root = Path(__file__).resolve().parent.parent / "custom_components" / "cuboai"
    for name in ("translations/en.json", "strings.json"):
        data = json.loads((root / name).read_text(encoding="utf-8"))
        options_labels = data["options"]["step"]["init"]["data"]
        assert all(options_labels.get(k, "").strip() for k in KEYS), f"{name}: options label missing"
        first_run = data["config"]["step"]["config"]["data"]
        assert all(first_run.get(k) for k in (OPT_PROTECT_ENABLED, OPT_PROTECT_USERNAME, OPT_PROTECT_PASSWORD))
        for section in ("config", "options"):
            errors = data[section]["error"]
            for err in (
                "unifi_password_required",
                "onvif_port_in_use",
                "unifi_protect_other_entry",
                "unifi_protect_no_camera",
                "onvif_camera_port_in_use",
            ):
                assert errors.get(err), f"{name}: {section}.error.{err} missing"


# =============================================================================
# The diagnostics download
# =============================================================================


def test_the_protect_password_is_a_secret_key_by_name():
    """Found before it shipped: `unifi_protect_password` was not on the secret
    list and would have been published in the options section of every report.
    Kill: the 'password' substring rule removed."""
    from custom_components.cuboai.diagnostics import _is_secret_key

    assert _is_secret_key("unifi_protect_password")
    assert _is_secret_key("some_future_token")
    assert not _is_secret_key("unifi_protect_port")


def test_the_manifest_declares_every_integration_the_protect_code_uses():
    """Hassfest rejected v2.6.37: onvif_server.py calls the `network`
    integration (async_get_source_ip) without the manifest declaring it, so HA
    does not guarantee it is set up first. Kill: 'network' removed from
    dependencies."""
    import re

    root = Path(__file__).resolve().parent.parent / "custom_components" / "cuboai"
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    declared = set(manifest.get("dependencies", [])) | set(manifest.get("after_dependencies", []))
    source = (root / "onvif_server.py").read_text(encoding="utf-8")
    used = set(re.findall(r"homeassistant\.components\.(\w+)", source)) | set(
        re.findall(r"from homeassistant\.components import (\w+)", source)
    )
    # Hassfest lets any integration use these without declaring them.
    always_allowed = {"persistent_notification"}
    assert used - always_allowed <= declared, f"undeclared: {sorted(used - always_allowed - declared)}"


# =============================================================================
# Every field is labelled (not just Protect's)
# =============================================================================


async def _form_keys():
    """The field keys of the first-run options step and of Configure, built
    through the real flows with two cameras."""
    hass = _hass()
    entry = MagicMock(entry_id="entryA", options={}, data={"cameras": CAMS, "all_cameras": CAMS})
    options_flow = cf.CuboAIOptionsFlowHandler()
    options_flow.hass, options_flow.config_entry = hass, entry
    options_flow.async_show_form = lambda **kw: kw
    setup_flow = cf.CuboAIConfigFlow()
    setup_flow.hass = hass
    setup_flow._auth_data = {"username": "u", "cameras": CAMS}
    setup_flow.async_show_form = lambda **kw: kw
    with (
        patch.object(cf, "setup_file_logger", MagicMock()),
        patch("custom_components.cuboai.utils.find_available_port", return_value=8557),
    ):
        configure = await options_flow.async_step_init()
        setup = await setup_flow.async_step_config()
    keys = lambda form: [str(k) for k in form["data_schema"].schema]  # noqa: E731
    return keys(setup), keys(configure)


@pytest.mark.asyncio
async def test_every_setup_and_configure_field_is_labelled_in_both_files():
    """An unlabelled field renders as its raw key (max_saved_photos and
    rtsp_timestamp_cameras did in Configure). camera_ip_<id> is exempt: its key
    is per camera, and HA translations are static. Kill: any label removed."""
    setup_keys, configure_keys = await _form_keys()
    assert "max_saved_photos" in configure_keys and "rtsp_timestamp_cameras" in configure_keys
    root = Path(__file__).resolve().parent.parent / "custom_components" / "cuboai"
    missing = []
    for name in ("translations/en.json", "strings.json"):
        data = json.loads((root / name).read_text(encoding="utf-8"))
        for section, step, keys in (("config", "config", setup_keys), ("options", "init", configure_keys)):
            labels = data[section]["step"][step]["data"]
            missing += [
                f"{name}:{section}.{k}"
                for k in keys
                if not k.startswith("camera_ip_") and not str(labels.get(k, "")).strip()
            ]
    assert not missing, missing
