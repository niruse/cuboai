"""Three reports from the same week.

#137  A reload with a recorder connected moved the RTSP port (8557 -> 8558):
      the port probe counted the stopped go2rtc's connections, which sit in
      TIME_WAIT for a minute, as "held by another process".
#138  UniFi Protect signs in to the stream with its own username and password,
      so an NVR stream password that differs locks it out with no error.
#139  Alert photos were written under `www`, which Home Assistant serves to
      anyone without a login.

Every test names the change that would make it fail.
"""

import importlib.util
import json
import os
import socket
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from custom_components.cuboai import config_flow as cf
from custom_components.cuboai import const, images
from custom_components.cuboai import diagnostics as diag
from custom_components.cuboai import go2rtc as go2rtc_module
from custom_components.cuboai.const import (
    DOMAIN,
    OPT_PROTECT_ENABLED,
    OPT_PROTECT_PASSWORD,
    OPT_PROTECT_PORT,
    OPT_PROTECT_USERNAME,
    protect_nvr_credentials_differ,
)

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "cuboai"

# conftest replaces utils with a mock (it writes log files); the port scan is
# tested on the real module.
_uspec = importlib.util.spec_from_file_location("custom_components.cuboai.utils", COMPONENT / "utils.py")
utils = importlib.util.module_from_spec(_uspec)
_uspec.loader.exec_module(utils)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# =============================================================================
# #137: the port probe
# =============================================================================


def _leave_time_wait(port: int) -> None:
    """A listener on `port` that accepted one connection and closed it first,
    then went away: what a stopped go2rtc with a recorder connected leaves."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # as Go does
    srv.bind(("0.0.0.0", port))
    srv.listen()
    cli = socket.create_connection(("127.0.0.1", port))
    conn, _ = srv.accept()
    conn.close()
    time.sleep(0.2)
    cli.close()
    srv.close()


@pytest.mark.skipif(sys.platform == "win32", reason="TIME_WAIT blocks a plain bind on Linux and macOS only")
def test_a_port_left_in_time_wait_is_free_for_go2rtc():
    """THE #137 case. Kill: SO_REUSEADDR removed from the probe."""
    port = _free_port()
    _leave_time_wait(port)
    with socket.socket() as plain:
        try:
            plain.bind(("0.0.0.0", port))
            pytest.skip("this system let a plain bind through, so the case cannot be shown here")
        except OSError:
            pass
    assert const.port_bindable(port), "the probe took TIME_WAIT for a holder: a reload would move the port"
    assert go2rtc_module._port_bindable(port)


def test_a_port_someone_listens_on_is_still_taken():
    """The fallback exists for a real holder (HA's own go2rtc on 8555).
    Kill: the probe made to always succeed, or SO_REUSEADDR set on Windows."""
    with socket.socket() as holder:
        if sys.platform != "win32":
            holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("0.0.0.0", 0))
        holder.listen()
        port = holder.getsockname()[1]
        assert not const.port_bindable(port)
        assert not go2rtc_module._port_bindable(port)
    assert const.port_bindable(port)


def test_the_port_scan_uses_the_same_probe(monkeypatch):
    """`find_available_port` probed on its own, without the option.
    Kill: the scan binding a socket itself again."""
    asked = []
    monkeypatch.setattr(const, "port_bindable", lambda port: asked.append(port) or port == 8559)
    assert utils.find_available_port(start_port=8557) == 8559
    assert asked == [8557, 8558, 8559]


def test_both_probes_are_one_function():
    """Kill: go2rtc.py growing its own bind again."""
    source = (COMPONENT / "go2rtc.py").read_text(encoding="utf-8")
    body = source.split("def _port_bindable", 1)[1].split("\ndef ", 1)[0]
    assert "port_bindable(port)" in body and ".bind(" not in body


# =============================================================================
# #138: UniFi Protect and the NVR stream password
# =============================================================================


