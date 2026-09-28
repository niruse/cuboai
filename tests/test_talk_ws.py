"""The card's microphone, Home Assistant side: the `cuboai/talk` command.

A talk runs in a child process fed from one websocket connection (talk.py).
This is a baby monitor, so most of what is pinned here is how a talk ENDS:
every way of losing the person talking must stop the child, the stop must
escalate until the child is gone, and the camera's speaker is handed back only
after that — to the next talk, or to the Speaker entity's songs.

Nothing here spawns a process or opens a socket: the child is a FakeProc, the
websocket a FakeConnection, and the talk timers run on a fake clock.

Every test names the mutation it kills.
"""

import asyncio
import logging
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import voluptuous as vol

from custom_components.cuboai import talk
from custom_components.cuboai.const import DOMAIN

DEVICE = "SW05TESTDEVICE01"
OTHER = "SW05TESTDEVICE02"
SPEAKER = "media_player.baby_speaker"
OTHER_SPEAKER = "media_player.other_speaker"
CREDS = {"uid": "UIDSECRET0000001", "account": "acct-secret-1", "password": "pw-s3cret-9"}
TICK = 0.01
GRACE = 0.3  # stands in for STOP/TERM/KILL grace: long enough to look inside, short enough to wait out

END_LINE = "@@TALK END sent=40 speech=31 rx=64000 dropped=2 camera=0"


# ── fakes ────────────────────────────────────────────────────────────────────


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, secs):
        self.now += secs


class FakeStates:
    def __init__(self):
        self._states = {}

    def get(self, entity_id):
        return self._states.get(entity_id)

    def set(self, entity_id, state):
        self._states[entity_id] = SimpleNamespace(state=state)


class FakeHass:
    """Just what talk.py and the Speaker's queue use; tasks really run."""

    def __init__(self, entries):
        self.data = {DOMAIN: {e.entry_id: {} for e in entries}}
        self.config_entries = SimpleNamespace(
            async_entries=lambda domain: list(entries) if domain == DOMAIN else [],
            async_unload_platforms=AsyncMock(return_value=True),
        )
        self.states = FakeStates()
        self.bus = SimpleNamespace(async_listen_once=MagicMock())
        self.config = SimpleNamespace(path=lambda *parts: "/nonexistent/" + "/".join(parts))
        self.is_stopping = False
        self.task_names = []

    def _task(self, coro, name):
        self.task_names.append(name)
        return asyncio.get_running_loop().create_task(coro)

    def async_create_task(self, coro, name=None, eager_start=True):
        return self._task(coro, name)

    def async_create_background_task(self, coro, name, eager_start=True):
        return self._task(coro, name)

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class FakeConnection:
    """ActiveConnection as talk.py uses it, with Home Assistant's binary
    dispatch: a handler that raises is silently unregistered.

    Handler ids start at 7 so a hard-coded id cannot pass by luck.
    """

    def __init__(self, allowed=True, first_handler_id=7):
        self.binary_handlers = [None] * (first_handler_id - 1)
        self.subscriptions = {}
        self.log = []  # ("result", id) / ("event", type) / ("error", code), in order
        self.events = []
        self.errors = []
        self.subscribed_at_result = []
        self.checked = []
        self.handler_raised = 0

        def check_entity(entity_id, policy):
            self.checked.append((entity_id, policy))
            return allowed

        self.user = SimpleNamespace(permissions=SimpleNamespace(check_entity=check_entity))

    def async_register_binary_handler(self, handler):
        self.binary_handlers.append(handler)
        index = len(self.binary_handlers) - 1

        def unsub():
            self.binary_handlers[index] = None

        return index + 1, unsub

    def send_binary(self, handler_id, payload):
        """What http.py + connection.py do with `[handler_id] + payload`."""
        handler = self.binary_handlers[handler_id - 1]
        if handler is None:
            return False
        try:
            handler(None, self, payload)
        except Exception:
            self.handler_raised += 1
            self.binary_handlers[handler_id - 1] = None
        return True

    def send_result(self, msg_id, result=None):
        self.subscribed_at_result.append(msg_id in self.subscriptions)
        self.log.append(("result", msg_id))

    def send_event(self, msg_id, event):
        self.events.append(event)
        self.log.append(("event", event["type"]))

    def send_error(self, msg_id, code, message):
        self.errors.append(code)
        self.log.append(("error", code))

    def close(self):
        """ActiveConnection.async_handle_close: call every subscription hook it
        holds NOW, then clear them. A hook added later is never called."""
        for hook in list(self.subscriptions.values()):
            hook()
        self.subscriptions.clear()

    def types(self):
        return [e["type"] for e in self.events]

    def event(self, kind):
        return next(e for e in self.events if e["type"] == kind)


class FakeTransport:
    def __init__(self):
        self.buffered = 0
        self.closing = False

    def get_write_buffer_size(self):
        return self.buffered

    def is_closing(self):
        return self.closing


class FakeStdin:
    def __init__(self, proc):
        self._proc = proc
        self.transport = FakeTransport()
        self.data = bytearray()
        self.closed = False
        self.fail = None

    def write(self, payload):
        if self.fail is not None:
            raise self.fail
        self.data += payload

    def close(self):
        if not self.closed:
            self.closed = True
            self.transport.closing = True
            self._proc.on_eof()


class FakeProc:
    """The talk child: `@@TALK` lines out, exits on stdin EOF and on SIGTERM
    (each can be refused, to walk the escalation), and on SIGKILL always.

    wait() really suspends, as reaping a process does.
    """

    def __init__(self, *, on_eof=True, on_term=True, end=END_LINE):
        self.stdin = FakeStdin(self)
        self.stdout = asyncio.StreamReader()
        self.returncode = None
        self.signals = []
        self._exited = asyncio.Event()
        self._exit_on_eof = on_eof
        self._exit_on_term = on_term
        self._end = end

    def emit(self, line):
        self.stdout.feed_data(line.encode() + b"\n")

    def exit(self, code=0):
        if self.returncode is not None:
            return
        if self._end:
            self.emit(self._end)
        self.returncode = code
        self.stdout.feed_eof()
        self._exited.set()

    def on_eof(self):
        self.signals.append("eof")
        if self._exit_on_eof:
            asyncio.get_running_loop().call_later(TICK, self.exit, 0)

    def terminate(self):
        self.signals.append("term")
        if self._exit_on_term:
            asyncio.get_running_loop().call_later(TICK, self.exit, 0)

    def kill(self):
        self.signals.append("kill")
        self._end = None  # a killed child writes nothing more
        asyncio.get_running_loop().call_later(TICK, self.exit, -9)

    async def wait(self):
        await self._exited.wait()
        return self.returncode


