#!/usr/bin/env python3
"""
cuboai_stream_backchannel.py — Two-way audio backchannel for CuboAI cameras.

Two modes:
  1. File/URL mode (TTS): called with a URL or file path as sys.argv[1].
     Downloads the file and sends it to the camera speaker in one shot.

  2. Live stdin mode (WebRTC mic): called with no arguments by go2rtc.
     go2rtc writes PCMA (G.711 A-law, 8kHz, mono) to our stdin continuously.
     We read it in small chunks (~1s), save each chunk to a temp file, and
     send it to the camera speaker. This repeats until stdin closes (user
     releases the mic button / go2rtc kills us with SIGTERM).
"""
import os
import sys
import time
import signal
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cuboai_session import get_session


def _send_file(sess, audio_path, audio_format=None, audio_options=None):
    """Send a single audio file/chunk to the camera speaker."""
    sess.send_audio_file(audio_path, format=audio_format, options=audio_options)


def _handle_file_or_url(media_id, uid, account, password, camera_ip):
    """TTS / file mode: download if needed, send once, exit."""
    import urllib.request

    audio_path = media_id
    audio_format = None
    audio_options = None

    if media_id.startswith(("http://", "https://")):
        if "googlevideo.com" in media_id:
            # YouTube streams: pass directly to PyAV for live streaming with a valid User-Agent
            print("DEBUG: YouTube stream detected. Streaming directly via PyAV...", file=sys.stderr)
            audio_path = media_id
            audio_options = {'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36'}
        else:
            # TTS or small files: download to a temp file
            print("DEBUG: Downloading HTTP URL...", file=sys.stderr)
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
            temp_file.close()
            import urllib.request
            req = urllib.request.Request(media_id, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req) as response, open(temp_file.name, 'wb') as out_file:
                out_file.write(response.read())
            audio_path = temp_file.name
            print(f"DEBUG: Downloaded to {audio_path}", file=sys.stderr)

    with get_session(uid, account, password,
                     camera_ip=camera_ip if camera_ip else None,
                     defer_stream_start=True, defer_video_start_late=True,
                     auto_discover_lib=False) as sess:

        if hasattr(sess, '_inner') and hasattr(sess._inner, '_send_video_start_mid'):
            print("DEBUG: Sending AUDIOSTART IOCTL to wake camera...", file=sys.stderr)
            sess._inner._send_video_start_mid()
            time.sleep(0.5)

        _send_file(sess, audio_path, audio_format, audio_options)

    # Only remove files we downloaded ourselves (never a caller-supplied local path)
    if audio_path != media_id and os.path.exists(audio_path):
        os.remove(audio_path)


def _handle_live_stdin(uid, account, password, camera_ip):
    """Live WebRTC mic mode: read PCMA from stdin in chunks and send each to the camera.

    go2rtc writes G.711 A-law (8kHz mono, 1 byte/sample) to our stdin.
    We accumulate ~1 second of audio (8000 bytes), write it to a temp file,
    and call send_audio_file() with format='alaw' so PyAV can decode it.
    Then we repeat until stdin is closed or we receive SIGTERM.
    """
    CHUNK_BYTES = 8000  # 1 second of 8kHz mono alaw (1 byte per sample)

    print("DEBUG: Live stdin mode — reading PCMA from go2rtc...", file=sys.stderr, flush=True)

    # Open a persistent session so we don't reconnect for every chunk
    with get_session(uid, account, password,
                     camera_ip=camera_ip if camera_ip else None,
                     defer_stream_start=True, defer_video_start_late=True,
                     auto_discover_lib=False) as sess:

        if hasattr(sess, '_inner') and hasattr(sess._inner, '_send_video_start_mid'):
            print("DEBUG: Sending AUDIOSTART IOCTL to wake camera...", file=sys.stderr, flush=True)
            sess._inner._send_video_start_mid()
            time.sleep(0.5)

        stdin_fd = sys.stdin.buffer
        chunk_count = 0

        while True:
            # Read a chunk of PCMA audio from stdin
            data = stdin_fd.read(CHUNK_BYTES)
            if not data:
                print("DEBUG: stdin closed, exiting live mode.", file=sys.stderr, flush=True)
                break

            # Write chunk to a temp file so PyAV can open it
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".alaw")
            tmp.write(data)
            tmp.close()

            try:
                chunk_count += 1
                print(f"DEBUG: Sending chunk #{chunk_count} ({len(data)} bytes)...",
                      file=sys.stderr, flush=True)
                _send_file(sess, tmp.name,
                           audio_format="alaw",
                           audio_options={"sample_rate": "8000", "channels": "1"})
            except Exception as e:
                print(f"DEBUG: Chunk #{chunk_count} send error: {e}", file=sys.stderr, flush=True)
            finally:
                try:
                    os.remove(tmp.name)
                except OSError:
                    pass

    print(f"DEBUG: Live stdin finished after {chunk_count} chunks.", file=sys.stderr, flush=True)


def main() -> None:
    # Graceful shutdown on SIGTERM (go2rtc sends this when the stream stops)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    media_id = "pipe:0"
    if len(sys.argv) >= 2:
        media_id = sys.argv[1].strip("'").strip('"')

    uid       = os.environ.get('CUBOAI_UID', '').strip('"')
    account   = os.environ.get('CUBOAI_ACCOUNT', '').strip('"')
    password  = os.environ.get('CUBOAI_PASSWORD', '').strip('"')
    camera_ip = os.environ.get('CUBOAI_CAMERA_IP', '').strip('"')

    if not all([uid, account, password]):
        print("ERROR: Missing CUBOAI_UID/ACCOUNT/PASSWORD env vars.", file=sys.stderr)
        sys.exit(1)

    try:
        print(f"DEBUG: media_id is {repr(media_id)}", file=sys.stderr, flush=True)

        if media_id == "pipe:0":
            _handle_live_stdin(uid, account, password, camera_ip)
        else:
            _handle_file_or_url(media_id, uid, account, password, camera_ip)

    except SystemExit:
        raise
    except Exception as e:
        print(f"Backchannel error: {e}", file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
