"""The microphone's three parts against each other.

The card (www/cuboai-card.js), Home Assistant's `cuboai/talk` command (talk.py)
and the talk child (tutk/cuboai_stream_backchannel.py --live) each have their
own tests against fakes of the other two. These pin the seams between them:
the child's flags as Home Assistant writes them, every code one side sends and
the other must understand, the card's numbers against the server's limits, and
one whole talk run through all three — the card's own frame helpers (in Node),
Home Assistant's binary dispatch, talk.py, and the real child on its --dry-run
path.

Nothing here reaches a camera or makes a sound: the child is a dry run (no
camera stack, no socket), and its camera account is empty, so even a talk that
had lost --dry-run would stop on the missing credentials before any camera
code loads.

Every test names the mutation it kills.
"""

import ast
import asyncio
import base64
import importlib.util
import json
import os
import re
import shutil
import struct
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import voluptuous as vol

from custom_components.cuboai import talk
from custom_components.cuboai.const import DOMAIN
from tests.test_talk_ws import FakeConnection, FakeHass

ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "cuboai"
CARD = ROOT / "www" / "cuboai-card.js"
CHILD = ROOT / "tutk" / "cuboai_stream_backchannel.py"
DEVICE = "SW05CONTRACT0001"
SPEAKER = "media_player.baby_speaker"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _card_src() -> str:
    return CARD.read_text(encoding="utf-8")