def _entry(entry_id, device_id, **options):
    cam = {"device_id": device_id, "baby_name": "Baby", "camera_ip": "10.0.0.9", **CREDS}
    return SimpleNamespace(entry_id=entry_id, data={"cameras": [cam]}, options=options, state=None)


class Rig:
    def __init__(self, monkeypatch):
        self.clock = FakeClock()
        self.entries = [_entry("entryA", DEVICE), _entry("entryB", OTHER)]
        self.hass = FakeHass(self.entries)
        self.conn = FakeConnection()
        self.spawned = []
        self.procs = []  # FakeProcs to hand out next; a default FakeProc otherwise
        self.spawn_error = None
        self.on_spawn = None
        self._ids = iter(range(10, 1000))
        speakers = {f"cuboai_speaker_{DEVICE}": SPEAKER, f"cuboai_speaker_{OTHER}": OTHER_SPEAKER}
        registry = SimpleNamespace(
            async_get_entity_id=lambda domain, platform, uid: (
                speakers.get(uid) if (domain, platform) == ("media_player", DOMAIN) else None
            )
        )
        monkeypatch.setattr(talk, "er", SimpleNamespace(async_get=lambda hass: registry))
        monkeypatch.setattr(talk, "_clock", self.clock)
        monkeypatch.setattr(talk, "TIMER_TICK_SECS", 3600)  # the tests drive _check_timers by hand
        for name in ("STOP_GRACE_SECS", "TERM_GRACE_SECS", "KILL_WAIT_SECS", "PLAYER_EXIT_GRACE_SECS"):
            monkeypatch.setattr(talk, name, GRACE)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", self._exec)

    async def _exec(self, *argv, **kwargs):
        if self.spawn_error is not None:
            raise self.spawn_error
        proc = self.procs.pop(0) if self.procs else FakeProc()
        self.spawned.append(SimpleNamespace(argv=list(argv), kwargs=kwargs, proc=proc))
        if self.on_spawn is not None:
            self.on_spawn()
        return proc

    def msg(self, **fields):
        """A command as Home Assistant hands it over: validated, defaults filled."""
        raw = {"id": next(self._ids), "type": "cuboai/talk", "device_id": DEVICE, "sample_rate": 16000, **fields}
        return vol.Schema({vol.Required("id"): int, **talk.ws_talk._ws_schema})(raw)

    async def start(self, conn=None, **fields):
        msg = self.msg(**fields)
        await talk.ws_talk(self.hass, conn or self.conn, msg)
        session = talk._talk_data(self.hass).sessions.get(msg["device_id"])
        return session, msg

    @property
    def arbiter(self):
        return talk.get_arbiter(self.hass)


@pytest.fixture
def rig(monkeypatch):
    return Rig(monkeypatch)


async def _closed(session, timeout=3.0):
    await asyncio.wait_for(session.wait_closed(), timeout)


def _handler_id(conn):
    return conn.event("started")["handler_id"]


# ── starting ─────────────────────────────────────────────────────────────────


async def test_the_stop_hook_is_registered_before_the_result(rig):
    """Kill: send_result() moved above `connection.subscriptions[msg_id] = ...`."""
    session, msg = await rig.start()
    assert rig.conn.subscribed_at_result == [True]
    assert rig.conn.subscriptions[msg["id"]] == session.request_stop
    await _stop(session)


async def test_started_follows_the_result_and_carries_the_real_handler_id(rig):
    """Kill: a hard-coded handler id, a missing started event, or started sent
    before the result (subscribeMessage drops events that come first)."""
    session, msg = await rig.start(max_secs=30)
    assert rig.conn.log[:2] == [("result", msg["id"]), ("event", "started")]
    started = rig.conn.event("started")
    assert started == {"type": "started", "handler_id": 7, "dry_run": False, "max_secs": 30}
    assert rig.conn.binary_handlers[6] == session.on_binary
    await _stop(session)


async def test_the_child_gets_the_contract_argv_and_the_credentials_only_in_its_env(rig):
    """Kill: a credential moved into argv (readable by every process on the box),
    the env not built from the camera, the options' camera IP ignored, or the
    child's cap not past Home Assistant's own."""
    rig.entries[0].options = {f"camera_ip_{DEVICE}": "10.0.0.77"}
    session, _msg = await rig.start(sample_rate=48000, max_secs=60)
    spawn = rig.spawned[0]
    assert spawn.argv == [
        sys.executable,
        talk.BACKCHANNEL_SCRIPT,
        "--live",
        "--in-codec",
        "pcm_s16le",
        "--in-rate",
        "48000",
        "--max-secs",
        "65",
    ]
    for secret in CREDS.values():
        assert not any(secret in arg for arg in spawn.argv)
    env = spawn.kwargs["env"]
    assert (env["CUBOAI_UID"], env["CUBOAI_ACCOUNT"], env["CUBOAI_PASSWORD"]) == (
        CREDS["uid"],
        CREDS["account"],
        CREDS["password"],
    )
    assert env["CUBOAI_CAMERA_IP"] == "10.0.0.77"
    assert spawn.kwargs["stdin"] == asyncio.subprocess.PIPE
    assert spawn.kwargs["stdout"] == asyncio.subprocess.PIPE
    assert spawn.kwargs["stderr"] == asyncio.subprocess.DEVNULL
    await _stop(session)


async def test_with_debug_logs_the_childs_stderr_goes_to_the_log_and_our_copy_is_closed(rig, tmp_path):
    """Kill: the debug log not used, or its file object left open after the
    spawn (the child holds its own copy; ours would leak an fd per talk)."""
    rig.hass.config = SimpleNamespace(path=lambda *parts: str(tmp_path.joinpath(*parts)))
    rig.entries[0].options = {"enable_debug_logs": True}
    session, _msg = await rig.start()
    stderr = rig.spawned[0].kwargs["stderr"]
    assert stderr.name == str(tmp_path / "cuboai_debug.log") and stderr.closed
    await _stop(session)