def _opts(**kw):
    base = {
        OPT_PROTECT_ENABLED: True,
        OPT_PROTECT_USERNAME: "cuboai",
        OPT_PROTECT_PASSWORD: "protect-pw",
        "nvr_enabled": True,
        "nvr_username": "cuboai",
        "nvr_password": "nvr-pw",
    }
    base.update(kw)
    return base


def test_different_passwords_lock_protect_out():
    assert protect_nvr_credentials_differ(_opts())


def test_a_different_username_alone_locks_protect_out():
    """Kill: only the passwords compared."""
    assert protect_nvr_credentials_differ(_opts(nvr_username="recorder", nvr_password="protect-pw"))


def test_the_same_username_and_password_let_protect_in():
    assert not protect_nvr_credentials_differ(_opts(nvr_password="protect-pw"))


def test_an_empty_nvr_username_means_the_default_one():
    """go2rtc is given "cuboai" for an empty username. Kill: "" compared as is."""
    assert not protect_nvr_credentials_differ(_opts(nvr_username="", nvr_password="protect-pw"))
    assert not protect_nvr_credentials_differ(
        _opts(nvr_username="", nvr_password="protect-pw", **{OPT_PROTECT_USERNAME: ""})
    )


@pytest.mark.parametrize(
    "change",
    [
        {"nvr_password": ""},  # open stream: Protect is let in
        {"nvr_enabled": False},  # the password is not applied at all
        {OPT_PROTECT_ENABLED: False},  # nobody to lock out
    ],
)
def test_nothing_to_report_when_the_stream_is_open_or_protect_is_off(change):
    """Kill: any of the three conditions dropped."""
    assert not protect_nvr_credentials_differ(_opts(**change))
    assert not protect_nvr_credentials_differ(None)


def _flow_hass():
    hass = MagicMock()
    hass.data = {DOMAIN: {}}

    async def _run(func, *args):
        return func(*args)

    hass.async_add_executor_job = AsyncMock(side_effect=_run)
    hass.config_entries.async_entries = lambda domain: []
    return hass


async def _plan_errors(user_input):
    cams = [{"device_id": "CB02AAAA00000001", "baby_name": "A"}]
    return (await cf._protect_plan(_flow_hass(), {}, user_input, cams, "entryA"))[0]


async def test_configure_refuses_credentials_that_lock_protect_out(monkeypatch):
    """Kill: the check removed from the options form."""
    monkeypatch.setattr(go2rtc_module, "_port_bindable", lambda port: True)
    errors = await _plan_errors({**_opts(), OPT_PROTECT_PORT: 8899, "rtsp_port": 8557})
    assert errors.get("nvr_password") == "nvr_protect_credentials_differ", errors


async def test_configure_accepts_matching_credentials_and_an_open_stream(monkeypatch):
    """Kill: the error raised for every NVR password."""
    monkeypatch.setattr(go2rtc_module, "_port_bindable", lambda port: True)
    for change in ({"nvr_password": "protect-pw"}, {"nvr_password": ""}):
        errors = await _plan_errors({**_opts(**change), OPT_PROTECT_PORT: 8899, "rtsp_port": 8557})
        assert "nvr_password" not in errors, errors


@pytest.mark.parametrize("name", ["strings.json", "translations/en.json"])
def test_the_error_has_a_text(name):
    """A key with no text shows as the bare key. Kill: the text dropped from either file."""
    text = json.loads((COMPONENT / name).read_text(encoding="utf-8"))["options"]["error"]
    assert "same username and password" in text["nvr_protect_credentials_differ"]


def test_diagnostics_say_why_protect_is_frozen():
    """Protect shows no error for this. Kill: the verdict removed."""
    report = {"unifi_protect": {"enabled": True, "cameras": [], "nvr_credentials_differ": True}}
    protect = dict(report["unifi_protect"], cameras=[{"camera": "camera_1"}])
    hits = [
        v
        for v in diag._protect_verdicts({"unifi_protect": protect, "cameras": {}}, set())
        if "refused by the NVR stream password" in v
    ]
    assert hits

    protect["nvr_credentials_differ"] = False
    assert not any(
        "NVR stream password" in v for v in diag._protect_verdicts({"unifi_protect": protect, "cameras": {}}, set())
    )


