#!/usr/bin/env python3
"""
cuboai_stream_audio.py — Raw AAC audio stream for go2rtc.

Connects to a CuboAI camera and writes a continuous stream of raw
AAC-ADTS audio frames to stdout. Designed to be used as an 'exec'
source in go2rtc alongside cuboai_stream_video.py.

STATUS: Untested with go2rtc — audio format confirmed working (verified
by recording and playing back .aac files), go2rtc integration not yet
validated.

Usage in go2rtc config (go2rtc.yaml):
    streams:
      cuboai:
        - exec:python3 /path/to/cuboai_stream_video.py#{killsignal=SIGTERM}
        - exec:python3 /path/to/cuboai_stream_audio.py#{killsignal=SIGTERM}

    go2rtc combines the two exec sources into a single stream with both
    video and audio tracks.

Environment variables (required):
    CUBOAI_UID        Device UID (license_id from the REST API)
    CUBOAI_ACCOUNT    dev_admin_id  (e.g. admin@YOUR_DEVICE_HEX)
    CUBOAI_PASSWORD   dev_admin_pwd
    CUBOAI_CAMERA_IP  LAN IP of camera (optional but recommended for LAN)
    CUBOAI_LIB        Path to libIOTCAPIs_ALL.so (optional, auto-detected)

Audio format:
    AAC-LC in ADTS container.
    Confirmed via frame header analysis:
      - Sync word:   0xFFF1 (MPEG-4 AAC, no CRC)
      - Profile:     AAC-LC (profile=2)
      - Sample rate: 16000 Hz
      - Channels:    1 (mono)
      - Frame size:  448 bytes (64ms per frame at 1024 samples/frame)
      - Frame rate:  ~50 frames/second

    Each frame is self-contained with an ADTS header — go2rtc can parse
    the sample rate and channel count directly from the header bytes.

Important note about TUTK audio architecture:
    The TUTK library requires that video frames (avRecvFrameData2) are
    consumed concurrently with audio frames (avRecvAudioData). If only
    audio is requested, the camera stops sending both streams.

    This script handles this internally: a background thread drains video
    frames while the main thread reads and outputs audio. The video frames
    are discarded — for combined AV streaming, the video script is more
    efficient.

    This script makes a SEPARATE camera connection from cuboai_stream_video.py.
    Both scripts together = 2 connections to the camera simultaneously.
    The camera supports multiple concurrent clients, so this is fine.

See also:
    cuboai_stream_video.py — companion video stream
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

    # ── Stream audio frames to stdout ────────────────────────────────────
    # Each yielded frame is a complete ADTS packet starting with 0xFFF1.
    # go2rtc detects AAC-ADTS from the sync word automatically.
    # The audio_frames() generator internally drains video in a background
    # thread — this is required by the TUTK protocol.
    try:
        stdout = sys.stdout.buffer
        for frame in sess.audio_frames():
            stdout.write(frame)
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