async def test_dry_run_reaches_the_child_and_the_client(rig):
    """Kill: dry_run dropped on the way to the child (a test that plays at the camera)."""
    session, _msg = await rig.start(dry_run=True)
    assert rig.spawned[0].argv[-1] == "--dry-run"
    assert rig.conn.event("started")["dry_run"] is True
    await _stop(session)
    other, _msg = await rig.start()
    assert "--dry-run" not in rig.spawned[1].argv
    await _stop(other)


def test_the_command_schema_is_the_contract():
    """Kill: a rate outside the list accepted, a float rate accepted (16000.0 is
    `in` the list, reaches argv as "16000.0" and the child refuses it: the user
    gets an internal error for a talk that should never have started), the
    5..120 bounds moved, or the defaults changed (max 120 s and not a dry run)."""
    schema = vol.Schema({vol.Required("id"): int, **talk.ws_talk._ws_schema})
    base = {"id": 1, "type": "cuboai/talk", "device_id": DEVICE}
    got = schema({**base, "sample_rate": 16000})
    assert (got["max_secs"], got["dry_run"]) == (120, False)
    for rate in talk.ALLOWED_RATES:
        schema({**base, "sample_rate": rate})
    for bad in (
        {"sample_rate": 11025},
        {"sample_rate": 16000.0},
        {"sample_rate": True},
        {"sample_rate": 16000, "max_secs": 4},
        {"sample_rate": 16000, "max_secs": 121},
    ):
        with pytest.raises(vol.Invalid):
            schema({**base, **bad})
    assert schema({**base, "sample_rate": 8000, "max_secs": 5})["max_secs"] == 5
    assert talk.ws_talk._ws_command == "cuboai/talk"


def test_the_owner_decisions_and_contract_numbers_are_pinned():
    """Kill: any of these moved without a decision (they are the owner's choices
    and the numbers the card and the child are built against)."""
    assert talk.TALK_MAX_SECS == 120
    assert talk.IDLE_SECS == 6
    assert talk.READY_TIMEOUT_SECS == 35
    assert (talk.STOP_GRACE_SECS, talk.TERM_GRACE_SECS) == (4, 3)
    assert (talk.MAX_PENDING_BYTES, talk.MAX_FRAME_BYTES) == (32000, 16384)
    assert talk.STATUS_EVENT_MIN_SECS == 5
    assert talk.ALLOWED_RATES == (8000, 16000, 22050, 24000, 32000, 44100, 48000)
    assert talk.TALK_DATA == "cuboai_talk" and talk.TALK_DATA != DOMAIN


# ── audio in ─────────────────────────────────────────────────────────────────


async def test_frames_go_to_the_childs_stdin_in_order(rig):
    """Kill: frames not written, or written out of order."""
    session, _msg = await rig.start()
    hid = _handler_id(rig.conn)
    rig.conn.send_binary(hid, b"\x01\x00" * 320)
    rig.conn.send_binary(hid, b"\x02\x00\x03")  # odd length: the child re-aligns
    assert bytes(rig.spawned[0].proc.stdin.data) == b"\x01\x00" * 320 + b"\x02\x00\x03"
    assert (session.rx_bytes, session.dropped_bytes) == (643, 0)
    await _stop(session)


async def test_the_binary_handler_never_raises(rig):
    """Kill: the try/except around on_binary removed — Home Assistant would then
    silently unregister the handler and the talk would go on without audio."""
    session, _msg = await rig.start()
    hid = _handler_id(rig.conn)
    proc = rig.spawned[0].proc

    proc.stdin.fail = BrokenPipeError("child gone")
    rig.conn.send_binary(hid, b"\x00" * 640)
    proc.stdin.fail = None
    proc.stdin.transport = None  # a stdin with no transport
    rig.conn.send_binary(hid, b"\x00" * 640)
    proc.stdin = None  # no stdin at all
    rig.conn.send_binary(hid, b"\x00" * 640)
    session.proc = None  # no child at all
    rig.conn.send_binary(hid, b"\x00" * 640)

    assert rig.conn.handler_raised == 0
    assert rig.conn.binary_handlers[hid - 1] == session.on_binary
    session.proc = proc
    proc.stdin = FakeStdin(proc)
    await _stop(session)


async def test_a_closed_stdin_drops_the_frame(rig):
    """Kill: writing into a pipe that is already closing."""
    session, _msg = await rig.start()
    proc = rig.spawned[0].proc
    proc.stdin.transport.closing = True
    rig.conn.send_binary(_handler_id(rig.conn), b"\x00" * 640)
    assert proc.stdin.data == b"" and session.dropped_bytes == 640
    proc.stdin.transport.closing = False
    await _stop(session)


async def test_back_pressure_drops_past_the_pending_limit_instead_of_queueing(rig):
    """Kill: `>` turned into `>=` (drops at the limit), or frames queued past it
    (a talk that lags instead of catching up)."""
    session, _msg = await rig.start()
    hid = _handler_id(rig.conn)
    stdin = rig.spawned[0].proc.stdin
    stdin.transport.buffered = talk.MAX_PENDING_BYTES
    rig.conn.send_binary(hid, b"a" * 640)
    stdin.transport.buffered = talk.MAX_PENDING_BYTES + 1
    rig.conn.send_binary(hid, b"b" * 640)
    assert bytes(stdin.data) == b"a" * 640
    assert (session.rx_bytes, session.dropped_bytes) == (1280, 640)
    await _stop(session)


async def test_oversize_frames_are_dropped(rig):
    """Kill: the frame-size check removed or off by one."""
    session, _msg = await rig.start()
    hid = _handler_id(rig.conn)
    rig.conn.send_binary(hid, b"x" * talk.MAX_FRAME_BYTES)
    rig.conn.send_binary(hid, b"y" * (talk.MAX_FRAME_BYTES + 1))
    assert bytes(rig.spawned[0].proc.stdin.data) == b"x" * talk.MAX_FRAME_BYTES
    assert session.dropped_bytes == talk.MAX_FRAME_BYTES + 1
    await _stop(session)