async def test_the_log_names_the_mismatch_at_start(tmp_path, caplog):
    """An install that already has the mismatch never opens Configure.
    Kill: the warning removed from the config writer."""
    hass = MagicMock()
    hass.data = {}

    async def _run(func, *args):
        return func(*args)

    hass.async_add_executor_job = AsyncMock(side_effect=_run)
    manager = go2rtc_module.Go2RTCManager(hass, "entryA")
    manager._config_path = str(tmp_path / "go2rtc.yaml")
    manager._rtsp_port, manager._webrtc_port, manager._api_port = 8557, 8556, 1985
    manager.update_streams({}, _opts())
    with caplog.at_level("WARNING"):
        await manager._generate_config()
    assert any("differ from the UniFi Protect ones" in r.getMessage() for r in caplog.records)

    caplog.clear()
    manager.update_streams({}, _opts(nvr_password="protect-pw"))
    with caplog.at_level("WARNING"):
        await manager._generate_config()
    assert not any("UniFi Protect ones" in r.getMessage() for r in caplog.records)


# =============================================================================
# #139: alert photos are not public
# =============================================================================

KEY = bytes(range(32))
NAME = "CB02AAAA00000001_CB02AAAA00000001-1795872383-cf7.jpg"


def _images(tmp_path) -> images.AlertImages:
    return images.AlertImages(str(tmp_path / "cuboai_images"), KEY)


def test_a_photo_address_is_not_under_local_and_carries_a_signature(tmp_path):
    """Kill: the address pointed back at /local/."""
    url = _images(tmp_path).url(NAME)
    assert url.startswith("/api/cuboai/alert_image/" + NAME + "?token=")
    assert "/local/" not in url
    assert len(url.split("token=")[1]) == 32


def test_the_signature_belongs_to_one_photo_and_one_installation(tmp_path):
    """Kill: a constant token, or a token that ignores the file name or the key."""
    mine = _images(tmp_path)
    other = images.AlertImages(mine.directory, bytes(reversed(KEY)))
    assert mine.token(NAME) != mine.token("x" + NAME)
    assert mine.token(NAME) != other.token(NAME)


def test_only_the_right_signature_opens_a_photo(tmp_path):
    """Kill: the comparison removed, or a missing token accepted."""
    store = _images(tmp_path)
    assert store.path_for(NAME, store.token(NAME)) == os.path.join(store.directory, NAME)
    assert store.path_for(NAME, None) is None
    assert store.path_for(NAME, "") is None
    assert store.path_for(NAME, store.token(NAME)[:-1] + "0") is None
    assert store.path_for(NAME, store.token("other.jpg")) is None


@pytest.mark.parametrize(
    "name",
    ["../secrets.yaml", "..%2Fsecrets.jpg", "a/b.jpg", "a\\b.jpg", ".hidden.jpg", "photo.png", "", "x.jpg/..", "..jpg"],
)
def test_no_name_reaches_outside_the_folder(tmp_path, name):
    """Even with a correct signature for that name. Kill: the name check removed."""
    store = _images(tmp_path)
    assert store.path_for(name, store.token(name)) is None


def test_the_key_is_made_once_and_kept(tmp_path):
    """A new key at every start would break every stored address.
    Kill: the key regenerated on each load."""
    path = str(tmp_path / ".storage" / "cuboai_alert_images.json")
    first = images._load_or_create_key(path)
    assert len(first) == 32
    assert images._load_or_create_key(path) == first
    Path(path).write_text("not json", encoding="utf-8")
    assert len(images._load_or_create_key(path)) == 32  # a damaged file is replaced, not fatal


