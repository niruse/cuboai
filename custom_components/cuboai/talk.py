"""Live talk: the card's microphone, relayed through the Home Assistant websocket.

The card captures 16 kHz PCM and sends it as binary websocket frames after
starting a `cuboai/talk` subscription. Home Assistant only relays those bytes:
each talk runs in its own child process (`tutk/cuboai_stream_backchannel.py
--live`), which opens a camera session, encodes AAC and drives the camera's talk
channel. The child also receives the camera's full live stream and decodes every
packet in pure Python — 5-7% of a core with the GIL held, which must not run
inside Home Assistant. The process boundary also gives a clean stop: stdin EOF
ends the talk (the child sends SPEAKERSTOP), SIGTERM runs the same `finally`,
and if Home Assistant dies the child sees EOF too.

This is a baby monitor: a talk must never outlive the person talking. Every way
of losing the talker stops it — the card's end frame, unsubscribing, the
websocket closing, no audio for IDLE_SECS, the TALK_MAX_SECS cap, no camera
grant within READY_TIMEOUT_SECS, the entry unloading and Home Assistant stopping.

One talker per camera speaker: SpeakerArbiter is shared with the Speaker media
player, whose songs and TTS use the same talk channel through the same script.
"""

import asyncio
import logging
import math
import os
import sys
import time
from contextlib import suppress

import voluptuous as vol
from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.components import websocket_api
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

#: hass.data key for this module's state. Its own key, not hass.data[DOMAIN],
#: whose values are the per-entry stores.
TALK_DATA = "cuboai_talk"
#: Set in an entry's store (hass.data[DOMAIN][entry_id]) once its unload has
#: begun: no new talk may start on it. A re-setup builds a fresh store.
STORE_UNLOADING = "talk_unloading"

TALK_MAX_SECS = 120  # longest single talk (the card's own backup timer is 125 s)
MIN_TALK_SECS = 5
IDLE_SECS = 6  # no audio frame for this long ends the talk; the card streams its silence too
READY_TIMEOUT_SECS = 35  # the camera must start pulling audio within this
STOP_GRACE_SECS = 4  # after stdin EOF: the silent tail, SPEAKERSTOP, the close burst, exit
TERM_GRACE_SECS = 3  # after SIGTERM, which runs the same finally (SPEAKERSTOP still goes out)
KILL_WAIT_SECS = 5
READER_DRAIN_SECS = 2  # after the child exits, for its last lines (END) to be read
PREVIOUS_TALK_WAIT_SECS = 5  # a new talk waits this long for a stopping one to exit
PLAYER_EXIT_GRACE_SECS = 5  # release_after_exit: then kill
SHUTDOWN_WAIT_SECS = STOP_GRACE_SECS + TERM_GRACE_SECS + KILL_WAIT_SECS + READER_DRAIN_SECS + 1
#: Back-pressure. Beyond ~1 s of 16 kHz s16 waiting for the child, frames are
#: DROPPED, not queued: a talk that falls behind must catch up, never lag.
MAX_PENDING_BYTES = 32000
MAX_FRAME_BYTES = 16384
#: Outbound events stay rare: Home Assistant disconnects a client whose pending
#: outbound messages pile up (MAX_PENDING_MSG / PENDING_MSG_PEAK), and a slow
#: phone link must not lose its websocket because of a debug counter.
STATUS_EVENT_MIN_SECS = 5
TIMER_TICK_SECS = 0.5
ALLOWED_RATES = (8000, 16000, 22050, 24000, 32000, 44100, 48000)
#: The child's own cap sits past Home Assistant's, so the HA timer (which reports
#: `max_duration`) always fires first and the child's cap is only the backstop.
CHILD_MAX_SECS_MARGIN = 5

BACKCHANNEL_SCRIPT = os.path.join(os.path.dirname(__file__), "tutk", "cuboai_stream_backchannel.py")