async def test_the_one_byte_end_frame_ends_the_talk(rig):
    """Kill: an empty payload written as audio or ignored (the talk would run
    on until the idle timer), or the handler left registered after the end."""
    session, _msg = await rig.start()
    hid = _handler_id(rig.conn)
    rig.conn.send_binary(hid, b"")
    assert session.stopping and session.reason == talk.REASON_CLIENT_END
    assert rig.spawned[0].proc.signals == ["eof"]
    assert rig.conn.binary_handlers[hid - 1] is None
    await _closed(session)
    ended = rig.conn.event("ended")
    assert ended == {"type": "ended", "reason": "client_end", "sent": 40, "speech": 31, "camera": False}
    assert rig.conn.types().count("ended") == 1


# ── the timers ───────────────────────────────────────────────────────────────


async def test_the_idle_timer_stops_a_talk_with_no_audio_for_6_s(rig):
    """Kill: the idle timer removed, its 6 s moved, or frames not resetting it."""
    session, _msg = await rig.start()
    hid = _handler_id(rig.conn)
    rig.clock.advance(5.0)
    rig.conn.send_binary(hid, b"\x00" * 640)
    rig.clock.advance(5.75)  # binary-exact steps: the 6 s edge is compared exactly
    session._check_timers()
    assert not session.stopping
    rig.clock.advance(0.25)
    session._check_timers()
    assert session.reason == talk.REASON_IDLE
    await _closed(session)
    assert rig.conn.event("ended")["reason"] == "idle"
    assert "error" not in rig.conn.types()


async def test_the_max_duration_ends_the_talk(rig):
    """Kill: the max-duration timer removed or measured from the wrong moment."""
    session, _msg = await rig.start(max_secs=5)
    rig.spawned[0].proc.emit("@@TALK READY")
    await asyncio.sleep(TICK)
    hid = _handler_id(rig.conn)
    for _ in range(4):
        rig.clock.advance(1.0)
        rig.conn.send_binary(hid, b"\x00" * 640)
    rig.clock.advance(0.75)
    session._check_timers()
    assert not session.stopping
    rig.clock.advance(0.25)
    session._check_timers()
    assert session.reason == talk.REASON_MAX
    await _closed(session)
    assert rig.conn.event("ended")["reason"] == "max_duration"


async def test_no_ready_within_35_s_is_a_camera_timeout(rig):
    """Kill: the ready timer removed or moved, or it firing once the camera is live."""
    session, _msg = await rig.start()
    hid = _handler_id(rig.conn)
    for _ in range(34):
        rig.clock.advance(1.0)
        rig.conn.send_binary(hid, b"\x00" * 640)
    rig.clock.advance(0.75)
    session._check_timers()
    assert not session.stopping
    rig.clock.advance(0.25)
    session._check_timers()
    assert rig.conn.event("error") == {
        "type": "error",
        "code": "camera_timeout",
        "message": talk.ERROR_TEXT["camera_timeout"],
    }
    await _closed(session)
    assert rig.conn.event("ended")["reason"] == "camera_timeout"


async def test_a_live_talk_has_no_ready_timeout(rig):
    """Kill: the ready timer still armed after READY."""
    session, _msg = await rig.start()
    rig.spawned[0].proc.emit("@@TALK READY")
    await asyncio.sleep(TICK)
    hid = _handler_id(rig.conn)
    for _ in range(40):
        rig.clock.advance(1.0)
        rig.conn.send_binary(hid, b"\x00" * 640)
        session._check_timers()
    assert not session.stopping
    await _stop(session)


async def test_the_timers_run_by_themselves(rig, monkeypatch):
    """Kill: the timer task never started (every timer would be dead code)."""
    monkeypatch.setattr(talk, "TIMER_TICK_SECS", TICK)
    session, _msg = await rig.start()
    rig.clock.advance(talk.IDLE_SECS)
    await _closed(session)
    assert session.reason == talk.REASON_IDLE


# ── stopping ─────────────────────────────────────────────────────────────────


async def _stop(session, reason="client_end"):
    session.request_stop(reason)
    await _closed(session)


async def test_the_stop_escalates_from_eof_to_sigterm_to_sigkill(rig):
    """Kill: SIGTERM sent before the EOF grace (the child's tail and SPEAKERSTOP
    cut short), or an escalation step missing (a child that ignores EOF and
    SIGTERM would keep the camera in talk mode)."""
    rig.procs.append(FakeProc(on_eof=False, on_term=False))
    session, _msg = await rig.start()
    proc = rig.spawned[0].proc
    session.request_stop(talk.REASON_CLIENT_END)
    await asyncio.sleep(GRACE / 3)
    assert proc.signals == ["eof"]
    await asyncio.sleep(GRACE)
    assert proc.signals == ["eof", "term"]
    await _closed(session)
    assert proc.signals == ["eof", "term", "kill"]
    # killed: no END line, so the talk cannot claim the camera was untouched
    assert rig.conn.event("ended")["camera"] is True


async def test_the_speaker_is_released_only_after_the_child_exits(rig):
    """Kill: the arbiter released when the stop is requested rather than when the
    child is gone — a new talk or a song would start while it still talks."""
    rig.procs.append(FakeProc(on_eof=False))
    session, _msg = await rig.start()
    assert talk.mic_active(rig.hass, DEVICE)
    session.request_stop(talk.REASON_CLIENT_END)
    await asyncio.sleep(GRACE / 2)
    assert rig.spawned[0].proc.returncode is None
    assert rig.arbiter.owner(DEVICE) == "mic" and talk.mic_active(rig.hass, DEVICE)
    assert talk._talk_data(rig.hass).sessions.get(DEVICE) is session
    await _closed(session)
    assert rig.spawned[0].proc.returncode is not None
    assert rig.arbiter.owner(DEVICE) is None and not talk.mic_active(rig.hass, DEVICE)
    assert DEVICE not in talk._talk_data(rig.hass).sessions


