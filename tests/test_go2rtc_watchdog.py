"""go2rtc must not stay dead when it crashes on its own.

Observed live on 2026-09-12: go2rtc 1.9.14 took a SIGSEGV inside its OWN
mp4/fMP4 consumer — `pkg/mp4/consumer.go` -> `core.WriteBuffer.Write` ->
`http.response.Flush` -> `bufio.(*Writer).Flush` on a nil writer — when a
browser holding an MSE stream dropped mid-write. The process exited at 23:24:43
and nothing restarted it. `start()` spawns the subprocess and checks its exit
code exactly once, one second later; after that nobody looks. The config entry
still reported `loaded`, every entity kept its last state, and the camera was
blank for 12.5 hours until a human opened the card and saw
"Cannot connect to host <ha-host>:1985".

These tests pin the supervisor that closes that hole, including the two ways it
could make things worse: respawning the instance a deliberate stop() is tearing
down, and cancelling itself half way through its own restart.

Every test names the mutation it kills.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.cuboai import go2rtc as go2rtc_module
from custom_components.cuboai.const import DOMAIN, OPT_NOTIFY_ON_RESTART

# Fast enough that a test finishes in milliseconds, slow enough that the loop
# gets to run between ticks.
TICK = 0.01


def _make_hass():
    """A hass whose async_create_task really schedules the coroutine.

    conftest stubs `homeassistant` as a MagicMock, so the default
    `hass.async_create_task(coro)` would return a MagicMock and silently never
    run the watchdog — every test here would pass vacuously.
    """
    hass = MagicMock()
    hass.data = {}

    async def _run(func, *args):
        return func(*args)

    hass.async_add_executor_job = AsyncMock(side_effect=_run)
    hass.async_create_task = lambda coro, *a, **kw: asyncio.get_running_loop().create_task(coro)
    return hass


def _manager(hass, entry_id="entryA"):
    m = go2rtc_module.Go2RTCManager(hass, entry_id)
    m.update_streams([], {})
    hass.data.setdefault(DOMAIN, {}).setdefault(entry_id, {})["go2rtc"] = m
    return m


def _proc(pid=1234, returncode=None):
    """A stand-in subprocess. `returncode=None` means still running.

    `wait()` really suspends. That is not decoration: a bare
    `AsyncMock(return_value=...)` finishes without ever yielding to the loop, and
    `asyncio.wait_for` then SWALLOWS a pending cancellation (3.11: on
    CancelledError it returns `fut.result()` when the inner future is already
    done). With an instant wait() the self-cancel hazard in stop() cannot
    reproduce and the test that guards it passes no matter what the source says
    — verified by mutation. Reaping a process that was just terminated takes
    real time, so suspending here is also the honest model.
    """
    p = MagicMock()
    p.pid = pid
    p.returncode = returncode

    async def _wait():
        await asyncio.sleep(TICK)
        return p.returncode

    p.wait = _wait
    return p


def _armed(manager, uptime=999.0, pid=1234):
    """Arm the watchdog over a live process that started `uptime` seconds ago."""
    manager.process = _proc(pid)
    manager._started_at = time.monotonic() - uptime
    manager._start_watchdog()
    return manager


async def _settle(ticks=4):
    """Let the watchdog run a few of its intervals."""
    await asyncio.sleep(TICK * ticks)


# =============================================================================
# The hole that was open: a dead go2rtc stays dead
# =============================================================================


@pytest.mark.asyncio
async def test_watchdog_restarts_a_process_that_died_on_its_own():
    """Kill: the watchdog removed, or never armed by start()."""
    hass = _make_hass()
    m = _manager(hass)
    restarted = asyncio.Event()

    async def fake_start():
        restarted.set()

    with patch.object(go2rtc_module, "WATCHDOG_INTERVAL", TICK), patch.object(m, "start", fake_start):
        _armed(m)
        m.process.returncode = -11  # SIGSEGV, exactly what go2rtc did
        await asyncio.wait_for(restarted.wait(), timeout=2.0)

    assert restarted.is_set()


@pytest.mark.asyncio
async def test_watchdog_leaves_a_live_process_alone():
    """Kill: the `returncode is None -> continue` check dropped (restart storm)."""
    hass = _make_hass()
    m = _manager(hass)
    start = AsyncMock()

    with patch.object(go2rtc_module, "WATCHDOG_INTERVAL", TICK), patch.object(m, "start", start):
        _armed(m)
        await _settle(8)

    start.assert_not_called()
    assert m._watchdog_task is not None and not m._watchdog_task.done()
    m._cancel_watchdog()


@pytest.mark.asyncio
async def test_watchdog_retires_quietly_when_there_is_no_process_to_supervise():
    """Without the guard, `self.process.returncode` raises AttributeError on
    None; the blanket except still retires the task, so "did it restart?" alone
    cannot tell the two apart. What separates them is the log: a supervisor that
    files an exception traceback every time a stop() races it is a bug report
    generator.

    Kill: the `process is None -> return` guard dropped."""
    hass = _make_hass()
    m = _manager(hass)
    start = AsyncMock()

    with (
        patch.object(go2rtc_module, "WATCHDOG_INTERVAL", TICK),
        patch.object(m, "start", start),
        patch.object(go2rtc_module._LOGGER, "exception") as logged,
    ):
        _armed(m)
        m.process = None
        await _settle(6)
        task = m._watchdog_task

    start.assert_not_called()
    assert task.done() and task.exception() is None
    logged.assert_not_called()


# =============================================================================
# It must not fight a deliberate shutdown
# =============================================================================


@pytest.mark.asyncio
async def test_stop_disarms_the_watchdog():
    """A reload/unload tears go2rtc down on purpose. A watchdog still ticking
    would see the dead process and resurrect the instance we just killed.

    Kill: `_cancel_watchdog()` removed from stop()."""
    hass = _make_hass()
    m = _manager(hass)
    start = AsyncMock()

    with patch.object(go2rtc_module, "WATCHDOG_INTERVAL", TICK), patch.object(m, "start", start):
        _armed(m)
        task = m._watchdog_task
        await m.stop()
        await _settle(8)

    assert task.cancelled() or task.done(), "watchdog still running after stop()"
    assert m._watchdog_task is None
    assert m.process is None
    start.assert_not_called()


@pytest.mark.asyncio
async def test_stop_clears_the_start_stamp():
    """Kill: `_started_at = None` dropped from stop() — a later crash would be
    measured against the PREVIOUS instance's start and always look healthy."""
    hass = _make_hass()
    m = _manager(hass)
    _armed(m)
    await m.stop()
    assert m._started_at is None


