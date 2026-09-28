"""The websocket microphone's child process, run for real on its --dry-run path.

Home Assistant starts
`cuboai_stream_backchannel.py --live --in-codec pcm_s16le --in-rate R --max-secs N [--dry-run]`,
writes the browser's PCM to its stdin and reads `@@TALK` lines from its stdout:
READY once, STATUS at most every 5 s, ERROR <code>, and END always last. A stray
print from anywhere must never reach that channel. --dry-run must not load the
camera stack or open a socket, because nothing may ever make a sound in the
nursery from a test. A URL argument (TTS / songs) and no argument (go2rtc) are
routed exactly as before.

Every test names the mutation it kills.
"""

import importlib
import importlib.util
import io
import math
import os
import queue
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import types

import pytest

pytest.importorskip("av")

_TUTK = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "custom_components", "cuboai", "tutk"))
SCRIPT = os.path.join(_TUTK, "cuboai_stream_backchannel.py")
RATE = 16000
CHUNK = int(RATE * 0.04) * 2  # the card's 40 ms frames
DRY = ["--live", "--in-codec", "pcm_s16le", "--in-rate", str(RATE), "--max-secs", "30", "--dry-run"]

_WRAPPER = """
import os, sys
sys.path.insert(0, {tutk!r})
import cuboai_stream_backchannel as bc
{prelude}
sys.argv = [bc.__file__] + sys.argv[1:]
bc.main()
"""

_STRAY = """
_run = bc._DryTalk.run
def _noisy(self):
    print("STRAY print")
    sys.stdout.flush()
    os.write(1, b"STRAY fd\\n")
    return _run(self)
bc._DryTalk.run = _noisy
"""

_NO_SOCKET = """
import socket
def _no_socket(*a, **k):
    raise AssertionError("a socket was opened")
socket.socket = _no_socket
"""

_SELF_SIGTERM = """
import signal, threading
threading.Timer(2.0, signal.raise_signal, (signal.SIGTERM,)).start()
"""


def _tone(secs, rate=RATE, hz=440):
    return b"".join(
        struct.pack("<h", int(8000 * math.sin(2 * math.pi * hz * i / rate))) for i in range(int(secs * rate))
    )