def test_photos_are_moved_out_of_www_and_the_folder_removed(tmp_path):
    """Kill: the migration skipped, or copying instead of moving."""
    old = tmp_path / "www" / "cuboai_images"
    old.mkdir(parents=True)
    (old / "a.jpg").write_bytes(b"A")
    (old / "b.jpg").write_bytes(b"B")
    new = tmp_path / "cuboai_images"
    assert images.migrate_public_images([str(old), str(old)], str(new)) == 2
    assert sorted(p.name for p in new.iterdir()) == ["a.jpg", "b.jpg"]
    assert not old.exists(), "the public folder is still there"
    assert images.migrate_public_images([str(old)], str(new)) == 0


def test_a_public_copy_is_deleted_even_when_the_photo_is_already_private(tmp_path):
    """Kill: an existing destination leaving the public file in place."""
    old = tmp_path / "www" / "cuboai_images"
    old.mkdir(parents=True)
    (old / "a.jpg").write_bytes(b"public")
    new = tmp_path / "cuboai_images"
    new.mkdir()
    (new / "a.jpg").write_bytes(b"private")
    images.migrate_public_images([str(old)], str(new))
    assert (new / "a.jpg").read_bytes() == b"private"
    assert not (old / "a.jpg").exists()


def test_other_files_in_the_old_folder_are_left_alone(tmp_path):
    """Only photos are ours to move. Kill: the folder removed with rmtree."""
    old = tmp_path / "www" / "cuboai_images"
    old.mkdir(parents=True)
    (old / "a.jpg").write_bytes(b"A")
    (old / "notes.txt").write_text("mine", encoding="utf-8")
    images.migrate_public_images([str(old)], str(tmp_path / "cuboai_images"))
    assert (old / "notes.txt").exists()


def _hass(tmp_path):
    hass = MagicMock()
    hass.data = {}
    hass.config.path = lambda *parts: str(tmp_path.joinpath(*parts))

    async def _run(func, *args):
        return func(*args)

    hass.async_add_executor_job = AsyncMock(side_effect=_run)
    return hass


async def test_setup_registers_one_view_and_moves_the_photos(tmp_path):
    """Kill: the view registered at every call (a reload would raise), or the
    photos left under www."""
    old = tmp_path / "www" / "cuboai_images"
    old.mkdir(parents=True)
    (old / NAME).write_bytes(b"jpeg")
    hass = _hass(tmp_path)
    first = await images.async_setup_alert_images(hass)
    second = await images.async_setup_alert_images(hass)
    assert first is second
    assert hass.http.register_view.call_count == 1
    assert Path(first.directory) == tmp_path / "cuboai_images"
    assert "www" not in Path(first.directory).parts
    assert (tmp_path / "cuboai_images" / NAME).read_bytes() == b"jpeg"
    assert not old.exists()


async def _get(view, hass, filename, token):
    request = SimpleNamespace(query={} if token is None else {"token": token}, app={"hass": hass})
    return await view.get(request, filename)


async def test_the_view_serves_a_signed_request_and_nothing_else(tmp_path):
    """Kill: requires_auth relied on without the signature check."""
    store = _images(tmp_path)
    os.makedirs(store.directory)
    Path(store.directory, NAME).write_bytes(b"jpeg")
    hass = _hass(tmp_path)
    view = images.AlertImageView(store)
    assert view.requires_auth is False, "an <img> tag cannot log in: the signature is the proof"

    response = await _get(view, hass, NAME, store.token(NAME))
    assert isinstance(response, web.FileResponse)
    assert "private" in response.headers["Cache-Control"]

    for token in (None, "", "0" * 32):
        with pytest.raises(web.HTTPNotFound):
            await _get(view, hass, NAME, token)
    with pytest.raises(web.HTTPNotFound):  # signed, but the photo was pruned
        await _get(view, hass, "gone.jpg", store.token("gone.jpg"))


def test_nothing_writes_alert_photos_under_www_any_more():
    """Kill: the coordinator pointed back at www, or at /local/."""
    source = (COMPONENT / "coordinator.py").read_text(encoding="utf-8")
    assert "/local/cuboai_images" not in source
    assert '"www", "cuboai_images"' not in source