#: MediaPlayerState.PLAYING. A literal, so this module needs no media_player import.
_STATE_PLAYING = "playing"

# Refusals (send_error codes). "unauthorized" comes from raising Unauthorized.
ERR_NOT_FOUND = "not_found"
ERR_SPEAKER_BUSY = "speaker_busy"
ERR_BUSY = "busy"
ERR_SPAWN_FAILED = "spawn_failed"
REFUSAL_TEXT = {
    ERR_NOT_FOUND: "No CuboAI camera with that device_id is set up for local access.",
    ERR_SPEAKER_BUSY: "The camera speaker is playing. Stop it first.",
    ERR_BUSY: "Someone is already talking through this camera.",
    ERR_SPAWN_FAILED: "The talk could not be started.",
}

# Errors reported during a talk (`error` events). The first four are the child's
# ERROR codes; camera_timeout is Home Assistant's own ready timer. A client only
# ever sees this fixed text; the child's raw line goes to the log.
ERR_CAMERA_TIMEOUT = "camera_timeout"
ERR_INTERNAL = "internal"
CHILD_ERROR_CODES = frozenset({"camera_unreachable", "camera_refused", "camera_lost", ERR_INTERNAL})
ERROR_TEXT = {
    "camera_unreachable": "Could not connect to the camera.",
    "camera_refused": "The camera did not accept the talk.",
    "camera_lost": "The connection to the camera was lost.",
    ERR_CAMERA_TIMEOUT: "The camera did not start the talk in time.",
    ERR_INTERNAL: "The talk stopped because of an internal error.",
}

# Why a talk ended (the `ended` event's reason) — besides the error codes above.
REASON_CLIENT_END = "client_end"  # the card's 1-byte end frame
REASON_UNSUBSCRIBED = "unsubscribed"  # unsubscribe_events, or the websocket closed
REASON_IDLE = "idle"
REASON_MAX = "max_duration"
REASON_CHILD_EXIT = "child_exit"  # the child finished on its own, without an error
REASON_SHUTDOWN = "shutdown"  # Home Assistant is stopping
REASON_UNLOADED = "unloaded"  # the camera's config entry was unloaded

#: Monotonic clock for every talk timer. Module-level so tests can drive it.
_clock = time.monotonic


# ── the speaker child, shared with the Speaker media player ──────────────────


def build_backchannel_env(cam: dict, options) -> dict:
    """Environment for a backchannel child: the camera's local credentials.

    Credentials travel ONLY here, never in argv — any process on the box can
    read another's command line from /proc/<pid>/cmdline.
    """
    env = os.environ.copy()
    device_id = cam.get("device_id", "")
    camera_ip = options.get(f"camera_ip_{device_id}", "") or cam.get("camera_ip", "")
    env["CUBOAI_UID"] = str(cam.get("uid") or "")
    env["CUBOAI_ACCOUNT"] = str(cam.get("account") or "")
    env["CUBOAI_PASSWORD"] = str(cam.get("password") or "")
    env["CUBOAI_CAMERA_IP"] = str(camera_ip or "")
    return env


async def open_debug_stderr(hass, options):
    """Where a backchannel child's stderr goes: the debug log file while debug
    logs are enabled, else nowhere.

    A returned file must be closed by the caller right after spawning (the
    child duplicates the fd).
    """
    if not options.get("enable_debug_logs", False):
        return asyncio.subprocess.DEVNULL
    # open() blocks — do it in the executor. Rotate first: this file collects
    # raw subprocess stderr in append mode, so without a cap it grows forever
    # while debug logs are enabled (one stream per track, looping).
    log_path = hass.config.path("cuboai_debug.log")

    def _open_rotated(path=log_path, max_bytes=5 * 1024 * 1024):
        try:
            if os.path.exists(path) and os.path.getsize(path) > max_bytes:
                os.replace(path, path + ".1")
        except OSError:
            pass
        return open(path, "a")

    return await hass.async_add_executor_job(_open_rotated)


