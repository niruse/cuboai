"""The speaker queue plays consecutive songs through ONE `--playlist` child (gapless).

A simulated child answers like the real one: TRACK i when song i starts (the first one at once,
each next one after the previous "song" plays for a moment), SKIP i for a URL it cannot play,
IDLE when it has played everything it was given, and END + exit when stdin closes.

Every test names the mutation it kills.
"""

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from homeassistant.components.media_player import MediaPlayerState, RepeatMode  # noqa: E402

from custom_components.cuboai import media_player, talk  # noqa: E402

SONG_S = 0.05  # how long the simulated child "plays" each song


class _Child:
    def __init__(self, argv):
        self.argv = argv
        self.written = []
        self.seeks = []  # SEEK lines received
        self.stdin_closed = False
        self.terminated = False
        self.returncode = None
        self.stdout = asyncio.StreamReader()
        self._exited = asyncio.Event()
        self._started = 0  # songs started or skipped
        self._closed = False
        self._idle_task = None
        self.idles = 0  # IDLE lines emitted so far
        self.idles_at_write = []  # ... at the moment each URL arrived
        child = self

        class _Stdin:
            def write(self, data):
                if child.stdin_closed:
                    raise BrokenPipeError("stdin is closed")
                for line in data.decode().splitlines():
                    if line == "END":
                        child._close()
                    elif line.startswith("SEEK "):
                        child.seeks.append(float(line.split()[1]))
                        child._emit(f"SEEKED {child._started - 1} {line.split()[1]}")
                    else:
                        child._got(line)

            async def drain(self):
                await asyncio.sleep(0)

            def close(self):
                child.stdin_closed = True
                child._close()

        self.stdin = _Stdin()

    def _emit(self, line):
        self.stdout.feed_data(f"@@TALK {line}\n".encode())

    def _got(self, url):
        i = len(self.written)
        self.written.append(url)
        self.idles_at_write.append(self.idles)
        delay = 0 if i == 0 else SONG_S
        asyncio.get_running_loop().call_later(delay, self._start, i, url)

    def _start(self, i, url):
        if self.returncode is not None:
            return
        if "/bad" not in url:
            self._emit(f"DUR {i} 120.0")
        self._emit(f"SKIP {i}" if "/bad" in url else f"TRACK {i}")
        self._started = i + 1
        if self._idle_task:
            self._idle_task.cancel()
        self._idle_task = asyncio.get_running_loop().call_later(SONG_S, self._maybe_idle)

    def _maybe_idle(self):
        if self.returncode is None and self._started == len(self.written) and not self._closed:
            self.idles += 1
            self._emit("IDLE")

    def _close(self):
        if self._closed:
            return
        self._closed = True
        asyncio.get_running_loop().call_later(SONG_S, self._exit, 0)

    def _exit(self, code):
        if self.returncode is None:
            self.returncode = code
            self._emit("END sent=1 speech=1 rx=0 dropped=0 camera=1")
            self.stdout.feed_eof()
            self._exited.set()

    def terminate(self):
        self.terminated = True
        self._exit(-15)

    async def wait(self):
        await self._exited.wait()
        return self.returncode


class _Arbiter:
    def __init__(self, free=True):
        self.free = free
        self.claims = 0
        self.released = []

    def try_claim(self, device_id, who):
        self.claims += 1
        return self.free

    def release_after_exit(self, device_id, who, proc):
        self.released.append(proc)


@pytest.fixture
def rig(monkeypatch):
    children, titles = [], []
    arb = _Arbiter()

    async def _spawn(*argv, **kw):
        c = _Child(argv)
        children.append(c)
        return c

    async def _stderr(hass, options):
        return asyncio.subprocess.DEVNULL

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    monkeypatch.setattr(talk, "get_arbiter", lambda hass: arb)
    monkeypatch.setattr(talk, "build_backchannel_env", lambda cam, options: {})
    monkeypatch.setattr(talk, "open_debug_stderr", _stderr)

    mp = object.__new__(media_player.CuboAIMediaPlayer)
    mp.hass = SimpleNamespace(data={})
    mp._device_id = "DEV1"
    mp._cam = {}
    mp._options = {}
    mp._queue = []
    mp._attr_repeat = RepeatMode.OFF
    mp._attr_state = MediaPlayerState.IDLE
    mp._backchannel_proc = None
    mp.async_write_ha_state = lambda: titles.append(mp._attr_media_title)

    async def _extract(media_id):
        if "noextract" in media_id:
            raise ValueError("yt-dlp said no")
        return "u:" + media_id

    mp._extract_media_url = _extract
    return SimpleNamespace(mp=mp, children=children, titles=titles, arb=arb)


