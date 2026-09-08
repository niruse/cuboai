"""Two config entries (two CuboAI accounts) must not fight over go2rtc.

Go2RTCManager is constructed PER CONFIG ENTRY, so a second account starts a
SECOND go2rtc that has to self-heal onto its own ports. Two things made that
break before v2.6.28, and both are pinned here:

* the resolved ports were published to domain-global keys, so whichever entry
  started last overwrote the first — entry A's camera stream_source, snapshots
  and NVR URLs then pointed at entry B's go2rtc;
* the orphan reclaim identifies a stale instance by "answers the API with
  cuboai_* streams" and kills by binary path, which is an exact description of a
  healthy SIBLING. The two entries killed each other on every restart.

Camera COUNT is unaffected by any of this: N cameras always share one go2rtc and
one RTSP port, keyed by stream name.
"""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.cuboai import go2rtc as go2rtc_module
from custom_components.cuboai.const import DOMAIN, effective_ports


def _make_hass():
    hass = MagicMock()
    hass.data = {}

    async def _run(func, *args):
        return func(*args)

    hass.async_add_executor_job = AsyncMock(side_effect=_run)
    return hass


def _manager(hass, entry_id, options=None):
    m = go2rtc_module.Go2RTCManager(hass, entry_id)
    m.update_streams([], options or {})
    hass.data.setdefault(DOMAIN, {}).setdefault(entry_id, {})["go2rtc"] = m
    return m


def _running(manager, pid=1234, rtsp=None, api=None):
    """Mark a manager as having a live go2rtc on the given ports."""
    proc = MagicMock()
    proc.pid = pid
    proc.returncode = None
    manager.process = proc
    if rtsp is not None:
        manager._rtsp_port = rtsp
    if api is not None:
        manager._api_port = api
    return manager


# =============================================================================
# Ports are published and read per entry
# =============================================================================


@pytest.mark.asyncio
async def test_two_entries_publish_independent_ports():
    hass = _make_hass()
    a = _manager(hass, "entryA", {"rtsp_port": 8557})
    b = _manager(hass, "entryB", {"rtsp_port": 8600})

    with patch.object(go2rtc_module, "_port_bindable", return_value=True):
        await a._resolve_ports()
        await b._resolve_ports()

    # Each entry reads back ITS OWN port, not whichever started last.
    assert effective_ports(hass, "entryA")[0] == 8557
    assert effective_ports(hass, "entryB")[0] == 8600


def test_effective_ports_falls_back_to_the_legacy_global_keys():
    """Single-entry installs (every install today) predate the per-entry keys."""
    hass = _make_hass()
    hass.data[DOMAIN] = {"rtsp_port_effective": 8557, "api_port_effective": 1986}
    assert effective_ports(hass, "entryA") == (8557, 1986)


def test_effective_ports_defaults_when_nothing_is_published():
    hass = _make_hass()
    assert effective_ports(hass, "entryA") == (8555, 1985)
    assert effective_ports(hass, "entryA", rtsp_default=8557)[0] == 8557


def test_own_last_ports_never_borrows_a_siblings_port():
    """The self-reads in start() must NOT fall back to the global key: on entry
    B's first start that key holds entry A's LIVE port, and start() would then
    wait on it and reclaim it — killing a healthy second account."""
    hass = _make_hass()
    # Entry A has started and published globally + per entry.
    hass.data[DOMAIN] = {
        "rtsp_port_effective": 8557,
        "api_port_effective": 1985,
        "entryA": {"rtsp_port_effective": 8557, "api_port_effective": 1985},
    }
    # (a) entry B registered but has published nothing yet
    b = _manager(hass, "entryB")
    assert b._own_last_ports() == (None, None)

    # (b) entry B not in hass.data at all — the very first start. This is the
    # case a `.get(entry_id) or domain_data` fallback would silently turn into
    # "entry A's live ports are mine".
    fresh = go2rtc_module.Go2RTCManager(hass, "entryC")
    assert fresh._own_last_ports() == (None, None)


# =============================================================================
# A live sibling is never treated as an orphan
# =============================================================================


