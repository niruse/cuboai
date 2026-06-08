#!/usr/bin/env python3
"""
cuboai_stream_video.py — Raw HEVC video stream for go2rtc.

Connects to a CuboAI camera and writes a continuous stream of raw HEVC
(H.265) frames to stdout. Designed to be used as an 'exec' source in
go2rtc.

STATUS: Untested with go2rtc — stream format confirmed working, go2rtc
integration not yet validated.

Usage in go2rtc config (go2rtc.yaml):
    streams:
      cuboai_video:
        - exec:python3 /path/to/cuboai_stream_video.py#{killsignal=SIGTERM}

Environment variables (required):
    CUBOAI_UID        Device UID (license_id from the REST API)
    CUBOAI_ACCOUNT    dev_admin_id  (e.g. admin@YOUR_DEVICE_HEX)
    CUBOAI_PASSWORD   dev_admin_pwd
    CUBOAI_CAMERA_IP  LAN IP of camera (optional but recommended for LAN)
    CUBOAI_LIB        Path to libIOTCAPIs_ALL.so (optional, auto-detected)

Why exec source?
    go2rtc's 'exec' source runs a subprocess and reads its stdout as a
    media stream. This avoids the need for a full RTSP server and lets
    go2rtc handle all the WebRTC/HLS/RTSP re-streaming to Home Assistant.

Video format:
    Raw HEVC (H.265) Annex B bytestream, no container.
    Each frame starts with 00 00 00 01 (start code).
    Keyframes start with 00 00 00 01 40 (VPS NAL unit, type 32).
    Camera streams at approximately 3fps keyframes with P-frames between.
    Resolution: depends on camera setting (typically 1080p or 360p).

Known limitations:
    - Resolution cannot be changed via IOCTL (camera ignores it).
    - Frame rate is fixed by camera firmware.
    - No audio — use cuboai_stream_audio.py for audio.

See also:
    cuboai_stream_audio.py — companion audio stream
    LIBRARY_SETUP.md       — how to obtain the required native library
"""

import os
import sys

# ── Locate our modules ────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cuboai_tutk import TUTKSession


def main() -> None:
    # ── Configuration from environment ───────────────────────────────────
    uid       = os.environ.get('CUBOAI_UID')
    account   = os.environ.get('CUBOAI_ACCOUNT')
    password  = os.environ.get('CUBOAI_PASSWORD')
    camera_ip = os.environ.get('CUBOAI_CAMERA_IP')
    lib_path  = os.environ.get('CUBOAI_LIB')

    if not all([uid, account, password]):
        print(
            "Error: CUBOAI_UID, CUBOAI_ACCOUNT, CUBOAI_PASSWORD must be set.",
            file=sys.stderr
        )
        sys.exit(1)

    # ── Connect ───────────────────────────────────────────────────────────
    sess = TUTKSession(
        uid=uid,
        account=account,
        password=password,
        lib_path=lib_path,
        camera_ip=camera_ip,
    )

    try:
        sess.connect()
    except Exception as e:
        print(f"Connection failed: {e}", file=sys.stderr)
        sys.exit(1)

    # ── Stream video frames to stdout ────────────────────────────────────
    # go2rtc reads from stdout and handles the re-streaming.
    # We write raw HEVC Annex B frames — go2rtc detects the format from
    # the 00 00 00 01 start codes.
    try:
        stdout = sys.stdout.buffer
        for frame_type, data in sess.av_frames():
            if frame_type == 'video':
                stdout.write(data)
                stdout.flush()
    except (BrokenPipeError, KeyboardInterrupt):
        # go2rtc closed the pipe (stream stopped) — clean exit
        pass
    except Exception as e:
        print(f"Stream error: {e}", file=sys.stderr)
    finally:
        sess.disconnect()


if __name__ == '__main__':
    main()
