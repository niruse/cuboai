"""The streaming engine stops when Home Assistant stops (issue #111).

Home Assistant does not unload config entries when it stops or restarts, so the
unload path, the only place go2rtc used to be stopped, never ran then. Where a
restart did not also end the container, go2rtc outlived Home Assistant and kept
the RTSP port, the next start moved to another port (8557 -> 8558), and every
NVR / Scrypted URL broke until the integration was reloaded.

Every test names the mutation it kills.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import custom_components.cuboai as cuboai
from custom_components.cuboai.go2rtc import Go2RTCManager

INIT = Path(__file__).resolve().parents[1] / "custom_components" / "cuboai" / "__init__.py"


class _Bus:
    """Records listeners like hass.bus, and can fire them."""

    def __init__(self):
        self.listeners: list[tuple[object, object]] = []
        self.once_calls = 0

    def async_listen(self, event_type, handler):
        item = (event_type, handler)
        self.listeners.append(item)
        return lambda: self.listeners.remove(item)

    def async_listen_once(self, event_type, handler):
        self.once_calls += 1
        return self.async_listen(event_type, handler)

    async def fire(self, event_type):
        for etype, handler in list(self.listeners):
            if etype is event_type:
                await handler(SimpleNamespace(event_type=event_type))


def _entry():
    unloads = []
    return SimpleNamespace(async_on_unload=unloads.append, unloads=unloads)


class _Process:
    """An asyncio subprocess stand-in that exits when terminated."""

    def __init__(self):
        self.returncode = None
        self.pid = 4242
        self.terminated = 0

    def terminate(self):
        self.terminated += 1
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


def test_stopping_home_assistant_stops_the_engine():
    """Kill: the listener not registered, or registered on another event."""
    hass = SimpleNamespace(bus=_Bus())
    manager = SimpleNamespace(stop=AsyncMock())
    cuboai._stop_engine_on_shutdown(hass, _entry(), manager)

    asyncio.run(hass.bus.fire(cuboai.EVENT_HOMEASSISTANT_STOP))
    manager.stop.assert_awaited_once()


def test_unloading_the_entry_removes_the_listener():
    """A reload must not stack listeners, each holding an old manager. Kill:
    the unsubscribe not handed to entry.async_on_unload."""
    hass = SimpleNamespace(bus=_Bus())
    entry = _entry()
    cuboai._stop_engine_on_shutdown(hass, entry, SimpleNamespace(stop=AsyncMock()))
    assert len(hass.bus.listeners) == 1 and len(entry.unloads) == 1

    for unload in entry.unloads:
        unload()
    assert hass.bus.listeners == []


def test_a_plain_listener_not_a_once_listener():
    """Removing a once-listener after it has fired is an error in Home
    Assistant. Kill: async_listen_once used."""
    hass = SimpleNamespace(bus=_Bus())
    cuboai._stop_engine_on_shutdown(hass, _entry(), SimpleNamespace(stop=AsyncMock()))
    assert hass.bus.once_calls == 0


def test_the_real_engine_is_terminated_and_not_respawned():
    """Through the real Go2RTCManager.stop(): the process is terminated and the
    watchdog is disarmed first, so it cannot restart the engine during the
    shutdown. A second stop event is harmless. Kill: stop() not called, or the
    watchdog left armed."""

    async def scenario():
        hass = MagicMock()
        hass.bus = _Bus()
        mgr = Go2RTCManager(hass, "entryA")
        proc = _Process()
        mgr.process = proc
        mgr._watchdog_task = asyncio.ensure_future(asyncio.sleep(3600))
        watchdog = mgr._watchdog_task
        cuboai._stop_engine_on_shutdown(hass, _entry(), mgr)

        await hass.bus.fire(cuboai.EVENT_HOMEASSISTANT_STOP)
        await asyncio.sleep(0)
        assert proc.terminated == 1
        assert mgr.process is None
        assert watchdog.cancelled() or watchdog.done()
        assert mgr._watchdog_task is None

        await hass.bus.fire(cuboai.EVENT_HOMEASSISTANT_STOP)
        assert proc.terminated == 1

    asyncio.run(scenario())


def test_setup_registers_it_right_after_the_engine_starts():
    """Kill: the call removed from async_setup_entry, or moved before start()
    (the manager would not exist yet in hass.data)."""
    src = INIT.read_text(encoding="utf-8")
    setup = src[src.index("async def async_setup_entry") : src.index("def _stop_engine_on_shutdown")]
    start = setup.index("await go2rtc_manager.start()")
    call = setup.index("_stop_engine_on_shutdown(hass, entry, go2rtc_manager)")
    assert start < call
