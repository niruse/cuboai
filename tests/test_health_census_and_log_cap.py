"""Two ways the diagnostics themselves failed, found through issue #105.

The reporter soaked a wedged Nest Hub cast for weeks against a **549 MB**
`go2rtc.log` that contained 21 `[health] verbose ON` banners and **zero**
`[health t=…]` census lines. Both halves of that sentence are bugs in us:

1. `_verbose_loop` ran its whole body bare. One exception — a renamed stats key,
   a None where a number belonged — killed the census thread for the life of the
   producer. The banner stayed as the last word on the subject and the only
   instrument that can explain a wedged stream was gone, leaving a log that reads
   as "nothing to report". A monitor that can die silently is worse than none.

2. The 2 MB cap on `go2rtc.log` was only ever enforced when go2rtc STARTED, so a
   healthy long-running instance appended forever. (This box was found holding an
   11 MB `go2rtc.log.1`, and the README claimed rotation at 2 MB.)

Every test names the mutation it kills.
"""

import importlib.util
import os
import threading
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.cuboai import go2rtc as go2rtc_module
from custom_components.cuboai.const import DOMAIN

_TUTK = os.path.join(os.path.dirname(__file__), "..", "custom_components", "cuboai", "tutk")


def _load(name, fname):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_TUTK, fname))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sv = _load("live_stream_video_health", "cuboai_stream_video.py")


# =============================================================================
# 1. The census thread must survive its own bugs
# =============================================================================

# A stats snapshot with every key the census line reads.
GOOD = {
    "ts_valid": 100,
    "ts_garbage": 0,
    "recovery_pct": 100.0,
    "gap_now": 0,
    "gap_max": 3,
    "gap_cap_jumps": 0,
    "ts_regress": 0,
}


class _Sess:
    """A session whose get_stats() can be made to misbehave on demand."""

    def __init__(self, fail_times=0, bad_payload=False):
        self.calls = 0
        self.fail_times = fail_times
        self.bad_payload = bad_payload
        self.connected = True

    def get_stats(self):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("stats exploded")
        if self.bad_payload:
            return {"ts_valid": 1}  # every other key missing -> KeyError downstream
        return dict(GOOD)


def _run_census(sess, ticks, interval=0.01, camera_stats=False):
    """Run the real _verbose_loop on a thread for roughly `ticks` intervals."""
    lines = []
    stop = threading.Event()
    orig = sv._stderr
    sv._stderr = lines.append
    try:
        t = threading.Thread(target=sv._verbose_loop, args=(sess, interval, camera_stats, stop), daemon=True)
        t.start()
        deadline = time.time() + interval * ticks + 2.0
        while time.time() < deadline and len(lines) < ticks:
            time.sleep(interval)
        stop.set()
        t.join(2.0)
        alive = t.is_alive()
    finally:
        sv._stderr = orig
    return lines, alive


def test_the_census_prints_when_all_is_well():
    """Baseline — without this the failure tests below could pass vacuously."""
    lines, _ = _run_census(_Sess(), ticks=3)
    assert len(lines) >= 3
    assert all(line.startswith("[health t=") for line in lines), lines[:3]


def test_a_broken_stats_payload_does_not_kill_the_census():
    """THE bug: one KeyError used to end the census for the whole session.

    Kill: narrow the try back to `sess.get_stats()` only."""
    sess = _Sess(bad_payload=True)
    lines, _ = _run_census(sess, ticks=3)

    assert sess.calls >= 3, f"the loop stopped calling get_stats after {sess.calls} tick(s)"
    assert any("FAILED" in line for line in lines), f"the failure was silent: {lines}"
    assert any("health" in line for line in lines), "a failure must be findable by the census grep"


def test_a_failing_census_reports_a_traceback_once_then_backs_off():
    """Loud enough to diagnose, quiet enough to leave on for weeks: a permanently
    broken census must not turn into a line every tick forever — that is how a
    549 MB log happens.

    Kill: the backoff removed (every tick logs), or the traceback dropped."""
    sess = _Sess(bad_payload=True)
    lines, _ = _run_census(sess, ticks=12)

    assert sum("Traceback" in line for line in lines) == 1, "want exactly one traceback, got more"
    reports = [line for line in lines if "census tick FAILED" in line]
    counts = [int(line.split("(")[1].split("x")[0]) for line in reports]
    assert counts == [1, 10, 100][: len(counts)], f"not the 1/10/100 backoff: {counts}"
    # The real assertion: reports grow far slower than failures.
    assert sess.calls > len(reports) * 5, f"{sess.calls} failures produced {len(reports)} reports"


def test_a_transient_failure_is_recovered_from_and_said_so():
    """A blip must cost one tick, not the session's visibility.

    Kill: `continue`-on-error restored, or the recovery note dropped."""
    sess = _Sess(fail_times=1)
    lines, _ = _run_census(sess, ticks=4)

    good = [line for line in lines if line.startswith("[health t=")]
    assert good, f"never recovered: {lines}"
    assert any("census recovered after 1 failed tick" in line for line in good), good


