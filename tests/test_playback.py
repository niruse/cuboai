"""Tests for Speaker Play Time budget logic (regression guard).

Play Time is a TOTAL session budget measured from when playback started. A
past regression read the value only once at loop start, so setting Play Time
*after* pressing Play was silently ignored. The live queue loop now re-reads
the value and calls playtime_expired() on every poll — these tests lock the
pure decision in place.
"""

from custom_components.cuboai.playback import playtime_expired


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
