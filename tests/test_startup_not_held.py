"""Long-lived loops must not hold Home Assistant's startup (or shutdown).

Observed live on 2026-10-03 (HA 2026.7.1): after every restart the system log
carried

    WARNING homeassistant.bootstrap: Setup timed out for bootstrap waiting on
    {<Task pending ... coro=<Go2RTCManager._watchdog() running at go2rtc.py:1053>}

followed by "Something is blocking Home Assistant from wrapping up the start up
phase", and HA reported RUNNING only ~397 s after the restart. The go2rtc
supervisor was started with `hass.async_create_task`, which HA TRACKS: the
bootstrap wrap-up waits for every tracked task, and a `while True` loop never
finishes, so every restart sat out the full wrap-up timeout. The shutdown
stages wait for tracked tasks the same way.

`hass.async_create_background_task` is the factory for loops: bootstrap does not
wait for it and HA cancels it on shutdown. These tests model that one rule —
tracked tasks are waited for, background tasks are not — and pin every
long-lived task in the integration to the background side.

Every test names the mutation it kills.
"""

import asyncio
import importlib
import time
from unittest.mock import MagicMock, patch

import pytest

from custom_components.cuboai import go2rtc as go2rtc_module
from custom_components.cuboai.const import DOMAIN

TICK = 0.01


def _make_hass():
    """A hass with HA's two task factories, kept apart.

    `tracked` is what bootstrap's wrap-up (and the shutdown stages) wait for;
    `background` is what they do not. Both really run their coroutine, so a
    loop that is started at all is started for real.
    """
    hass = MagicMock()
    hass.data = {}
    hass.tracked = []
    hass.background = []

    def _create_task(coro, name=None, eager_start=True):
        task = asyncio.get_running_loop().create_task(coro, name=name)
        hass.tracked.append(task)
        return task

    def _create_background_task(coro, name, eager_start=True):
        task = asyncio.get_running_loop().create_task(coro, name=name)
        hass.background.append(task)
        return task

    hass.async_create_task = _create_task
    hass.async_create_background_task = _create_background_task
    return hass


async def _bootstrap_wrap_up(hass, timeout=0.2):
    """What HA does before it reports RUNNING: wait for every tracked task.

    Returns the tracked tasks still pending when the timeout gives up — the
    ones that would be named in "Setup timed out for bootstrap waiting on".
    """
    pending = [t for t in hass.tracked if not t.done()]
    if not pending:
        return set()
    _, still = await asyncio.wait(pending, timeout=timeout)
    return still


async def _cancel_all(hass):
    tasks = hass.tracked + hass.background
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def _alive_proc():
    p = MagicMock()
    p.pid = 1234
    p.returncode = None
    return p


# =============================================================================
# go2rtc watchdog — the task named in the live warning
# =============================================================================


@pytest.mark.asyncio
async def test_the_go2rtc_watchdog_does_not_hold_startup():
    """Kill: _start_watchdog back on hass.async_create_task (the 2026-10-03 bug)."""
    hass = _make_hass()
    m = go2rtc_module.Go2RTCManager(hass, "entryA")
    m.update_streams([], {})
    hass.data.setdefault(DOMAIN, {}).setdefault("entryA", {})["go2rtc"] = m
    m.process = _alive_proc()
    m._started_at = time.monotonic()

    try:
        with patch.object(go2rtc_module, "WATCHDOG_INTERVAL", TICK):
            m._start_watchdog()
            stuck = await _bootstrap_wrap_up(hass)

            assert not stuck, "bootstrap would wait on the endless watchdog until its timeout"
            task = m._watchdog_task
            assert task in hass.background
            assert not task.done(), "the watchdog must still be supervising after startup"
            assert "entryA" in task.get_name(), "the task name must say whose watchdog it is"
    finally:
        await _cancel_all(hass)


@pytest.mark.asyncio
async def test_the_background_watchdog_is_still_cancelled_by_stop_disarm():
    """Moving to a background task must keep _cancel_watchdog() working.

    Kill: _start_watchdog creating the background task without storing it in
    _watchdog_task, so _cancel_watchdog (and stop()) can no longer reach it."""
    hass = _make_hass()
    m = go2rtc_module.Go2RTCManager(hass, "entryA")
    m.update_streams([], {})
    m.process = _alive_proc()
    m._started_at = time.monotonic()

    try:
        with patch.object(go2rtc_module, "WATCHDOG_INTERVAL", TICK):
            m._start_watchdog()
            task = m._watchdog_task
            await asyncio.sleep(TICK * 3)
            m._cancel_watchdog()
            await asyncio.gather(task, return_exceptions=True)

        assert task.cancelled()
        assert m._watchdog_task is None
    finally:
        await _cancel_all(hass)


# =============================================================================
# The other long-lived tasks: same rule
# =============================================================================


@pytest.mark.asyncio
async def test_the_camera_warm_hold_does_not_hold_startup():
    """A stream request during startup (HomeKit, preload stream) kicks a hold
    that lives WARM_HOLD_SECONDS.

    Kill: _kick_warm_hold back on hass.async_create_task."""
    camera_platform = importlib.import_module("custom_components.cuboai.camera")
    hass = _make_hass()
    cam = camera_platform.CuboLocalCamera.__new__(camera_platform.CuboLocalCamera)
    cam.hass = hass
    cam._device_id = "DEV1"
    cam._warm_hold_task = None
    cam._warm_hold_deadline = 0.0
    cam._warm_hold = lambda name: asyncio.sleep(3600)

    try:
        cam._kick_warm_hold("cuboai_h264_DEV1")
        assert not await _bootstrap_wrap_up(hass)
        assert cam._warm_hold_task in hass.background
    finally:
        await _cancel_all(hass)


@pytest.mark.asyncio
async def test_the_speaker_queue_loop_is_a_background_task():
    """The queue loop plays for as long as there are songs (hours).

    Kill: _start_queue_loop back on hass.async_create_task."""
    media_player = importlib.import_module("custom_components.cuboai.media_player")
    hass = _make_hass()
    mp = object.__new__(media_player.CuboAIMediaPlayer)
    mp.hass = hass
    mp._device_id = "DEV1"
    mp._queue_task = None
    mp._queue_loop = lambda: asyncio.sleep(3600)

    try:
        mp._start_queue_loop()
        assert not await _bootstrap_wrap_up(hass)
        assert mp._queue_task in hass.background
    finally:
        await _cancel_all(hass)


@pytest.mark.asyncio
async def test_the_lullaby_timer_is_a_background_task():
    """The timer sleeps for the whole Play Time (minutes).

    Kill: _schedule_stop back on hass.async_create_task."""
    media_player = importlib.import_module("custom_components.cuboai.media_player")
    hass = _make_hass()
    lp = object.__new__(media_player.CuboLullabyPlayer)
    lp.hass = hass
    lp._device_id = "DEV1"
    lp._stop_timer_task = None
    lp._stop_after = lambda minutes: asyncio.sleep(3600)

    try:
        lp._schedule_stop(30, "song-uuid")
        assert not await _bootstrap_wrap_up(hass)
        assert lp._stop_timer_task in hass.background
    finally:
        await _cancel_all(hass)