async def test_stopping_twice_is_one_stop(rig):
    """Kill: request_stop not idempotent (a second stop task, a second SIGTERM,
    the first reason overwritten)."""
    session, msg = await rig.start()
    stop = rig.conn.subscriptions[msg["id"]]
    session.request_stop(talk.REASON_IDLE)
    stop()
    stop()
    session.request_stop(talk.REASON_MAX)
    await _closed(session)
    assert sum(1 for n in rig.hass.task_names if n.startswith("cuboai talk stop")) == 1
    assert session.reason == talk.REASON_IDLE
    assert rig.spawned[0].proc.signals == ["eof"]


async def test_unsubscribing_stops_the_talk_and_nothing_more_is_sent(rig):
    """Kill: the subscription hook not stopping the talk, or events still sent to
    a client that has gone (its frontend answers each with another unsubscribe)."""
    session, msg = await rig.start()
    rig.conn.subscriptions.pop(msg["id"])()  # unsubscribe_events / socket close
    assert session.stopping and session.reason == talk.REASON_UNSUBSCRIBED
    await _closed(session)
    assert "ended" not in rig.conn.types()
    assert rig.arbiter.owner(DEVICE) is None


async def test_a_stop_while_the_child_is_starting_still_stops_it(rig):
    """Kill: begin() ignoring a stop that arrived during the spawn (Home Assistant
    stopping mid-start would leave that child talking)."""
    rig.on_spawn = lambda: talk._talk_data(rig.hass).sessions[DEVICE].request_stop(talk.REASON_SHUTDOWN)
    session, _msg = await rig.start()
    assert rig.spawned[0].proc.signals == ["eof"]
    await _closed(session)
    assert rig.conn.types() == ["started", "ended"]
    assert rig.conn.event("ended")["reason"] == "shutdown"


async def test_a_stop_the_reader_makes_inside_begin_is_still_one_stop(rig):
    """Home Assistant starts tasks eagerly (eager_start=True), so the reader that
    begin() creates runs at once, and an ERROR line already waiting stops the
    talk before begin() returns. The fake runs tasks lazily; this stands in for
    that eager first step. Kill: begin() scheduling a second stop task for a
    stop already under way (two escalations racing each other's grace)."""
    create = rig.hass.async_create_background_task

    def eager(coro, name, eager_start=True):
        task = create(coro, name)
        if name.startswith("cuboai talk reader"):
            talk._talk_data(rig.hass).sessions[DEVICE]._on_line(b"@@TALK ERROR camera_lost\n")
        return task

    rig.hass.async_create_background_task = eager
    session, _msg = await rig.start()
    await _closed(session)
    assert sum(1 for n in rig.hass.task_names if n.startswith("cuboai talk stop")) == 1
    assert session.reason == "camera_lost"
    assert rig.conn.types() == ["started", "error", "ended"]


async def test_a_websocket_that_closes_during_the_spawn_stops_the_child(rig):
    """The phone locks (or 5G drops) while the child is starting. Home Assistant
    calls the hooks it holds when the socket closes, then clears them; this
    handler runs on. Kill: the stop hook set only after the spawn (the child
    would run for nobody until the idle timer, SPEAKERSTART clicking at the
    camera meanwhile), or the dead connection still given a binary handler,
    a result or events."""
    rig.on_spawn = rig.conn.close
    session, _msg = await rig.start()
    proc = rig.spawned[0].proc
    assert session.stopping and session.reason == talk.REASON_UNSUBSCRIBED
    assert proc.signals == ["eof"], "the child was not told to stop"
    assert rig.conn.subscriptions == {} and rig.conn.log == []
    assert rig.conn.binary_handlers == [None] * 6  # none registered
    await _closed(session)
    assert rig.arbiter.owner(DEVICE) is None and rig.conn.log == []


async def test_a_websocket_that_closes_while_waiting_for_the_last_talk_starts_nothing(rig):
    """Stop, then talk again at once: the new request waits for the old child's
    SPEAKERSTOP, and the phone drops meanwhile. Kill: no hook in place during
    that wait (a real talk would start for a client that is gone)."""
    rig.procs.append(FakeProc(on_eof=False))  # exits only on SIGTERM, after GRACE
    first, _msg = await rig.start()
    first.request_stop(talk.REASON_CLIENT_END)
    second_conn = FakeConnection()
    waiting = asyncio.ensure_future(rig.start(conn=second_conn))
    await asyncio.sleep(TICK * 3)
    assert not waiting.done()
    second_conn.close()
    second, _msg = await asyncio.wait_for(waiting, 3)
    assert second is None and len(rig.spawned) == 1, "a talk started for a client that had left"
    assert second_conn.log == [] and second_conn.subscriptions == {}
    assert rig.arbiter.owner(DEVICE) is None


async def test_a_talk_waiting_for_the_last_one_does_not_hold_a_hook_after_it_starts(rig):
    """Kill: the waiting request's own hook left in place (or replaced by the
    wrong one): the client's unsubscribe would then miss the talk it started."""
    rig.procs.append(FakeProc(on_eof=False))
    first, _msg = await rig.start()
    first.request_stop(talk.REASON_CLIENT_END)
    second_conn = FakeConnection()
    second, msg = await rig.start(conn=second_conn)
    assert second is not first and second_conn.errors == []
    assert second_conn.subscriptions == {msg["id"]: second.request_stop}
    second_conn.subscriptions.pop(msg["id"])()
    assert second.stopping and second.reason == talk.REASON_UNSUBSCRIBED
    await _closed(second)


# ── the child's lines ────────────────────────────────────────────────────────


async def test_ready_sends_live_exactly_once(rig):
    """Kill: `live` sent on every READY line, or never."""
    session, _msg = await rig.start()
    proc = rig.spawned[0].proc
    proc.emit("@@TALK READY")
    proc.emit("@@TALK READY")
    await asyncio.sleep(TICK)
    assert rig.conn.types().count("live") == 1
    await _stop(session)


async def test_only_talk_protocol_lines_count(rig):
    """Kill: a stray line read as protocol (the child's prints are not ours)."""
    session, _msg = await rig.start()
    proc = rig.spawned[0].proc
    proc.emit("READY")
    proc.emit("[pure] @@TALK READY")
    proc.emit("@@TALKREADY")
    await asyncio.sleep(TICK)
    assert not session.live and "live" not in rig.conn.types()
    await _stop(session)


