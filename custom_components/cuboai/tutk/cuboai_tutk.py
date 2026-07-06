"""
cuboai_tutk.py — Python ctypes wrapper around the TUTK native library.

This is the core connectivity layer. It wraps the ThroughTek (TUTK) Kalay P2P
native library (`libIOTCAPIs_ALL.so`) via Python ctypes, giving us full camera
control without needing to re-implement the encrypted P2P protocol.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHY CTYPES INSTEAD OF PURE PYTHON?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

We attempted a pure Python transport first. It works for LAN discovery
(nL/NO probes) but fails at the 51cc AV negotiation step: the ThroughTek
relay servers require a proprietary bootstrap TLS registration before they
accept the HELLO packet. Without this, we cannot derive the ECDH key needed
to encrypt nl frame payloads.

The native library handles all of this internally: bootstrap, ECDH, relay
communication, and payload encryption. We just call avSendIOCtrl() and
avRecvIOCtrl() and it all works.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
THE BROADCAST REDIRECT SHIM
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

On Linux, the TUTK library discovers cameras by broadcasting an nL probe
to the network's broadcast address (e.g. 192.0.2.255:32761) using IPv6
with IPv4-mapped addresses (::ffff:192.0.2.255). In some network configs
(VMs, containers, certain Linux setups), broadcast packets don't reach the
camera even when it's on the same subnet.

Solution: we compile a tiny C shim that intercepts sendto() via LD_PRELOAD
and redirects any port-32761 broadcast to the camera IP directly. The shim
is compiled from the C source string `_REDIRECT_C` at first run and cached
at /tmp/cuboai_redirect.so.

When camera_ip is provided, TUTKSession.connect() detects if the shim is
active (via LD_PRELOAD env var) and relaunches the current process with it
if not. This causes a "double Connecting" message — this is expected.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STRUCT LAYOUT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The _AVClientStartInConfig struct layout (48 bytes) was confirmed via Frida
by inspecting memory at the point avClientStartEx() is called. The offsets
are specific to the x86-64 version (4.2.1.1-H) of libIOTCAPIs_ALL.so and
may differ in other versions. If you get avClientStartEx returning -20000,
the struct layout is likely wrong for your library version.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
AUDIO FORMAT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The camera sends audio as AAC-LC in ADTS container, confirmed by analysing
the raw bytes from avRecvAudioData(). Each ADTS frame starts with 0xFFF1
(sync word). Sample rate: 16kHz, channels: 1 (mono), frame size: 448 bytes
= 1024 samples = 64ms per frame.

Despite the codec_id field in the frame info struct reading 0x0088, the
actual format is ADTS-AAC. The codec_id interpretation is incorrect for
this camera (it doesn't map to standard TUTK codec constants).

This native backend is optional: it is used only if you supply your own TUTK
library via lib_path (or CUBOAI_LIB). The pure-Python backend is the default and
needs no library. The shared library is not distributed with this project.
"""
import ctypes
import io
import os
import platform
import subprocess
import tempfile
import threading
import time
from ctypes import (
    POINTER, Structure, byref, cast,
    c_char, c_char_p, c_int, c_int32,
    c_uint, c_uint8, c_uint32, c_void_p, sizeof,
)
from typing import Optional, Tuple

IOCTL_TIMEOUT_MS = 5000
FRAME_BUF_SIZE   = 1024 * 1024   # 1 MB per video frame

