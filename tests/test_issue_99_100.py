"""Tests for issues #99 (timestamp badge) and #100 (card section toggles).

Source-pin tests over cuboai-card.js (pattern from test_dvr_in_card.py): the
card is plain JS with no test runner, so pin the load-bearing markers the
implementation must keep.

The behaviors:
- #100 bug half: the mat/env badges used to render literal "?? BPM"/"??°C"
  when their sensors were unavailable/absent (mat & thermometer are optional
  accessories) — they must now auto-hide instead.
- #100 feature half: `show_mat_overlay` / `show_env_overlay` / `show_music`
  config keys, all defaulting ON (`!== false` guards, so absent keys change
  nothing for existing users).
- #99: `show_timestamp` (opt-in, `=== true`) drives a badge from FRAME
  PROGRESS — it freezes and turns red when frames stop, which a wall clock
  cannot show.
"""

from pathlib import Path

CARD = (Path(__file__).parent.parent / "custom_components" / "cuboai" / "www" / "cuboai-card.js").read_text(
    encoding="utf-8"
)


class TestOverlayAutoHide:
    def test_the_question_marks_are_gone(self):
        """No render path may produce a '??' badge again (the old code seeded
        each badge with a '??' placeholder that rendered when the sensor was
        absent; a comment may still mention it, code may not)."""
        assert 'bpmText = "??"' not in CARD
        assert 'tempText = "??"' not in CARD
        assert 'humiText = "??"' not in CARD
        assert "${bpmText} BPM" in CARD  # the badge itself still renders real data

    def test_badges_hide_when_state_is_dead(self):
        """The shared liveness gate exists and both badges honor a hide branch."""
        assert "sens.state !== 'unknown' && sens.state !== 'unavailable'" in CARD
        assert CARD.count("style.display = 'none'") >= 2

    def test_overlay_toggles_default_on(self):
        """Absent keys must behave exactly like before (default ON): the guard
        is `!== false`, never a truthiness check that would flip the default."""
        assert "this._config.show_mat_overlay !== false" in CARD
        assert "this._config.show_env_overlay !== false" in CARD


class TestMusicToggle:
    def test_music_section_gates_on_show_music(self):
        assert "this._config.show_music === false" in CARD
        # both halves: the element hides AND the status updater bails
        assert CARD.count("show_music === false") >= 2


class TestTimestampBadge:
    def test_opt_in_only(self):
        """#99 asked for opt-in; the badge must require an explicit true."""
        assert "this._config.show_timestamp === true" in CARD

    def test_driven_by_frame_progress_not_wall_clock(self):
        """The whole point: currentTime advancement is what feeds the badge."""
        assert "v.currentTime > this._tsLastMediaTime" in CARD

    def test_stall_turns_the_badge_red_at_the_freeze_time(self):
        assert "STALL_MS" in CARD
        assert "fmt(new Date(this._tsLastAdvance))" in CARD

    def test_reconnect_reanchors_instead_of_staying_red(self):
        """Found live in the harness: a reconnect swaps in a fresh MediaStream
        whose currentTime restarts near 0 — below the remembered high-water
        mark — so without the re-anchor the badge stays red forever after a
        recovery."""
        assert "v.currentTime < this._tsLastMediaTime - 1" in CARD

    def test_interval_is_cleaned_up_and_rearmable(self):
        """disconnectedCallback clears the interval; the (re)arm guard is
        separate from element creation so a re-mounted card ticks again."""
        assert "if (this._tsClock) { clearInterval(this._tsClock); this._tsClock = null; }" in CARD
        assert "if (!this._tsClock) {" in CARD

    def test_overlay_survives_player_rebuilds(self):
        """Same re-attach treatment as the bpm/env badges."""
        assert "this.tsOverlay && (!this.tsOverlay.isConnected" in CARD


class TestEditor:
    def test_all_four_toggles_are_clickable(self):
        for tid in ("show-env-toggle", "show-mat-toggle", "show-music-toggle", "show-timestamp-toggle"):
            assert f'id="{tid}"' in CARD
            assert f'target.id === "{tid}"' in CARD

    def test_default_on_keys_are_removed_not_written_true(self):
        """Checking a default-on box must DELETE the key (clean config), and
        the opt-in timestamp writes true only when checked."""
        assert "delete newConfig.show_env_overlay" in CARD
        assert "delete newConfig.show_mat_overlay" in CARD
        assert "delete newConfig.show_music" in CARD
        assert "newConfig.show_timestamp = true" in CARD


class TestVersions:
    def test_card_badge_matches_release(self):
        assert "const CARD_VERSION = 'v2.6.20';" in CARD