def talk_argv(rate: int, max_secs: int, dry_run: bool) -> list:
    """The talk child's command line — never credentials (see build_backchannel_env)."""
    argv = [
        sys.executable or "python3",
        BACKCHANNEL_SCRIPT,
        "--live",
        "--in-codec",
        "pcm_s16le",
        "--in-rate",
        str(rate),
        "--max-secs",
        str(max_secs + CHILD_MAX_SECS_MARGIN),
    ]
    if dry_run:
        argv.append("--dry-run")
    return argv


class SpeakerArbiter:
    """One talker per camera speaker: the live talk ('mic') or the Speaker
    entity's child ('player').

    What the camera does with two talk sessions at once is undocumented, and
    SPEAKERSTART is audible in the nursery, so the two never overlap. Used on
    the event loop only: nothing awaits between a check and a claim, so no lock.
    """

    #: Claims that stack. The Speaker can briefly have two children of its own —
    #: a replaced song is terminated without waiting — and each releases its own
    #: claim when it exits. A talk is one session per camera.
    _STACKING = frozenset({"player"})

    def __init__(self, hass):
        self._hass = hass
        self._owners = {}  # device_id -> [who, claims]

    def owner(self, device_id):
        held = self._owners.get(device_id)
        return held[0] if held else None

    def try_claim(self, device_id, who) -> bool:
        held = self._owners.get(device_id)
        if held is None:
            self._owners[device_id] = [who, 1]
            return True
        if held[0] == who and who in self._STACKING:
            held[1] += 1
            return True
        return False

    def release(self, device_id, who) -> None:
        held = self._owners.get(device_id)
        if held is None or held[0] != who:
            return
        held[1] -= 1
        if held[1] <= 0:
            del self._owners[device_id]

    def release_after_exit(self, device_id, who, proc) -> None:
        """Release once `proc` has exited, however long its SPEAKERSTOP takes.

        Callers terminate() first; this is the backstop that kills a child still
        running PLAYER_EXIT_GRACE_SECS later. Until then the speaker stays
        claimed: a terminated child is still talking to the camera.
        """
        if proc is None or proc.returncode is not None:
            self.release(device_id, who)
            return
        self._hass.async_create_task(
            self._release_when_exited(device_id, who, proc), f"cuboai speaker release {device_id}"
        )

    async def _release_when_exited(self, device_id, who, proc) -> None:
        try:
            try:
                await asyncio.wait_for(proc.wait(), PLAYER_EXIT_GRACE_SECS)
            except TimeoutError:
                _LOGGER.warning(
                    "Speaker child for %s did not exit in %ss — killing it", device_id, PLAYER_EXIT_GRACE_SECS
                )
                with suppress(ProcessLookupError):
                    proc.kill()
                with suppress(TimeoutError):
                    await asyncio.wait_for(proc.wait(), KILL_WAIT_SECS)
        finally:
            self.release(device_id, who)


class _TalkData:
    """hass.data[TALK_DATA]: the arbiter and the live talks, one per camera."""

    def __init__(self, hass):
        self.arbiter = SpeakerArbiter(hass)
        self.sessions = {}  # device_id -> TalkSession
        self.registered = False


def _talk_data(hass) -> _TalkData:
    data = hass.data.get(TALK_DATA)
    if not isinstance(data, _TalkData):
        data = _TalkData(hass)
        hass.data[TALK_DATA] = data
    return data


def get_arbiter(hass) -> SpeakerArbiter:
    return _talk_data(hass).arbiter


def mic_active(hass, device_id) -> bool:
    """Whether a live talk owns this camera's speaker — until its child exits."""
    data = hass.data.get(TALK_DATA)
    return isinstance(data, _TalkData) and data.arbiter.owner(device_id) == "mic"


# ── one talk ─────────────────────────────────────────────────────────────────