async def test_status_events_are_throttled_to_one_per_5_s(rig):
    """Kill: the throttle removed (outbound events are what disconnect a slow client)."""
    session, _msg = await rig.start()
    proc = rig.spawned[0].proc
    proc.emit("@@TALK STATUS sent=16 speech=10 delivered=12 rx=20480 dropped=0")
    await asyncio.sleep(TICK)
    rig.clock.advance(4.75)
    proc.emit("@@TALK STATUS sent=32 speech=20 delivered=30 rx=40960 dropped=1")
    await asyncio.sleep(TICK)
    rig.clock.advance(0.25)
    proc.emit("@@TALK STATUS sent=48 speech=30 delivered=45 rx=61440 dropped=1")
    await asyncio.sleep(TICK)
    statuses = [e for e in rig.conn.events if e["type"] == "status"]
    assert statuses == [
        {"type": "status", "sent": 16, "speech": 10, "rx": 20480, "dropped": 0},
        {"type": "status", "sent": 48, "speech": 30, "rx": 61440, "dropped": 1},
    ]
    await _stop(session)


async def test_a_child_error_becomes_fixed_text_and_ends_the_talk(rig, caplog):
    """Kill: the child's raw text passed to the client, the talk left running
    after an error, or an unknown code passed through."""
    session, _msg = await rig.start()
    rig.spawned[0].proc.emit("@@TALK ERROR camera_lost")
    await _closed(session)
    assert rig.conn.event("error") == {
        "type": "error",
        "code": "camera_lost",
        "message": talk.ERROR_TEXT["camera_lost"],
    }
    assert rig.conn.event("ended")["reason"] == "camera_lost"

    other, _msg = await rig.start()
    with caplog.at_level(logging.WARNING, logger=talk.__name__):
        rig.spawned[1].proc.emit("@@TALK ERROR <script>alert(1)</script>")
        await _closed(other)
    error = [e for e in rig.conn.events if e["type"] == "error"][-1]
    assert error == {"type": "error", "code": "internal", "message": talk.ERROR_TEXT["internal"]}
    assert "<script>" in caplog.text  # the raw line is in the log, and only there


async def test_an_error_after_the_stop_is_not_reported(rig):
    """Kill: the person who just stopped talking told the camera was lost during
    the tail."""
    rig.procs.append(FakeProc(on_eof=False))
    session, _msg = await rig.start()
    session.request_stop(talk.REASON_CLIENT_END)
    rig.spawned[0].proc.emit("@@TALK ERROR camera_lost")
    await _closed(session)
    assert "error" not in rig.conn.types()
    assert rig.conn.event("ended")["reason"] == "client_end"


async def test_a_child_that_ends_by_itself_ends_the_talk(rig):
    """Kill: the reader's EOF not ending the talk (the subscription and the
    speaker would stay held by a dead child)."""
    session, _msg = await rig.start(dry_run=True)
    rig.spawned[0].proc.exit(0)
    await _closed(session)
    assert rig.conn.event("ended") == {
        "type": "ended",
        "reason": "child_exit",
        "sent": 40,
        "speech": 31,
        "camera": False,
    }
    assert rig.arbiter.owner(DEVICE) is None


# ── refusals ─────────────────────────────────────────────────────────────────


async def test_a_second_talk_on_the_same_camera_is_busy(rig):
    """Kill: the busy check removed (two talkers on one camera speaker)."""
    session, _msg = await rig.start()
    other_conn = FakeConnection()
    await rig.start(conn=other_conn)
    assert other_conn.errors == ["busy"] and len(rig.spawned) == 1
    assert other_conn.subscriptions == {}
    await _stop(session)


async def test_a_playing_speaker_refuses_the_talk(rig):
    """Kill: the speaker-state check removed (the talk would cut into a song or
    a lullaby the Speaker delegated)."""
    rig.hass.states.set(SPEAKER, "playing")
    await rig.start()
    assert rig.conn.errors == ["speaker_busy"] and rig.spawned == []
    assert rig.arbiter.owner(DEVICE) is None


async def test_a_speaker_child_still_exiting_refuses_the_talk(rig):
    """Kill: only the entity state checked — a stopped song's child still sending
    SPEAKERSTOP is a talker too."""
    rig.hass.states.set(SPEAKER, "idle")
    assert rig.arbiter.try_claim(DEVICE, "player")
    await rig.start()
    assert rig.conn.errors == ["speaker_busy"] and rig.spawned == []


async def test_an_unknown_or_unloaded_camera_is_not_found(rig):
    """Kill: a talk started for a camera that is not set up (or whose entry was
    unloaded), or with no Speaker entity to authorize against."""
    await rig.start(device_id="NOPE")
    del rig.hass.data[DOMAIN]["entryB"]
    await rig.start(device_id=OTHER)
    assert rig.conn.errors == ["not_found", "not_found"] and rig.spawned == []


async def test_the_talk_needs_control_of_the_speaker_entity(rig):
    """Kill: the permission check removed, or made on another entity or policy."""
    denied = FakeConnection(allowed=False)
    with pytest.raises(talk.Unauthorized):
        await rig.start(conn=denied)
    assert denied.checked == [(SPEAKER, talk.POLICY_CONTROL)]
    assert rig.spawned == [] and rig.arbiter.owner(DEVICE) is None


async def test_a_failed_spawn_gives_the_speaker_back(rig):
    """Kill: the claim kept after the child failed to start (the camera could
    never be talked through again until a restart)."""
    rig.spawn_error = OSError("no python")
    await rig.start()
    assert rig.conn.errors == ["spawn_failed"] and rig.conn.subscriptions == {}
    assert rig.arbiter.owner(DEVICE) is None and talk._talk_data(rig.hass).sessions == {}


