#!/usr/bin/env python3
"""cuboai_stream_playback.py — play RECORDED footage off the camera's own DVR.

The live counterpart of this script is cuboai_stream_video.py. Both are go2rtc
`exec:` producers that write MPEG-TS to stdout; the only difference is where
the frames come from — the camera's live encoder, or its on-board recording.
Nothing here touches the live path.

    go2rtc.yaml:
        - exec:python3 /path/to/cuboai_stream_playback.py#{killsignal=SIGTERM}

Environment (the first four match the live script exactly):
    CUBOAI_UID          Device UID
    CUBOAI_ACCOUNT      dev_admin_id
    CUBOAI_PASSWORD     dev_admin_pwd
    CUBOAI_CAMERA_IP    LAN IP of the camera (optional, recommended)
    CUBOAI_LIB          Path to libIOTCAPIs_ALL.so (optional, auto-detected)

    CUBOAI_PLAY_FROM    Epoch seconds (UTC) to start playing from. Required.
    CUBOAI_PLAY_SECONDS Seconds of FOOTAGE to play, by recorded timestamp
                        (default 300). There is no fast-forward or seek in the
                        protocol, so this is a span of recorded time, not wall
                        time.
    CUBOAI_PLAY_SNAP    1 (default) to snap the start to the nearest minute
                        that actually has footage; 0 to fail if the exact
                        requested minute is missing.

Exit codes: 0 normal end-of-footage, 2 nothing recorded near that time,
3 could not start playback on the camera.
"""
from __future__ import annotations

import datetime as _dt
import os
import signal
import sys
import threading
import time as _time

# Playback needs a newer TUTK engine than the live path currently vendors: with
# the older one the camera opens the playback channel but never delivers a
# frame (verified by swapping only cuboai_pure.py — 293 AUs became 0). Rather
# than upgrade the engine underneath live streaming, this script runs against
# its own copy in playback_engine/. It is a separate process, so the two never
# share an interpreter and the live path is untouched.
_ENGINE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "playback_engine")
if os.path.isdir(_ENGINE):
    sys.path.insert(0, _ENGINE)

# The camera speaks one playback session at a time and hands the channel back
# on close; leaving it open would block the next request (and the live view),
# so every exit path below goes through a close.
_stop = threading.Event()

#: Footage newer than this is still being written and will not play.
_RECENCY_FLOOR_S = 60


def _log(*parts) -> None:
    """Diagnostics go to stderr — stdout is the TS stream and must stay clean.

    go2rtc surfaces these in go2rtc.log at exec:debug, the same as the live
    producer's own logging.
    """
    print("[playback]", *parts, file=sys.stderr, flush=True)


def _install_signal_handlers() -> None:
    """go2rtc sends SIGTERM when the stream stops; end the session cleanly.

    Without this the camera keeps the playback channel assigned and the next
    request — or the live stream — is refused until it times out on its own.
    """

    def _handle(signum, _frame):
        _log(f"signal {signum} — stopping playback")
        _stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)


