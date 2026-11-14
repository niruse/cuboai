"""A YouTube download refused with 403 is tried once more with a fresh link.

YouTube signs each download link for one client and session, and its anti-bot checks refuse
some at random: on 11-14 one song of six got "HTTP Error 403: Forbidden" on the box and played
fine from another session minutes later. A fresh request gets a fresh link. Only a 403 is
retried (a private or removed video is not), and only once (a song that keeps failing is
skipped, as before). The yt-dlp floor is raised so Home Assistant upgrades an old install:
YouTube changes every few weeks and only a recent yt-dlp keeps up.

Every test names the mutation it kills.
"""

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from custom_components.cuboai import media_player  # noqa: E402

FORBIDDEN = "ERROR: unable to download video data: HTTP Error 403: Forbidden"


class _FakeYtDlp:
    """yt_dlp stand-in: each extract_info() takes the next scripted outcome."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        fake = self

        class YoutubeDL:
            def __init__(self, opts):
                self.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_info(self, url, download=False):
                fake.calls += 1
                out = fake.outcomes.pop(0)
                if isinstance(out, Exception):
                    raise out
                return out

        self.YoutubeDL = YoutubeDL


@pytest.fixture
def rig(monkeypatch, tmp_path):
    def _make(outcomes):
        fake = _FakeYtDlp(outcomes)
        monkeypatch.setitem(sys.modules, "yt_dlp", fake)
        monkeypatch.setattr(time, "sleep", lambda s: None)
        monkeypatch.setattr(media_player, "_internal_base_url", lambda hass: "http://ha:8123")

        async def _job(fn, *a):
            return fn(*a)

        mp = object.__new__(media_player.CuboAIMediaPlayer)
        mp.hass = SimpleNamespace(
            data={"cuboai": {"youtube_cache_enabled": False}},
            config=SimpleNamespace(path=lambda *parts: str(tmp_path.joinpath(*parts))),
            async_add_executor_job=_job,
        )
        return mp, fake

    return _make


async def test_a_403_is_retried_once_with_a_fresh_link(rig):
    """Kill: no retry (the song is skipped on the first 403)."""
    mp, fake = rig([Exception(FORBIDDEN), {"url": "https://rr1.googlevideo.com/fresh"}])
    url = await mp._extract_media_url("https://www.youtube.com/watch?v=abcdefghijk")
    assert url == "https://rr1.googlevideo.com/fresh"
    assert fake.calls == 2


async def test_a_second_403_skips_the_song(rig, caplog):
    """Only one retry, then the song fails as before (the queue moves on).

    Kill: retry forever (a blocked song would hold the queue)."""
    mp, fake = rig([Exception(FORBIDDEN), Exception(FORBIDDEN), {"url": "never"}])
    with pytest.raises(ValueError):
        await mp._extract_media_url("https://www.youtube.com/watch?v=abcdefghijk")
    assert fake.calls == 2
    assert "403" in caplog.text


async def test_a_private_video_is_not_retried(rig):
    """Content errors cannot change on a retry. Kill: retry every failure."""
    mp, fake = rig([Exception("ERROR: [youtube] abc: Private video. Sign in"), {"url": "never"}])
    with pytest.raises(ValueError):
        await mp._extract_media_url("https://www.youtube.com/watch?v=abcdefghijk")
    assert fake.calls == 1


@pytest.mark.parametrize(
    ("message", "forbidden"),
    [
        (FORBIDDEN, True),
        ("ERROR: Unable to download API page: HTTP Error 403: Forbidden", True),
        ("HTTP Error 403", True),
        ("ERROR: unable to download video data: HTTP Error 500", False),
        ("ERROR: [youtube] abc: This video is unavailable", False),
        ("ERROR: [youtube] abc: video id 4031234567 not found", False),
    ],
)
def test_only_a_refused_download_counts_as_403(message, forbidden):
    """Kill: match any '403' in the text (a video id containing 403 would be retried)."""
    assert media_player._is_yt_forbidden(message) is forbidden


def test_the_yt_dlp_floor_is_recent_but_never_above_home_assistants_pin():
    """Home Assistant only upgrades a requirement whose floor the install no longer meets, so the
    floor must be recent. But Home Assistant's own media_extractor pins yt-dlp EXACTLY
    (`yt-dlp[default]==2026.07.04` in 2026.11): a floor above that pin conflicts with it (hassfest
    fails, and both integrations fight over the installed version). v2.6.60 shipped 2026.10.26
    and hassfest rejected it. Raise the floor only when Home Assistant raises its pin.

    Kill: put the 2024 floor back, or a floor above Home Assistant's pin."""
    manifest = json.loads(Path("custom_components/cuboai/manifest.json").read_text(encoding="utf-8"))
    pins = [r for r in manifest["requirements"] if r.startswith("yt-dlp")]
    assert pins == ["yt-dlp>=2026.07.04"]