def _parse_fields(tokens) -> dict:
    """`key=int` tokens of a protocol line; anything else is ignored."""
    fields = {}
    for token in tokens:
        key, sep, value = token.partition("=")
        if sep:
            with suppress(ValueError):
                fields[key] = int(value)
    return fields


class TalkSession:
    """One live talk: a child process fed by one websocket connection."""

    def __init__(self, hass, connection, msg_id, data, entry_id, device_id, rate, max_secs, dry_run):
        self._hass = hass
        self._connection = connection
        self._data = data
        self.msg_id = msg_id
        self.entry_id = entry_id
        self.device_id = device_id
        self.rate = rate
        self.max_secs = max_secs
        self.dry_run = dry_run
        self.proc = None
        self.stopping = False
        self.reason = None
        self.live = False
        #: Whether events may go to the client: from the result until it
        #: unsubscribes or its websocket closes.
        self.subscribed = False
        #: The client unsubscribed or its websocket closed — possibly before
        #: the result, while the child was still starting.
        self.client_gone = False
        self.rx_bytes = 0
        self.dropped_bytes = 0
        self.child = {"sent": 0, "speech": 0, "rx": 0, "dropped": 0}
        self.camera = None  # from the END line: whether the child loaded the camera stack
        self._unsub_binary = None
        self._reader_task = None
        self._timer_task = None
        self._stop_task = None
        self._started_at = self._last_rx = _clock()
        self._last_status_event = -math.inf
        self._closed = asyncio.Event()

    # -- start ---------------------------------------------------------------

    async def async_spawn(self, cam, options) -> None:
        env = build_backchannel_env(cam, options)
        stderr_dest = await open_debug_stderr(self._hass, options)
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *talk_argv(self.rate, self.max_secs, self.dry_run),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr_dest,
                env=env,
            )
        finally:
            if hasattr(stderr_dest, "close"):
                stderr_dest.close()
        self._started_at = self._last_rx = _clock()

    def begin(self, unsub_binary) -> None:
        """Arm the reader and the timers, once `started` has gone out.

        Not earlier: a reader already reading could otherwise send `live` or
        `ended` ahead of `started`.
        """
        self._unsub_binary = unsub_binary
        self._last_rx = _clock()
        self._reader_task = self._hass.async_create_background_task(
            self._read_stdout(), f"cuboai talk reader {self.device_id}"
        )
        self._timer_task = self._hass.async_create_background_task(
            self._watch_timers(), f"cuboai talk timers {self.device_id}"
        )
        # Stopped while the child was starting. Not when a stop is already under
        # way: Home Assistant starts tasks eagerly, so the reader above may have
        # read a waiting ERROR line and stopped the talk itself by now.
        if self.stopping and self._stop_task is None:
            self._stop_io()
            self._schedule_stop()

    def abandon(self) -> None:
        """The child never started: give the speaker back."""
        self.stopping = True
        self._release()

    # -- audio in ------------------------------------------------------------

    def on_binary(self, hass, connection, payload) -> None:
        """Binary handler: one frame of PCM from the card, straight to the child.

        Runs on the event loop inside the connection's receive loop, so it only
        ever writes to a pipe — and it NEVER raises: Home Assistant silently
        unregisters a handler that raises once, and the talk would then carry
        on with no audio until a timer noticed.
        """
        try:
            if not payload:  # a 1-byte frame (just the handler id): the card is done
                self.request_stop(REASON_CLIENT_END)
                return
            size = len(payload)
            self._last_rx = _clock()
            self.rx_bytes += size
            proc = self.proc
            stdin = proc.stdin if proc is not None else None
            transport = stdin.transport if stdin is not None else None
            if (
                self.stopping
                or transport is None
                or transport.is_closing()
                or size > MAX_FRAME_BYTES
                or transport.get_write_buffer_size() > MAX_PENDING_BYTES
            ):
                self.dropped_bytes += size
                return
            stdin.write(payload)  # buffered by the transport; never blocks
        except Exception:
            _LOGGER.debug("Talk %s: an audio frame could not be handled", self.device_id, exc_info=True)

    # -- stop ----------------------------------------------------------------

    def request_stop(self, reason: str = REASON_UNSUBSCRIBED) -> None:
        """Stop the talk. Synchronous and idempotent.

        Also the subscription's callback: Home Assistant calls it (with no
        arguments) on unsubscribe_events and when the websocket closes.
        """
        if reason == REASON_UNSUBSCRIBED:
            self.subscribed = False  # the client is gone: nothing more is sent to it
            self.client_gone = True
        if self.stopping:
            return
        self.stopping = True
        self.reason = reason
        if self.proc is None:
            return  # still spawning: begin() finishes the stop once the child exists
        self._stop_io()
        self._schedule_stop()

    def _stop_io(self) -> None:
        """No more audio in, no more timers, and EOF to the child — which then
        sends its silent tail, SPEAKERSTOP and the close burst, and exits."""
        unsub, self._unsub_binary = self._unsub_binary, None
        if unsub is not None:
            with suppress(Exception):
                unsub()
        task = self._timer_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        stdin = self.proc.stdin if self.proc is not None else None
        if stdin is not None:
            with suppress(Exception):
                stdin.close()  # buffered audio is still flushed first

    def _schedule_stop(self) -> None:
        self._stop_task = self._hass.async_create_task(self._async_stop(), f"cuboai talk stop {self.device_id}")

    async def _wait_exit(self, timeout) -> bool:
        try:
            await asyncio.wait_for(self.proc.wait(), timeout)
        except TimeoutError:
            return False
        return True

    async def _async_stop(self) -> None:
        """EOF was sent; escalate to SIGTERM, then SIGKILL, until the child exits.
        The speaker is released only after that."""
        proc = self.proc
        try:
            if not await self._wait_exit(STOP_GRACE_SECS):
                _LOGGER.debug(
                    "Talk %s: child still running %ss after EOF — terminating", self.device_id, STOP_GRACE_SECS
                )
                with suppress(ProcessLookupError):
                    proc.terminate()
                if not await self._wait_exit(TERM_GRACE_SECS):
                    _LOGGER.warning("Talk %s: child ignored SIGTERM — killing it", self.device_id)
                    with suppress(ProcessLookupError):
                        proc.kill()
                    if not await self._wait_exit(KILL_WAIT_SECS):
                        _LOGGER.error("Talk %s: child did not exit after SIGKILL", self.device_id)
            reader = self._reader_task
            if reader is not None and not reader.done():
                done, _pending = await asyncio.wait({reader}, timeout=READER_DRAIN_SECS)
                if not done:
                    reader.cancel()
        finally:
            self._finish()

    def _finish(self) -> None:
        """The child has exited: tell the client, give the speaker back, log."""
        if self._closed.is_set():
            return
        self._stop_io()
        camera = self.camera if self.camera is not None else not self.dry_run
        self.send_event(
            {
                "type": "ended",
                "reason": self.reason,
                "sent": self.child["sent"],
                "speech": self.child["speech"],
                "camera": camera,
            }
        )
        self._release()
        # Never the env or argv here: the env carries the camera's credentials.
        _LOGGER.info(
            "Talk ended device=%s reason=%s secs=%.1f rx=%d dropped=%d sent=%d speech=%d "
            "queue_dropped=%d camera=%s dry_run=%s",
            self.device_id,
            self.reason,
            _clock() - self._started_at,
            self.rx_bytes,
            self.dropped_bytes,
            self.child["sent"],
            self.child["speech"],
            self.child["dropped"],
            camera,
            self.dry_run,
        )

    def _release(self) -> None:
        if self._data.sessions.get(self.device_id) is self:
            del self._data.sessions[self.device_id]
        self._data.arbiter.release(self.device_id, "mic")
        self._closed.set()

    async def wait_closed(self) -> None:
        await self._closed.wait()

    # -- events out ----------------------------------------------------------

    def send_event(self, event) -> None:
        if not self.subscribed:
            return
        try:
            self._connection.send_event(self.msg_id, event)
        except Exception:
            _LOGGER.debug("Talk %s: could not send %s", self.device_id, event.get("type"), exc_info=True)

    def _fail(self, code) -> None:
        if self.stopping:
            # Already ending (e.g. the camera dropped during the tail after the
            # card's stop): not news to the person who just stopped talking.
            _LOGGER.debug("Talk %s: %s while stopping", self.device_id, code)
            return
        self.send_event({"type": "error", "code": code, "message": ERROR_TEXT[code]})
        self.request_stop(code)

    # -- the child's protocol lines -------------------------------------------

    async def _read_stdout(self) -> None:
        reason = REASON_CHILD_EXIT
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                self._on_line(line)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.debug("Talk %s: reading the child's output failed", self.device_id, exc_info=True)
            reason = ERR_INTERNAL
        # EOF: the child closed its protocol channel, i.e. it is exiting. A no-op
        # when a stop is already under way.
        self.request_stop(reason)

    def _on_line(self, raw: bytes) -> None:
        """One line from the child. Only `@@TALK ...` lines count (contract A)."""
        parts = raw.decode("utf-8", "replace").split()
        if len(parts) < 2 or parts[0] != "@@TALK":
            return
        kind, args = parts[1], parts[2:]
        if kind == "READY":
            if not self.live and not self.stopping:
                self.live = True
                self.send_event({"type": "live"})
        elif kind in ("STATUS", "END"):
            fields = _parse_fields(args)
            for key in self.child:
                if key in fields:
                    self.child[key] = fields[key]
            if kind == "END":
                if "camera" in fields:
                    self.camera = fields["camera"] != 0
            else:
                now = _clock()
                if now - self._last_status_event >= STATUS_EVENT_MIN_SECS:
                    self._last_status_event = now
                    self.send_event({"type": "status", **self.child})
        elif kind == "ERROR":
            code = args[0] if args else ""
            # The raw line is for the log only; the client gets fixed text.
            _LOGGER.warning("Talk %s: the child reported %r", self.device_id, " ".join(parts)[:200])
            self._fail(code if code in CHILD_ERROR_CODES else ERR_INTERNAL)

    # -- timers --------------------------------------------------------------

    async def _watch_timers(self) -> None:
        while not self.stopping:
            await asyncio.sleep(TIMER_TICK_SECS)
            self._check_timers()

    def _check_timers(self) -> None:
        if self.stopping:
            return
        now = _clock()
        if not self.live and now - self._started_at >= READY_TIMEOUT_SECS:
            self._fail(ERR_CAMERA_TIMEOUT)
        elif now - self._started_at >= self.max_secs:
            self.request_stop(REASON_MAX)
        elif now - self._last_rx >= IDLE_SECS:
            self.request_stop(REASON_IDLE)


