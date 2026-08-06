"""
cuboai_session.py — Session factory for CuboAI camera.

Two interchangeable backends implement the same session interface:
  - TUTKSession  (cuboai_tutk.py)        — native TUTK library via ctypes (full AV stack)
  - PureSession  (cuboai_transport_py.py) — pure Python, no library (LAN handshake WORKING)

Usage:
    from cuboai_session import get_session

    sess = get_session(uid, account, password,
                       lib_path=None,           # None = prefer pure Python
                       camera_ip='192.0.2.x')
    with sess:
        print(type(sess).__name__)              # TUTKSession or PureSession
        print(sess.session_hdr.hex())           # 16-byte session token (both backends)

Backend selection:
    --lib specified              → TUTKSession  (explicit native library)
    --lib omitted, lib found     → TUTKSession  (auto-detected in a standard install path)
    --lib omitted, no lib found  → PureSession  (pure Python — connect WORKING)

Status of PureSession (full stack solved 2026-05-30, sessions 9–12):
    ✅ Connection handshake WORKING — connects over LAN with no native library and
       derives the 16-byte session_hdr. There is NO relay and NO crypto on connect
       (100% LAN, direct UDP, security_mode 0); the earlier "51cc ECDH relay
       handshake" theory was wrong. See HANDOFF.md / PROTOCOL_RESEARCH.md.
    ✅ AV layer WORKING — ioctl, snapshot, video/audio streaming, AND two-way talk
       (send_audio_file) all run in pure Python; verified live. The pure backend is the
       default when no --lib is given. Two-way talk is PURE-ONLY: the native (--lib) TUTK
       4.2.1.1 lib can't perform the camera's 4.3.x talk handshake, so send_audio_file
       there raises NotImplementedError.
"""

from __future__ import annotations
import os
import platform
import sys
from typing import Optional


def _lib_names() -> list:
    """TUTK library filenames to try, host shared-library extension first.

    .so (Linux) / .dylib (macOS) / .dll (Windows). The non-native extensions are
    also tried so a manually-placed build of another flavour is still found; on
    Linux .so is first, so detection is unchanged here.
    """
    sysname = platform.system()
    exts = {'Darwin': ('.dylib', '.so'), 'Windows': ('.dll',)}.get(sysname, ('.so',))
    names = []
    for e in (*exts, '.so', '.dylib', '.dll'):
        n = 'libIOTCAPIs_ALL' + e
        if n not in names:
            names.append(n)
    return names


def _find_library() -> Optional[str]:
    """Auto-detect a TUTK library in standard *install* paths.

    Deliberately excludes the script's own directory and /tmp so a dev artifact
    sitting next to the sources does not silently override pure-Python mode — pass
    --lib explicitly to use such a library. Cross-platform via _lib_names().
    """
    arch = platform.machine().lower()
    base = os.path.dirname(os.path.abspath(__file__))
    dirs = [
        os.path.join(base, 'libs', arch),   # libs/<arch>/
        os.path.expanduser('~'),
        '/usr/local/lib',
        '/usr/lib',
    ]
    for p in (os.path.join(d, n) for d in dirs for n in _lib_names()):
        if os.path.exists(p):
            return p
    return None


def get_session(uid: str,
                account: str,
                password: str,
                lib_path: Optional[str] = None,
                camera_ip: Optional[str] = None,
                channels=None,
                verbose: bool = False,
                full_fidelity: bool = True,
                defer_stream_start=None,
                defer_video_start_late=None,
                auto_discover_lib: bool = True):
    """Return the appropriate session backend, printing which one is selected.

    Args:
        uid:        Device UID (license_id from REST API)
        account:    dev_admin_id (e.g. admin@YOUR_ACCOUNT)
        password:   dev_admin_pwd
        lib_path:   Path to libIOTCAPIs_ALL.so. If given → native. If None, a
                    standard install path is checked; if none is found → pure Python.
        camera_ip:  Camera LAN IP (recommended for reliable LAN connection).
        channels:   (pure backend, S62) AV channel set to open, e.g. [0,1] or [1].
                    None = native default [0,1,2,3]. Ignored by the native backend.
        verbose:    (pure backend, S62) print a connect/stream trace. Native ignores.
        full_fidelity: (pure backend, S82) MASTER wire-fidelity flag. True (default,
                    per the "always match native" preference) = byte-match native on the
                    ACK timestamp [48:52], NAK pair cadence, SACK list AND the S81 IOCTL
                    cadence. False (the --fast-start path) reverts all of these to the
                    simpler/faster pre-S82 behaviour (~0.5 s TTFF). Arming is firmware-
                    gated either way, so it changes only wire-fidelity/latency.
        defer_stream_start:     (pure backend, S81) defer 0x0300 (stream-start) ~5 s
                    after 0x00FF. None (default) = FOLLOW full_fidelity; True/False
                    overrides just this stage (e.g. fidelity ON but fast video start).
        defer_video_start_late: (pure backend, S81/S71) defer 0x01FF (START) ~5 s after
                    0x0300. None (default) = FOLLOW full_fidelity; True/False overrides.

    Returns:
        TUTKSession or PureSession instance (both support the context manager and
        expose .connect()/.disconnect()/.session_hdr).
    """
    # auto_discover_lib=False forces pure Python unless an EXPLICIT lib_path is given. The
    # streaming deployment uses this so a stray/wrong-vendor libIOTCAPIs_ALL.so sitting in ~ or a
    # standard path can never silently override pure mode (the local .so here is a WYZE build, the
    # wrong vendor for this camera). Other callers keep the original auto-detect behaviour.
    resolved = lib_path or (_find_library() if auto_discover_lib else None)
    if resolved:
        from cuboai_tutk import TUTKSession
        print(f"Using native library: {resolved}", file=sys.stderr, flush=True)
        return TUTKSession(uid, account, password,
                           lib_path=resolved, camera_ip=camera_ip)

    from cuboai_transport_py import PureSession
    print("Using pure Python transport (library not found)", file=sys.stderr, flush=True)
    return PureSession(uid, account, password, camera_ip=camera_ip,
                       channels=channels, verbose=verbose,
                       full_fidelity=full_fidelity,
                       defer_stream_start=defer_stream_start,
                       defer_video_start_late=defer_video_start_late)