# =============================================================================
# The self-cancel hazard
# =============================================================================


@pytest.mark.asyncio
async def test_watchdog_does_not_cancel_itself_while_restarting():
    """The watchdog restarts go2rtc by calling start(), and start() calls stop()
    — which disarms the watchdog. Without the `is not current_task()` guard that
    cancels the very task performing the restart, midway, and go2rtc stays down
    forever. This is the regression that would silently undo the whole fix.

    Kill: drop `task is not asyncio.current_task()` from _cancel_watchdog()."""
    hass = _make_hass()
    m = _manager(hass)
    done = asyncio.Event()

    async def fake_start():
        # The real start()'s ordering: stop the old instance, then spawn and arm.
        await m.stop()
        m.process = _proc(pid=5678)
        m._started_at = time.monotonic()
        m._start_watchdog()
        done.set()

    with patch.object(go2rtc_module, "WATCHDOG_INTERVAL", TICK), patch.object(m, "start", fake_start):
        _armed(m, pid=1234)
        first = m._watchdog_task
        m.process.returncode = -11
        await asyncio.wait_for(done.wait(), timeout=2.0)
        await _settle(2)

        assert m.process is not None and m.process.pid == 5678, "restart did not complete"
        assert m._watchdog_task is not None and not m._watchdog_task.done(), "replacement watchdog is not running"
        assert m._watchdog_task is not first, "the retired watchdog was left armed"
        m._cancel_watchdog()

    assert first.done() and not first.cancelled(), "the restarting task should retire cleanly, not be cancelled"