def _node(body: str, tmp_path) -> dict:
    """Run `body` after the card's mic helpers (sliced out as in test_mic_capture.py)."""
    src = _card_src()
    helpers = src[src.index("// ── cuboai mic helpers ──") : src.index("// ── end mic helpers ──")]
    path = tmp_path / "contract.js"
    path.write_text(helpers + "\n" + body, encoding="utf-8")
    proc = subprocess.run(["node", str(path)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.fixture
def child():
    """The child script as a module, for its argument parser (nothing runs)."""
    spec = importlib.util.spec_from_file_location("mic_contract_child", CHILD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _server_codes() -> set:
    """Everything talk.py can put in front of the person: refusal codes, error
    codes and `ended` reasons."""
    codes = set(talk.REFUSAL_TEXT) | set(talk.ERROR_TEXT) | {"unauthorized"}
    codes |= {value for name, value in vars(talk).items() if name.startswith("REASON_")}
    codes.discard(talk.REASON_UNSUBSCRIBED)  # never sent: the client has already gone
    return codes


# ── A: Home Assistant -> the child ───────────────────────────────────────────


def test_the_child_parses_the_command_line_home_assistant_writes(child):
    """Kill: a flag renamed on either side (the child would exit 2 on every tap),
    a rate talk.py allows that the child refuses, --dry-run lost on the way (a
    test that plays at the camera), or the script path pointing elsewhere."""
    for rate in talk.ALLOWED_RATES:
        for dry_run in (False, True):
            argv = talk.talk_argv(rate, talk.TALK_MAX_SECS, dry_run)
            assert os.path.samefile(argv[1], CHILD)
            args = child._parse_live_args(argv[2:])
            assert (args.live, args.in_codec, args.in_rate, args.dry_run) == (True, "pcm_s16le", rate, dry_run)
            assert args.max_secs == talk.TALK_MAX_SECS + talk.CHILD_MAX_SECS_MARGIN


def test_every_error_the_child_reports_is_one_home_assistant_knows():
    """Kill: the child reporting a code talk.py does not know (the person would
    read 'internal error' for a camera that refused), or talk.py expecting one
    the child never sends."""

    def values(expr):  # the strings an argument can evaluate to: a literal, or either arm of `a if c else b`
        if isinstance(expr, ast.IfExp):
            return values(expr.body) | values(expr.orelse)
        return {expr.value} if isinstance(expr, ast.Constant) and isinstance(expr.value, str) else set()

    codes = set()
    for node in ast.walk(ast.parse(CHILD.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "fail":
            codes |= values(node.args[0])
    assert codes == set(talk.CHILD_ERROR_CODES)


# ── B/C: Home Assistant <-> the card ─────────────────────────────────────────


@needs_node
def test_the_card_has_words_for_everything_home_assistant_can_say(tmp_path):
    """Kill: a refusal, error or `ended` reason the card has no text for (it
    would show the bare code, e.g. 'Talk ended (shutdown).'), or a notice for
    the person's own stop."""
    codes = sorted(_server_codes())
    got = _node(f"console.log(JSON.stringify({json.dumps(codes)}.map((c) => [c, cuboaiMicErrorText(c)])));", tmp_path)
    texts = dict(got)
    assert texts.pop(talk.REASON_CLIENT_END) == ""
    for code, text in texts.items():
        assert text and not text.startswith(("Talk ended (", "Two-way audio failed:")), (code, text)


def test_the_card_opens_the_talk_the_server_registers():
    """Kill: the command renamed on one side, or a request field the schema does
    not know (Home Assistant refuses the whole message: no talk at all)."""
    src = _card_src()
    m = re.search(
        r"const msg = \{ type: '([^']+)', device_id: deviceId, sample_rate: CUBOAI_MIC_RATE, dry_run: dryRun \}", src
    )
    assert m, "the card's cuboai/talk request changed shape"
    assert m.group(1) == talk.ws_talk._ws_command
    keys = {getattr(k, "schema", k) for k in talk.ws_talk._ws_schema}
    assert {"type", "device_id", "sample_rate", "dry_run"} <= keys


def test_the_card_listens_for_every_event_the_server_sends():
    """Kill: an event renamed on one side (the card would ignore it — a `live`
    the button never shows, an `ended` it never cleans up after)."""
    sent = set(re.findall(r'"type": "(\w+)"', (ROOT / "talk.py").read_text(encoding="utf-8")))
    assert sent == {"started", "live", "status", "error", "ended"}
    handled = set(re.findall(r"ev\.type === '(\w+)'", _card_src()))
    assert sent <= handled, sent - handled


@needs_node
def test_the_cards_numbers_fit_the_servers_limits(tmp_path):
    """Kill: a rate the server's schema refuses, a frame bigger than the server
    accepts (every one dropped as oversize), frames spaced beyond the idle stop,
    or a card timer that fires before the server's own (the server's reason —
    'the camera did not start the talk', 'time limit' — would be lost)."""
    got = _node(
        "console.log(JSON.stringify({ rate: CUBOAI_MIC_RATE, chunk: CUBOAI_MIC_CHUNK, noLive: CUBOAI_MIC_NO_LIVE_MS,"
        " backup: CUBOAI_MIC_BACKUP_MS, frame: cuboaiMicFrame(7, new Int16Array(CUBOAI_MIC_CHUNK)).length }));",
        tmp_path,
    )
    assert got["rate"] in talk.ALLOWED_RATES
    assert got["frame"] == 1 + 2 * got["chunk"] and got["frame"] - 1 <= talk.MAX_FRAME_BYTES
    assert got["chunk"] / got["rate"] < talk.IDLE_SECS / 10
    assert got["noLive"] > talk.READY_TIMEOUT_SECS * 1000
    assert got["backup"] > talk.TALK_MAX_SECS * 1000


# ── one whole talk: card frames -> Home Assistant -> the real child (dry run) ──

# What the card reads from each event (cuboai-card.js _cuboMicEvent).
_CARD_READS = {"started": ("handler_id", "dry_run", "max_secs"), "status": ("speech",), "ended": ("reason",)}


async def _until(cond, timeout) -> bool:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.02)
    return True


@needs_node
async def test_one_whole_talk_through_the_card_home_assistant_and_the_child(tmp_path, monkeypatch):
    """A 48 kHz microphone through the card's own resampler, chunker and frame
    helpers; Home Assistant's dispatch (byte 0 picks the handler); talk.py; the
    real child on --dry-run; the card's 1-byte end; the card's unsubscribe.
    Kill: any byte-layout, flag, protocol-line or event mismatch between the
    parts — the talk would never go `live`, lose audio, end for another reason
    or need SIGTERM to stop."""
    pytest.importorskip("av")
    card = _node(
        r"""
const IN = 48000, SECS = 12, rs = cuboaiMicResampler(IN, CUBOAI_MIC_RATE), chunk = cuboaiMicChunker(CUBOAI_MIC_CHUNK);
const frames = [];
for (let o = 0; o < IN * SECS; o += 1024) {            // the worklet's 1024-frame batches
  const x = new Float32Array(Math.min(1024, IN * SECS - o));
  for (let i = 0; i < x.length; i++) x[i] = 0.3 * Math.sin(2 * Math.PI * 440 * (o + i) / IN);
  for (const c of chunk(rs(x))) frames.push(Buffer.from(cuboaiMicFrame(7, c)).toString('base64'));
}
console.log(JSON.stringify({ frames, end: Buffer.from(cuboaiMicEndFrame(7)).toString('base64'),
  rate: CUBOAI_MIC_RATE, chunk: CUBOAI_MIC_CHUNK }));
""",
        tmp_path,
    )
    frames = [base64.b64decode(f) for f in card["frames"]]
    end = base64.b64decode(card["end"])
    # The payload is what the child reads as --in-codec pcm_s16le: the 440 Hz tone at 16 kHz, peak 0.3.
    pcm = b"".join(f[1:] for f in frames)
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    crossings = sum(1 for a, b in zip(samples, samples[1:]) if (a < 0) != (b < 0))
    assert abs(crossings / (len(samples) / card["rate"]) - 880) < 10, crossings
    assert abs(max(samples) - 0.3 * 32767) < 0.02 * 32767, max(samples)

    # An empty camera account: a talk that lost --dry-run stops on it, before any camera code.
    cam = {"device_id": DEVICE, "baby_name": "Baby", "uid": "uid-dry-run-only", "account": "", "password": ""}
    entry = SimpleNamespace(entry_id="entryC", data={"cameras": [cam]}, options={"enable_debug_logs": True})
    hass = FakeHass([entry])
    hass.config = SimpleNamespace(path=lambda *parts: str(tmp_path.joinpath(*parts)))
    registry = SimpleNamespace(
        async_get_entity_id=lambda domain, platform, uid: (
            SPEAKER if (domain, platform, uid) == ("media_player", DOMAIN, f"cuboai_speaker_{DEVICE}") else None
        )
    )
    monkeypatch.setattr(talk, "er", SimpleNamespace(async_get=lambda _hass: registry))
    conn = FakeConnection()
    raw = {"id": 5, "type": "cuboai/talk", "device_id": DEVICE, "sample_rate": card["rate"], "dry_run": True}
    msg = vol.Schema({vol.Required("id"): int, **talk.ws_talk._ws_schema})(raw)

    await talk.ws_talk(hass, conn, msg)
    session = talk._talk_data(hass).sessions[DEVICE]
    log = tmp_path / "cuboai_debug.log"

    def child_log():
        return log.read_text(encoding="utf-8", errors="replace") if log.exists() else "(no child log)"

    sent = after_live = 0
    try:
        started = conn.event("started")
        assert started["dry_run"] is True and started["handler_id"] == frames[0][0], started
        # Talk until 30 frames (1.2 s) have gone out after `live`, however slowly the child starts:
        # that is past the first STATUS, which comes after 16 frames the camera pulled.
        for frame in frames:
            assert len(frame) == 1 + 2 * card["chunk"] and frame[0] == started["handler_id"]
            assert conn.send_binary(frame[0], frame[1:])  # http.py: byte 0 is the handler, the rest the payload
            sent += len(frame) - 1
            await asyncio.sleep(card["chunk"] / card["rate"])  # the card's pace: one frame per 40 ms
            after_live += "live" in conn.types()
            if after_live >= 30:
                break
        assert after_live >= 30, (conn.types(), child_log())
        assert len(end) == 1
        conn.send_binary(end[0], end[1:])  # the card's end: the handler id alone
        await asyncio.wait_for(session.wait_closed(), 15)
    finally:
        if session.proc is not None and session.proc.returncode is None:
            session.proc.kill()
            await session.proc.wait()

    assert conn.errors == [] and "error" not in conn.types(), (conn.events, child_log())
    types = conn.types()
    assert types[:2] == ["started", "live"] and types[-1] == "ended" and types.count("ended") == 1, types
    assert "status" in types, types
    for kind, fields in _CARD_READS.items():
        for event in (e for e in conn.events if e["type"] == kind):
            assert all(f in event for f in fields), event
    assert set(conn.event("status")) == {"type", "sent", "speech", "rx", "dropped"}

    ended = conn.event("ended")
    assert ended["reason"] == talk.REASON_CLIENT_END and ended["camera"] is False, (ended, child_log())
    assert "DRY RUN" in child_log()
    assert session.proc.returncode == 0, "stdin EOF alone must end the child (no SIGTERM)"

    # Every byte the card sent reached the child, bar what Home Assistant dropped and counted.
    assert session.rx_bytes == sent
    assert session.child["rx"] == sent - session.dropped_bytes, (session.child, session.dropped_bytes)
    frames_in = session.child["rx"] // 2 // 1024
    assert frames_in - 4 <= ended["speech"] <= frames_in + 1, (ended, frames_in)
    assert ended["sent"] > 8  # speech and silence, and the 8-frame tail

    assert not talk.mic_active(hass, DEVICE) and DEVICE not in talk._talk_data(hass).sessions
    before = len(conn.events)
    conn.subscriptions.pop(msg["id"])()  # the card's unsubscribe after `ended`
    assert len(conn.events) == before
