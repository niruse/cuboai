"""Pressing play must never pip-install anything, and must say what went wrong.

The old failure path ran `pip install --upgrade yt-dlp` on EVERY extraction
failure and then told the user to restart Home Assistant. Three things were wrong
with that:

* it mutated the Home Assistant environment as a side effect of pressing play,
* it ran a network install synchronously inside a worker thread, and
* the advice was almost always wrong. The common causes are a private, removed or
  region-locked video, or a stale player client — none of which an upgrade fixes.
  The real yt-dlp message was buried in a warning while the user chased a restart.

yt-dlp is a manifest requirement, so Home Assistant installs it before the
integration loads; the "bootstrap" install for a missing yt-dlp was unreachable
in a healthy install and equally wrong as a repair.
"""

import ast
from pathlib import Path

import pytest

SRC_DIR = Path("custom_components/cuboai")
MEDIA_PLAYER = SRC_DIR / "media_player.py"


def test_no_module_in_the_integration_pip_installs_at_runtime():
    """A play/poll must never mutate the Python environment."""
    offenders = []
    for path in SRC_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # subprocess.check_call([...]) / subprocess.run([...]) carrying "pip"
            flat = ast.dump(node)
            if ("check_call" in flat or "'run'" in flat) and "'pip'" in flat and "'install'" in flat:
                offenders.append(f"{path}:{node.lineno}")
    assert not offenders, f"runtime pip install(s) reintroduced: {offenders}"


def test_the_failure_path_does_not_tell_the_user_to_restart():
    """The restart advice was the misleading half of the old behaviour."""
    src = MEDIA_PLAYER.read_text(encoding="utf-8")
    extract = src[src.index("def _extract_yt_url") : src.index("media_id = await self.hass.async_add_executor_job")]
    assert "RESTART Home Assistant" not in extract
    assert "attempting automatic upgrade" not in extract


def _handler_source():
    """The except block that handles a failed extraction."""
    src = MEDIA_PLAYER.read_text(encoding="utf-8")
    start = src.index("except Exception as e:", src.index("def _extract_yt_url"))
    return src[start : src.index("media_id = await self.hass.async_add_executor_job", start)]


def test_the_real_yt_dlp_message_reaches_the_log():
    """Whatever yt-dlp said must be surfaced, not swallowed."""
    handler = _handler_source()
    assert "reason" in handler
    assert "str(e)" in handler, "the underlying error text must be logged"
    assert "media_id" in handler, "the log must name what failed to play"


@pytest.mark.parametrize(
    ("message", "expect_content_error"),
    [
        # "this item cannot be fetched" — a warning, not a fault to chase
        ("ERROR: [youtube] Fb9OLvw_0ts: This video is unavailable", True),
        ("ERROR: [youtube] abc: Private video. Sign in if you've been granted access", True),
        ("ERROR: [youtube] abc: Video unavailable. This video has been removed by the uploader", True),
        ("ERROR: [youtube] abc: This video is not available in your country", True),
        ("ERROR: [youtube:tab] PLxxx: YouTube said: The playlist does not exist", True),
        ("ERROR: [youtube] abc: Sign in to confirm your age", True),
        ("ERROR: [youtube] abc: Join this channel to get access to members-only content", True),
        # genuine breakage — worth an error and the "yt-dlp may need updating" hint
        ("ERROR: unable to download video data: HTTP Error 500", False),
        ("ERROR: Unable to extract yt initial data", False),
        ("ERROR: Unable to download API page: HTTP Error 403: Forbidden", False),
    ],
)
def test_content_errors_are_classified_apart_from_real_faults(message, expect_content_error):
    """A private or region-locked video is not a fault to chase; an extractor
    breakage is. Calls the SHIPPED classifier, so it cannot drift from the code."""
    from custom_components.cuboai.media_player import _is_yt_content_error

    assert _is_yt_content_error(message) is expect_content_error


def test_the_classifier_is_case_insensitive():
    from custom_components.cuboai.media_player import _is_yt_content_error

    assert _is_yt_content_error("THIS VIDEO IS UNAVAILABLE") is True
    assert _is_yt_content_error("") is False