# =============================================================================
# Crash-loop budget
# =============================================================================


def test_a_healthy_run_is_always_worth_restarting():
    """Kill: the MIN_HEALTHY branch removed."""
    m = _manager(_make_hass())
    m._watchdog_restarts = 3
    assert m._watchdog_should_restart(go2rtc_module.WATCHDOG_MIN_HEALTHY_S) is True
    assert m._watchdog_restarts == 0, "a healthy run must reset the failure budget"


def test_crash_loop_gives_up_after_max_restarts():
    """A binary that dies instantly every time must not be respawned forever —
    every start reclaims ports and re-opens TUTK sessions against the camera.

    Kill: the budget removed, or the comparison loosened."""
    m = _manager(_make_hass())
    short = go2rtc_module.WATCHDOG_MIN_HEALTHY_S - 1

    verdicts = [m._watchdog_should_restart(short) for _ in range(go2rtc_module.WATCHDOG_MAX_RESTARTS)]

    assert verdicts[:-1] == [True] * (go2rtc_module.WATCHDOG_MAX_RESTARTS - 1)
    assert verdicts[-1] is False, "watchdog never gives up on a crash loop"
    assert m._watchdog_should_restart(short) is False, "gave up, then tried again anyway"


def test_one_good_run_clears_a_partial_crash_streak():
    """Kill: the reset removed — an integration up for months would eventually
    exhaust its budget on unrelated, well-separated crashes."""
    m = _manager(_make_hass())
    short = go2rtc_module.WATCHDOG_MIN_HEALTHY_S - 1
    for _ in range(go2rtc_module.WATCHDOG_MAX_RESTARTS - 1):
        m._watchdog_should_restart(short)
    assert m._watchdog_restarts == go2rtc_module.WATCHDOG_MAX_RESTARTS - 1

    m._watchdog_should_restart(go2rtc_module.WATCHDOG_MIN_HEALTHY_S * 2)

    assert m._watchdog_restarts == 0
    assert m._watchdog_should_restart(short) is True


@pytest.mark.asyncio
async def test_watchdog_stops_after_giving_up():
    """Kill: the give-up verdict ignored by the loop."""
    hass = _make_hass()
    m = _manager(hass)
    start = AsyncMock()

    with (
        patch.object(go2rtc_module, "WATCHDOG_INTERVAL", TICK),
        patch.object(m, "start", start),
        patch.object(m, "_watchdog_should_restart", return_value=False),
    ):
        _armed(m)
        task = m._watchdog_task
        m.process.returncode = 1
        await _settle(8)

    assert task.done()
    start.assert_not_called()


# =============================================================================
# Arming
# =============================================================================


@pytest.mark.asyncio
async def test_a_successful_start_arms_the_watchdog():
    """The whole fix hangs off this one line in start(). Without it every test
    above still passes (they arm by hand) while production supervises nothing.

    Kill: the `self._start_watchdog()` call in start() removed."""
    hass = _make_hass()
    m = _manager(hass)
    proc = _proc(pid=4242)

    with (
        patch.object(go2rtc_module.os.path, "exists", return_value=True),
        patch.object(go2rtc_module.os, "chmod"),
        patch.object(m, "_reclaim_stale_instance", AsyncMock()),
        patch.object(m, "_own_last_ports", return_value=(None, None)),
        patch.object(m, "_wait_for_port_free", AsyncMock()),
        patch.object(m, "_resolve_ports", AsyncMock()),
        patch.object(m, "_resolve_codecs", AsyncMock()),
        patch.object(m, "_generate_config", AsyncMock()),
        patch.object(go2rtc_module.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)),
        patch.object(go2rtc_module.asyncio, "sleep", AsyncMock()),
    ):
        await m.start()

    assert m.process is proc
    assert m._started_at is not None, "start() did not stamp the start time"
    assert m._watchdog_task is not None and not m._watchdog_task.done(), "start() left go2rtc unsupervised"
    m._cancel_watchdog()