def test_the_census_thread_stays_alive_and_stops_when_asked():
    """It must exit on the stop event — a thread that ignores it would keep a
    reaped producer's thread running.

    Kill: the stop.wait() condition inverted or ignored."""
    _, alive = _run_census(_Sess(bad_payload=True), ticks=3)
    assert not alive, "census thread ignored the stop event"


# =============================================================================
# 2. The log cap must be enforced while go2rtc runs
# =============================================================================


def _manager(tmp_path, debug=True):
    hass = MagicMock()
    hass.data = {}

    async def _run(func, *args):
        return func(*args)

    hass.async_add_executor_job = AsyncMock(side_effect=_run)
    m = go2rtc_module.Go2RTCManager(hass, "entryA")
    m.update_streams([], {"enable_debug_logs": debug})
    m._config_path = str(tmp_path / "go2rtc.yaml")
    hass.data.setdefault(DOMAIN, {}).setdefault("entryA", {})["go2rtc"] = m
    return m


def _write_log(m, size):
    p = m._log_file_path()
    with open(p, "wb") as fh:
        fh.write(b"x" * size)
    return p


def test_an_oversized_log_is_rolled(tmp_path):
    """Kill: the size check removed, or the comparison inverted."""
    m = _manager(tmp_path)
    path = _write_log(m, go2rtc_module.LOG_MAX_BYTES + 5000)

    rolled = m._rotate_oversized_log()

    assert rolled == go2rtc_module.LOG_MAX_BYTES + 5000
    assert os.path.getsize(path) == 0, "the live log was not truncated"
    assert os.path.getsize(path + ".1") == go2rtc_module.LOG_MAX_BYTES + 5000, "history was not kept"


def test_a_small_log_is_left_alone(tmp_path):
    """Rotating a 1 KB log every 15s would churn the disk and shred history.

    Kill: the cap comparison dropped."""
    m = _manager(tmp_path)
    path = _write_log(m, 1024)

    assert m._rotate_oversized_log() == 0
    assert os.path.getsize(path) == 1024
    assert not os.path.exists(path + ".1")


def test_rotation_truncates_in_place_rather_than_renaming(tmp_path):
    """go2rtc holds an inherited O_APPEND fd for its whole life. A rename leaves
    it writing into the renamed backup, so the rotation reclaims nothing — the
    exact reason a reporter's log reached 549 MB. Truncating the inode it holds
    works, and O_APPEND puts the next write at byte 0.

    Kill: os.truncate swapped back to os.rename."""
    m = _manager(tmp_path)
    path = _write_log(m, go2rtc_module.LOG_MAX_BYTES + 10)

    # Exactly how the child holds it: same inode, append mode.
    with open(path, "ab") as child_fd:
        inode_before = os.fstat(child_fd.fileno()).st_ino
        m._rotate_oversized_log()
        child_fd.write(b"after rotation\n")
        child_fd.flush()
        assert os.fstat(child_fd.fileno()).st_ino == inode_before, "rotation swapped the inode"

    assert os.path.getsize(path) == len(b"after rotation\n"), (
        "the writer's bytes did not land at the start of the truncated file — rotation reclaimed nothing"
    )


def test_a_missing_log_is_not_an_error(tmp_path):
    """Debug logs off, or a first run. Kill: the OSError guard removed."""
    m = _manager(tmp_path)
    assert m._rotate_oversized_log() == 0


@pytest.mark.asyncio
async def test_rotation_is_skipped_when_debug_logs_are_off(tmp_path):
    """Nothing is being written, so rolling would only destroy the last capture.

    Kill: the enable_debug_logs check removed."""
    m = _manager(tmp_path, debug=False)
    path = _write_log(m, go2rtc_module.LOG_MAX_BYTES + 5000)

    await m._maybe_rotate_log()

    assert os.path.getsize(path) == go2rtc_module.LOG_MAX_BYTES + 5000
    assert not os.path.exists(path + ".1")


@pytest.mark.asyncio
async def test_a_rotation_failure_never_breaks_supervision(tmp_path):
    """Keeping the camera alive outranks housekeeping.

    Kill: the try/except around the executor call removed."""
    m = _manager(tmp_path)
    m.hass.async_add_executor_job = AsyncMock(side_effect=RuntimeError("disk on fire"))

    await m._maybe_rotate_log()  # must not raise


@pytest.mark.asyncio
async def test_the_watchdog_enforces_the_cap_while_go2rtc_runs(tmp_path):
    """The whole point: the cap used to apply only at startup, so a healthy
    instance grew without bound.

    Kill: the _maybe_rotate_log call removed from the watchdog loop."""
    import asyncio

    m = _manager(tmp_path)
    m.hass.async_create_task = lambda coro, *a, **kw: asyncio.get_running_loop().create_task(coro)
    path = _write_log(m, go2rtc_module.LOG_MAX_BYTES + 5000)

    proc = MagicMock()
    proc.pid = 1234
    proc.returncode = None  # alive the whole time
    m.process = proc
    m._started_at = time.monotonic()

    from unittest.mock import patch

    with patch.object(go2rtc_module, "WATCHDOG_INTERVAL", 0.01):
        m._start_watchdog()
        await asyncio.sleep(0.15)
        m._cancel_watchdog()

    assert os.path.getsize(path) == 0, "a running go2rtc's log was never capped"
    assert os.path.exists(path + ".1")