def _env():
    """No camera credentials at all: nothing here may ever reach a camera."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("CUBOAI")}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run(argv, pcm=b"", *, tmp_path=None, prelude=None, close=True, sigterm_after_ready=False, timeout=30):
    """Start the child, feed `pcm` in real time (40 ms chunks), then EOF (unless `close` is
    False). Returns the protocol lines with arrival times, stderr, exit code and timings."""
    cmd = [sys.executable, SCRIPT]
    if prelude is not None:
        wrapper = tmp_path / "child.py"
        wrapper.write_text(_WRAPPER.format(tutk=_TUTK, prelude=prelude), encoding="utf-8")
        cmd = [sys.executable, str(wrapper)]
    proc = subprocess.Popen(
        cmd + argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_env()
    )
    t0 = time.time()
    lines, err = [], []
    ready = threading.Event()

    def read_out():
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            lines.append((time.time() - t0, line))
            if line == "@@TALK READY":
                ready.set()

    def read_err():
        err.append(proc.stderr.read().decode("utf-8", "replace"))

    readers = [threading.Thread(target=read_out), threading.Thread(target=read_err)]
    for th in readers:
        th.start()
    signalled = False
    try:
        for i in range(0, len(pcm), CHUNK):
            if proc.poll() is not None:
                break
            if sigterm_after_ready and ready.is_set() and not signalled:
                proc.send_signal(signal.SIGTERM)
                signalled = True
            proc.stdin.write(pcm[i : i + CHUNK])
            proc.stdin.flush()
            time.sleep(0.04)
    except OSError:
        pass  # the child ended first (max-secs, SIGTERM): nothing left to feed
    eof_at = time.time() - t0
    if close:
        try:
            proc.stdin.close()
        except OSError:
            pass
    try:
        proc.wait(timeout)
    finally:
        if proc.poll() is None:
            proc.kill()
    exit_at = time.time() - t0
    for th in readers:
        th.join(5)
    try:
        proc.stdin.close()
    except OSError:
        pass
    return types.SimpleNamespace(
        lines=[ln for _, ln in lines],
        timed=lines,
        err="".join(err),
        code=proc.returncode,
        eof_at=eof_at,
        exit_at=exit_at,
    )


def _fields(line):
    return dict(kv.split("=", 1) for kv in line.split()[2:])


def _only(res, kind):
    return [ln for ln in res.lines if ln.split()[:2] == ["@@TALK", kind]]


def test_dry_run_talk_end_to_end():
    """5 s of a 440 Hz tone, as the card sends it. Kill: READY per pull or missing, END not last
    or missing, STATUS every 16 frames, the camera stack imported at module level (camera=1),
    or the child hanging after EOF."""
    pcm = _tone(5.0)
    res = _run(DRY, pcm)
    assert res.code == 0, res.err
    assert res.lines and all(ln.startswith("@@TALK ") for ln in res.lines), res.lines
    assert res.lines[0] == "@@TALK READY"
    assert len(_only(res, "READY")) == 1
    assert _only(res, "ERROR") == []
    assert len(_only(res, "END")) == 1 and res.lines[-1].startswith("@@TALK END ")
    end = _fields(res.lines[-1])
    assert 74 <= int(end["speech"]) <= 79, end  # 5 s = 78.1 frames of 1024 samples at 16 kHz
    assert int(end["rx"]) == len(pcm)
    assert end["camera"] == "0"
    assert int(end["sent"]) > 50
    statuses = _only(res, "STATUS")
    assert 1 <= len(statuses) <= 2, statuses
    assert set(_fields(statuses[0])) == {"sent", "speech", "delivered", "rx", "dropped"}
    assert res.exit_at - res.eof_at <= 2.0


def test_stray_output_lands_on_stderr(tmp_path):
    """A print() and a raw write to fd 1 after the protocol channel is claimed. Kill: fd 1 not
    pointed at stderr (both would reach Home Assistant's parser; sys.stdout writes to fd 1, so
    also pointing sys.stdout at stderr is defence in depth, not separately observable)."""
    res = _run(DRY, _tone(1.5), tmp_path=tmp_path, prelude=_STRAY)
    assert res.code == 0, res.err
    assert all(ln.startswith("@@TALK ") for ln in res.lines), res.lines
    assert "STRAY print" in res.err and "STRAY fd" in res.err


def test_dry_run_never_opens_a_socket(tmp_path):
    """Kill: the dry run reaching the camera path (it would open the talk socket)."""
    res = _run(DRY, _tone(1.5), tmp_path=tmp_path, prelude=_NO_SOCKET)
    assert res.code == 0, res.err
    assert "a socket was opened" not in res.err
    assert res.lines[0] == "@@TALK READY" and _fields(res.lines[-1])["camera"] == "0"


def test_sigterm_mid_talk_exits_0_with_end_last(tmp_path):
    """Home Assistant's escalation is stdin EOF, then SIGTERM, then kill. SIGTERM must unwind
    through the talk's `finally` (SPEAKERSTOP on a real camera) and still write END. Raised inside
    the child here, so it runs on Windows too. Kill: the handler removed (exit code not 0, no END),
    END written outside `finally`, or the feed reading through the BUFFERED stdin (exiting while
    that thread is blocked in a read crashes interpreter shutdown on its lock: non-zero exit)."""
    res = _run(DRY, _tone(4.0), tmp_path=tmp_path, prelude=_SELF_SIGTERM, close=False)
    assert res.code == 0, res.err
    assert res.exit_at < 3.5
    assert res.lines[0] == "@@TALK READY" and res.lines[-1].startswith("@@TALK END ")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal delivery")
def test_external_sigterm_exits_0_with_end_last():
    """The real thing: a SIGTERM from outside while the feed thread is blocked reading stdin.
    Kill: the handler removed (SIGTERM's default action: no END, exit code -15)."""
    res = _run(DRY, _tone(4.0), close=False, sigterm_after_ready=True)
    assert res.code == 0, res.err
    assert res.lines[0] == "@@TALK READY" and res.lines[-1].startswith("@@TALK END ")
    assert res.exit_at < 3.5


def test_eof_during_the_handshake_ends_without_ready():
    """The user tapped stop before the camera was listening. Kill: the dry run ignoring an ended
    feed during its handshake (the real engine would click SPEAKERSTART for nothing)."""
    res = _run(DRY, _tone(0.2))
    assert res.code == 0, res.err
    assert _only(res, "READY") == []
    assert res.lines[-1].startswith("@@TALK END ") and _fields(res.lines[-1])["sent"] == "0"
    assert res.exit_at - res.eof_at <= 1.5


def test_max_secs_caps_the_talk_even_with_stdin_open():
    """Kill: --max-secs ignored (a stuck feed would talk forever), or the feed reading through
    the buffered stdin (the exit, with that thread blocked in a read, crashes)."""
    argv = DRY[:-2] + ["2", "--dry-run"]
    assert argv[-3:] == ["--max-secs", "2", "--dry-run"]
    res = _run(argv, _tone(6.0), close=False)
    assert res.code == 0, res.err
    assert res.exit_at < 4.5
    assert res.lines[-1].startswith("@@TALK END ")


def test_bad_arguments_still_report_and_end():
    """Kill: an argparse exit escaping before the protocol lines (HA would see a bare EOF)."""
    res = _run(["--live", "--in-rate", "100", "--dry-run"])
    assert res.code == 2
    assert res.lines == ["@@TALK ERROR internal", res.lines[-1]] and res.lines[-1].startswith("@@TALK END ")


def test_real_mode_without_credentials_refuses_before_any_camera_work(tmp_path):
    """No CUBOAI_* in the environment (and sockets disabled, belt and braces). Kill: the
    credential check moved after the feed/session start."""
    argv = DRY[:-1]
    assert "--dry-run" not in argv
    res = _run(argv, _tone(0.5), tmp_path=tmp_path, prelude=_NO_SOCKET)
    assert res.code == 1
    assert res.lines == ["@@TALK ERROR internal", "@@TALK END sent=0 speech=0 rx=0 dropped=0 camera=0"]


def test_importing_the_script_does_not_load_the_camera_stack():
    """Kill: `from cuboai_session import get_session` back at module level."""
    code = (
        f"import sys; sys.path.insert(0, {_TUTK!r}); import cuboai_stream_backchannel; "
        "print(sorted(m for m in ('cuboai_session', 'cuboai_transport_py') if m in sys.modules))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=_env(), check=True)
    assert out.stdout.strip() == "[]"


# ── the dry run checks every frame on the camera's behalf (in-process) ──


@pytest.fixture
def plain_bc():
    spec = importlib.util.spec_from_file_location("live_backchannel_dry", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_GOOD = bytes.fromhex("fff16080") + bytes([(12 >> 3) & 0xFF, ((12 & 7) << 5) | 0x1F, 0xFC]) + bytes(5)


@pytest.mark.parametrize(
    "byte, value",
    [(0, 0xFE), (2, 0x20), (2, 0x64), (3, 0x40), (4, 0x02)],
    ids=["sync", "profile-main", "sampling-index-9", "channel-config-1", "length"],
)
def test_dry_run_rejects_a_frame_the_camera_would_not_take(plain_bc, byte, value):
    """Kill: any of the header checks removed (sync, AAC-LC, 16 kHz, stereo config, length)."""
    plain_bc._check_adts(_GOOD)
    bad = bytearray(_GOOD)
    bad[byte] = value
    with pytest.raises(ValueError):
        plain_bc._check_adts(bytes(bad))


def test_dry_run_checks_every_speech_frame(plain_bc, monkeypatch):
    """Kill: the dry run sending speech frames unchecked (a broken encoder would pass the box's
    dry-run verification and first show up as noise in the nursery)."""
    monkeypatch.setattr(plain_bc, "DRY_HANDSHAKE_SECS", 0.05)

    class _Feed:
        silent_unit = _GOOD
        done = False

        def next_unit(self):
            return bytes.fromhex("fff1") + b"not-adts"

    with pytest.raises(ValueError):
        plain_bc._DryTalk(_Feed(), max_secs=2).run()


# ── routing: a URL argument and no argument are unchanged (in-process, handlers stubbed) ──


@pytest.fixture
def bc(monkeypatch):
    spec = importlib.util.spec_from_file_location("live_backchannel_cli", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    calls = []
    monkeypatch.setattr(mod.signal, "signal", lambda *a: None)
    monkeypatch.setattr(mod, "_handle_file_or_url", lambda *a: calls.append(("file", a)))
    monkeypatch.setattr(mod, "_handle_live_stdin", lambda *a: calls.append(("go2rtc", a)))
    monkeypatch.setattr(mod, "_live_main", lambda argv: calls.append(("ws", argv)) or 0)
    # placeholders: the handlers are stubbed, nothing connects anywhere
    monkeypatch.setenv("CUBOAI_UID", "uid")
    monkeypatch.setenv("CUBOAI_ACCOUNT", "acct")
    monkeypatch.setenv("CUBOAI_PASSWORD", "pw")
    monkeypatch.setenv("CUBOAI_CAMERA_IP", "")
    mod.calls = calls
    return mod


@pytest.mark.parametrize(
    "argv, expected",
    [
        (
            ["http://ha.local/api/tts_proxy/x.mp3"],
            ("file", ("http://ha.local/api/tts_proxy/x.mp3", "uid", "acct", "pw", "")),
        ),
        (["/media/song.mp3"], ("file", ("/media/song.mp3", "uid", "acct", "pw", ""))),
        ([], ("go2rtc", ("uid", "acct", "pw", ""))),
    ],
    ids=["url", "path", "no-argument"],
)
def test_url_and_no_argument_routing_unchanged(bc, monkeypatch, argv, expected):
    """Kill: a media URL or path swallowed by the --live branch, or go2rtc's no-argument mode lost."""
    monkeypatch.setattr(sys, "argv", ["cuboai_stream_backchannel.py", *argv])
    bc.main()
    assert bc.calls == [expected]


def test_dash_dash_argument_goes_to_the_websocket_mode(bc, monkeypatch):
    """Kill: --live treated as a media URL (the TTS path would try to download "--live")."""
    monkeypatch.setattr(sys, "argv", ["cuboai_stream_backchannel.py", *DRY])
    with pytest.raises(SystemExit) as exc:
        bc.main()
    assert exc.value.code == 0
    assert bc.calls == [("ws", DRY)]


# ── the camera path, in-process: a fake camera stack, no socket at all ──
#
# The only paths that reach a real camera (--live without --dry-run, and go2rtc's no-argument
# mode) run here against a fake `cuboai_session` whose session records what the talk asks the
# engine for. socket.socket raises, belt and braces: nothing here can reach a camera.


class _FakeCameraStack:
    """sys.modules['cuboai_session'] for one test."""

    def __init__(self):
        self.calls = []  # ("connect", None) / ("talk", kwargs), in order
        self.enter_error = None
        self.talk_error = None
        self.on_connect = None
        stack = self

        class _Session:
            def send_audio_file(self, path, **kw):
                stack.calls.append(("talk", dict(kw, path=path)))
                feed = kw.get("live_source")
                deadline = time.time() + 5
                while feed is not None and not feed.done and time.time() < deadline:
                    time.sleep(0.005)  # as the engine does: the talk lasts until the feed ends
                if stack.talk_error is not None:
                    raise stack.talk_error
                return 7

        class _Connected:
            def __enter__(self):
                if stack.enter_error is not None:
                    raise stack.enter_error
                return _Session()

            def __exit__(self, *exc):
                return False

        def get_session(*args, **kwargs):
            stack.calls.append(("connect", None))
            if stack.on_connect is not None:
                stack.on_connect()
            return _Connected()

        self.module = types.ModuleType("cuboai_session")
        self.module.get_session = get_session

    def kinds(self):
        return [kind for kind, _ in self.calls]

    def talk_kwargs(self):
        return next(kw for kind, kw in self.calls if kind == "talk")


@pytest.fixture
def camera_path(monkeypatch):
    spec = importlib.util.spec_from_file_location("live_backchannel_camera", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    stack = _FakeCameraStack()
    monkeypatch.setitem(sys.modules, "cuboai_session", stack.module)

    def _no_socket(*a, **k):
        raise AssertionError("a socket was opened")

    monkeypatch.setattr(socket, "socket", _no_socket)
    for key, value in (("CUBOAI_UID", "uid"), ("CUBOAI_ACCOUNT", "acct"), ("CUBOAI_PASSWORD", "pw")):
        monkeypatch.setenv(key, value)  # placeholders: the session is fake
    monkeypatch.setenv("CUBOAI_CAMERA_IP", "")
    mod.stack = stack
    mod.pure = importlib.import_module("cuboai_pure")  # the one the script takes TalkTimeout from
    return mod


def _ws_talk(bc, stdin=b"", on_connect=None):
    """_handle_ws_live on `stdin` (bytes, or a stream). Returns (exit code, protocol lines, talk)."""
    bc._stdin_raw = lambda: io.BytesIO(stdin) if isinstance(stdin, bytes) else stdin
    lines = []
    talk = bc._LiveTalk(types.SimpleNamespace(emit=lines.append))
    if on_connect is not None:
        bc.stack.on_connect = lambda: on_connect(talk)
    args = bc._parse_live_args(["--live", "--in-codec", "pcm_s16le", "--in-rate", "16000", "--max-secs", "125"])
    return bc._handle_ws_live(args, talk), lines, talk


def test_the_camera_talk_arms_every_engine_guard(camera_path):
    """The one child path that reaches a real camera, wired to the engine. Kill: grant_timeout,
    liveness_timeout, tail_frames or max_secs dropped or changed (a refusing camera, one gone
    off Wi-Fi mid-talk, the last word cut by SPEAKERSTOP, or no child-side cap on a leaked
    talk), or the feed started only after connecting (stdin not drained during the handshake)."""
    feed_at_connect = []
    pcm = _tone(0.2)
    code, lines, talk = _ws_talk(camera_path, pcm, on_connect=lambda t: feed_at_connect.append(t.feed is not None))
    assert code == 0 and lines == []
    assert camera_path.stack.kinds() == ["connect", "talk"]
    kw = camera_path.stack.talk_kwargs()
    assert kw["path"] == "live" and kw["live_source"] is talk.feed
    assert kw["max_secs"] == 125
    assert (kw["grant_timeout"], kw["liveness_timeout"], kw["tail_frames"]) == (10, 5, 8)
    assert kw["on_status"] == talk.status
    assert feed_at_connect == [True], "the feed started after connecting"
    assert talk.feed.received == len(pcm)


@pytest.mark.parametrize(
    "where, error, want",
    [
        ("talk", lambda cp: cp.TalkTimeout("grant", 0), "camera_refused"),
        ("talk", lambda cp: cp.TalkTimeout("camera_lost", 9), "camera_lost"),
        ("connect", lambda cp: RuntimeError("handshake failed (no 0x2041)"), "camera_unreachable"),
        ("connect", lambda cp: OSError("network unreachable"), "camera_unreachable"),
        ("talk", lambda cp: RuntimeError("handshake failed (no 0x2041)"), "camera_unreachable"),
        ("talk", lambda cp: OSError("socket died"), "camera_lost"),
        ("talk", lambda cp: RuntimeError("unexpected"), "internal"),
    ],
    ids=["no-grant", "gone-quiet", "no-handshake", "connect-oserror", "talk-handshake", "talk-oserror", "other"],
)
def test_the_child_maps_each_camera_failure_to_its_code(camera_path, where, error, want):
    """What the card tells the parent comes from this code. Kill: the two TalkTimeout reasons
    swapped, a connect-time OSError reported as camera_lost (the `phase == "connect"` test
    dropped), or an unknown error passed off as a camera one."""
    err = error(camera_path.pure)
    if where == "connect":
        camera_path.stack.enter_error = err
    else:
        camera_path.stack.talk_error = err
    code, lines, _talk = _ws_talk(camera_path)
    assert (code, lines) == (1, [f"ERROR {want}"])


def test_a_dead_feed_is_an_internal_error(camera_path):
    """stdin failing mid-talk (a broken pipe, the encoder dying). Kill: the feed's error not
    checked after the talk (a clean exit 0 and `child_exit`, with no word of why)."""

    class _Broken:
        def read(self, n):
            raise OSError("stdin broke")

    code, lines, _talk = _ws_talk(camera_path, _Broken())
    assert (code, lines) == (1, ["ERROR internal"])


class _Pipe:
    """A stdin that blocks until the test writes: put() bytes, and b"" for end of file."""

    def __init__(self):
        self.q = queue.Queue()

    def put(self, data):
        self.q.put(data)

    def read(self, n):
        return self.q.get(timeout=10)


def _alaw(secs):
    return b"\xd5" * int(8000 * secs)  # A-law silence, 8 kHz mono


def test_go2rtc_mode_touches_nothing_until_audio_arrives(camera_path):
    """go2rtc's no-argument mode (kept, inert on a stock install). Kill: the talk opened before
    the first audio byte (a producer started with no microphone behind it would click
    SPEAKERSTART in the nursery), the bytes that opened it lost, or the talk left without the
    engine's guards (it runs in go2rtc's process, outside Home Assistant's timers and arbiter)."""
    stack = camera_path.stack
    pipe = _Pipe()
    camera_path._stdin_raw = lambda: pipe
    th = threading.Thread(target=camera_path._handle_live_stdin, args=("uid", "acct", "pw", ""), daemon=True)
    th.start()
    time.sleep(0.3)
    assert stack.calls == [], "the camera was contacted with no audio"
    first, rest = _alaw(0.1), _alaw(0.5)
    pipe.put(first)
    pipe.put(rest)
    pipe.put(b"")
    th.join(10)
    assert not th.is_alive()
    assert stack.kinds() == ["connect", "talk"]
    kw = stack.talk_kwargs()
    assert kw["live_source"].received == len(first) + len(rest), "the opening bytes were lost"
    assert (kw["grant_timeout"], kw["liveness_timeout"], kw["max_secs"]) == (10, 5, 125)


def test_go2rtc_mode_with_no_audio_at_all_never_connects(camera_path):
    """Kill: the gate treating end of file as audio (a connect, and a talk, for nothing)."""
    camera_path._stdin_raw = lambda: io.BytesIO(b"")
    camera_path._handle_live_stdin("uid", "acct", "pw", "")
    assert camera_path.stack.calls == []