# ── the websocket command ────────────────────────────────────────────────────


def _find_camera(hass, device_id):
    """(entry, camera) for `device_id` on a LOADED entry, else None.

    Looked up on every call: the command outlives entry reloads (Home Assistant
    cannot unregister it), so nothing about an entry may be captured at setup.
    An entry whose unload has begun counts as gone: its store is only removed
    after the platforms unload, and async_stop_for_entry has already run.
    """
    loaded = hass.data.get(DOMAIN, {})
    for entry in hass.config_entries.async_entries(DOMAIN):
        store = loaded.get(entry.entry_id)
        if store is None or (isinstance(store, dict) and store.get(STORE_UNLOADING)):
            continue
        for cam in entry.data.get("cameras", []):
            if cam.get("device_id") == device_id and cam.get("uid"):
                return entry, cam
    return None


def _refusal(hass, data, device_id, speaker):
    """Why a talk cannot start on this camera right now, or None.

    v1 policy: the talk REFUSES while the Speaker is playing (a song, TTS, or a
    lullaby it delegated) rather than stopping it — no hidden side effects.
    """
    state = hass.states.get(speaker)
    if (state is not None and state.state == _STATE_PLAYING) or data.arbiter.owner(device_id) == "player":
        return ERR_SPEAKER_BUSY
    if device_id in data.sessions or data.arbiter.owner(device_id) is not None:
        return ERR_BUSY
    return None