@pytest.mark.asyncio
async def test_a_failed_start_arms_no_watchdog():
    """start() nulls `process` when go2rtc exits immediately. Arming a
    supervisor over a process that never existed would spin on a None forever.

    Kill: the `if self.process is not None` guard around the arming dropped."""
    hass = _make_hass()
    m = _manager(hass)

    with patch.object(go2rtc_module.os.path, "exists", return_value=False):
        await m.start()  # bails at the binary check

    assert m._watchdog_task is None
    assert m._started_at is None


# =============================================================================
# Notifications
# =============================================================================
#
# A recovery nobody hears about teaches you nothing about a camera that keeps
# dying — which is the whole reason this failure went unnoticed for 12.5 hours.
# So the notice is ON by default, and says how to switch it off.


def _notifier():
    """The persistent_notification stub, reset for one test.

    MUST use the same `from homeassistant.components import ...` form that
    _notify() uses. `import homeassistant.components.persistent_notification as
    pn` resolves through the MagicMock `homeassistant` attribute chain instead
    of sys.modules and hands back a DIFFERENT object, so every assertion here
    would watch a mock production never touches and report zero calls.
    """
    from homeassistant.components import persistent_notification as pn

    pn.async_create.reset_mock()
    return pn.async_create


@pytest.mark.asyncio
async def test_a_restart_notifies_by_default():
    """No option set at all must still notify.

    Kill: the default flipped to False, or the _notify call removed."""
    hass = _make_hass()
    m = _manager(hass)
    assert m._options.get(go2rtc_module.OPT_NOTIFY_ON_RESTART) is None, "fixture must not preset the option"
    created = _notifier()

    async def fake_start():
        m.process = _proc(pid=999)

    with patch.object(go2rtc_module, "WATCHDOG_INTERVAL", TICK), patch.object(m, "start", fake_start):
        _armed(m, uptime=3600.0)
        m.process.returncode = -11
        await _settle(8)
        m._cancel_watchdog()

    created.assert_called_once()
    body = created.call_args[0][1]
    assert "restarted it automatically" in body
    assert "Configure" in body, "the notice must say how to turn itself off"
    assert created.call_args[1]["notification_id"].startswith("cuboai_engine_restarted_")


@pytest.mark.asyncio
async def test_the_option_switches_restart_notices_off():
    """Kill: the option is read but ignored."""
    hass = _make_hass()
    m = _manager(hass)
    m.update_streams([], {go2rtc_module.OPT_NOTIFY_ON_RESTART: False})
    created = _notifier()

    async def fake_start():
        m.process = _proc(pid=999)

    with patch.object(go2rtc_module, "WATCHDOG_INTERVAL", TICK), patch.object(m, "start", fake_start):
        _armed(m, uptime=3600.0)
        m.process.returncode = -11
        await _settle(8)
        m._cancel_watchdog()

    created.assert_not_called()


@pytest.mark.asyncio
async def test_a_restart_that_failed_is_not_announced_as_a_recovery():
    """start() can fail — the binary is gone, the port is stuck. Telling the
    user "restarted it, no action needed" while the camera is still dead is
    worse than saying nothing.

    Kill: the `if self.is_running` guard around the notice dropped."""
    hass = _make_hass()
    m = _manager(hass)
    created = _notifier()

    async def failed_start():
        m.process = None  # what start() does when the process did not survive

    with patch.object(go2rtc_module, "WATCHDOG_INTERVAL", TICK), patch.object(m, "start", failed_start):
        _armed(m, uptime=3600.0)
        m.process.returncode = -11
        await _settle(8)

    created.assert_not_called()