async def _run(rig, first, budget=None):
    session, refill = [], {"ok": True}
    calls = {"n": 0}

    def expired():
        calls["n"] += 1
        return budget is not None and calls["n"] > budget

    res = await asyncio.wait_for(rig.mp._play_song_run(first, session, refill, lambda: 0, expired), timeout=5)
    return res, session


async def test_consecutive_songs_share_one_child_and_play_in_order(rig):
    """Three queued songs: ONE child, every URL handed over in order, each title shown in turn.

    Kill: spawn a child per song (the old way), or stop after the first song."""
    rig.mp._queue[:] = ["https://youtu.be/b", "https://open.spotify.com/track/c"]
    res, session = await _run(rig, "https://youtu.be/a")
    assert res == "next"
    assert len(rig.children) == 1
    child = rig.children[0]
    assert "--playlist" in child.argv
    assert child.written == ["u:https://youtu.be/a", "u:https://youtu.be/b", "u:https://open.spotify.com/track/c"]
    assert [t for t in rig.titles if t] == [
        "https://youtu.be/a",
        "https://youtu.be/b",
        "https://open.spotify.com/track/c",
    ]
    assert session == ["https://youtu.be/a", "https://youtu.be/b", "https://open.spotify.com/track/c"]
    assert rig.arb.claims == 1 and rig.arb.released == [child]


async def test_the_next_song_is_handed_over_while_the_current_one_plays(rig):
    """The next URL is written when the current song starts, not after it ends.

    Kill: wait for IDLE before handing over the next song (a gap per song again)."""
    rig.mp._queue[:] = ["https://youtu.be/b"]
    task = asyncio.ensure_future(_run(rig, "https://youtu.be/a"))
    for _ in range(100):
        await asyncio.sleep(0.001)
        if rig.children and len(rig.children[0].written) == 2:
            break
    child = rig.children[0]
    assert len(child.written) == 2
    assert child._started == 1  # song b was handed over while song a is playing
    assert child.idles_at_write[1] == 0  # ...before the child ran out of songs
    await task


async def test_a_song_that_fails_is_skipped_and_the_run_goes_on(rig):
    """A failed extraction is never written; a SKIP from the child moves on to the next song.

    Kill: end the run on a failed extraction or on SKIP."""
    rig.mp._queue[:] = ["https://youtu.be/noextract", "https://youtu.be/bad", "https://youtu.be/d"]
    res, session = await _run(rig, "https://youtu.be/a")
    child = rig.children[0]
    assert child.written == ["u:https://youtu.be/a", "u:https://youtu.be/bad", "u:https://youtu.be/d"]
    assert session == ["https://youtu.be/a", "https://youtu.be/d"]
    assert len(rig.children) == 1


async def test_a_lullaby_ends_the_run_and_stays_first_in_the_queue(rig):
    """A lullaby is played natively by the queue loop after the run: it must not be streamed.

    Kill: hand the lullaby name to the child, or drop it from the queue."""
    rig.mp._queue[:] = ["https://youtu.be/b", "Brahms Lullaby", "https://youtu.be/c"]
    res, _ = await _run(rig, "https://youtu.be/a")
    assert res == "next"
    assert rig.children[0].written == ["u:https://youtu.be/a", "u:https://youtu.be/b"]
    assert rig.mp._queue == ["Brahms Lullaby", "https://youtu.be/c"]


async def test_repeat_one_keeps_handing_over_the_same_song(rig):
    """Repeat One: the song is queued again as it starts, so it loops without a gap.

    Kill: drop the Repeat One re-queue."""
    rig.mp._attr_repeat = RepeatMode.ONE
    task = asyncio.ensure_future(_run(rig, "https://youtu.be/a"))
    for _ in range(200):
        await asyncio.sleep(0.005)
        if rig.children and len(rig.children[0].written) >= 3:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert rig.children[0].written[:3] == ["u:https://youtu.be/a"] * 3