# C source for broadcast redirect shim (compiled at runtime on Linux)
_REDIRECT_C = r"""
#define _GNU_SOURCE
#include <dlfcn.h>
#include <string.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <stdlib.h>

typedef ssize_t (*sendto_t)(int,const void*,size_t,int,const struct sockaddr*,socklen_t);
static sendto_t real_sendto = NULL;
static unsigned char camera_ip[4] = {0,0,0,0};

void set_camera_ip(unsigned char a, unsigned char b, unsigned char c, unsigned char d) {
    camera_ip[0]=a; camera_ip[1]=b; camera_ip[2]=c; camera_ip[3]=d;
}

ssize_t sendto(int fd, const void* buf, size_t len, int flags,
               const struct sockaddr* addr, socklen_t addrlen) {
    if (!real_sendto) real_sendto = dlsym(RTLD_NEXT, "sendto");
    if (!addr || addr->sa_family != AF_INET6 || len != 88 || camera_ip[0] == 0)
        return real_sendto(fd, buf, len, flags, addr, addrlen);

    struct sockaddr_in6* s6 = (struct sockaddr_in6*)addr;
    if (ntohs(s6->sin6_port) != 32761) 
        return real_sendto(fd, buf, len, flags, addr, addrlen);

    /* Redirect any broadcast (last byte = 255) to camera IP */
    if (s6->sin6_addr.s6_addr[15] == 255) {
        struct sockaddr_in6 cam = *s6;
        memset(cam.sin6_addr.s6_addr, 0, 10);
        cam.sin6_addr.s6_addr[10] = 0xff;
        cam.sin6_addr.s6_addr[11] = 0xff;
        memcpy(cam.sin6_addr.s6_addr + 12, camera_ip, 4);
        return real_sendto(fd, buf, len, flags, (struct sockaddr*)&cam, addrlen);
    }
    return real_sendto(fd, buf, len, flags, addr, addrlen);
}
"""

_redirect_lib = None  # module-level singleton


SHIM_PATH = '/tmp/cuboai_redirect.so'
SHIM_SRC   = '/tmp/cuboai_redirect.c'


def _ensure_shim_compiled() -> bool:
    """Compile the broadcast redirect shim if not already compiled.

    Best-effort: returns False (never raises) when gcc is missing, the /tmp
    source can't be written, or compilation fails. The caller then proceeds
    WITHOUT the shim — native streaming still works wherever the LAN delivers
    the TUTK broadcast probe to the camera (the shim is only a workaround for
    broadcast-blocked setups like VMs/containers).
    """
    if os.path.exists(SHIM_PATH):
        return True
    try:
        with open(SHIM_SRC, 'w') as f:
            f.write(_REDIRECT_C)
        ret = subprocess.run(
            ['gcc', '-shared', '-fPIC', '-O2', '-o', SHIM_PATH, SHIM_SRC, '-ldl'],
            capture_output=True
        )
        return ret.returncode == 0
    except (FileNotFoundError, OSError):
        # gcc not installed (FileNotFoundError) or any I/O error — skip the shim.
        return False


def _load_redirect_shim(camera_ip: str) -> None:
    """Install the broadcast redirect shim (Linux only) — BEST-EFFORT, never raises.

    LD_PRELOAD must be set before the TUTK library loads, so when the shim is not
    already active we compile it and re-exec the process with LD_PRELOAD set.

    The shim ONLY matters in broadcast-blocked networks (VMs/containers). When
    gcc is missing or anything else fails we return silently and let the native
    lib connect via ordinary LAN broadcast discovery — which works on a normal
    LAN where the camera and host share a subnet. So a box without gcc still
    streams; it just loses the broadcast-redirect fallback.
    """
    global _redirect_lib
    if platform.system() != 'Linux':
        return

    try:
        # Already active (LD_PRELOAD was set before process start) — just set the IP.
        if SHIM_PATH in os.environ.get('LD_PRELOAD', ''):
            if _redirect_lib is None:
                _redirect_lib = ctypes.CDLL(SHIM_PATH)
            parts = list(map(int, camera_ip.split('.')))
            _redirect_lib.set_camera_ip.argtypes = [ctypes.c_ubyte] * 4
            _redirect_lib.set_camera_ip(*parts)
            return

        # Not active — compile and relaunch with LD_PRELOAD. If gcc is missing,
        # _ensure_shim_compiled() returns False and we skip (no shim, no re-exec).
        if not _ensure_shim_compiled():
            return

        import sys
        # SAFETY: os.execve REPLACES the whole current process. That is only
        # acceptable for the standalone stream/CLI scripts — never inside a
        # host application (e.g. Home Assistant imports this module and runs
        # sessions in executor threads; re-exec'ing would kill the host).
        # Standalone scripts opt in by setting CUBOAI_ALLOW_REEXEC=1.
        if os.environ.get('CUBOAI_ALLOW_REEXEC') != '1':
            return

        env = os.environ.copy()
        existing = env.get('LD_PRELOAD', '')
        env['LD_PRELOAD'] = (SHIM_PATH + ':' + existing).strip(':')
        env['CUBOAI_CAMERA_IP'] = camera_ip
        # Relaunch current process with LD_PRELOAD (replaces this process).
        os.execve(sys.executable, [sys.executable] + sys.argv, env)
        # execve replaces the process — code below never runs
    except Exception:
        # CDLL load / execve / malformed IP / any failure: proceed WITHOUT the
        # shim rather than crashing the session. Native still connects wherever
        # LAN broadcast reaches the camera.
        return


