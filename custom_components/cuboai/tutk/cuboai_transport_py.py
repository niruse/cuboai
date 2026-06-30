# cuboai_transport_py.py
"""Pure Python CuboAI camera session — no native library required.

PureSession provides the same interface as the native TUTKSession but is implemented
entirely in Python: connect, ioctl (multiple IOCTLs on one connection), snapshot, and
av_frames / video_frames / audio_frames. Only two-way audio (send_audio_file) is a
stub.

The connection is a direct LAN UDP handshake — no relay server and no crypto on
connect. Once connected, session_hdr (the 16-byte AV-login token) is available and the
AV layer streams over the same socket.

Architecture:
  PureSession is a thin wrapper around cuboai_pure.TUTKDirectSession, which implements
  the handshake and the AV transport. This module adapts that into the session API
  (context manager, ioctl, snapshot, frame iterators) shared with the native backend.
"""

from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cuboai_pure import TUTKDirectSession
from typing import Optional, Iterator, Tuple


def _as_bytes(v):
    return v.encode() if isinstance(v, str) else v


class PureSession:
    """Pure Python CuboAI camera session (no native library).

    Provides the same interface as TUTKSession. The connection handshake and the
    full AV layer (ioctl, snapshot, av_frames/video_frames/audio_frames) work in
    pure Python with no native library. Only two-way audio (send_audio_file)
    remains a stub.

    Usage:
        sess = PureSession(uid, account, password, camera_ip='192.0.2.10')
        with sess:
            print(sess.session_hdr.hex())   # 16-byte session token
            tc, data = sess.ioctl(*build_get_hw_control())
            jpeg_or_hevc = sess.snapshot()
            for kind, buf in sess.av_frames(duration=10):
                ...
    """

    def __init__(self, uid: str, account: str, password: str,
                 camera_ip: Optional[str] = None, **kwargs):
        self.uid       = uid
        self.account   = account
        self.password  = password
        if not camera_ip:
            camera_ip = "255.255.255.255"
        self.camera_ip = camera_ip
        # cuboai_pure.TUTKDirectSession signature is
        #   (camera_ip, camera_port, account, password, uid) — pass by keyword,
        # and the builders need bytes for account/password.
        self._inner = TUTKDirectSession(
            camera_ip=camera_ip,
            account=_as_bytes(account),
            password=_as_bytes(password),
            uid=_as_bytes(uid),
            channels=kwargs.get('channels'),     # AV channel set (None = [0,1,2,3])
            verbose=kwargs.get('verbose', False), # print a connect/stream trace
            # full_fidelity is the master wire-fidelity flag (default True = byte-match the
            # native app: IOCTL cadence, ACK timestamp, NAK cadence, SACK list). The cadence
            # sub-flags default to None => follow full_fidelity; pass an explicit bool to
            # override just that stage. Set full_fidelity False for the low-latency path
            # (~0.5 s time-to-first-frame).
            full_fidelity=kwargs.get('full_fidelity', True),
            defer_stream_start=kwargs.get('defer_stream_start', None),
            defer_video_start_late=kwargs.get('defer_video_start_late', None),
        )
        self.session_hdr: Optional[bytes] = None
        # Single-frame API state (start_video/recv_frame/recv_audio_frame): one shared
        # AV iterator drains both streams (the camera stops sending audio unless video is
        # consumed too), and per-kind buffers hand frames back one at a time.
        self._av_iter: Optional[Iterator] = None
        self._vbuf: list = []
        self._abuf: list = []

    # ── connection (WORKING) ──────────────────────────────────────────────
    def connect(self, timeout_sec: int = 20) -> None:
        if not self._inner.connect(timeout=float(timeout_sec)):
            raise RuntimeError(
                "Pure Python handshake failed — camera did not grant the session "
                "(no 0x2041). Retry; the camera rate-limits rapid attempts.")
        self.session_hdr = self._inner.session_hdr

    def disconnect(self) -> None:
        self._inner.disconnect()
        self.session_hdr = None

    @property
    def connected(self) -> bool:
        return self.session_hdr is not None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.disconnect()

    # ── AV layer ──────────────────────────────────────────────────────────
    def ioctl(self, type_code: int, payload: bytes) -> Tuple[int, bytes]:
        """Send an AV IOCTL over the pure-Python LAN data channel.

        Returns (response_type_code, response_payload). Implemented in
        cuboai_pure.TUTKDirectSession.ioctl (reversed + validated against the
        native library — a GET_HW_CONTROL request reproduces native's wire
        packet byte-for-byte). Establishes the session lazily if needed.
        """
        return self._inner.ioctl(type_code, bytes(payload))

    # ── high-level GET commands (forward to the pure engine) ──────────────
    # Each parses the camera's response into a dict via the shared cuboai_messages
    # parsers (same parser the native backend uses → identical output). GET only.
    def get_hw_control(self) -> dict:        return self._inner.get_hw_control()
    def get_light_style(self) -> dict:       return self._inner.get_light_style()
    def get_sleep_safety(self) -> dict:      return self._inner.get_sleep_safety()
    def get_sleep_mode(self) -> dict:        return self._inner.get_sleep_mode()
    def get_lullaby(self) -> dict:           return self._inner.get_lullaby()
    def get_cry_detection(self) -> dict:     return self._inner.get_cry_detection()
    def get_cough_detection(self) -> dict:   return self._inner.get_cough_detection()
    def check_firmware_update(self) -> dict: return self._inner.check_firmware_update()
    def get_connected_users(self) -> dict:   return self._inner.get_connected_users()
    # additional GET controls
    def get_temp_humidity(self) -> dict:        return self._inner.get_temp_humidity()
    def get_night_light(self) -> dict:          return self._inner.get_night_light()
    def get_status_light(self) -> dict:         return self._inner.get_status_light()
    def get_hw_policy(self) -> dict:            return self._inner.get_hw_policy()
    def get_sleep_safety_setting(self) -> dict: return self._inner.get_sleep_safety_setting()
    def get_auto_capture(self) -> dict:         return self._inner.get_auto_capture()
    def get_smart_temp_config(self) -> dict:    return self._inner.get_smart_temp_config()
    def get_lullaby_schedule(self) -> dict:     return self._inner.get_lullaby_schedule()
    def get_light_way_config(self) -> dict:     return self._inner.get_light_way_config()
    def get_detection_zone_v2(self) -> dict:    return self._inner.get_detection_zone_v2()
    # further GET controls
    def get_event_list(self) -> dict:              return self._inner.get_event_list()
    def get_wifi(self) -> dict:                    return self._inner.get_wifi()
    def get_danger_zone(self) -> dict:             return self._inner.get_danger_zone()
    def get_danger_zone2(self) -> dict:            return self._inner.get_danger_zone2()
    def get_detection_zone(self) -> dict:          return self._inner.get_detection_zone()
    def get_media_profiles(self) -> dict:          return self._inner.get_media_profiles()
    def get_lightweight_status(self) -> dict:      return self._inner.get_lightweight_status()
    def get_lullaby_schedules(self) -> dict:       return self._inner.get_lullaby_schedules()
    def get_lullaby_schedule_action(self) -> dict: return self._inner.get_lullaby_schedule_action()
    def get_mat_config(self) -> dict:              return self._inner.get_mat_config()
    def get_mat_info(self) -> dict:                return self._inner.get_mat_info()
    def get_smart_temp_info(self) -> dict:         return self._inner.get_smart_temp_info()
    def get_feature_support(self) -> dict:         return self._inner.get_feature_support()
    # undocumented telemetry GETs (camera-side session stats + connected users)
    def get_session_stats(self) -> dict:           return self._inner.get_session_stats()
    def get_user_list(self) -> dict:               return self._inner.get_user_list()

    # ── read-only stats / diagnostics ─────────────────────────────────────
    def get_stats(self) -> dict:
        """Cumulative read-only snapshot of the engine's transport/decode counters.

        The single source consumed by cuboai_validate's --benchmark and
        cuboai_stream_video's verbose mode. See TUTKDirectSession.get_stats and the
        module helper cuboai_pure.stats_delta for per-interval rates."""
        return self._inner.get_stats()

    def get_during_stream(self, name: str, timeout: float = 2.5):
        """Poll a GET_METHODS endpoint safely while av_frames() is streaming (the read
        is injected onto the engine's reader thread, the sole socket sender). Returns the
        parsed dict or None on timeout. Read-only; used by the benchmark/verbose telemetry."""
        return self._inner.get_during_stream(name, timeout=timeout)

    def start_video(self) -> None:
        """Begin AV streaming for the legacy single-frame API.

        Opens one shared pure-Python AV iterator (see __init__). recv_frame() and
        recv_audio_frame() pull from it; modern code should prefer the av_frames/
        video_frames/audio_frames generators directly.
        """
        self._av_iter = self._inner.av_frames()
        self._vbuf.clear()
        self._abuf.clear()

    def _pump(self, want: str) -> Optional[bytes]:
        """Advance the shared AV iterator until a `want` ('video'|'audio') frame is
        buffered, returning it (or None when the stream ends)."""
        if self._av_iter is None:
            self.start_video()
        buf = self._vbuf if want == 'video' else self._abuf
        while not buf:
            try:
                kind, data = next(self._av_iter)
            except StopIteration:
                return None
            (self._vbuf if kind == 'video' else self._abuf).append(data)
        return buf.pop(0)

    def recv_frame(self) -> Optional[bytes]:
        """Return the next HEVC video access unit (raw bytes), or None at end."""
        return self._pump('video')

    def recv_audio_frame(self) -> Optional[bytes]:
        """Return the next AAC-ADTS audio frame (raw bytes), or None at end."""
        return self._pump('audio')

    def av_frames(self, duration=None) -> Iterator:
        """Yield ('video'|'audio', bytes) access units as they arrive."""
        yield from self._inner.av_frames(duration=duration)

    def av_frames_timed(self, duration=None) -> Iterator:
        """Yield (kind, bytes, frameinfo) — the parsed per-AU FRAMEINFO (carrying the
        camera timestamp) travels with its AU for PTS assignment. frameinfo is None for
        audio / unparsed AUs."""
        yield from self._inner.av_frames_timed(duration=duration)

    def video_frames_timed(self, duration=None, max_frames=None) -> Iterator:
        """Yield (video_bytes, frameinfo) for video AUs only (drives the mpegts PTS path)."""
        yield from self._inner.video_frames_timed(duration=duration, max_frames=max_frames)

    def audio_frames(self, max_frames=None) -> Iterator:
        """Yield raw AAC-ADTS frame bytes (audio only)."""
        yield from self._inner.audio_frames(max_frames=max_frames)

    def video_frames(self, max_frames=None) -> Iterator:
        """Yield raw HEVC access-unit bytes (video only)."""
        yield from self._inner.video_frames(max_frames=max_frames)

    def snapshot(self, timeout_sec: float = 20.0) -> bytes:
        """Capture one raw HEVC keyframe access unit (VPS+SPS+PPS+IDR, starting with
        ``00000001 40``). Returns a complete HEVC keyframe in a few seconds; convert it
        to JPEG downstream with PyAV/ffmpeg if needed.
        """
        return self._inner.snapshot(timeout_sec=timeout_sec)

    def save_snapshot(self, path: str, timeout_sec: float = 20.0,
                      quality: int = 90) -> str:
        """Capture a keyframe and save it as JPEG (decoded via PyAV). Returns path."""
        return self._inner.save_snapshot(path, timeout_sec=timeout_sec, quality=quality)

    def record_video(self, path: str, duration_sec: float = 10.0) -> str:
        """Record video+audio for duration_sec and mux to a playable .mp4. Returns path."""
        return self._inner.record_video(path, duration_sec=duration_sec)

    def record_audio(self, path: str, duration_sec: float = 10.0) -> str:
        """Record audio for duration_sec to a raw AAC-ADTS .aac file. Returns path."""
        return self._inner.record_audio(path, duration_sec=duration_sec)

    # ── SET commands (forward to the pure engine) ─────────────────────────
    def set_night_light(self, on: bool):          return self._inner.set_night_light(on)
    def set_light_brightness(self, brightness):   return self._inner.set_light_brightness(brightness)
    def set_sleep_mode(self, enabled: bool):      return self._inner.set_sleep_mode(enabled)
    def set_lullaby(self, sound_id, volume=None, duration=None):
        return self._inner.set_lullaby(sound_id, volume=volume, duration=duration)
    def set_lullaby_stop(self):                   return self._inner.set_lullaby_stop()
    def set_cry_detection(self, enabled=None, sensitivity=None):
        return self._inner.set_cry_detection(enabled=enabled, sensitivity=sensitivity)
    def set_cough_detection(self, enabled=None, in_crib=None, sensitivity=None):
        return self._inner.set_cough_detection(enabled=enabled, in_crib=in_crib,
                                               sensitivity=sensitivity)
    def set_auto_capture(self, mode):             return self._inner.set_auto_capture(mode)
    def set_lullaby_schedule(self, volume=None, duration=None):
        return self._inner.set_lullaby_schedule(volume=volume, duration=duration)
    def add_lullaby_schedule(self, name, **kw):   return self._inner.add_lullaby_schedule(name, **kw)
    def delete_lullaby_schedule(self, name):      return self._inner.delete_lullaby_schedule(name)
    def set_sleep_safety_setting(self, **kw):     return self._inner.set_sleep_safety_setting(**kw)
    # hardware-control SETs (read-modify-write of the 96-byte HW struct)
    def set_hw_control(self, **kw):               return self._inner.set_hw_control(**kw)
    def set_night_vision(self, mode):             return self._inner.set_night_vision(mode)
    def set_video_flip(self, on):                 return self._inner.set_video_flip(on)
    def set_mic_volume(self, value):              return self._inner.set_mic_volume(value)
    def set_speaker_volume(self, value):          return self._inner.set_speaker_volume(value)
    def set_status_light(self, on):               return self._inner.set_status_light(on)
    def set_sleep_safety(self, safety_alert, cover_alert, sensitivity, baby_presence_alert):
        return self._inner.set_sleep_safety(safety_alert, cover_alert, sensitivity, baby_presence_alert)
    def set_detection_zone(self, **kw):           return self._inner.set_detection_zone(**kw)
    def set_danger_zone(self, **kw):              return self._inner.set_danger_zone(**kw)
    def set_environment_alert(self, **kw):        return self._inner.set_environment_alert(**kw)

    def send_audio_file(self, path: str, channel: int = 1, loop: bool = False,
                        max_secs=None, rate: int = 16000, warmup: float = 2.5, on_status=None,
                        gain: float = 1.0, format=None, options=None):
        """
        Opens an av-server on a reversed-role talk channel; the camera logs in and pulls AAC-LC audio.
        See cuboai_pure.TUTKDirectSession.send_audio_file for the full flow. `loop`/`max_secs` give a
        continuous talk stream; `gain` is a linear volume multiplier (<1 quieter); `on_status` is an
        optional progress callback. Returns frames delivered.
        """
        return self._inner.send_audio_file(path, channel=channel, loop=loop, max_secs=max_secs,
                                           rate=rate, warmup=warmup, on_status=on_status, gain=gain,
                                           format=format, options=options)

    def stop_audio(self):
        """Ask an in-flight (e.g. looping) send_audio_file to stop at the next tick."""
        return self._inner.stop_audio()