async def test_no_free_binary_handler_stops_the_child_and_gives_the_speaker_back(rig):
    """Every binary handler slot of the connection is taken. Kill: the child left
    running (no EOF, no reader, no timers: a real talk would click SPEAKERSTART
    and send silence for max_secs + 5 s, and nothing would release the speaker
    after it — every later talk `busy`, every song refused), or the refused
    request's hook left on the connection."""
    conn = FakeConnection()

    def no_slot(handler):
        raise RuntimeError("too many binary handlers")

    conn.async_register_binary_handler = no_slot
    await rig.start(conn=conn)
    assert conn.errors == ["spawn_failed"] and conn.subscriptions == {}
    proc = rig.spawned[0].proc
    assert proc.signals == ["eof"]
    for _ in range(100):
        if rig.arbiter.owner(DEVICE) is None:
            break
        await asyncio.sleep(TICK)
    assert proc.returncode is not None
    assert rig.arbiter.owner(DEVICE) is None and talk._talk_data(rig.hass).sessions == {}


async def test_a_start_cancelled_mid_spawn_gives_the_speaker_back(rig):
    """Kill: a start cancelled during the spawn (Home Assistant stopping) keeping
    its claim, its place in the session map, or its hook on the connection."""
    rig.spawn_error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await rig.start()
    assert rig.arbiter.owner(DEVICE) is None and talk._talk_data(rig.hass).sessions == {}
    assert rig.conn.subscriptions == {}


async def test_a_new_talk_waits_for_a_stopping_one_to_exit(rig):
    """Kill: a stopping talk refused as busy (stop-then-talk would fail), or not
    awaited (two children on one camera)."""
    rig.procs.append(FakeProc(on_eof=False))
    first, _msg = await rig.start()
    first.request_stop(talk.REASON_CLIENT_END)
    first_exit_at_second_spawn = []
    rig.on_spawn = lambda: first_exit_at_second_spawn.append(rig.spawned[0].proc.returncode)
    second_conn = FakeConnection()
    second, _msg = await rig.start(conn=second_conn)
    assert second_conn.errors == []
    assert first_exit_at_second_spawn == [0], "spawned while the first child still ran"
    assert second is not first and rig.arbiter.owner(DEVICE) == "mic"
    await _stop(second)


# ── setup, unload, shutdown ──────────────────────────────────────────────────


async def test_home_assistant_stopping_ends_every_talk(rig):
    """Kill: the stop listener not registered, or not stopping every talk."""
    websocket_api = sys.modules["homeassistant.components.websocket_api"]
    websocket_api.async_register_command.reset_mock()
    talk.async_setup_talk(rig.hass)
    talk.async_setup_talk(rig.hass)  # once only
    websocket_api.async_register_command.assert_called_once_with(rig.hass, talk.ws_talk)
    event, listener = rig.hass.bus.async_listen_once.call_args.args
    assert event is talk.EVENT_HOMEASSISTANT_STOP

    first, _msg = await rig.start()
    second, _msg = await rig.start(conn=FakeConnection(), device_id=OTHER)
    await asyncio.wait_for(listener(None), 3)
    assert first.reason == second.reason == talk.REASON_SHUTDOWN
    assert talk._talk_data(rig.hass).sessions == {}
    assert rig.arbiter.owner(DEVICE) is None and rig.arbiter.owner(OTHER) is None


async def test_unloading_an_entry_ends_only_its_talks(rig):
    """Kill: the unload hook removed from async_unload_entry, or stopping every
    camera's talk instead of that entry's."""
    import custom_components.cuboai as integration

    mine, _msg = await rig.start()
    theirs, _msg = await rig.start(conn=FakeConnection(), device_id=OTHER)
    assert await integration.async_unload_entry(rig.hass, rig.entries[0])
    assert mine.reason == talk.REASON_UNLOADED and mine.proc.returncode is not None
    assert not theirs.stopping
    await _stop(theirs)


async def test_no_talk_starts_on_an_entry_that_is_unloading(rig):
    """The unload stops the entry's talks first, and its store goes only after
    the platforms unload. Kill: a talk that waited for a stopping one (or that
    arrives during the platform unload) starting anyway, with the old entry's
    camera and credentials, and outliving the unload; or the camera looked up
    only before that wait. A re-setup (a fresh store) talks again."""
    rig.procs.append(FakeProc(on_eof=False))
    first, _msg = await rig.start()
    first.request_stop(talk.REASON_CLIENT_END)
    waiting_conn = FakeConnection()
    waiting = asyncio.ensure_future(rig.start(conn=waiting_conn))
    await asyncio.sleep(TICK * 3)
    await talk.async_stop_for_entry(rig.hass, rig.entries[0])
    await asyncio.wait_for(waiting, 3)
    assert waiting_conn.errors == ["not_found"] and len(rig.spawned) == 1
    assert waiting_conn.subscriptions == {}, "the refused request left its hook behind"
    during_unload = FakeConnection()
    await rig.start(conn=during_unload)
    assert during_unload.errors == ["not_found"] and len(rig.spawned) == 1
    assert rig.arbiter.owner(DEVICE) is None

    rig.hass.data[DOMAIN]["entryA"] = {}  # set up again
    again, _msg = await rig.start(conn=FakeConnection())
    assert again is not None and len(rig.spawned) == 2
    await _stop(again)


async def test_home_assistant_stopping_refuses_a_talk_that_waited(rig):
    """Kill: a talk that waited for a stopping one starting while Home Assistant
    itself stops (its stop event has already ended every talk it knew of)."""
    rig.procs.append(FakeProc(on_eof=False))
    first, _msg = await rig.start()
    first.request_stop(talk.REASON_CLIENT_END)
    waiting_conn = FakeConnection()
    waiting = asyncio.ensure_future(rig.start(conn=waiting_conn))
    await asyncio.sleep(TICK * 3)
    rig.hass.is_stopping = True
    await asyncio.wait_for(waiting, 3)
    assert waiting_conn.errors == ["spawn_failed"] and len(rig.spawned) == 1
    assert rig.arbiter.owner(DEVICE) is None


def test_setup_registers_the_command():
    """Kill: the async_setup hook removed (the card's mic would get 'unknown command')."""
    import ast
    import inspect

    import custom_components.cuboai as integration

    tree = ast.parse(inspect.getsource(integration.async_setup))
    calls = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert "talk.async_setup_talk" in calls


