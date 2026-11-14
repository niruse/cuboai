"""YouTube and Spotify playlists: read without a token, played as their songs, added two ways.

Before: a Spotify playlist link played ONE YouTube video found by the list's name, and a
YouTube /playlist link only its first song. Now the speaker queues every song in order, and
the card adds a playlist link either as one item or as its songs (optionally into a saved
playlist). Every test names the mutation it kills.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
CARD = ROOT / "custom_components" / "cuboai" / "www" / "cuboai-card.js"

# playlists.py has no Home Assistant imports: load it on its own, so these run anywhere.
_spec = importlib.util.spec_from_file_location("cuboai_playlists", ROOT / "custom_components/cuboai/playlists.py")
pl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pl)

SP_ID = "5zDL71gEZIT2ndrUxl5IgF"


@pytest.mark.parametrize(
    ("url", "ref", "whole"),
    [
        (f"https://open.spotify.com/playlist/{SP_ID}?si=abc&pi=x", ("spotify", "playlist", SP_ID), True),
        (f"https://open.spotify.com/intl-he/album/{SP_ID}", ("spotify", "album", SP_ID), True),
        (f"https://open.spotify.com/embed/playlist/{SP_ID}", ("spotify", "playlist", SP_ID), True),
        ("https://www.youtube.com/playlist?list=PLabcdefghij12", ("youtube", "playlist", "PLabcdefghij12"), True),
        ("https://music.youtube.com/playlist?list=PLabcdefghij12", ("youtube", "playlist", "PLabcdefghij12"), True),
        # A song played from a list: the card asks; play_media keeps it ONE song, as before.
        (
            "https://www.youtube.com/watch?v=abcdefghijk&list=PLabcdefghij12",
            ("youtube", "playlist", "PLabcdefghij12"),
            False,
        ),
        (f"https://open.spotify.com/track/{SP_ID}", None, False),
        ("https://www.youtube.com/watch?v=abcdefghijk", None, False),
        ("https://evil.example/playlist?list=PLabcdefghij12", None, False),
        ("https://www.youtube.com.evil.example/playlist?list=PLabcdefghij12", None, False),
        ("https://www.youtube.com/playlist?list=bad'quote", None, False),
        ("ytsearch1:some song", None, False),
    ],
)
def test_which_links_are_playlists(url, ref, whole):
    """Kill: any host with list= counted (a lookalike host would be fetched), a track link
    expanded, or a watch?v=&list= link turned from one song into the whole list."""
    assert pl.playlist_ref(url) == ref
    assert pl.is_playlist_link(url) is whole


def test_the_list_link_loses_its_tracking_parameters():
    """Kill: the si=/pi= tracking ids stored in the library (and shown, synced, logged)."""
    assert pl.canonical_url(f"https://open.spotify.com/playlist/{SP_ID}?si=abc&pi=x") == (
        f"https://open.spotify.com/playlist/{SP_ID}"
    )
    assert pl.canonical_url("https://www.youtube.com/watch?v=abcdefghijk&list=PLabcdefghij12&index=3") == (
        "https://www.youtube.com/playlist?list=PLabcdefghij12"
    )


def _embed(tracks, name="מזרחית שקטים"):
    data = {"props": {"pageProps": {"state": {"data": {"entity": {"name": name, "trackList": tracks}}}}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'


def test_spotify_embed_tracks_become_track_links_in_order():
    """Kill: artist/title swapped or dropped, order changed, or unplayable items kept
    (an episode or a removed song would stall the queue)."""
    html = _embed(
        [
            {
                "uri": "spotify:track:AAAAAAAAAAAAAAAAAAAAAA",
                "title": "Song A",
                "subtitle": "Artist A",
                "duration": 183519,
            },
            {"uri": "spotify:episode:BBBBBBBBBBBBBBBBBBBBBB", "title": "A podcast", "subtitle": "Show"},
            {"uri": "spotify:track:CCCCCCCCCCCCCCCCCCCCCC", "title": "Gone", "subtitle": "X", "isPlayable": False},
            {"uri": "spotify:track:DDDDDDDDDDDDDDDDDDDDDD", "title": "Song D", "subtitle": "", "duration": 0},
        ]
    )
    out = pl.parse_spotify_embed(html)
    assert out["name"] == "מזרחית שקטים"
    assert out["tracks"] == [
        {"name": "Artist A – Song A", "url": "https://open.spotify.com/track/AAAAAAAAAAAAAAAAAAAAAA", "seconds": 184},
        {"name": "Song D", "url": "https://open.spotify.com/track/DDDDDDDDDDDDDDDDDDDDDD", "seconds": None},
    ]


def test_a_spotify_page_without_the_list_is_an_error_not_an_empty_list():
    """Kill: a changed Spotify page read as 'no songs' silently."""
    with pytest.raises(ValueError):
        pl.parse_spotify_embed("<html>nothing here</html>")


def test_youtube_flat_entries_skip_deleted_and_private_videos():
    """Kill: placeholders queued (yt-dlp lists removed videos as '[Deleted video]')."""
    out = pl.parse_youtube_flat(
        {
            "title": "Popular",
            "entries": [
                {"id": "abcdefghijk", "title": "One", "duration": 200.0},
                {"id": "bbbbbbbbbbb", "title": "[Deleted video]"},
                {"id": "ccccccccccc", "title": "[Private video]"},
                None,
                {"id": "not-an-id", "title": "bad"},
                {"id": "ddddddddddd", "title": "Two"},
            ],
        }
    )
    assert out == {
        "name": "Popular",
        "tracks": [
            {"name": "One", "url": "https://www.youtube.com/watch?v=abcdefghijk", "seconds": 200},
            {"name": "Two", "url": "https://www.youtube.com/watch?v=ddddddddddd", "seconds": None},
        ],
    }


def test_a_huge_list_is_capped(monkeypatch):
    """Kill: no cap (a 5000-song mix would flood the speaker queue)."""
    tracks = [{"uri": f"spotify:track:{i:022d}", "title": f"S{i}", "subtitle": "A"} for i in range(pl.MAX_TRACKS + 50)]
    assert len(pl.parse_spotify_embed(_embed(tracks))["tracks"]) == pl.MAX_TRACKS


def test_fetch_playlist_reads_the_embed_page_and_reports_the_clean_link(monkeypatch):
    """Kill: the page fetched from the link the user pasted (any host) instead of the fixed
    Spotify embed address, or an empty list returned instead of an error."""
    seen = []

    def fake_get(url, timeout=15):
        seen.append(url)
        return _embed([{"uri": "spotify:track:AAAAAAAAAAAAAAAAAAAAAA", "title": "A", "subtitle": "B"}])

    monkeypatch.setattr(pl, "_get", fake_get)
    out = pl.fetch_playlist(f"https://open.spotify.com/playlist/{SP_ID}?si=zzz")
    assert seen == [f"https://open.spotify.com/embed/playlist/{SP_ID}"]
    assert out["url"] == f"https://open.spotify.com/playlist/{SP_ID}" and out["source"] == "spotify"

    monkeypatch.setattr(pl, "_get", lambda url, timeout=15: _embed([]))
    with pytest.raises(ValueError):
        pl.fetch_playlist(f"https://open.spotify.com/playlist/{SP_ID}")


# ---- the speaker: a playlist link queues its songs ---------------------------------------


@pytest.fixture
def speaker(monkeypatch):
    pytest.importorskip("homeassistant")
    import sys
    import types

    from custom_components.cuboai import media_player, playlists, talk

    monkeypatch.setattr(talk, "mic_active", lambda hass, dev: False)
    # The test stubs of Home Assistant have no media_source / browse_media: give the two
    # functions async_play_media imports (a plain URL passes through both unchanged).
    ms = types.ModuleType("homeassistant.components.media_source")
    ms.is_media_source_id = lambda m: False
    ms.async_resolve_media = None
    bm = types.ModuleType("homeassistant.components.media_player.browse_media")
    bm.async_process_play_media_url = lambda hass, m: m
    monkeypatch.setitem(sys.modules, "homeassistant.components.media_source", ms)
    monkeypatch.setitem(sys.modules, "homeassistant.components.media_player.browse_media", bm)
    fetched = []

    def fake_fetch(url):
        fetched.append(url)
        return {
            "name": "L",
            "tracks": [{"url": "https://open.spotify.com/track/A"}, {"url": "https://open.spotify.com/track/B"}],
        }

    monkeypatch.setattr(playlists, "fetch_playlist", fake_fetch)

    async def _job(fn, *a):
        return fn(*a)

    e = object.__new__(media_player.CuboAIMediaPlayer)
    e.hass = SimpleNamespace(async_add_executor_job=_job)
    e._device_id = "dev"
    e._queue = []
    e._queue_task = None
    e._attr_state = None
    e.started = 0

    async def _stop():
        e._queue.clear()

    def _start():
        e.started += 1

    e.async_media_stop = _stop
    e._start_queue_loop = _start
    return e, fetched


async def test_a_playlist_link_queues_every_song_in_order(speaker):
    """Kill: the expansion removed (the list link queued as one 'song', which plays one
    YouTube video found by the list's name)."""
    e, fetched = speaker
    await e.async_play_media("music", f"https://open.spotify.com/playlist/{SP_ID}")
    assert e._queue == ["https://open.spotify.com/track/A", "https://open.spotify.com/track/B"]
    assert e.started == 1 and len(fetched) == 1


async def test_adding_a_playlist_appends_its_songs_behind_the_queue(speaker):
    """Kill: 'add' replacing the queue, or appending the list as one item."""
    e, _ = speaker
    e._queue = ["https://www.youtube.com/watch?v=abcdefghijk"]
    e._attr_state = "playing"
    e._queue_task = object()
    await e.async_play_media("music", "https://www.youtube.com/playlist?list=PLabcdefghij12", enqueue="add")
    assert e._queue == [
        "https://www.youtube.com/watch?v=abcdefghijk",
        "https://open.spotify.com/track/A",
        "https://open.spotify.com/track/B",
    ]


async def test_a_song_link_is_not_fetched_as_a_list(speaker):
    """Kill: every link sent to the playlist reader (a song played from a list would become
    the whole list)."""
    e, fetched = speaker
    await e.async_play_media("music", "https://www.youtube.com/watch?v=abcdefghijk&list=PLabcdefghij12")
    assert e._queue == ["https://www.youtube.com/watch?v=abcdefghijk&list=PLabcdefghij12"] and fetched == []


async def test_an_unreadable_playlist_stops_nothing(speaker, monkeypatch):
    """Kill: the queue stopped (or the list link queued) when the list can't be read."""
    from custom_components.cuboai import playlists

    def boom(url):
        raise ValueError("no")

    monkeypatch.setattr(playlists, "fetch_playlist", boom)
    e, _ = speaker
    e._queue = ["keep"]
    await e.async_play_media("music", f"https://open.spotify.com/playlist/{SP_ID}")
    assert e._queue == ["keep"] and e.started == 0


# ---- the card: add a playlist as one item or as its songs ---------------------------------

node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _card(body):
    src = CARD.read_text(encoding="utf-8")
    helpers = src[src.index("function cuboaiPlaylistRef") : src.index('// "Play all" for the songs a collection view')]
    out = subprocess.run(
        ["node", "-"], input=helpers + body, capture_output=True, text=True, encoding="utf-8", check=True
    )
    return json.loads(out.stdout)


LIST = {
    "source": "spotify",
    "url": f"https://open.spotify.com/playlist/{SP_ID}",
    "name": "מזרחית שקטים",
    "tracks": [{"name": "A – 1", "url": "u1"}, {"name": "A – 2", "url": "u2"}],
}


@node
def test_card_reads_links_like_the_server():
    """Kill: the card and playlists.py disagree on what a playlist link is."""
    urls = [
        f"https://open.spotify.com/playlist/{SP_ID}?si=abc",
        f"https://open.spotify.com/intl-he/album/{SP_ID}",
        "https://www.youtube.com/watch?v=abcdefghijk&list=PLabcdefghij12",
        "https://www.youtube.com.evil.example/playlist?list=PLabcdefghij12",
        f"https://open.spotify.com/track/{SP_ID}",
    ]
    got = _card(f"console.log(JSON.stringify({json.dumps(urls)}.map(u => cuboaiPlaylistRef(u))))")
    want = []
    for u in urls:
        ref = pl.playlist_ref(u)
        want.append({"source": ref[0], "id": ref[2], "url": pl.canonical_url(u)} if ref else None)
    assert got == want


@node
def test_card_adds_the_whole_list_as_one_item_into_a_new_playlist():
    """Kill: 'whole' adding the songs, the list's tracking link stored, or the new saved
    playlist left empty / unnamed."""
    out = _card(
        f"console.log(JSON.stringify(cuboaiAddPlaylist('whole', {json.dumps(LIST)}, '', [], [], '__new__', 'Nir', 42)))"
    )
    assert out["songs"] == [
        {
            "category": "Spotify",
            "custom": True,
            "inPlaylist": False,
            "addedBy": "Nir",
            "name": "מזרחית שקטים",
            "url": LIST["url"],
            "isPlaylist": True,
        }
    ]
    assert out["playlists"] == [{"id": "42", "name": "מזרחית שקטים", "songs": [LIST["url"]], "addedBy": "Nir"}]


@node
def test_card_adds_the_songs_once_each_and_into_an_existing_playlist():
    """Kill: songs already in the library added twice, the target playlist ignored, or the
    caller's arrays changed in place (the card saves only on success)."""
    out = _card(
        "const lib = [{name: 'old', url: 'u1', category: 'Spotify'}];"
        "const pls = [{id: 'p1', name: 'Mine', songs: ['x']}];"
        f"const r = cuboaiAddPlaylist('songs', {json.dumps(LIST)}, '', lib, pls, 'p1', 'Nir');"
        "console.log(JSON.stringify({r, lib, pls}))"
    )
    r = out["r"]
    assert [s["url"] for s in r["songs"]] == ["u1", "u2"] and r["added"] == 1
    assert r["playlists"][0]["songs"] == ["x", "u1", "u2"] and r["inPlaylist"] == 2
    assert out["lib"] == [{"name": "old", "url": "u1", "category": "Spotify"}]
    assert out["pls"][0]["songs"] == ["x"]


@node
def test_card_library_only_touches_no_playlist():
    """Kill: a saved playlist changed when 'Library only' was chosen."""
    out = _card(
        "const pls = [{id: 'p1', name: 'Mine', songs: []}];"
        f"console.log(JSON.stringify(cuboaiAddPlaylist('songs', {json.dumps(LIST)}, '', [], pls, '', 'Nir')))"
    )
    assert out["inPlaylist"] == 0 and out["playlists"][0]["songs"] == []
