"""
cuboai_session.py — Session factory for CuboAI cameras.

Two interchangeable backends implement the same session interface:
  - PureSession  (cuboai_transport_py.py) — pure Python, no native library. This is
                                            the default and the focus of this library.
  - TUTKSession  (cuboai_tutk.py)         — optional native TUTK library via ctypes,
                                            used only if you supply your own library.

Usage:
    from cuboai_session import get_session

    sess = get_session(uid, account, password,
                       lib_path=None,           # None = pure Python (recommended)
                       camera_ip='192.0.2.10')  # the camera's LAN IP
    with sess:
        print(type(sess).__name__)              # PureSession or TUTKSession
        print(sess.session_hdr.hex())           # 16-byte session token (both backends)

Backend selection:
    lib_path given               -> TUTKSession  (explicit native library)
    lib_path omitted, lib found  -> TUTKSession  (auto-detected in a standard install path)
    lib_path omitted, none found -> PureSession  (pure Python; the normal case)

The pure backend connects directly over the LAN with no native library and no relay,
deriving the 16-byte session_hdr, and runs the full AV stack (ioctl, snapshot, and
video/audio streaming) in pure Python. Only two-way audio (send_audio_file) is a stub.
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
    parent_dir = os.path.dirname(base)
    dirs = [
        os.path.join(parent_dir, 'libs', arch),   # ../libs/<arch>/
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
        uid:        Device UID (the license_id field from the account's camera list).
        account:    Device admin id (e.g. admin@YOUR_ACCOUNT).
        password:   Device admin password.
        lib_path:   Path to a native libIOTCAPIs_ALL library. If given, the native
                    backend is used. If None, a standard install path is checked; if
                    no library is found, the pure-Python backend is used.
        camera_ip:  Camera LAN IP (recommended for a reliable LAN connection).
        channels:   (pure backend) AV channel set to open, e.g. [0,1] or [1].
                    None = the default [0,1,2,3]. Ignored by the native backend.
        verbose:    (pure backend) print a connect/stream trace. Native ignores it.
        full_fidelity: (pure backend) when True (default), the client byte-matches the
                    native app on the wire (ACK timestamp, NAK cadence, SACK list, and
                    IOCTL cadence). When False, it uses a simpler, lower-latency path
                    (~0.5 s time-to-first-frame). Streaming works either way; this only
                    affects wire fidelity and startup latency.
        defer_stream_start:     (pure backend) delay the stream-start message by ~5 s.
                    None (default) follows full_fidelity; pass True/False to override
                    just this stage.
        defer_video_start_late: (pure backend) delay the video START message by ~5 s.
                    None (default) follows full_fidelity; pass True/False to override.

    Returns:
        TUTKSession or PureSession instance (both support the context manager and
        expose .connect()/.disconnect()/.session_hdr).
    """
    # auto_discover_lib=False forces pure Python unless an EXPLICIT lib_path is given, so a
    # stray or wrong-vendor libIOTCAPIs_ALL library in a standard path can never silently
    # override pure mode. Callers that want auto-detection leave it at the default (True).
    resolved = lib_path or (_find_library() if auto_discover_lib else None)
    
    # TUTKSession cannot natively connect to a specific IP; it relies on a gcc-compiled 
    # LD_PRELOAD shim. HA OS lacks gcc, so the shim fails and the IP is ignored.
    # PureSession supports direct IP unicast natively in pure Python.
    if camera_ip and not lib_path:
        resolved = None
    
    import sys
    import os
    tutk_dir = os.path.dirname(os.path.abspath(__file__))
    if tutk_dir not in sys.path:
        sys.path.insert(0, tutk_dir)
        
    if resolved:
        from cuboai_tutk import TUTKSession
        print(f"Using native library: {resolved}", file=sys.stderr, flush=True)
        return TUTKSession(uid, account, password,
                           lib_path=resolved, camera_ip=camera_ip)

    from cuboai_transport_py import PureSession
    print("Using pure Python transport (library not found)", file=sys.stderr, flush=True)
    return PureSession(uid=uid, account=account, password=password, camera_ip=camera_ip,
                       channels=channels, verbose=verbose,
                       full_fidelity=full_fidelity,
                       defer_stream_start=defer_stream_start,
                       defer_video_start_late=defer_video_start_late)
