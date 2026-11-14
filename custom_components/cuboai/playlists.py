"""YouTube and Spotify playlists (and Spotify albums): what is in them, without any token.

A playlist link can be used two ways (the card offers both):
- as ONE item: played, it expands into its songs right then (so it follows the list's changes);
- as its SONGS: each one added to the library (and optionally to a saved playlist) on its own.

YouTube: yt-dlp lists a playlist flat (titles and ids, no download).
Spotify: the public embed page (open.spotify.com/embed/...) carries the list as JSON, the same
data Spotify's own embed player shows, with no login. Spotify audio is DRM-protected, so each
track plays from its YouTube match (the speaker resolves a Spotify track link that way already).

No Home Assistant imports: these run in an executor thread and are tested on their own.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

# Enough for any real list, small enough that a huge "mix" can't flood the speaker queue.
MAX_TRACKS = 300

_SPOTIFY_LIST = re.compile(
    r"^https?://open\.spotify\.com/(?:intl-[a-z]{2}(?:-[a-z]{2})?/)?(?:embed/)?(playlist|album)/([A-Za-z0-9]{22})(?:[/?#]|$)"
)
_YT_LIST_ID = re.compile(r"^[A-Za-z0-9_-]{10,64}$")
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
_UA = {"User-Agent": "Mozilla/5.0", "Accept-Language": "en"}


def playlist_ref(url: str):
    """("spotify", "playlist"|"album", id) or ("youtube", "playlist", list_id), or None.

    A YouTube link counts only with a list= id. A watch?v=...&list=... link is a song played
    from a list: it is a list here (the card asks which one is meant), but play_media keeps it a
    single song (see is_playlist_link), as it always did."""
    url = str(url or "").strip()
    m = _SPOTIFY_LIST.match(url)
    if m:
        return ("spotify", m.group(1), m.group(2))
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https"):
        return None
    if host not in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"):
        return None
    list_id = (urllib.parse.parse_qs(parts.query).get("list") or [""])[0]
    if not _YT_LIST_ID.match(list_id):
        return None
    return ("youtube", "playlist", list_id)


def is_playlist_link(url: str) -> bool:
    """A link that means the whole list (play_media expands it): any Spotify playlist/album, or a
    YouTube /playlist link. A watch?v= link with a list= is still the one song."""
    ref = playlist_ref(url)
    if not ref:
        return False
    if ref[0] == "spotify":
        return True
    return urllib.parse.urlsplit(str(url).strip()).path.rstrip("/") == "/playlist"


def canonical_url(url: str) -> str:
    """The list's own link, without tracking parameters (si=, pi=, utm_*) or the song in it."""
    ref = playlist_ref(url)
    if not ref:
        return url
    if ref[0] == "spotify":
        return f"https://open.spotify.com/{ref[1]}/{ref[2]}"
    return f"https://www.youtube.com/playlist?list={ref[2]}"


def _get(url: str, timeout: float = 15) -> str:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed https hosts)
        return resp.read().decode("utf-8", "replace")


def parse_spotify_embed(html: str) -> dict:
    """{"name", "tracks": [{"name", "url", "seconds"}]} from a Spotify embed page."""
    m = _NEXT_DATA.search(html or "")
    if not m:
        raise ValueError("Spotify did not return the list (no embed data)")
    data = json.loads(m.group(1))
    entity = (((data.get("props") or {}).get("pageProps") or {}).get("state") or {}).get("data", {}).get("entity") or {}
    tracks = []
    for t in entity.get("trackList") or []:
        uri = str(t.get("uri") or "")
        if not uri.startswith("spotify:track:") or t.get("isPlayable") is False:
            continue  # podcasts episodes, local files, removed songs
        title = str(t.get("title") or "").strip()
        artist = str(t.get("subtitle") or "").strip()
        if not title:
            continue
        tracks.append(
            {
                "name": f"{artist} – {title}" if artist else title,
                "url": "https://open.spotify.com/track/" + uri.rsplit(":", 1)[1],
                "seconds": round((t.get("duration") or 0) / 1000) or None,
            }
        )
        if len(tracks) >= MAX_TRACKS:
            break
    return {"name": str(entity.get("name") or entity.get("title") or "").strip(), "tracks": tracks}


def _youtube_list(list_id: str) -> dict:
    import yt_dlp

    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "playlistend": MAX_TRACKS,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/playlist?list={list_id}", download=False)
    return parse_youtube_flat(info)


def parse_youtube_flat(info: dict) -> dict:
    """{"name", "tracks"} from yt-dlp's flat playlist info."""
    tracks = []
    for e in (info or {}).get("entries") or []:
        if not e:
            continue
        vid = str(e.get("id") or "")
        title = str(e.get("title") or "").strip()
        # yt-dlp lists removed and private videos with these placeholder titles.
        if not re.fullmatch(r"[0-9A-Za-z_-]{11}", vid) or title in ("[Deleted video]", "[Private video]"):
            continue
        tracks.append(
            {
                "name": title or vid,
                "url": f"https://www.youtube.com/watch?v={vid}",
                "seconds": int(e["duration"]) if e.get("duration") else None,
            }
        )
        if len(tracks) >= MAX_TRACKS:
            break
    return {"name": str((info or {}).get("title") or "").strip(), "tracks": tracks}


def fetch_playlist(url: str) -> dict:
    """{"name", "url", "source", "tracks"} for a playlist/album link. Blocking: run in an executor.

    Raises ValueError for a link that is not a list, or a list with no playable songs."""
    ref = playlist_ref(url)
    if not ref:
        raise ValueError("Not a YouTube or Spotify playlist link")
    if ref[0] == "spotify":
        result = parse_spotify_embed(_get(f"https://open.spotify.com/embed/{ref[1]}/{ref[2]}"))
    else:
        result = _youtube_list(ref[2])
    if not result["tracks"]:
        raise ValueError("The list has no songs that can be played")
    result["url"] = canonical_url(url)
    result["source"] = ref[0]
    return result