async def test_the_log_never_carries_credentials(rig, caplog):
    """Kill: the env or argv logged (the env carries the camera's password)."""
    with caplog.at_level(logging.DEBUG, logger=talk.__name__):
        session, _msg = await rig.start()
        rig.spawned[0].proc.emit("@@TALK ERROR camera_refused")
        await _closed(session)
    assert "Talk started" in caplog.text and "Talk ended" in caplog.text
    for secret in CREDS.values():
        assert secret not in caplog.text


# ── the arbiter, and the Speaker entity's side of it ─────────────────────────


def test_one_talker_per_camera_speaker():
    """Kill: a talk claimable over a song or a second talk, or the player's
    stacking claims released all at once."""
    arbiter = talk.SpeakerArbiter(hass=None)
    assert arbiter.try_claim(DEVICE, "mic")
    assert not arbiter.try_claim(DEVICE, "mic")
    assert not arbiter.try_claim(DEVICE, "player")
    assert arbiter.try_claim(OTHER, "player")  # per camera
    arbiter.release(DEVICE, "player")  # not the owner: ignored
    assert arbiter.owner(DEVICE) == "mic"
    arbiter.release(DEVICE, "mic")
    assert arbiter.owner(DEVICE) is None

    assert arbiter.try_claim(DEVICE, "player") and arbiter.try_claim(DEVICE, "player")
    assert not arbiter.try_claim(DEVICE, "mic")
    arbiter.release(DEVICE, "player")
    assert arbiter.owner(DEVICE) == "player"
    arbiter.release(DEVICE, "player")
    assert arbiter.owner(DEVICE) is None


async def test_release_after_exit_waits_then_kills_a_child_that_stays(rig):
    """Kill: release_after_exit releasing straight away, or never killing a child
    that outlives its grace."""
    proc = FakeProc(on_term=False)
    rig.arbiter.try_claim(DEVICE, "player")
    rig.arbiter.release_after_exit(DEVICE, "player", proc)
    await asyncio.sleep(GRACE / 2)
    assert rig.arbiter.owner(DEVICE) == "player" and proc.signals == []
    await asyncio.sleep(GRACE)
    assert proc.signals == ["kill"]
    await asyncio.sleep(TICK * 3)
    assert rig.arbiter.owner(DEVICE) is None


def _speaker_entity(hass):
    from custom_components.cuboai import media_player

    entity = object.__new__(media_player.CuboAIMediaPlayer)
    entity.hass = hass
    entity.entity_id = SPEAKER
    entity._cam = {"device_id": DEVICE, "baby_name": "Baby", **CREDS}
    entity._options = {}
    entity._device_id = DEVICE
    entity._queue = []
    entity._queue_task = None
    entity._attr_repeat = media_player.RepeatMode.OFF
    entity.async_write_ha_state = lambda: None
    entity._extract_media_url = AsyncMock(side_effect=lambda media_id: media_id)
    return entity


async def test_the_speaker_refuses_to_play_over_a_live_talk(rig):
    """Kill: the mic check removed from async_play_media (a song or TTS would cut
    into the talk), or placed inside its swallow-everything try."""
    entity = _speaker_entity(rig.hass)
    rig.arbiter.try_claim(DEVICE, "mic")
    with pytest.raises(RuntimeError, match="live talk"):  # HomeAssistantError in these stubs
        await entity.async_play_media("music", "http://127.0.0.1:8123/local/song.mp3")
    assert entity._queue == [] and entity._queue_task is None


async def test_the_speaker_child_holds_the_speaker_until_it_exits(rig):
    """Kill: the Speaker's child spawned without a claim (a talk could start over
    a song), or the claim never released (no talk after the first song)."""
    entity = _speaker_entity(rig.hass)
    owner_at_spawn = []

    def _spawned():
        owner_at_spawn.append(rig.arbiter.owner(DEVICE))
        asyncio.get_running_loop().call_later(TICK, rig.spawned[-1].proc.exit, 0)

    rig.on_spawn = _spawned
    entity._queue = ["http://127.0.0.1:8123/api/tts_proxy/hello.mp3"]
    await asyncio.wait_for(entity._queue_loop(), 3)
    assert owner_at_spawn == ["player"]
    spawn = rig.spawned[0]
    assert spawn.argv == [sys.executable, talk.BACKCHANNEL_SCRIPT, "http://127.0.0.1:8123/api/tts_proxy/hello.mp3"]
    assert spawn.kwargs["env"]["CUBOAI_PASSWORD"] == CREDS["password"]
    assert rig.arbiter.owner(DEVICE) is None


async def test_a_cancelled_song_holds_the_speaker_until_its_child_exits(rig):
    """The user stops a song (the queue task is cancelled): its child is sent
    SIGTERM but is still sending SPEAKERSTOP. Kill: the claim released at once
    in the queue's `finally` (release() instead of release_after_exit()) — the
    talk the user taps next would open a second talk session on the camera."""
    entity = _speaker_entity(rig.hass)
    proc = FakeProc(on_term=False)
    rig.procs.append(proc)
    entity._queue = ["http://127.0.0.1:8123/local/a.mp3"]
    task = asyncio.get_running_loop().create_task(entity._queue_loop())
    entity._queue_task = task
    for _ in range(100):
        if rig.spawned:
            break
        await asyncio.sleep(TICK)
    await asyncio.sleep(TICK)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert proc.signals == ["term"] and proc.returncode is None
    assert rig.arbiter.owner(DEVICE) == "player", "released while the child still talks"
    await rig.start()
    assert rig.conn.errors == ["speaker_busy"] and len(rig.spawned) == 1
    proc.exit(0)
    await asyncio.sleep(TICK * 5)
    assert rig.arbiter.owner(DEVICE) is None


async def test_the_speaker_queue_stops_when_a_talk_holds_the_speaker(rig):
    """Kill: the queue's claim check removed (a queued song would start mid-talk)."""
    entity = _speaker_entity(rig.hass)
    rig.arbiter.try_claim(DEVICE, "mic")
    entity._queue = ["http://127.0.0.1:8123/local/a.mp3", "http://127.0.0.1:8123/local/b.mp3"]
    await asyncio.wait_for(entity._queue_loop(), 3)
    assert rig.spawned == [] and entity._queue == []
    assert rig.arbiter.owner(DEVICE) == "mic"