@websocket_api.websocket_command(
    {
        vol.Required("type"): "cuboai/talk",
        vol.Required("device_id"): str,
        # int first: 16000.0 is `in` the list, and the child's argparse refuses "16000.0".
        vol.Required("sample_rate"): vol.All(int, vol.In(ALLOWED_RATES)),
        vol.Optional("max_secs", default=TALK_MAX_SECS): vol.All(int, vol.Range(min=MIN_TALK_SECS, max=TALK_MAX_SECS)),
        vol.Optional("dry_run", default=False): bool,
    }
)
@websocket_api.async_response
async def ws_talk(hass, connection, msg) -> None:
    """Start a live talk. The subscription lasts exactly as long as the talk.

    Events: started {handler_id, dry_run, max_secs}, live, status, error {code,
    message}, ended {reason, sent, speech, camera}. Binary frames to handler_id
    carry s16le mono PCM at sample_rate; a 1-byte frame ends the talk.
    """
    msg_id = msg["id"]
    device_id = msg["device_id"]
    found = _find_camera(hass, device_id)
    speaker = None
    if found is not None:
        speaker = er.async_get(hass).async_get_entity_id("media_player", DOMAIN, f"cuboai_speaker_{device_id}")
    if not speaker:
        connection.send_error(msg_id, ERR_NOT_FOUND, REFUSAL_TEXT[ERR_NOT_FOUND])
        return
    entry, cam = found
    # The talk plays through the camera's speaker, so it takes the same right as
    # playing a song on the Speaker entity — not admin.
    if not connection.user.permissions.check_entity(speaker, POLICY_CONTROL):
        raise Unauthorized(entity_id=speaker, permission=POLICY_CONTROL)

    # Every await below comes after a stop hook in connection.subscriptions:
    # when the websocket closes, Home Assistant calls the hooks it holds at that
    # moment and then clears them, and this handler runs on regardless. A hook
    # added after that is never called, and the talk would run for nobody.
    data = _talk_data(hass)
    refusal = _refusal(hass, data, device_id, speaker)
    previous = data.sessions.get(device_id)
    if refusal == ERR_BUSY and previous is not None and previous.stopping:
        # Stop-then-talk-again: the last talk's child is still sending
        # SPEAKERSTOP. Let it finish rather than refuse, then look again at
        # everything — the client may have left, the entry may be unloading.
        left = []

        def hook():
            left.append(True)

        connection.subscriptions[msg_id] = hook
        with suppress(TimeoutError):
            await asyncio.wait_for(previous.wait_closed(), PREVIOUS_TALK_WAIT_SECS)
        if connection.subscriptions.get(msg_id) is hook:
            del connection.subscriptions[msg_id]
        if left:
            return  # unsubscribed, or the websocket closed: no claim, no child
        found = _find_camera(hass, device_id)
        if found is None:
            connection.send_error(msg_id, ERR_NOT_FOUND, REFUSAL_TEXT[ERR_NOT_FOUND])
            return
        entry, cam = found
        refusal = _refusal(hass, data, device_id, speaker)
    if refusal is None and hass.is_stopping:
        refusal = ERR_SPAWN_FAILED  # its stop has already ended every talk; no new one
    if refusal is not None or not data.arbiter.try_claim(device_id, "mic"):
        refusal = refusal or ERR_BUSY
        connection.send_error(msg_id, refusal, REFUSAL_TEXT[refusal])
        return

    rate, max_secs, dry_run = msg["sample_rate"], msg["max_secs"], msg["dry_run"]
    session = TalkSession(hass, connection, msg_id, data, entry.entry_id, device_id, rate, max_secs, dry_run)
    data.sessions[device_id] = session
    # From here the hook is the talk's own stop. Called while the child is
    # starting, it is recorded, and begin() carries it out.
    connection.subscriptions[msg_id] = session.request_stop
    try:
        await session.async_spawn(cam, entry.options)
    except asyncio.CancelledError:  # Home Assistant stopping (asyncio reaps a half-made child)
        connection.subscriptions.pop(msg_id, None)
        session.abandon()
        raise
    except Exception:
        _LOGGER.warning("Talk %s: the child process could not be started", device_id, exc_info=True)
        connection.subscriptions.pop(msg_id, None)
        session.abandon()
        connection.send_error(msg_id, ERR_SPAWN_FAILED, REFUSAL_TEXT[ERR_SPAWN_FAILED])
        return

    if session.client_gone:
        # The websocket closed (or the client unsubscribed) during the spawn:
        # nobody to answer and no audio to relay. begin() ends the child.
        session.begin(None)
        return

    try:
        handler_id, unsub_binary = connection.async_register_binary_handler(session.on_binary)
    except Exception:  # every binary handler slot of this connection is taken
        _LOGGER.warning("Talk %s: no binary handler available on this connection", device_id, exc_info=True)
        connection.subscriptions.pop(msg_id, None)
        session.request_stop(ERR_INTERNAL)
        connection.send_error(msg_id, ERR_SPAWN_FAILED, REFUSAL_TEXT[ERR_SPAWN_FAILED])
        return

    # The hook has been in since before the spawn, so it is in BEFORE the
    # result, as Home Assistant's own subscriptions do: once the client holds
    # the result it may unsubscribe or drop at any moment.
    session.subscribed = True
    connection.send_result(msg_id)
    session.send_event({"type": "started", "handler_id": handler_id, "dry_run": dry_run, "max_secs": max_secs})
    session.begin(unsub_binary)
    _LOGGER.info("Talk started device=%s rate=%s max_secs=%s dry_run=%s", device_id, rate, max_secs, dry_run)