def main() -> int:
    _install_signal_handlers()

    uid = os.environ.get("CUBOAI_UID", "")
    account = os.environ.get("CUBOAI_ACCOUNT", "")
    password = os.environ.get("CUBOAI_PASSWORD", "")
    camera_ip = os.environ.get("CUBOAI_CAMERA_IP") or None
    if not (uid and account and password):
        _log("missing CUBOAI_UID / CUBOAI_ACCOUNT / CUBOAI_PASSWORD")
        return 2

    # The moment to play comes either straight from the environment (running
    # this script by hand) or from a small state file. go2rtc will not accept
    # an `exec:` source over its API — quite rightly, it would be a remote
    # execution hole — so the stream is declared once in go2rtc.yaml and the
    # requested time is handed over through this file instead.
    start_epoch = None
    play_seconds = int(float(os.environ.get("CUBOAI_PLAY_SECONDS", "300") or 300))
    raw_from = os.environ.get("CUBOAI_PLAY_FROM")
    if raw_from:
        try:
            start_epoch = int(float(raw_from))
        except (TypeError, ValueError):
            _log("CUBOAI_PLAY_FROM must be epoch seconds (UTC)")
            return 2
    else:
        state_path = os.environ.get("CUBOAI_PLAY_STATE")
        if not state_path or not os.path.isfile(state_path):
            _log("no playback requested (no CUBOAI_PLAY_FROM and no state file)")
            return 2
        try:
            import json

            with open(state_path, encoding="utf-8") as handle:
                state = json.load(handle)
            start_epoch = int(state["start_epoch"])
            play_seconds = int(state.get("seconds", play_seconds))
        except (OSError, ValueError, KeyError, TypeError) as err:
            _log(f"could not read the playback request: {err}")
            return 2

        # A NEW seek while this producer is still running must not be served
        # by the OLD session: go2rtc reuses a live producer for new consumers,
        # so the service writing a new target into the state file changed
        # nothing — the viewer got the previous request's footage under the
        # new label (observed live: footage of "now" labeled with a two-day-old
        # timestamp). Watch the state file; when it changes, stop cleanly —
        # go2rtc respawns this producer on demand and the fresh process reads
        # the fresh target.
        try:
            _state_mtime = os.path.getmtime(state_path)

            def _watch_state():
                while not _stop.is_set():
                    _time.sleep(0.5)
                    try:
                        if os.path.getmtime(state_path) != _state_mtime:
                            _log("seek target changed — exiting so go2rtc respawns for the new request")
                            _stop.set()
                            return
                    except OSError:
                        pass  # transient rewrite window

            threading.Thread(target=_watch_state, daemon=True).start()
        except OSError:
            pass

    # The engine reads its A/V gates at construction, so the same production
    # profile the live producer installs has to be in the environment BEFORE
    # get_session() — without it the playback channel opens but assembles no
    # access units, and the stream comes out empty.
    from cuboai_stream_video import apply_env_profile

    apply_env_profile(False)

    import cuboai_playback as pb
    from cuboai_session import get_session

    # Cheap gates only. Deliberately NOT a coverage/manifest pull first: the
    # 0x910 + RDT manifest fetch is flaky, and its timeout path disconnects the
    # session socket — which leaves playback opening a channel that then
    # delivers no frames. Playback does not need the manifest; 0x31a serves
    # footage right up to ~a minute behind live, including the current hour
    # whose manifest the camera won't serve at all.
    now = int(_dt.datetime.now(_dt.UTC).timestamp())
    if start_epoch > now - _RECENCY_FLOOR_S:
        _log(f"too recent — the last ~{_RECENCY_FLOOR_S}s is still being written")
        return 2
    if now - start_epoch > pb.RETENTION_72H_S:
        _log("beyond the camera's ~72h retention window")
        return 2

    # defer_stream_start=False matches the live producer: the engine brings its
    # reader up immediately instead of waiting out a ~5s native defer, which
    # would otherwise elapse before the first recorded frame is expected.
    with get_session(uid, account, password, camera_ip=camera_ip,
                     auto_discover_lib=True,
                     defer_stream_start=False, defer_video_start_late=False) as sess:
        transport = sess
        inner = getattr(transport, "_inner", transport)

        began = _dt.datetime.fromtimestamp(start_epoch, _dt.UTC)
        pbsess = pb.PlaybackSession(transport, log=_log)
        try:
            # Revive the socket if anything upstream dropped it, or start()
            # sends on a None socket.
            if getattr(inner, "_sock", None) is None:
                _log("session socket dropped — reconnecting")
                transport.connect()

            # 0x31a can refuse (-1) when the moment is still being finalised;
            # start() already retries the same target, so fall back once to a
            # slightly older one, where footage has always settled.
            channel = None
            for back in (0, 120):
                if _stop.is_set():
                    return 0
                try:
                    channel = pbsess.start(start_epoch - back)
                    if back:
                        _log(f"requested moment refused; served {back}s earlier instead")
                        start_epoch -= back
                        began = _dt.datetime.fromtimestamp(start_epoch, _dt.UTC)
                    break
                except RuntimeError as err:
                    if "rejected" in str(err).lower() and back < 120:
                        _time.sleep(0.6)
                        continue
                    _log(f"camera refused to start playback: {err}")
                    return 3
            if channel is None:
                _log("camera refused to start playback")
                return 3
            _log(f"playing from {began:%Y-%m-%d %H:%M UTC} on channel {channel} "
                 f"for {play_seconds}s of footage")
            stats = pb.mux_playback_stream(
                pbsess,
                sys.stdout.buffer,
                record_seconds=play_seconds,
                # Wall-clock safety cap: recorded playback is real-time paced,
                # so allow generous headroom but never hang forever.
                duration=play_seconds * 3 + 25,
                # Recorded frames can take longer than the 3s default to begin
                # arriving after the channel opens; end on real EOS, not a slow start.
                idle_timeout=12.0,
                stop_flag=_stop,
                log=_log,
            )
            _log("finished:", stats)
        finally:
            # Always hand the channel back and restore live, including on
            # SIGTERM — playback stops the live encoder, so skipping this
            # leaves the camera unwatchable until it times out on its own.
            try:
                pbsess.close(restore_live=True)
            except Exception as err:  # noqa: BLE001 - shutting down regardless
                _log(f"close failed: {err}")
    return 0



if __name__ == "__main__":
    sys.exit(main())
