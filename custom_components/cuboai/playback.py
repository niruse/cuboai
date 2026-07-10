"""Pure playback-timing helpers for the CuboAI media player.

Kept free of Home Assistant imports so the timing rules can be unit-tested
directly (see tests/test_playback.py). The media_player entity wires these
into the live queue loop.
"""


def playtime_expired(session_start: float, now: float, timer_min: int) -> bool:
    """True when the Speaker Play Time budget is exhausted.

    Play Time is a TOTAL session budget measured from when playback started
    (``session_start``). ``timer_min == 0`` means 'Infinite' and never expires.

    Args:
        session_start: monotonic time (seconds) when playback began.
        now: current monotonic time (seconds).
        timer_min: the Play Time value in minutes (0 = infinite).
    """
    return timer_min > 0 and (now - session_start) >= timer_min * 60


def should_loop_playback(timer_min: int, has_tracks: bool, expired: bool) -> bool:
    """Whether to keep playing after the queue empties.

    Play Time is a soother-style 'play FOR this long' timer: while a Play Time
    is set (timer_min > 0), the played tracks loop until the budget is spent,
    rather than stopping after one pass. With no Play Time (Infinite), playback
    stops when the queue empties (unless the user's Repeat setting re-queues).
    """
    return timer_min > 0 and has_tracks and not expired