class _AVClientStartInConfig(Structure):
    """48-byte layout confirmed via Frida on x86-64."""
    _fields_ = [
        ("cb",                  c_uint32),
        ("iotc_session_id",     c_uint32),
        ("iotc_channel_id",     c_uint8),
        ("timeout_sec",         c_uint32),
        ("account_or_identity", c_char_p),
        ("password_or_token",   c_char_p),
        ("resend",              c_int32),
        ("security_mode",       c_uint32),
        ("auth_type",           c_uint32),
        ("sync_recv_data",      c_int32),
    ]


class _AVClientStartOutConfig(Structure):
    _fields_ = [
        ("cb",                c_uint32),
        ("server_type",       c_uint32),
        ("resend",            c_int32),
        ("two_way_streaming", c_int32),
        ("sync_recv_data",    c_int32),
        ("security_mode",     c_uint32),
    ]


def _find_library(hint: Optional[str] = None) -> str:
    # An explicit path (e.g. --lib) is honored strictly: if it is given but does
    # not exist, fail clearly NAMING it rather than silently falling through to
    # the auto-detect search (which produced a misleading "Searched: [...]" that
    # didn't even mention the path the user asked for).
    if hint:
        if os.path.exists(hint):
            return hint
        raise FileNotFoundError(f"native library not found at the given path: {hint}")
    arch = platform.machine().lower()
    base = os.path.dirname(os.path.abspath(__file__))
    # host shared-library extension first (.so Linux / .dylib macOS / .dll Windows),
    # other extensions tried as fallbacks; on Linux .so is first so this is unchanged.
    sysname = platform.system()
    exts = {'Darwin': ('.dylib', '.so'), 'Windows': ('.dll',)}.get(sysname, ('.so',))
    names, seen = [], set()
    for e in (*exts, '.so', '.dylib', '.dll'):
        n = 'libIOTCAPIs_ALL' + e
        if n not in seen:
            seen.add(n); names.append(n)
    dirs = [os.path.join(base, 'libs', arch), os.path.join(base, 'lib'), '/tmp']
    candidates = [os.path.join(d, n) for d in dirs for n in names]
    for p in candidates:
        if os.path.exists(p):
            return p
    raise FileNotFoundError(
        f"TUTK library not found for '{arch}' on {sysname}. Searched: {candidates}"
    )


