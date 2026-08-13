# Probing the camera for new features

The bundled TUTK library knows ~34 camera GET endpoints; this integration surfaces about 20 of
them. If you want to add a sensor for something the camera knows but Home Assistant doesn't yet
show, this is how to find out whether the data is actually there — **before** writing an entity
for it.

Everything here was run against firmware **2.0.2273**. Endpoint behaviour is firmware-specific:
a code that answers on one firmware can be silent or stubbed on another, so re-run the probe on
your own camera rather than trusting the table below.

## Safety rules

This talks to a live baby monitor. Two rules are not negotiable:

- **Never sweep even-numbered IOCTL codes blindly.** SET request codes are even too —
  `SET_HW_CONTROL` is `0x1122`, right next to `GET_HW_CONTROL` `0x1120` — so a "send 8 zero bytes
  to every even code" scan will silently change camera settings. Probe only codes whose name
  contains GET / LIST / SUPPORT. For a SET you genuinely want to test: read the current value,
  set, then restore.
- **Never send** format-storage, `SET_WIFI`, `SET_ACCOUNT_INFO`, `SET_PASSWORD`, `SET_SYSTEM`,
  shell, or firmware-update codes.

Also worth knowing: the camera wedges if you hammer it with back-to-back sessions, and then
refuses *everything* for several minutes — which looks exactly like a protocol bug and will waste
your afternoon. Probe **inside the session the coordinator already opens** (below) rather than
opening your own, and leave the camera on live view when you are done.

## The probe harness

The coordinator opens one session per poll and issues its GETs there. Adding a probe to that
session costs no extra connection. Paste this into `coordinator.py`, inside `_fetch_local_data`'s
session block (right after the `get_connected_users` block):

```python
# TEMPORARY PROBE HARNESS — reads /config/cuboai_probe.json each poll; delete the file to stop.
try:
    import json as _pj, os as _po, time as _pt
    _ppath = "/config/cuboai_probe.json"
    if _po.path.exists(_ppath):
        with open(_ppath) as _pf:
            _spec = _pj.load(_pf)
        for _case in (_spec if isinstance(_spec, list) else [_spec]):
            _code = int(_case["code"])
            _pl = bytes.fromhex(_case.get("hex", ""))
            _tag = _case.get("tag", "")
            try:
                _t0 = _pt.monotonic()
                _rt, _rd = sess.ioctl(_code, _pl)
                # dt matters: a code that answers returns in ~0.02s, a silent one
                # burns the full ioctl timeout before raising.
                _LOGGER.warning("CUBOAI_PROBE %s code=0x%x resp=%s len=%s dt=%.2fs",
                                _tag, _code, _rt, len(_rd or b""), _pt.monotonic() - _t0)
                _outp = "/config/cuboai_probe_out.json"
                _acc = {}
                if _po.path.exists(_outp):
                    with open(_outp) as _of:
                        _acc = _pj.load(_of)
                _acc[_tag] = {"code": _code, "resp": _rt, "len": len(_rd or b""),
                              "hex": (_rd or b"").hex()}
                with open(_outp, "w") as _of:
                    _pj.dump(_acc, _of, indent=1)
            except Exception as _pe:
                _LOGGER.warning("CUBOAI_PROBE %s code=0x%x FAILED: %r", _tag, _code, _pe)
except Exception as _pe:
    _LOGGER.warning("CUBOAI_PROBE harness error: %r", _pe)
```

Then write `/config/cuboai_probe.json`:

```json
[{"tag": "get_media_profiles", "code": 2376, "hex": "0000000000000000"}]
```

Full responses land in `/config/cuboai_probe_out.json`. The spec file is re-read **every poll**,
so you can iterate on payloads by editing it — only the harness code itself needs a restart.

Two things that will cost you time if you don't know them:

- **Restart Home Assistant, don't reload the integration.** A config-entry reload re-runs setup
  but does not re-import the module, so your edited code simply does not run.
- **Delete both files and restore `coordinator.py` when you're done.** The harness is debug code
  and has no place in a running install.

## What answers on firmware 2.0.2273

Probed with the canonical 8-zero-byte GET payload:

| endpoint | code | result |
|---|---|---|
| `get_user_list` | `0x0946` | **real** — JSON `{"users":[...]}`, one entry per open camera session |
| `get_detection_zone` | `0x0930` | **real** — a rectangle (observed `40,40 → 1879,1039`) |
| `get_mat_config` | `0x1302` | **real** — the sleep mat's MAC address |
| `get_danger_zone2` | `0x1204` | answers, but `##dzone_name_default_tag##` = not configured |
| `get_lullaby_schedules` | `0x098e` | answers, all zero = no schedules configured |
| `get_danger_zone` | `0x0908` | answers, all zero |
| `get_lullaby_schedule_action` | `0x0992` | answers, all zero |
| `get_event_list` (LISTEVENT) | `0x0318` | **stub — do not build on this** (see below) |
| `get_media_profiles` | `0x0948` | **no response on this firmware** (it does answer on 3.0.1369) |

### LISTEVENT is a stub, and it looks convincing

`0x0318` returns a well-formed 48-byte response with `count=3` and three event entries. It is not
real data:

- The entries are dated **2012-06-20 11:00 / 11:01 / 11:02** — a firmware placeholder, not a
  zero-result.
- The response is **byte-identical across five different request layouts** (8-byte zero payload,
  packed 21/25-byte `SMsgAVIoctrlListEventReq` with a real 24-hour time range, and two aligned
  variants). The camera ignores the request payload completely.
- It does not change over time.

Anyone implementing this from the response shape alone would ship a "Camera Events: 3" sensor
that is permanently wrong. The check that caught it — **vary the request and see whether the
response varies** — is cheap and worth doing on any endpoint before you trust it.

### Caveats on the ones that are real

`get_user_list` counts sessions but **cannot identify them**: every client authenticates with the
same account, so a foreign viewer and this integration are indistinguishable. It is fine for "how
many sessions are open", useless for "is someone else watching". Observed drifting between 2 and 3
during normal operation (coordinator poll, live stream, DVR), so a sensor built on it needs
smoothing or it will flap.

## Where the deeper research lives

The reverse-engineering that produced the wire protocol is not in this repo. The full command map
(297 IOCTL codes from the decompiled app, marked implemented / dead / destructive), the frame-level
risk register, and the investigation methodology live in Fredrik Ringertz's research bundle. If you
are extending the protocol layer rather than the Home Assistant layer, start there.
