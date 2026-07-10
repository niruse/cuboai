"""Tests for Speaker Play Time budget logic (regression guard).

Play Time is a TOTAL session budget measured from when playback started. A
past regression read the value only once at loop start, so setting Play Time
*after* pressing Play was silently ignored. The live queue loop now re-reads
the value and calls playtime_expired() on every poll — these tests lock the
pure decision in place.
"""

from custom_components.cuboai.playback import playtime_expired, should_loop_playback


class TestPlaytimeExpired:
    def test_infinite_never_expires(self):
        # timer_min == 0 means Infinite
        assert playtime_expired(0.0, 10**9, 0) is False

    def test_not_expired_just_before_deadline(self):
        start = 100.0
        assert playtime_expired(start, start + 30 * 60 - 1, 30) is False

    def test_expired_exactly_at_deadline(self):
        start = 100.0
        assert playtime_expired(start, start + 30 * 60, 30) is True

    def test_expired_after_deadline(self):
        start = 100.0
        assert playtime_expired(start, start + 45 * 60, 30) is True

    def test_short_budget(self):
        start = 0.0
        assert playtime_expired(start, 60, 1) is True
        assert playtime_expired(start, 59, 1) is False

    def test_negative_timer_treated_as_infinite(self):
        # Defensive: a bogus negative value must not expire immediately
        assert playtime_expired(0.0, 10**9, -5) is False

    def test_regression_set_after_play(self):
        """The exact regression: Play pressed at t=0 (Infinite), then the user
        sets 10 min at t=120. Because the value is re-read, it now expires at
        t=600 rather than being ignored for the whole session."""
        session_start = 0.0
        # while Infinite, never expires
        assert playtime_expired(session_start, 500, 0) is False
        # user sets 10 min mid-session -> expires at 600s from session start
        assert playtime_expired(session_start, 599, 10) is False
        assert playtime_expired(session_start, 600, 10) is True


class TestShouldLoopPlayback:
    """Play Time means 'play FOR this long' — loop content until the budget."""

    def test_loops_while_playtime_set_and_not_expired(self):
        # single song finished, 30-min Play Time still running -> keep playing
        assert should_loop_playback(30, has_tracks=True, expired=False) is True

    def test_no_loop_when_infinite(self):
        # no Play Time -> stop when the queue empties (Repeat handles looping)
        assert should_loop_playback(0, has_tracks=True, expired=False) is False

    def test_no_loop_when_expired(self):
        assert should_loop_playback(30, has_tracks=True, expired=True) is False

    def test_no_loop_when_no_tracks(self):
        # e.g. a lullaby-only session records no songs to loop
        assert should_loop_playback(30, has_tracks=False, expired=False) is False

    def test_single_song_soother_scenario(self):
        """A single 3-min song with Play Time 30 min: after the song ends
        (t=180) and the budget is not spent, it loops; at t>=1800 it stops."""
        # not expired at 180s -> loop the song again
        assert playtime_expired(0.0, 180, 30) is False
        assert should_loop_playback(30, True, playtime_expired(0.0, 180, 30)) is True
        # expired at 1800s -> stop
        assert playtime_expired(0.0, 1800, 30) is True
        assert should_loop_playback(30, True, playtime_expired(0.0, 1800, 30)) is False
