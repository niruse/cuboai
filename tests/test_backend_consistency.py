"""Every in-process session must use the same transport backend.

`async_ensure_dependencies` downloads an optional native `libIOTCAPIs_ALL.so`
into `custom_components/cuboai/libs/<arch>/`, and that is one of the directories
`cuboai_session._find_library()` searches. So a call site that leaves
`auto_discover_lib` at its default (True) can silently land on the native
backend while `switch.py`, `light.py` and the streamers — which pass False —
stay on the pure transport.

That split is currently masked by an incidental guard in `get_session`: a
truthy `camera_ip` discards the discovered library. But the camera IP is
auto-learned by the first successful poll, so a fresh install polls before it
is known, and any setup where it stays unset keeps the split permanently. The
DVR playback path is the sharpest case — `PlaybackSession` reaches through
`transport._inner`, which only the pure session has.

Pinning `auto_discover_lib=False` everywhere makes the backend a property of
the integration rather than of whether a file happens to exist. An explicit
`lib_path` (or CUBOAI_LIB) still selects the native backend.
"""

import io
import os
import re
import tokenize

_CC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "custom_components", "cuboai")

# The pure engine's own vendored copy is a separate upstream tree; it is not a
# call site of this integration and is excluded from the sweep.
_EXCLUDE_DIRS = {"playback_engine", "__pycache__"}


def _python_files():
    for root, dirs, files in os.walk(_CC):
        dirs[:] = [d for d in dirs if d not in _EXCLUDE_DIRS]
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(root, name)


def test_no_call_site_auto_discovers_a_native_library():
    offenders = []
    for path in _python_files():
        src = open(path, encoding="utf-8").read()
        for m in re.finditer(r"auto_discover_lib\s*=\s*True", src):
            line = src[: m.start()].count("\n") + 1
            # the parameter's own default in the factory signature is not a call site
            if os.path.basename(path) == "cuboai_session.py":
                continue
            offenders.append(f"{os.path.relpath(path, _CC)}:{line}")
    assert not offenders, (
        "these call sites can auto-discover a native TUTK library and split the "
        "backend away from the pure sessions used elsewhere: " + ", ".join(offenders)
    )


def test_every_get_session_call_site_pins_the_backend():
    """A call site that omits the argument entirely inherits the True default,
    which is the same hazard spelled differently.

    Tokenised rather than regexed so comments and docstrings that merely mention
    get_session() are not mistaken for call sites.
    """
    missing = []
    for path in _python_files():
        if os.path.basename(path) == "cuboai_session.py":
            continue
        src = open(path, encoding="utf-8").read()
        try:
            toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
        except (tokenize.TokenError, SyntaxError, IndentationError):
            continue
        code = [t for t in toks if t.type not in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE)]
        for i, t in enumerate(code):
            if t.type != tokenize.NAME or t.string != "get_session":
                continue
            if i and code[i - 1].type == tokenize.OP and code[i - 1].string == ".":
                continue  # attribute access, e.g. self._get_session
            if i + 1 >= len(code) or code[i + 1].string != "(":
                continue  # an import or a reference, not a call
            depth, j, args = 0, i + 1, []
            while j < len(code):
                if code[j].string == "(":
                    depth += 1
                elif code[j].string == ")":
                    depth -= 1
                    if depth == 0:
                        break
                else:
                    args.append(code[j].string)
                j += 1
            if "auto_discover_lib" not in args:
                missing.append(f"{os.path.relpath(path, _CC)}:{t.start[0]}")
    assert not missing, "get_session call sites that do not pin auto_discover_lib: " + ", ".join(missing)


if __name__ == "__main__":
    for fn in sorted(k for k in dict(globals()) if k.startswith("test_")):
        globals()[fn]()
        print("ok:", fn)