async def test_repeat_all_puts_each_song_back_at_the_end(rig):
    """Repeat All: each started song goes to the end of the queue.

    Kill: drop the Repeat All re-queue."""
    rig.mp._attr_repeat = RepeatMode.ALL
    rig.mp._queue[:] = ["https://youtu.be/b"]
    task = asyncio.ensure_future(_run(rig, "https://youtu.be/a"))
    for _ in range(200):
        await asyncio.sleep(0.005)
        if rig.children and len(rig.children[0].written) >= 4:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert rig.children[0].written[:4] == ["u:https://youtu.be/a", "u:https://youtu.be/b"] * 2


async def test_play_time_spent_stops_the_child_and_the_queue(rig):
    """Play Time over: the child is terminated, the queue cleared, and the loop told to stop.

    Kill: ignore expired() inside the run."""
    rig.mp._queue[:] = ["https://youtu.be/b"]
    res, _ = await _run(rig, "https://youtu.be/a", budget=1)
    assert res == "stop"
    assert rig.children[0].terminated
    assert rig.mp._queue == []


async def test_a_live_talk_holding_the_speaker_drops_the_queue(rig):
    """The speaker is one talker at a time: no child, queue dropped, loop told to stop.

    Kill: spawn the child without the arbiter's claim."""
    rig.arb.free = False
    rig.mp._queue[:] = ["https://youtu.be/b"]
    res, _ = await _run(rig, "https://youtu.be/a")
    assert res == "stop"
    assert rig.children == []
    assert rig.mp._queue == []


async def test_a_song_queued_after_the_last_one_started_still_plays(rig):
    """IDLE first asks the queue again: a song added late joins the same run.

    Kill: close stdin on IDLE without looking at the queue."""
    task = asyncio.ensure_future(_run(rig, "https://youtu.be/a"))
    for _ in range(100):
        await asyncio.sleep(0.001)
        if rig.titles:  # Home Assistant has handled TRACK 0 (and found nothing to hand over)
            break
    await asyncio.sleep(SONG_S / 5)
    assert rig.children[0].written == ["u:https://youtu.be/a"]
    rig.mp._queue.append("https://youtu.be/late")
    await task
    assert rig.children[0].written == ["u:https://youtu.be/a", "u:https://youtu.be/late"]
    assert len(rig.children) == 1


async def test_seek_reaches_the_child_and_the_position_follows_it(rig):
    """Seek goes to the running child as SEEK <secs>; the position moves when the child says
    the new audio is heard (SEEKED), and the duration comes from DUR.

    Kill: no async_media_seek, ignore SEEKED, or ignore DUR."""
    from homeassistant.components.media_player import MediaPlayerEntityFeature

    task = asyncio.ensure_future(_run(rig, "https://youtu.be/a"))
    for _ in range(200):
        await asyncio.sleep(0.001)
        if rig.titles:
            break
    assert rig.mp._attr_media_duration == 120.0 and rig.mp._attr_media_position == 0.0
    await rig.mp.async_media_seek(42)
    await asyncio.sleep(0.01)
    assert rig.children[0].seeks == [42.0]
    assert rig.mp._attr_media_position == 42.0
    await task
    assert rig.mp._attr_media_position is None  # the run is over: no position any more
    assert MediaPlayerEntityFeature.SEEK


async def test_a_seek_with_nothing_streaming_does_nothing(rig):
    """Idle (or a lullaby the camera plays itself): there is no child to move.

    Kill: write to a stale pipe / raise."""
    rig.mp._playlist_stdin = None
    await rig.mp.async_media_seek(10)
    assert rig.children == []


async def test_the_last_song_ends_with_end_not_eof(rig):
    """END keeps the child's stdin open, so a seek on the last song still reaches it.

    Kill: close stdin to end the run (a seek on the last song would be lost)."""
    task = asyncio.ensure_future(_run(rig, "https://youtu.be/a"))
    for _ in range(300):
        await asyncio.sleep(0.001)
        if rig.children and rig.children[0]._closed:
            break
    await rig.mp.async_media_seek(5)
    await task
    assert rig.children[0].seeks == [5.0]


def test_the_speaker_advertises_seek():
    """The seek bar shows only when the entity says it can seek. Kill: drop SEEK."""
    import inspect

    from homeassistant.components.media_player import MediaPlayerEntityFeature

    src = inspect.getsource(media_player.CuboAIMediaPlayer.__init__)
    assert "MediaPlayerEntityFeature.SEEK" in src and MediaPlayerEntityFeature.SEEK