def test_giving_up_notifies():
    """The one case that needs a human. Kill: the give-up notice removed."""
    m = _manager(_make_hass())
    created = _notifier()
    short = go2rtc_module.WATCHDOG_MIN_HEALTHY_S - 1

    for _ in range(go2rtc_module.WATCHDOG_MAX_RESTARTS):
        m._watchdog_should_restart(short)

    created.assert_called_once()
    title = created.call_args[1]["title"]
    body = created.call_args[0][1]
    assert "keeps crashing" in title
    assert "go2rtc.log" in body, "a notice a human must act on has to say where to look"


def test_a_notification_failure_never_breaks_recovery():
    """Filing a notice is the least important thing the supervisor does.

    Kill: the try/except around the notification removed."""
    m = _manager(_make_hass())
    from homeassistant.components import persistent_notification as pn

    with patch.object(pn, "async_create", side_effect=RuntimeError("no notify")):
        m._notify("t", "m", "restarted")  # must not raise


def test_notifications_are_scoped_per_entry():
    """Two accounts must not overwrite each other's notice.

    Kill: a constant notification_id."""
    hass = _make_hass()
    a, b = _manager(hass, "entryA"), _manager(hass, "entryB")
    created = _notifier()

    a._notify("t", "m", "restarted")
    b._notify("t", "m", "restarted")

    ids = [c[1]["notification_id"] for c in created.call_args_list]
    assert len(set(ids)) == 2, f"both entries filed the same notification id: {ids}"


@pytest.mark.asyncio
async def test_watchdog_survives_an_unexpected_error():
    """A supervisor that raises into the event loop takes the integration's
    task tree with it. It must log and retire instead.

    Kill: the blanket `except Exception` removed."""
    hass = _make_hass()
    m = _manager(hass)

    with (
        patch.object(go2rtc_module, "WATCHDOG_INTERVAL", TICK),
        patch.object(m, "_watchdog_should_restart", side_effect=RuntimeError("boom")),
    ):
        _armed(m)
        task = m._watchdog_task
        m.process.returncode = -11
        await _settle(8)

    assert task.done()
    assert task.exception() is None, "the watchdog let an error escape into the loop"


# =============================================================================
# The toggle the notification promises
# =============================================================================


@pytest.mark.asyncio
async def test_the_options_form_offers_the_notification_toggle():
    """Every restart notice ends with "you can turn these off in ... Configure".
    If the checkbox is not actually in the options form that sentence is a lie,
    and the user has no way out of a notification they did not ask for.

    Kill: the schema entry removed, or its default flipped to False."""
    from custom_components.cuboai import config_flow as cf

    hass = _make_hass()
    hass.data = {DOMAIN: {}}
    entry = MagicMock()
    entry.entry_id = "entryA"
    entry.options = {}
    entry.data = {"cameras": [], "all_cameras": []}

    flow = cf.CuboAIOptionsFlowHandler()
    flow.hass = hass
    flow.config_entry = entry
    # conftest's OptionsFlow stand-in has no async_show_form; hand back the
    # kwargs so the schema the real HA would render is what we inspect.
    flow.async_show_form = lambda **kw: kw
    with patch.object(cf, "setup_file_logger", MagicMock()):
        result = await flow.async_step_init()

    keys = {str(k): k for k in result["data_schema"].schema}
    assert OPT_NOTIFY_ON_RESTART in keys, f"toggle missing from the options form: {sorted(keys)}"
    assert keys[OPT_NOTIFY_ON_RESTART].default() is True, "the toggle must default to ON"


def test_the_toggle_is_labelled_in_both_string_files():
    """An unlabelled option renders as the raw key (`notify_on_engine_restart`)
    in the UI. Two files carry these strings and they drift apart easily.

    Kill: either label removed."""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "custom_components" / "cuboai"
    for name in ("translations/en.json", "strings.json"):
        data = json.loads((root / name).read_text(encoding="utf-8"))
        labels = data["options"]["step"]["init"]["data"]
        assert OPT_NOTIFY_ON_RESTART in labels, f"{name} has no label for the toggle"
        assert labels[OPT_NOTIFY_ON_RESTART].strip(), f"{name} label is empty"