@pytest.mark.asyncio
async def test_reclaim_leaves_a_live_siblings_api_port_alone():
    hass = _make_hass()
    a = _running(_manager(hass, "entryA"), pid=1111, api=1985, rtsp=8557)
    b = _manager(hass, "entryB")

    killed = MagicMock(return_value=0)
    with (
        patch.object(go2rtc_module, "_port_bindable", return_value=False),  # 1985 busy
        patch.object(go2rtc_module.Go2RTCManager, "_terminate_stale_processes", killed),
    ):
        await b._reclaim_stale_instance(1985)

    killed.assert_not_called(), "entry B killed entry A's healthy go2rtc"
    assert a.is_running


@pytest.mark.asyncio
async def test_reclaim_still_kills_a_genuine_orphan():
    """The sibling guard must not disable the issue-#84 reclaim itself."""
    hass = _make_hass()
    b = _manager(hass, "entryB")  # no sibling registered

    async def _fake_json(*a, **k):
        return {"cuboai_combined_X": {}}

    killed = MagicMock(return_value=1)
    session = MagicMock()
    resp = MagicMock()
    resp.status = 200
    resp.json = _fake_json
    session.get.return_value.__aenter__ = AsyncMock(return_value=resp)
    session.get.return_value.__aexit__ = AsyncMock(return_value=False)

    with (
        patch.object(go2rtc_module, "_port_bindable", return_value=False),
        patch.object(go2rtc_module.Go2RTCManager, "_terminate_stale_processes", killed),
        patch.dict(
            sys.modules,
            {"homeassistant.helpers.aiohttp_client": MagicMock(async_get_clientsession=lambda h: session)},
        ),
    ):
        await b._reclaim_stale_instance(1985)

    killed.assert_called_once()


def test_protected_pids_covers_self_and_live_siblings():
    hass = _make_hass()
    a = _running(_manager(hass, "entryA"), pid=1111)
    b = _running(_manager(hass, "entryB"), pid=2222)
    _running(_manager(hass, "entryC"), pid=3333).process.returncode = 0  # dead sibling

    protected = b._protected_pids()
    assert 2222 in protected, "own process not protected"
    assert 1111 in protected, "live sibling not protected"
    assert 3333 not in protected, "a DEAD sibling is an orphan and must stay collectable"
    assert a.is_running


def test_terminate_stale_processes_skips_protected_pids():
    """The sweep matches on binary path, which a sibling's go2rtc also has."""
    hass = _make_hass()
    m = _manager(hass, "entryA")
    m._binary_path = "/config/custom_components/cuboai/bin/go2rtc"

    killed = []

    def _open(path, *a, **k):
        pid = path.split("/")[2]
        if pid == "notapid":
            raise OSError
        handle = MagicMock()
        handle.__enter__ = lambda s: s
        handle.__exit__ = lambda s, *e: False
        handle.read = lambda: f"{m._binary_path}\x00-c\x00cfg".encode()
        return handle

    # First sweep sees the processes, the SIGTERM-follow-up sweep sees none —
    # so the test stops at SIGTERM (SIGKILL does not exist on Windows, and this
    # /proc sweep is Linux-only by design).
    listings = [["1111", "2222", "3333", "notapid"], []]

    with (
        patch.object(go2rtc_module.os, "listdir", side_effect=listings),
        patch("builtins.open", _open),
        patch.object(go2rtc_module.os, "kill", side_effect=lambda pid, sig: killed.append(pid)),
        patch("time.sleep"),
    ):
        m._terminate_stale_processes(protected_pids={2222})

    assert 1111 in killed and 3333 in killed
    assert 2222 not in killed, "a protected (live sibling) process was killed"


# =============================================================================
# Entities follow their OWN entry
# =============================================================================


def test_sensor_and_camera_read_their_own_entrys_port():
    hass = _make_hass()
    hass.data[DOMAIN] = {
        "entryA": {"rtsp_port_effective": 8557, "api_port_effective": 1985},
        "entryB": {"rtsp_port_effective": 8600, "api_port_effective": 1986},
        # stale global mirror from whichever entry started last
        "rtsp_port_effective": 8600,
        "api_port_effective": 1986,
    }
    assert effective_ports(hass, "entryA") == (8557, 1985)
    assert effective_ports(hass, "entryB") == (8600, 1986)
