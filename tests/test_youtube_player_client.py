"""YouTube extraction must not depend on a JavaScript runtime.

The web player client hands back stream URLs whose signature has to be deciphered
by running YouTube's own JavaScript. yt-dlp can only do that with a JS runtime
(deno) installed, and Home Assistant images ship none — so for a growing share of
videos the web client is refused outright and yt-dlp reports "This video is
unavailable" for a video that plays fine in any browser. A playlist fails the
same way on its first entry, taking the whole list down.

Reproduced on a live install (yt-dlp 2026.08.19), and this is why the fix is
pinned rather than left to a comment:

    default client   -> ERROR: [youtube] Fb9OLvw_0ts: This video is unavailable
    android client   -> OK, stream URL returned
    playlist/default -> ERROR: [youtube] Co50b0T7kxE: This video is unavailable
    playlist/android -> OK, 3 entries

The android client returns pre-signed URLs and needs no JS runtime.
"""

import ast
from pathlib import Path

MEDIA_PLAYER = Path("custom_components/cuboai/media_player.py")


def _ydl_opts_literal():
    """The ydl_opts dict built in _extract_media_url, as real Python data.

    Parsed from the AST rather than string-matched so it survives reformatting
    and asserts on the VALUES that reach yt-dlp.
    """
    tree = ast.parse(MEDIA_PLAYER.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == "ydl_opts" and isinstance(node.value, ast.Dict):
            return ast.literal_eval(node.value)
    raise AssertionError("ydl_opts dict literal not found in media_player.py")


def test_extraction_asks_for_a_client_that_needs_no_js_runtime():
    opts = _ydl_opts_literal()
    clients = opts["extractor_args"]["youtube"]["player_client"]
    assert "android" in clients, "android is the client that works without a JS runtime"
    assert clients[0] == "android", (
        "android must be tried FIRST — putting the web client first reintroduces "
        "the 'This video is unavailable' failure on installs with no JS runtime"
    )


def test_a_fallback_client_is_kept():
    """android must not be the only option, or a video only the web client can
    serve becomes unplayable instead."""
    clients = _ydl_opts_literal()["extractor_args"]["youtube"]["player_client"]
    assert len(clients) > 1, f"no fallback client configured: {clients}"


def test_the_rest_of_the_extraction_options_are_unchanged():
    """Guards the surrounding behaviour this fix sits next to."""
    opts = _ydl_opts_literal()
    assert opts["format"] == "bestaudio/best"
    assert opts["noplaylist"] is True
    assert opts["quiet"] is True