class TUTKSession:
    """Full-featured TUTK P2P session to a CuboAI camera.

    Provides IOCTL commands and raw HEVC video frame capture.
    On Linux, automatically handles broadcast redirect for LAN discovery.

    Usage::

        with TUTKSession(uid, account, password, lib_path,
                         camera_ip='192.0.2.10') as sess:
            tc, data = sess.ioctl(4384, b'\\x00'*8)   # GET_HW_CONTROL
            jpeg = sess.snapshot()                      # HEVC → JPEG
    """

    # The TUTK native library is a process-wide singleton with global state:
    # IOTC_Initialize2/IOTC_DeInitialize are NOT thread-safe against concurrent
    # sessions. Two threads each running their own TUTKSession (e.g. a
    # coordinator poll and a lullaby command) segfault the whole process when
    # one deinitializes while the other is inside connect()/ioctl(). Serialize
    # the full session lifetime across all threads.
    _GLOBAL_LOCK = threading.RLock()

    def __init__(self, uid: str, account: str, password: str,
                 lib_path: Optional[str] = None,
                 camera_ip: Optional[str] = None):
        self.uid       = uid.encode()
        self.account   = account.encode()
        self.password  = password.encode()
        self.camera_ip = camera_ip   # Optional: enable broadcast redirect
        self._lib      = ctypes.CDLL(_find_library(lib_path))
        self._sid      = -1
        self._av_id    = -1
        self._lock_held = False

    def connect(self, timeout_sec: int = 20) -> None:
        """Connect and open AV channel. Blocks ~2–5 s on LAN."""
        if not self._lock_held:
            TUTKSession._GLOBAL_LOCK.acquire()
            self._lock_held = True
        try:
            self._connect_locked(timeout_sec)
        except Exception:
            self.disconnect()
            raise

    def _connect_locked(self, timeout_sec: int = 20) -> None:
        # Use camera_ip from constructor or environment (set after relaunch)
        cam_ip = self.camera_ip or os.environ.get('CUBOAI_CAMERA_IP')
        if cam_ip:
            _load_redirect_shim(cam_ip)

        lib = self._lib
        lib.IOTC_Initialize2(0)
        time.sleep(2)

        lib.IOTC_Get_SessionID.restype = c_int
        self._sid = lib.IOTC_Get_SessionID()
        if self._sid < 0:
            raise RuntimeError(f"IOTC_Get_SessionID: {self._sid}")

        lib.IOTC_Connect_ByUID_Parallel.restype = c_int
        lib.IOTC_Connect_ByUID_Parallel.argtypes = [c_char_p, c_int]
        ret = lib.IOTC_Connect_ByUID_Parallel(self.uid, self._sid)
        if ret < 0:
            raise RuntimeError(f"IOTC_Connect_ByUID_Parallel: {ret}")

        lib.avInitialize.restype = c_int
        lib.avInitialize(10)

        cfg_in, cfg_out = _AVClientStartInConfig(), _AVClientStartOutConfig()
        cfg_in.cb                  = sizeof(cfg_in)
        cfg_out.cb                 = sizeof(cfg_out)
        cfg_in.iotc_session_id     = self._sid
        cfg_in.iotc_channel_id     = 0
        cfg_in.timeout_sec         = timeout_sec
        cfg_in.account_or_identity = self.account
        cfg_in.password_or_token   = self.password
        cfg_in.resend              = 1
        cfg_in.security_mode       = 0   # NON-SECURE (confirmed for CuboAI)
        cfg_in.auth_type           = 0

        lib.avClientStartEx.restype  = c_int
        lib.avClientStartEx.argtypes = [c_void_p, c_void_p]
        self._av_id = lib.avClientStartEx(byref(cfg_in), byref(cfg_out))
        if self._av_id < 0:
            raise RuntimeError(f"avClientStartEx: {self._av_id}")

    def disconnect(self) -> None:
        try:
            lib = self._lib
            if self._av_id >= 0:
                lib.avClientStop(self._av_id)
                self._av_id = -1
            if self._sid >= 0:
                lib.IOTC_Session_Close(self._sid)
                self._sid = -1
            lib.IOTC_DeInitialize()
        finally:
            if self._lock_held:
                self._lock_held = False
                TUTKSession._GLOBAL_LOCK.release()

    @property
    def connected(self) -> bool:
        return self._av_id >= 0

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.disconnect()

    def ioctl(self, type_code: int, payload: bytes) -> Tuple[int, bytes]:
        """Send IOCTL request, return (response_type, response_bytes)."""
        if self._av_id < 0:
            raise RuntimeError("Not connected")
        lib = self._lib

        lib.avSendIOCtrl.restype  = c_int
        lib.avSendIOCtrl.argtypes = [c_int, c_uint, c_char_p, c_int]
        ret = lib.avSendIOCtrl(self._av_id, type_code, payload, len(payload))
        if ret < 0:
            raise RuntimeError(f"avSendIOCtrl({type_code}): {ret}")

        resp   = (c_char * 4096)()
        tc_arr = (c_int * 1)(0)
        lib.avRecvIOCtrl.restype  = c_int
        lib.avRecvIOCtrl.argtypes = [c_int, POINTER(c_int), c_char_p, c_int, c_int]
        r = lib.avRecvIOCtrl(self._av_id, tc_arr, resp, 4096, IOCTL_TIMEOUT_MS)
        if r < 0:
            raise TimeoutError(f"IOCTL {type_code} timed out (err={r})")
        return tc_arr[0], bytes(resp[:r])

    # ── high-level GET commands ───────────────────────────────────────────────
    # Same thin wrappers as the pure backend (cuboai_pure.TUTKDirectSession), using
    # the SHARED cuboai_messages parsers so native and pure decode to identical
    # dicts. GET only (no SET commands).
    def _cubo_get(self, name):
        import cuboai_messages as cm
        builder, want_resp, parser = cm.GET_METHODS[name]
        io_type, payload = builder()
        rt, data = self.ioctl(io_type, payload)
        result = parser(data)
        if rt != want_resp:
            result['resp_type'] = rt
            result['warning'] = f"unexpected resp type {rt} (wanted {want_resp})"
        return result

    def get_hw_control(self):        return self._cubo_get('get_hw_control')
    def get_light_style(self):       return self._cubo_get('get_light_style')
    def get_sleep_safety(self):      return self._cubo_get('get_sleep_safety')
    def get_sleep_mode(self):        return self._cubo_get('get_sleep_mode')
    def get_lullaby(self):           return self._cubo_get('get_lullaby')
    def get_cry_detection(self):     return self._cubo_get('get_cry_detection')
    def get_cough_detection(self):   return self._cubo_get('get_cough_detection')
    def check_firmware_update(self): return self._cubo_get('check_firmware_update')
    def get_connected_users(self):   return self._cubo_get('get_connected_users')

    def start_video(self) -> None:
        """Send stream start commands to camera."""
        SETRESOLUTION = 255
        AUDIOSTART    = 768
        START         = 511
        start_payload = bytes([0, 0, 0, 0, 4, 0, 1, 0])
        for tc, payload in [
            (SETRESOLUTION, b'\x00\x00'),
            (AUDIOSTART,    start_payload),
            (START,         start_payload),
        ]:
            try:
                self.ioctl(tc, payload)
            except Exception:
                pass

    def recv_frame(self) -> Optional[bytes]:
        """Receive one raw HEVC video frame. Returns None on timeout/error.

        Correct 9-argument signature confirmed from wyzecam tutk.py:
          avRecvFrameData2(av_chan_id,
                           frame_data_buf, frame_data_max_len,
                           &actual_len, &expected_len,
                           frame_info_buf, frame_info_max_len,
                           &frame_info_actual_len, &frame_index)
        """
        lib = self._lib
        frame_data_buf = (c_char * FRAME_BUF_SIZE)()
        frame_info_buf = (c_char * 4096)()
        actual_len     = c_int32(0)
        expected_len   = c_int32(0)
        info_actual    = c_int32(0)
        frame_index    = c_uint32(0)

        lib.avRecvFrameData2.restype  = c_int
        lib.avRecvFrameData2.argtypes = [
            c_int,
            POINTER(c_char), c_int,
            POINTER(c_int32), POINTER(c_int32),
            POINTER(c_char), c_int,
            POINTER(c_int32), POINTER(c_uint32),
        ]
        r = lib.avRecvFrameData2(
            self._av_id,
            frame_data_buf, FRAME_BUF_SIZE,
            byref(actual_len), byref(expected_len),
            frame_info_buf, 4096,
            byref(info_actual), byref(frame_index),
        )
        if r < 0:
            return None
        return bytes(frame_data_buf[:r])

    def snapshot(self, timeout_sec: float = 15.0) -> bytes:
        """Capture one HEVC keyframe and return as JPEG bytes.

        Requires PyAV: pip install av
        """
        try:
            import av
        except ImportError:
            raise ImportError("Snapshot requires PyAV: pip install av")

        self.start_video()
        time.sleep(1.0)  # Allow camera to begin streaming
        deadline = time.time() + timeout_sec

        while time.time() < deadline:
            frame_data = self.recv_frame()
            if not frame_data:
                time.sleep(0.01)
                continue
            # HEVC keyframe: 00 00 00 01 40 (VPS NAL unit type=32)
            # CuboAI keyframes are ~22KB+ (not 50KB as assumed originally)
            if (len(frame_data) > 5_000
                    and frame_data[:4] == b'\x00\x00\x00\x01'
                    and frame_data[4] == 0x40):
                buf = io.BytesIO(frame_data)
                container = av.open(buf, format='hevc')
                for vframe in container.decode(video=0):
                    jpeg_buf = io.BytesIO()
                    vframe.to_image().save(jpeg_buf, format='JPEG', quality=90)
                    container.close()
                    return jpeg_buf.getvalue()
                container.close()

        raise TimeoutError(f"No HEVC keyframe within {timeout_sec}s")

    def save_snapshot(self, path: str, timeout_sec: float = 20.0,
                      quality: int = 90) -> str:
        """Capture a snapshot and save it as JPEG. Returns the path.

        Native snapshot() already returns JPEG bytes, so this just writes them.
        """
        jpeg = self.snapshot(timeout_sec=timeout_sec)
        path = os.path.expanduser(path)
        with open(path, 'wb') as f:
            f.write(jpeg)
        return path

    def record_audio(self, path: str, duration_sec: float = 10.0) -> str:
        """Record audio for duration_sec to a raw AAC-ADTS .aac file. Returns path."""
        path = os.path.expanduser(path)
        with open(path, 'wb') as f:
            for kind, data in self.av_frames(duration=duration_sec):
                if kind == 'audio':
                    f.write(data)
        return path

    def record_video(self, path: str, duration_sec: float = 10.0) -> str:
        """Record video+audio for duration_sec and mux to a playable .mp4. Returns path."""
        from cuboai_pure import mux_to_mp4
        path = os.path.expanduser(path)
        video, audio = [], []
        t0 = time.time()
        for kind, data in self.av_frames(duration=duration_sec):
            (video if kind == 'video' else audio).append(data)
        elapsed = max(1e-3, time.time() - t0)
        if not video:
            raise RuntimeError("no video frames captured")
        mux_to_mp4(path, video, audio, video_fps=max(1.0, len(video) / elapsed))
        return path

    def recv_audio_frame(self) -> Optional[bytes]:
        """Receive one AAC-ADTS audio frame. Returns None on timeout/error.

        Audio format confirmed via Frida: AAC-LC in ADTS container.
        Each frame is self-contained with 0xFFF1 sync header.
        Sample rate: 16000Hz, channels: 1 (mono).
        Typical frame size: 448 bytes.
        """
        lib = self._lib
        audio_buf = (c_char * 51200)()
        info_buf  = (c_char * 1024)()
        frame_idx = c_uint32(0)

        lib.avRecvAudioData.restype  = c_int
        lib.avRecvAudioData.argtypes = [
            c_int, POINTER(c_char), c_int,
            POINTER(c_char), c_int,
            POINTER(c_uint32),
        ]
        r = lib.avRecvAudioData(
            self._av_id,
            audio_buf, 51200,
            info_buf, 1024,
            byref(frame_idx),
        )
        if r <= 0:
            return None
        return bytes(audio_buf[:r])

    def audio_frames(self, max_frames: Optional[int] = None):
        """Generator yielding raw AAC-ADTS audio frame bytes.

        Runs video receive in a background thread (required by TUTK protocol —
        the camera will not send audio unless video is also being consumed).
        For combined audio+video use av_frames() instead.

        Usage::

            with TUTKSession(...) as sess:
                for frame in sess.audio_frames():
                    sys.stdout.buffer.write(frame)
        """
        import threading, queue as _queue
        self.start_video()
        time.sleep(0.5)

        q = _queue.Queue(maxsize=200)
        stop = threading.Event()

        def _recv_both():
            while not stop.is_set():
                v = self.recv_frame()          # must drain video
                a = self.recv_audio_frame()
                if a and not q.full():
                    q.put(a)
                if not v and not a:
                    time.sleep(0.001)

        t = threading.Thread(target=_recv_both, daemon=True)
        t.start()
        try:
            count = 0
            while max_frames is None or count < max_frames:
                try:
                    yield q.get(timeout=1.0)
                    count += 1
                except Exception:
                    continue
        finally:
            stop.set()

    def av_frames(self, duration: Optional[float] = None):
        """Generator yielding (type, data) tuples for combined AV streaming.

        type is 'video' (raw HEVC) or 'audio' (raw AAC-ADTS).
        Both streams are received concurrently over the single TUTK AV channel.
        This is the most efficient way to get both streams — no duplicate
        connections or wasted bandwidth.

        Args:
            duration: Optional stop after this many seconds (measured in
                      producer thread for accuracy — avoids queue buffering lag).

        Usage::

            with TUTKSession(...) as sess:
                for frame_type, data in sess.av_frames():
                    if frame_type == 'video':
                        video_out.write(data)
                    else:
                        audio_out.write(data)
        """
        import threading, queue as _queue

        self.start_video()
        time.sleep(0.5)

        # Use a small queue — reduces buffering lag for duration accuracy
        q = _queue.Queue(maxsize=50)
        stop = threading.Event()
        _SENTINEL = object()

        def _recv():
            deadline = time.time() + duration if duration else None
            while not stop.is_set():
                if deadline and time.time() >= deadline:
                    q.put(_SENTINEL)
                    return
                v = self.recv_frame()
                a = self.recv_audio_frame()
                if v:
                    try: q.put(('video', v), timeout=0.1)
                    except: pass
                if a:
                    try: q.put(('audio', a), timeout=0.1)
                    except: pass
                if not v and not a:
                    time.sleep(0.001)

        t = threading.Thread(target=_recv, daemon=True)
        t.start()
        try:
            while True:
                try:
                    item = q.get(timeout=2.0)
                    if item is _SENTINEL:
                        return
                    yield item
                except Exception:
                    continue
        finally:
            stop.set()

    def send_audio_file(self, path: str, *args, **kwargs) -> None:
        """NOT SUPPORTED on the native backend — two-way talk is a PURE-PYTHON-ONLY feature.

        The camera's talk path needs the 4.3.x av-server grant (the e0fefe01 capability word)
        that this lib (WYZE TUTK 4.2.1.1) omits, so the native avServStartEx times out
        (-20011). The pure backend writes the grant itself; use it (omit lib_path /
        CUBOAI_LIB) for talk. See cuboai_pure.TUTKDirectSession.send_audio_file.
        """
        raise NotImplementedError(
            "two-way talk (send_audio_file) is pure-Python only — omit lib_path / CUBOAI_LIB "
            "to use it (the native TUTK 4.2.1.1 lib can't perform the camera's talk handshake).")

    def video_frames(self, max_frames: Optional[int] = None):
        """Generator yielding raw HEVC frame bytes for live streaming."""
        self.start_video()
        count = 0
        while max_frames is None or count < max_frames:
            frame = self.recv_frame()
            if frame:
                yield frame
                count += 1
            else:
                time.sleep(0.001)
