"""The per-camera poll runs cameras CONCURRENTLY and merges their discoveries.

Three cameras polled one after another can exceed the whole 60s update interval
(each blocks up to 20s on its local TUTK read, 40s with history sensors), which
leaves every entity stale. _fetch_all now gathers them.

Making them concurrent introduces failure modes a sequential loop could not have,
and these tests pin each one:

* two cameras auto-discovering their LAN IP in the same poll must BOTH persist
  (the old code did a read-modify-write of entry.options per camera, so the
  second overwrote the first),
* that merged write must stay within the contract async_update_options relies on
  in __init__.py — only previously-empty camera_ip_* keys — or the entry reloads
  mid-poll and tears down the coordinator that is still running it,
* one camera failing must not blank the others,
* a 401 must still reach _async_update_data so the token is refreshed.
"""

import asyncio
import importlib
import time
from unittest.mock import MagicMock, patch

import aiohttp
import pytest

COORD = "custom_components.cuboai.coordinator"


def _coord():
    """Resolve the coordinator module at CALL time, never at import time.

    Another test module drops it from sys.modules while collecting, so a
    module-level `import ... as coord_module` can end up holding a different
    object than the one mock.patch() resolves — the instance would then run
    unpatched globals and hit the network. Importing here keeps the object the
    tests build from and the object the patches land on identical.
    """
    return importlib.import_module(COORD)


def _make_entry(cameras, options=None):
    entry = MagicMock()
    entry.data = {"cameras": cameras}
    entry.options = dict(options or {})
    entry.entry_id = "entry1"
    return entry


def _make_hass():
    hass = MagicMock()
    hass.config.path = lambda *parts: "/tmp/cuboai_test_images"
    hass.data = {}

    def _executor(func, *args):
        # Must be a REAL thread hop like HA's: running the blocking fn inline on
        # the event loop would serialize the cameras no matter what the code
        # under test does, and quietly turn the concurrency test into a no-op.
        return asyncio.get_running_loop().run_in_executor(None, func, *args)

    hass.async_add_executor_job = _executor
    return hass


def _make_coordinator(cameras, options=None, hass=None):
    hass = hass or _make_hass()
    # These are read-only properties off the entry — keep image download and the
    # slow history pull out of the paths under test.
    opts = {"download_images": False, "history_sensors": False, "alerts_count": 0, "hours_back": 1}
    opts.update(options or {})
    entry = _make_entry(cameras, opts)
    c = _coord().CuboAICoordinator(hass, entry, "tok", "refresh", "ua")
    c.data = None
    return c


def _cams(*ids):
    return [{"device_id": d, "uid": f"uid-{d}", "account": "a", "password": "p", "baby_name": d} for d in ids]


class _Patches:
    """Patch the four network/IO calls _fetch_all fans out to."""

    def __init__(self, local_impl, alerts=None, state=None, profiles=None):
        self.local_impl = local_impl
        self.alerts = alerts if alerts is not None else (lambda *a, **k: [])
        self.state = state if state is not None else (lambda *a, **k: {})
        self.profiles = profiles if profiles is not None else []

    def __enter__(self):
        async def _profiles(*a, **k):
            return self.profiles

        async def _sub(*a, **k):
            return {}

        async def _alerts(device_id, *a, **k):
            return self.alerts(device_id)

        async def _state(device_id, *a, **k):
            return self.state(device_id)

        self._ps = [
            patch(f"{COORD}.get_camera_profiles_raw", _profiles),
            patch(f"{COORD}.get_subscription_info", _sub),
            patch(f"{COORD}.get_n_alerts_paged", _alerts),
            patch(f"{COORD}.get_camera_state", _state),
            patch(f"{COORD}._fetch_local_data", self.local_impl),
        ]
        for p in self._ps:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._ps:
            p.stop()
        return False


# =============================================================================
# Concurrency
# =============================================================================


@pytest.mark.asyncio
async def test_three_cameras_are_polled_concurrently_not_serially():
    """Three 0.2s local reads must overlap. Serially they take 0.6s, which
    against a 60s interval is exactly how a 3-camera install goes stale."""
    delay = 0.2

    def local(uid, *a, **k):
        time.sleep(delay)
        return {"wifi_ip": None, "connection_mode": "lan"}

    c = _make_coordinator(_cams("A", "B", "C"))
    with _Patches(local):
        started = time.monotonic()
        result = await c._fetch_all(session=MagicMock())
        elapsed = time.monotonic() - started

    assert set(result["cameras"]) == {"A", "B", "C"}
    # Generous ceiling: the point is "not 3x", not a precise timing assertion.
    assert elapsed < delay * 2, f"cameras still appear serialized ({elapsed:.2f}s for 3x{delay}s)"


@pytest.mark.asyncio
async def test_local_fetch_concurrency_is_bounded():
    """The LAN bound exists so N cameras don't all burst UDP discovery at once."""
    c = _make_coordinator(_cams("A"))
    assert isinstance(c._local_fetch_slots, asyncio.Semaphore)
    assert c._local_fetch_slots._value == 4

    import inspect

    src = inspect.getsource(_coord().CuboAICoordinator._local_fetch)
    flat = " ".join(src.split())
    # The slot must be taken OUTSIDE the timeout, or a queued camera burns its
    # whole budget waiting in line and times out never having been contacted.
    assert flat.index("async with self._local_fetch_slots") < flat.index("asyncio.wait_for")