# ── setup, unload, shutdown ──────────────────────────────────────────────────


async def _async_stop_sessions(sessions, reason) -> None:
    for session in sessions:
        session.request_stop(reason)
    waits = [asyncio.ensure_future(session.wait_closed()) for session in sessions]
    if waits:
        _done, pending = await asyncio.wait(waits, timeout=SHUTDOWN_WAIT_SECS)
        for waiter in pending:
            waiter.cancel()


def async_setup_talk(hass) -> None:
    """Register `cuboai/talk` (once per Home Assistant run: 2026.7 has no way to
    unregister a command) and stop every talk when Home Assistant stops."""
    data = _talk_data(hass)
    if data.registered:
        return
    data.registered = True
    websocket_api.async_register_command(hass, ws_talk)

    async def _async_stop_all(_event) -> None:
        await _async_stop_sessions(list(_talk_data(hass).sessions.values()), REASON_SHUTDOWN)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop_all)


async def async_stop_for_entry(hass, entry) -> None:
    """Stop the talks on an entry's cameras, and wait for their children to exit.

    Marks the entry's store first, so no talk starts on it any more — not one
    arriving during the platform unload, nor one that was waiting for a
    stopping talk and would otherwise start the moment that talk is gone.
    """
    store = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if isinstance(store, dict):
        store[STORE_UNLOADING] = True
    data = hass.data.get(TALK_DATA)
    if not isinstance(data, _TalkData):
        return
    device_ids = {cam.get("device_id") for cam in entry.data.get("cameras", [])}
    sessions = [s for s in data.sessions.values() if s.entry_id == entry.entry_id or s.device_id in device_ids]
    await _async_stop_sessions(sessions, REASON_UNLOADED)
