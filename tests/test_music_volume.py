"""Music volume, for both ways music reaches the camera.

A song streamed through the talk channel has no volume on the camera (its speaker-level command
is ignored by the firmware), so the Speaker entity's volume is a gain applied when the song is
encoded. A lullaby the camera plays itself has the camera's own level (the Lullaby entity). The
card shows one slider and moves whichever of the two is playing.

Here: the encoder's gain, the `VOL` line of the `--playlist` child, the Lullaby entity's
volume_set, and the card's choice of entity. The Speaker entity's side is in
test_gapless_speaker_queue.py (it needs that file's simulated child).

Every test names the mutation it kills.
"""

import asyncio
import importlib.util
import io
import json
import math
import os
import queue
import shutil
import struct
import subprocess
import time
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_ROOT = Path(__file__).parent.parent
_TUTK = _ROOT / "custom_components" / "cuboai" / "tutk"
CARD = _ROOT / "custom_components" / "cuboai" / "www" / "cuboai-card.js"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, str(_TUTK / filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── the encoder's gain ───────────────────────────────────────────────────────


def _tone(path, secs, hz=440, rate=8000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(
            b"".join(
                struct.pack("<h", int(8000 * math.sin(2 * math.pi * hz * i / rate))) for i in range(int(secs * rate))
            )
        )
    return str(path)


def _encode(cp, path, **kw):
    enc = cp._TrackAacEncoder(**kw)
    return list(enc.track(path)) + list(enc.finish())


def _rms(units):
    """Decode ADTS frames back to samples and return their RMS (the first frames are the
    encoder's priming and are left out)."""
    av = pytest.importorskip("av")
    total, n = 0.0, 0
    with av.open(io.BytesIO(b"".join(units)), format="aac") as c:
        for k, frame in enumerate(c.decode(audio=0)):
            if k < 4:
                continue
            arr = frame.to_ndarray().astype("float64")
            total += float((arr * arr).sum())
            n += arr.size
    assert n > 0
    return math.sqrt(total / n)


def test_gain_makes_the_encoded_song_quieter(tmp_path):
    """A quarter gain comes out at about a quarter of the amplitude.

    Kill: ignore the gain in track(), or apply it to the wrong stream."""
    pytest.importorskip("av")
    pytest.importorskip("numpy")
    cp = _load("volume_pure", "cuboai_pure.py")
    song = _tone(tmp_path / "a.wav", 2.0)
    full = _rms(_encode(cp, song))
    quiet = _rms(_encode(cp, song, gain=0.25))
    assert full > 0
    assert 0.15 < quiet / full < 0.35, quiet / full


def test_full_volume_leaves_the_stream_exactly_as_it_was(tmp_path):
    """At gain 1.0 no filter is built, so the bytes are the ones sent before volume existed.

    Kill: always run the frames through the volume filter."""
    pytest.importorskip("av")
    cp = _load("volume_pure", "cuboai_pure.py")
    song = _tone(tmp_path / "a.wav", 1.0)
    enc = cp._TrackAacEncoder(gain=1.0)
    assert enc._gain_graph() is None
    assert _encode(cp, song) == _encode(cp, song, gain=1.0)
    assert _encode(cp, song) != _encode(cp, song, gain=0.5)


def test_gain_is_clamped_and_garbage_is_ignored():
    """No amplification (it clips), nothing below silence, and a non-number changes nothing.

    Kill: store the raw value."""
    pytest.importorskip("av")
    cp = _load("volume_pure", "cuboai_pure.py")
    enc = cp._TrackAacEncoder(gain=3)
    assert enc.gain == 1.0
    enc.set_gain(-1)
    assert enc.gain == 0.0
    enc.set_gain(0.4)
    for bad in ("loud", None, float("nan")):
        enc.set_gain(bad)
        assert enc.gain == 0.4


def test_a_gain_change_applies_to_the_next_track_decoded(tmp_path):
    """track() reads the gain when it starts, so decoding a song again picks up the new one.

    Kill: build the filter once in __init__."""
    pytest.importorskip("av")
    pytest.importorskip("numpy")
    cp = _load("volume_pure", "cuboai_pure.py")
    song = _tone(tmp_path / "a.wav", 2.0)
    enc = cp._TrackAacEncoder()
    loud = list(enc.track(song))
    enc.reset()
    enc.set_gain(0.25)
    quiet = list(enc.track(song))
    assert 0.15 < _rms(quiet) / _rms(loud) < 0.35


# ── the --playlist child's VOL line ──────────────────────────────────────────


class _Lines:
    def __init__(self):
        self._q = queue.Queue()

    def push(self, line):
        self._q.put(b"" if line is None else line.encode() + b"\n")

    def readline(self):
        return self._q.get(timeout=5)


class _Enc:
    """A track named 'nX' yields X frames named <name>/<k>@<gain>. One frame is one second
    (rate 1024), so `start` and the heard position count frames."""

    silent_unit = b"silence"
    rate = 1024

    def __init__(self):
        self.gain = 1.0
        self.resets = 0

    def set_gain(self, gain):
        self.gain = min(1.0, max(0.0, float(gain)))

    def reset(self):
        self.resets += 1

    def track(self, path, options=None, start=0.0):
        gain = self.gain
        n = int(path.rsplit("n", 1)[1])
        for k in range(int(start), n):
            yield f"{path}/{k}@{gain:g}".encode()

    def finish(self):
        yield b"tail"


def _feed(**kw):
    bc = _load("volume_backchannel", "cuboai_stream_backchannel.py")
    events, lines, enc = [], _Lines(), _Enc()
    feed = bc._PlaylistFeed(lines, encoder=enc, emit=events.append, open_track=lambda u: (u, None, None), **kw)
    return feed, lines, events, enc


def _pull(feed, n, timeout=3):
    out, t0 = [], time.time()
    while len(out) < n and time.time() - t0 < timeout:
        u = feed.next_unit()
        if u is None:
            time.sleep(0.005)
        else:
            out.append(u)
    return out


def _wait(cond, timeout=2.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.005)
    return False


def test_vol_mid_song_goes_on_from_where_it_is_at_the_new_volume():
    """After 5 frames were heard, VOL drops what was queued (encoded at the old volume) and the
    song goes on from frame 5 at the new one. It is the same song: no second TRACK.

    Kill: only set the gain (the ~10 s already queued would still play loud), restart the song
    from 0, or announce the song again."""
    feed, lines, events, enc = _feed()
    lines.push("s-n40")
    assert _pull(feed, 5) == [f"s-n40/{k}@1".encode() for k in range(5)]
    lines.push("VOL 0.25")
    assert _wait(lambda: enc.resets == 1)
    after = _pull(feed, 3)
    assert after == [f"s-n40/{k}@0.25".encode() for k in (5, 6, 7)], after
    assert events.count("TRACK 0") == 1
    assert "SEEKED 0 5.0" in events


def test_vol_before_anything_is_heard_plays_the_first_song_from_its_start():
    """The decoder runs ahead of the camera's grant: a VOL then must re-encode song 0 from the
    start and still announce it as a normal first play.

    Kill: leave the frames decoded ahead at the old volume, or report a seek instead of TRACK."""
    feed, lines, events, enc = _feed()
    lines.push("s-n20")
    assert _wait(lambda: len(feed._queue) > 5)  # decoded ahead, nothing pulled yet
    lines.push("VOL 0.5")
    assert _wait(lambda: enc.resets == 1)
    assert _pull(feed, 2) == [b"s-n20/0@0.5", b"s-n20/1@0.5"]
    assert events.count("TRACK 0") == 1
    assert not any(e.startswith("SEEKED") for e in events)


def test_vol_also_reaches_the_song_decoded_ahead():
    """The next song is decoded while the current one plays; it must come out at the new volume.

    Kill: keep the songs after the one heard as they were queued."""
    feed, lines, events, enc = _feed()
    lines.push("a-n4")
    lines.push("b-n4")
    assert _pull(feed, 2) == [b"a-n4/0@1", b"a-n4/1@1"]
    lines.push("VOL 0.5")
    assert _wait(lambda: enc.resets == 1)
    rest = _pull(feed, 6)
    assert rest == [b"a-n4/2@0.5", b"a-n4/3@0.5"] + [f"b-n4/{k}@0.5".encode() for k in range(4)], rest
    assert events.count("TRACK 1") == 1


def test_the_same_volume_again_changes_nothing():
    """A VOL that does not change the gain must not re-encode (each one is a small hiccup).

    Kill: always re-decode on VOL."""
    feed, lines, events, enc = _feed(gain=0.5)
    assert enc.gain == 0.5  # the session's starting volume
    lines.push("s-n30")
    assert _pull(feed, 3) == [f"s-n30/{k}@0.5".encode() for k in range(3)]
    lines.push("VOL 0.5")
    lines.push("VOL nonsense")
    time.sleep(0.1)
    assert enc.resets == 0
    assert _pull(feed, 1) == [b"s-n30/3@0.5"]


def test_vol_keeps_a_seek_that_was_just_asked_for():
    """A SEEK and a VOL close together: the song lands where the SEEK asked, at the new volume,
    whether the decoder had already started on the seek or not (its audio is not heard yet).

    Kill: replace the seek's target with the old heard position."""
    feed, lines, events, enc = _feed()
    lines.push("s-n60")
    _pull(feed, 2)
    lines.push("SEEK 30")
    lines.push("VOL 0.5")
    assert _wait(lambda: enc.gain == 0.5 and feed._seek is None and enc.resets >= 1)
    assert _wait(lambda: any(u == b"s-n60/30@0.5" for u in feed._queue))
    assert _pull(feed, 1) == [b"s-n60/30@0.5"]


def test_the_child_starts_at_the_volume_it_is_given_and_takes_vol_lines(tmp_path):
    """The real script, no camera: `--gain` and a VOL line are accepted and the run ends well.

    Kill: crash on --gain or on VOL."""
    pytest.importorskip("av")
    song = _tone(tmp_path / "a.wav", 1.0)
    out = subprocess.run(
        [os.sys.executable, str(_TUTK / "cuboai_stream_backchannel.py"), "--playlist", "--gain", "0.3", "--dry-run"],
        input=f"{song}\nVOL 0.6\nEND\n".encode(),
        capture_output=True,
        timeout=60,
    )
    text = out.stdout.decode("ascii", "replace")
    assert out.returncode == 0, out.stderr.decode("utf-8", "replace")[-800:]
    assert "@@TALK TRACK 0" in text and "@@TALK ERROR" not in text


# ── the Lullaby entity: the camera's own volume ──────────────────────────────


def _lullaby(monkeypatch, local, card_session=False):
    pytest.importorskip("homeassistant")
    from custom_components.cuboai import media_player

    sent = []
    monkeypatch.setattr(media_player, "_execute_lullaby_cmd", lambda *a: sent.append(a))
    monkeypatch.setattr(media_player, "_get_timer_minutes", lambda hass, uid: 30)

    async def _job(fn, *a):
        return fn(*a)

    async def _refresh():
        refreshed.append(True)

    refreshed, written = [], []
    e = object.__new__(media_player.CuboLullabyPlayer)
    e.hass = SimpleNamespace(async_add_executor_job=_job)
    e.coordinator = MagicMock()
    e.coordinator.data = {"cameras": {"DEV1": {"local": local}}}
    e.coordinator.async_request_refresh = _refresh
    e._device_id, e._uid, e._account, e._password, e._camera_ip = "DEV1", "uid", "acct", "pw", "192.0.2.10"
    e._stop_timer_task = object() if card_session else None
    e.async_write_ha_state = lambda: written.append(e.volume_level)
    return e, sent, refreshed, written


def test_lullaby_volume_is_rounded_not_cut(monkeypatch):
    """29% is 0.29 * 100 = 28.999...: the camera must get 29.

    Kill: int() instead of round()."""
    e, sent, _, _ = _lullaby(monkeypatch, {"lullaby_volume": 70})
    asyncio.run(e.async_set_volume_level(0.29))
    assert sent[0][4] == "volume" and sent[0][6] == 29


def test_lullaby_volume_shows_at_once_and_survives_a_stale_poll(monkeypatch):
    """Reported 2026-11-21: the slider jumped back and forth. A poll of the camera takes ~13 s;
    one that began before the volume was set ended after it and put the old level back, and
    the refresh asked for after every change made it happen every time. The entity now shows
    the level it set for a while whatever a poll says, and asks for no poll.

    Kill: show the poll's level right after a set, ask for a refresh, or hold it for ever."""
    e, sent, refreshed, written = _lullaby(monkeypatch, {"lullaby_volume": 70})
    asyncio.run(e.async_set_volume_level(0.4))
    assert written and written[0] == 0.4
    assert refreshed == []
    e.coordinator.data = {"cameras": {"DEV1": {"local": {"lullaby_volume": 70}}}}  # the stale poll lands
    assert e.volume_level == 0.4
    at = e._held_values["volume"][1]
    e._held_values["volume"] = (40, at - e.LULLABY_HOLD_S - 1)  # ... and later the camera's word counts
    assert e.volume_level == 0.7


def test_a_slider_moved_twice_writes_one_level_at_a_time_and_ends_on_the_last(monkeypatch):
    """Each write opens its own connection to the camera and takes seconds: two at once could
    land in either order. The second waits for the first and only the latest level is sent.

    Kill: start a second write while one is running, or drop the newer level."""
    e, sent, _, _ = _lullaby(monkeypatch, {"lullaby_volume": 70})
    running = {"n": 0, "max": 0}

    async def _job(fn, *a):
        running["n"] += 1
        running["max"] = max(running["max"], running["n"])
        await asyncio.sleep(0.02)
        running["n"] -= 1
        return fn(*a)

    e.hass.async_add_executor_job = _job

    async def _go():
        first = asyncio.ensure_future(e.async_set_volume_level(0.2))
        await asyncio.sleep(0.005)
        await e.async_set_volume_level(0.5)
        await e.async_set_volume_level(0.9)
        await first

    asyncio.run(_go())
    assert running["max"] == 1
    assert [s[6] for s in sent] == [20, 90]
    assert e.volume_level == 0.9


def test_a_stale_poll_cannot_undo_a_play_or_a_stop(monkeypatch):
    """Same report ("the button stopped working"): Stop was pressed and a poll that had begun
    before it showed the lullaby as playing again; a lullaby just started showed as idle, and
    the Speaker's queue, seeing it idle, stopped it. What was commanded is shown for a while.

    Kill: let the poll's playing flag or song through right after a command."""
    from custom_components.cuboai import media_player

    uuid_a, uuid_b = list(media_player.LULLABY_CATALOG)[:2]
    e, sent, _, _ = _lullaby(monkeypatch, {"lullaby_playing": False, "lullaby_song": uuid_a})
    e._catalog = media_player.LULLABY_CATALOG
    e._schedule_stop = lambda timer, uuid: None
    e._cancel_scheduled_stop = lambda: None
    stale = {"cameras": {"DEV1": {"local": {"lullaby_playing": False, "lullaby_song": uuid_a}}}}

    asyncio.run(e.async_select_source(media_player.LULLABY_CATALOG[uuid_b][1]))
    e.coordinator.data = stale
    assert e.state == media_player.MediaPlayerState.PLAYING
    assert e.source == media_player.LULLABY_CATALOG[uuid_b][1]

    asyncio.run(e.async_media_stop())
    assert sent[-1][4] == "stop" and sent[-1][5] == uuid_b, "stopped the song a stale poll named, not the one playing"
    e.coordinator.data = {"cameras": {"DEV1": {"local": {"lullaby_playing": True, "lullaby_song": uuid_b}}}}
    assert e.state == media_player.MediaPlayerState.IDLE


def test_lullaby_volume_keeps_a_card_sessions_repeat_forever(monkeypatch):
    """A lullaby played from the card repeats until Home Assistant stops it: the volume write
    must not put the camera's own timer back. A native play re-sends its Lullaby Timer.

    Kill: always send the timer."""
    e, sent, _, _ = _lullaby(monkeypatch, {"lullaby_volume": 70}, card_session=True)
    asyncio.run(e.async_set_volume_level(0.5))
    assert sent[0][7] is None
    e, sent, _, _ = _lullaby(monkeypatch, {"lullaby_volume": 70})
    asyncio.run(e.async_set_volume_level(0.5))
    assert sent[0][7] == 30


# ── the card: one slider, the entity that is playing ─────────────────────────

_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _card(body):
    src = CARD.read_text(encoding="utf-8")
    helpers = src[
        src.index("function cuboaiVolumeTarget") : src.index("// The collection song's name for a playing URL")
    ]
    out = subprocess.run(
        ["node", "-"], input=helpers + body, capture_output=True, text=True, encoding="utf-8", check=True
    )
    return json.loads(out.stdout)


@_node
def test_the_slider_moves_the_lullaby_while_the_camera_plays_one():
    """A playing lullaby wins, even when the Speaker says `playing` too (it does, for a lullaby
    handed over from its queue).

    Kill: always target the speaker."""
    r = _card(
        """
        const sp = { state: 'playing', attributes: { volume_level: 1 } };
        const lu = { state: 'playing', attributes: { volume_level: 0.4 } };
        console.log(JSON.stringify(cuboaiVolumeTarget('media_player.x_speaker', sp, 'media_player.x_lullaby', lu)));
        """
    )
    assert r == {"entity_id": "media_player.x_lullaby", "kind": "lullaby", "percent": 40}


@_node
def test_the_slider_moves_the_speaker_for_a_streamed_song():
    """Kill: target the lullaby entity whenever it exists."""
    r = _card(
        """
        const sp = { state: 'playing', attributes: { volume_level: 0.55 } };
        const lu = { state: 'idle', attributes: { volume_level: 0.4 } };
        console.log(JSON.stringify([
          cuboaiVolumeTarget('media_player.x_speaker', sp, 'media_player.x_lullaby', lu),
          cuboaiVolumeTarget('media_player.x_speaker', sp, null, undefined),
          cuboaiVolumeTarget('media_player.x_speaker', { state: 'playing', attributes: {} }, null, null),
          cuboaiVolumeTarget(null, undefined, null, null),
        ]));
        """
    )
    assert r[0] == {"entity_id": "media_player.x_speaker", "kind": "stream", "percent": 55}
    assert r[1] == r[0]
    assert r[2]["percent"] is None  # no level reported: the slider is left where it is
    assert r[3] is None


def test_the_slider_is_wired_to_volume_set_and_hidden_when_nothing_plays():
    """Wiring: sent on release only, to the entity chosen at that moment; a drag is not fought
    by state updates; the row hides with Now Playing.

    Kill: send on every `input`, hard-code the entity, or leave the row up when idle."""
    src = CARD.read_text(encoding="utf-8")
    assert 'id="volBar"' in src and 'id="nowVol"' in src
    assert "volBar.addEventListener('input'" in src and "volBar.addEventListener('change'" in src
    assert "callService('media_player', 'volume_set', { entity_id: target.entity_id, volume_level: level })" in src
    assert src.count("callService('media_player', 'volume_set'") == 1
    assert "if (this._volDragging) return;" in src
    assert "this._updateVolumeControl(true);" in src and "this._updateVolumeControl(false);" in src