# =============================================================================
# The auto-learned IP merge
# =============================================================================


@pytest.mark.asyncio
async def test_two_cameras_learning_ips_both_persist_in_one_write():
    """The race the concurrency introduces: per-camera read-modify-write of
    entry.options meant the second discovery overwrote the first."""

    ips = {"uid-A": "192.168.1.11", "uid-B": "192.168.1.22"}

    def local(uid, *a, **k):
        return {"wifi_ip": ips[uid]}

    hass = _make_hass()
    c = _make_coordinator(_cams("A", "B"), hass=hass)
    with _Patches(local):
        await c._fetch_all(session=MagicMock())

    calls = hass.config_entries.async_update_entry.call_args_list
    assert len(calls) == 1, f"expected ONE merged options write, got {len(calls)}"
    written = calls[0].kwargs["options"]
    # Both survive — the old per-camera write kept only the last.
    assert written["camera_ip_A"] == "192.168.1.11"
    assert written["camera_ip_B"] == "192.168.1.22"


@pytest.mark.asyncio
async def test_merged_write_only_touches_previously_empty_camera_ip_keys():
    """async_update_options in __init__.py only skips the (coordinator-killing,
    mid-poll) reload when EVERY changed key is a camera_ip_* that was empty. A
    write that touches anything else silently reintroduces that reload."""

    def local(uid, *a, **k):
        return {"wifi_ip": "10.0.0.9"}

    hass = _make_hass()
    c = _make_coordinator(_cams("A", "B"), options={"rtsp_port": 8557, "camera_ip_A": "10.0.0.1"}, hass=hass)
    before = dict(c._entry.options)
    with _Patches(local):
        await c._fetch_all(session=MagicMock())

    calls = hass.config_entries.async_update_entry.call_args_list
    assert len(calls) == 1
    written = calls[0].kwargs["options"]
    changed = {k for k in set(before) | set(written) if before.get(k) != written.get(k)}
    assert changed == {"camera_ip_B"}, f"write changed unexpected keys: {changed}"
    # An already-known IP is never overwritten by discovery.
    assert written["camera_ip_A"] == "10.0.0.1"
    assert written["rtsp_port"] == 8557


@pytest.mark.asyncio
async def test_no_options_write_when_nothing_new_is_learned():
    """A write with zero changed keys still triggers the reload branch."""

    def local(uid, *a, **k):
        return {"wifi_ip": "10.0.0.1"}

    hass = _make_hass()
    c = _make_coordinator(_cams("A"), options={"camera_ip_A": "10.0.0.1"}, hass=hass)
    with _Patches(local):
        await c._fetch_all(session=MagicMock())

    hass.config_entries.async_update_entry.assert_not_called()


# =============================================================================
# Failure isolation
# =============================================================================


def _fail_camera(coordinator, failing_id, exc):
    """Make ONE camera's whole _process_camera raise, leaving the rest real.

    Failing it at the alerts/state/local layer would not do: those are already
    caught inside _process_camera and degrade to empty data. This exercises the
    triage in _fetch_all, which is the code the concurrency actually added.
    """
    real = coordinator._process_camera

    async def _wrapped(camera, *a, **k):
        if camera["device_id"] == failing_id:
            raise exc
        return await real(camera, *a, **k)

    coordinator._process_camera = _wrapped
    return coordinator


@pytest.mark.asyncio
async def test_one_camera_crashing_does_not_blank_the_others():
    """Sequentially, a crash aborted the whole poll. Concurrently we keep the
    healthy cameras and carry the failed one's last good values forward."""

    def local(uid, *a, **k):
        return {"connection_mode": "lan"}

    c = _make_coordinator(_cams("A", "B"))
    c.data = {"cameras": {"B": {"local": {"connection_mode": "lan"}, "alerts": [], "profile": {"baby": "old"}}}}
    _fail_camera(c, "B", RuntimeError("boom"))

    with _Patches(local):
        result = await c._fetch_all(session=MagicMock())

    assert "A" in result["cameras"], "healthy camera lost because a sibling failed"
    # B carried forward rather than being blanked
    assert result["cameras"]["B"]["profile"]["baby"] == "old"


@pytest.mark.asyncio
async def test_401_from_one_camera_still_reaches_the_token_refresh():
    """_async_update_data refreshes the token off this exception; swallowing it
    would leave the integration polling with a dead token until restart."""

    def local(uid, *a, **k):
        return {}

    c = _make_coordinator(_cams("A", "B"))
    _fail_camera(c, "B", aiohttp.ClientResponseError(request_info=MagicMock(), history=(), status=401))
    with _Patches(local):
        with pytest.raises(aiohttp.ClientResponseError) as excinfo:
            await c._fetch_all(session=MagicMock())
    assert excinfo.value.status == 401


@pytest.mark.asyncio
async def test_every_camera_failing_raises_update_failed():
    """A total failure must surface as unavailable, not as a silently empty poll."""
    from homeassistant.helpers.update_coordinator import UpdateFailed

    def local(uid, *a, **k):
        return {}

    c = _make_coordinator(_cams("A", "B"))

    async def _boom(*a, **k):
        raise RuntimeError("boom")

    c._process_camera = _boom
    with _Patches(local):
        with pytest.raises(UpdateFailed):
            await c._fetch_all(session=MagicMock())
