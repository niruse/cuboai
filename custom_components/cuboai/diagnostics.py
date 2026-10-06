"""Diagnostics: one redacted download that answers "which stream, which codec?".

Settings → Devices & Services → CuboAI → ⋮ → Download diagnostics.

Built for issue #85, where a HomeKit "No Response" survived five releases and
the question that decides everything could not be answered from the thread:
does HomeKit receive the H.264 transcode, or the camera's native HEVC? The
facts that decide it lived in three places nobody had been asked for together:

* the integration's options (is the per-camera H.264 transcode on?),
* go2rtc's live state (what codec each stream really carries, who is connected),
* go2rtc.log (every `[rtsp] new consumer stream=…` a client dialed, and the
  `codec=` of the camera's frames), plus HomeKit's own per-entity config, which
  can override the stream the integration hands out.

This collects them into one JSON and states the verdict in plain words.

Everything leaves the box redacted: camera credentials, TUTK UIDs, account
e-mails and URL credentials are removed, and camera ids / entity ids (which carry
the baby's name) are replaced by stable aliases such as `camera_1`.
"""

from __future__ import annotations

import json
import os
import re

from .const import (
    DOMAIN,
    OPT_PROTECT_ENABLED,
    OPT_PROTECT_PASSWORD,
    effective_ports,
    h264_resolution,
    live_stream_name,
    protect_stream_name,
)

REDACTED = "**REDACTED**"

#: How much of go2rtc.log goes into the download. The file is capped at ~2 MB
#: (plus one rolled copy); these bounds keep the JSON attachable to an issue.
LOG_TAIL_LINES = 300
LOG_KEY_EVENTS = 200

# Lines worth keeping from anywhere in the log, not only the tail.
_KEY_EVENT = re.compile(
    r"\[rtsp\] new consumer|\[mpegts\] muxing|kind=video|\[stall\]|\[reconnect\]|"
    r"Unsupported transport|\b461\b|\bERR\b|\bWRN\b|panic:|Traceback|FAILED|"
    r"stop producer|start producer|census tick"
)
_CONSUMER = re.compile(r"\[rtsp\] new consumer stream=(\S+)")
# What the CAMERA sends: the engine's frame census and its muxer. A conversion
# never shows up here (#85: `{"hevc": 271}` was read as "HomeKit gets HEVC"
# although HomeKit got the H.264 conversion).
_VIDEO_CODEC = re.compile(r"kind=video\b.*?\bcodec=([a-z0-9]+)")
_MUXING = re.compile(r"\[mpegts\] muxing ([a-z0-9]+)")
# ffmpeg's progress line, which go2rtc logs for its conversions with debug logs
# on (ffmpeg 8.1, #85):
#   frame= 1120 fps= 20 q=17.0 size=N/A time=00:00:37.43 bitrate=N/A
#   dup=604 drop=0 speed=0.661x elapsed=0:00:56.67
# ffmpeg leaves dup/drop out while both are 0, and elapsed= out before 7.x.
# q= is -1 when the video is only copied, so q >= 0 means a video encoder ran.
_PROGRESS = re.compile(
    r"\bframe=\s*(?P<frame>\d+)\s+fps=\s*(?P<fps>[\d.]+)\s+q=\s*(?P<q>-?[\d.]+)"
    r".*?\btime=\s*(?P<time>\d+:\d{2}:\d{2}(?:\.\d+)?)"
    r"(?:.*?\bdup=\s*(?P<dup>\d+)\s+drop=\s*(?P<drop>\d+))?"
    r".*?\bspeed=\s*(?P<speed>[\d.]+)x"
    r"(?:.*?\belapsed=\s*(?P<elapsed>\d+:\d{2}:\d{2}(?:\.\d+)?))?"
)
#: A conversion is judged only after this long: its first seconds are spent
#: catching up with what ffmpeg buffered while it waited for a keyframe.
PROGRESS_WARMUP_S = 20.0
#: Below this, a conversion of a live camera is falling behind. A machine that
#: keeps up holds ~1.00x: the camera paces the input.
SLOW_SPEED = 0.9
#: Progress lines kept in the download.
PROGRESS_KEPT = 10

# The two ways the camera handshake fails (see cuboai_transport_py.connect, #98).
# They look identical to go2rtc but have opposite causes, so they get opposite
# advice: nothing came back = the network path; answered-but-refused = the camera.
_NO_DISCOVERY_REPLY = "no nO reply"
_NO_GRANT = "no 0x2041 after nO"
# The engine's OWN failure line. go2rtc then echoes the same failure to every
# consumer waiting on the stream ("WRN [rtsp] error=…", "ERR …mjpeg…"), so a raw
# line count overstates the attempts ~15x (#107: 30 lines, 2 attempts).
_ENGINE_FAILURE_LINE = "[exec] Connection failed"

_ENV_SECRET = re.compile(r"(CUBOAI_(?:UID|ACCOUNT|PASSWORD))=\S+")
_URL_USERINFO = re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^/\s@:]+:[^/\s@]*@", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

_SECRET_OPTION_KEYS = {"nvr_password", "nvr_username", "password", "account", "token", "name"}


def _is_secret_key(key: str) -> bool:
    """Whether a dict key holds a secret. Any key that MENTIONS a password or a
    token counts, not only the ones listed: `unifi_protect_password` arrived
    after the list was written and would otherwise have been published in the
    options section of every report that had Protect enabled."""
    k = key.lower()
    return k in _SECRET_OPTION_KEYS or "password" in k or "token" in k


# ── redaction ────────────────────────────────────────────────────────────────


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


class Scrubber:
    """Removes secrets and replaces every camera identifier with `camera_N`.

    Identifiers are the device id and TUTK uid (long hex strings, replaced as
    substrings) and the baby's name — raw and slugified, because entity ids are
    built from it (`camera.cuboai_<name>`, `binary_sensor.cuboai_<name>_crying`)
    and HomeKit's per-entity config can reference any of them. Names are matched
    on word boundaries only, so a short name cannot mangle unrelated text.
    """

    def __init__(self, cameras: list[dict], secrets=()):
        # Exact secret VALUES (camera passwords, account names, the login and its
        # tokens). The regexes below catch the shapes we know; knowing the values
        # themselves catches them in any shape a log line happens to use.
        self.secrets = sorted({str(s) for s in secrets if isinstance(s, str) and len(s) >= 4}, key=len, reverse=True)
        self.ids: dict[str, str] = {}
        self.names: list[tuple[re.Pattern, str]] = []
        for n, cam in enumerate(cameras, start=1):
            alias = f"camera_{n}"
            for key in ("password", "account"):
                value = cam.get(key)
                if isinstance(value, str) and len(value) >= 4 and value not in self.secrets:
                    self.secrets.append(value)
            for key in ("device_id", "uid"):
                value = str(cam.get(key) or "")
                if len(value) >= 6 and value not in self.ids:
                    self.ids[value] = alias
            name = str(cam.get("baby_name") or "").strip()
            for form in {name, _slug(name)}:
                if len(form) >= 2:
                    pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(form)}(?![A-Za-z0-9])", re.IGNORECASE)
                    self.names.append((pattern, alias))
        # Longest first, so an identifier that contains another is replaced whole.
        self.names.sort(key=lambda item: len(item[0].pattern), reverse=True)
        # A legacy single-camera entry keeps device_id / baby_name at the top of
        # entry.data. Those are identifiers, not secrets: alias them (camera_1)
        # rather than blank them, or every stream name would read **REDACTED**.
        self.secrets = [s for s in self.secrets if s not in self.ids and not any(p.fullmatch(s) for p, _ in self.names)]

    def text(self, value: str) -> str:
        for secret in sorted(self.secrets, key=len, reverse=True):
            value = value.replace(secret, REDACTED)
        value = _ENV_SECRET.sub(rf"\1={REDACTED}", value)
        value = _URL_USERINFO.sub(rf"\1{REDACTED}@", value)
        value = _EMAIL.sub(REDACTED, value)
        for ident in sorted(self.ids, key=len, reverse=True):
            value = value.replace(ident, self.ids[ident])
        for pattern, alias in self.names:
            value = pattern.sub(alias, value)
        return value

    def obj(self, value):
        """text() applied through dicts and lists; secret-named keys are blanked."""
        if isinstance(value, dict):
            out = {}
            for key, item in value.items():
                new_key = self.text(str(key))
                if _is_secret_key(str(key)) and item:
                    out[new_key] = REDACTED
                else:
                    out[new_key] = self.obj(item)
            return out
        if isinstance(value, list):
            return [self.obj(v) for v in value]
        if isinstance(value, str):
            return self.text(value)
        return value

    def alias_of(self, device_id: str) -> str:
        return self.ids.get(device_id, "camera_?")


# ── go2rtc.log ───────────────────────────────────────────────────────────────


def read_log_lines(log_path: str) -> list[str]:
    """go2rtc.log.1 then go2rtc.log, oldest first. Blocking — run in the executor."""
    lines: list[str] = []
    for path in (f"{log_path}.1", log_path):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines.extend(fh.read().splitlines())
        except OSError:
            continue
    return lines


def _seconds(clock: str | None) -> float | None:
    """ffmpeg's H:MM:SS.ss -> seconds."""
    if not clock:
        return None
    h, m, s = clock.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def parse_progress(line: str) -> dict | None:
    """One ffmpeg progress line of a VIDEO conversion, or None.

    Copy-only processes (q=-1: an audio-only conversion for WebRTC) are left
    out: their speed says nothing about the H.264 conversion.
    """
    m = _PROGRESS.search(line)
    if not m or float(m.group("q")) < 0:
        return None
    frame = int(m.group("frame"))
    dup = int(m.group("dup") or 0)
    return {
        "frame": frame,
        "encode_fps": float(m.group("fps")),
        "video_time_s": _seconds(m.group("time")),
        "elapsed_s": _seconds(m.group("elapsed")),
        "speed": float(m.group("speed")),
        "dup": dup,
        "drop": int(m.group("drop") or 0),
        "repeated_frames_pct": round(100.0 * dup / frame, 1) if frame else 0.0,
    }


def _warm(progress: dict) -> bool:
    """Past its first PROGRESS_WARMUP_S seconds (wall time when ffmpeg says so, else video time)."""
    age = progress["elapsed_s"] if progress["elapsed_s"] is not None else progress["video_time_s"]
    return (age or 0.0) >= PROGRESS_WARMUP_S


def transcode_facts(progress: list[dict]) -> dict:
    """What the conversions (go2rtc's ffmpeg) did, apart from what the camera sends.

    go2rtc.log does not say which stream a progress line belongs to, so this
    is about the box: the latest warmed-up line that fell behind is `slow`.
    """
    warm = [p for p in progress if _warm(p)]
    slow = [p for p in warm if p["speed"] < SLOW_SPEED]
    return {
        "progress_lines": len(progress),
        "latest": progress[-PROGRESS_KEPT:],
        "slowest_speed": min((p["speed"] for p in warm), default=None),
        "slow": slow[-1] if slow else None,
    }


def log_facts(lines: list[str], scrubber: Scrubber) -> dict:
    """What go2rtc.log says: who dialed which stream, what the camera sends, and
    how the conversions kept up."""
    consumers: dict[str, int] = {}
    codecs: dict[str, int] = {}
    progress: list[dict] = []
    key_events: list[str] = []
    # [engine lines, echo lines] per failure kind
    failures = {_NO_DISCOVERY_REPLY: [0, 0], _NO_GRANT: [0, 0]}
    for line in lines:
        m = _CONSUMER.search(line)
        if m:
            stream = scrubber.text(m.group(1))
            consumers[stream] = consumers.get(stream, 0) + 1
        m = _VIDEO_CODEC.search(line) or _MUXING.search(line)
        if m:
            codecs[m.group(1)] = codecs.get(m.group(1), 0) + 1
        if "speed=" in line:
            p = parse_progress(line)
            if p:
                progress.append(p)
        for phrase, counts in failures.items():
            if phrase in line:
                counts[0 if _ENGINE_FAILURE_LINE in line else 1] += 1
        if _KEY_EVENT.search(line):
            key_events.append(line)
    return {
        "lines_read": len(lines),
        "rtsp_consumers_by_stream": consumers,
        # Every camera together: the log does not say which camera a frame
        # came from. What a conversion OUTPUTS is under "transcode" and, live,
        # under each camera's streams.h264.video_codec.
        "camera_video_codecs": codecs,
        "transcode": transcode_facts(progress),
        # Attempts, not lines: the engine's own line when present, else the echoes.
        "handshake_failures": {
            "no_discovery_reply": failures[_NO_DISCOVERY_REPLY][0] or failures[_NO_DISCOVERY_REPLY][1],
            "answered_but_no_grant": failures[_NO_GRANT][0] or failures[_NO_GRANT][1],
        },
        # FICENSUS lines alone would fill the budget; keep the few that show the
        # codec and every other kind of event.
        "key_events": [scrubber.text(x) for x in _thin_census(key_events)[-LOG_KEY_EVENTS:]],
        "tail": [scrubber.text(x) for x in lines[-LOG_TAIL_LINES:]],
    }


def _thin_census(events: list[str]) -> list[str]:
    """Keep only the first 3 video-frame census lines per run of them."""
    out, run = [], 0
    for line in events:
        if "kind=video" in line:
            run += 1
            if run > 3:
                continue
        else:
            run = 0
        out.append(line)
    return out


# ── go2rtc live state ────────────────────────────────────────────────────────


async def fetch_streams(hass, api_port: int) -> dict | None:
    """go2rtc's /api/streams, or None when it cannot be read. Read-only: asking
    never attaches a consumer, so a download cannot start a camera session."""
    try:
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        session = async_get_clientsession(hass)
        async with session.get(f"http://127.0.0.1:{api_port}/api/streams", timeout=5) as resp:
            if resp.status != 200:
                return None
            return await resp.json(content_type=None)
    except Exception:  # noqa: BLE001 - a diagnostics download must never fail on this
        return None


def stream_facts(streams: dict | None, name: str) -> dict:
    """Codec and consumers of one go2rtc stream, from /api/streams."""
    if streams is None:
        return {"state": "go2rtc API not reachable"}
    if name not in streams:
        return {"state": "not defined in go2rtc"}
    info = streams.get(name) or {}
    video, consumers = [], []
    for producer in info.get("producers") or []:
        for media in producer.get("medias") or []:
            if str(media).startswith("video"):
                video.append(str(media).split(",")[-1].strip())
    for consumer in info.get("consumers") or []:
        consumers.append(
            {
                "user_agent": consumer.get("user_agent"),
                "protocol": consumer.get("protocol"),
                "format": consumer.get("format_name"),
            }
        )
    return {
        "state": "running" if video else "idle (no producer — open the live view to start it)",
        "video_codec": video or None,
        "consumers": consumers,
    }


# ── HomeKit ──────────────────────────────────────────────────────────────────


def homekit_exposes(filt: dict, entity_id: str) -> bool:
    """Whether a HomeKit bridge's filter exposes `entity_id`.

    Follows the precedence of Home Assistant's own entity filter: an explicit
    include always wins, then an explicit exclude, then entity globs, then
    domains; with any include rule configured, anything unmatched is left out,
    otherwise it is in. Globs matter: a bridge that includes `camera.cuboai_*`
    exposes our camera although its entity id is listed nowhere.
    """
    from fnmatch import fnmatch

    domain = entity_id.split(".", 1)[0]
    inc_ent = set(filt.get("include_entities") or [])
    exc_ent = set(filt.get("exclude_entities") or [])
    inc_glob = list(filt.get("include_entity_globs") or [])
    exc_glob = list(filt.get("exclude_entity_globs") or [])
    inc_dom = set(filt.get("include_domains") or [])
    exc_dom = set(filt.get("exclude_domains") or [])
    if entity_id in inc_ent:
        return True
    if entity_id in exc_ent:
        return False
    if any(fnmatch(entity_id, g) for g in inc_glob):
        return True
    if any(fnmatch(entity_id, g) for g in exc_glob):
        return False
    if domain in inc_dom:
        return True
    if domain in exc_dom:
        return False
    return not (inc_ent or inc_glob or inc_dom)


def homekit_facts(hass, our_entity_ids: list[str]) -> list[dict]:
    """Per HomeKit bridge: which of our camera entities it exposes, and with what
    per-entity config. HomeKit's entity_config can name its own stream_source, in
    which case the integration's stream choice (and the H.264 transcode) is
    bypassed entirely. Bridges are numbered, not named: a bridge title is
    user-chosen text and could carry anything."""
    out = []
    try:
        entries = hass.config_entries.async_entries("homekit")
    except Exception:  # noqa: BLE001
        return out
    for n, entry in enumerate(entries, start=1):
        options = dict(getattr(entry, "options", {}) or {})
        filt = options.get("filter") or {}
        entity_config = options.get("entity_config") or {}
        exposed = {eid: entity_config.get(eid) or {} for eid in our_entity_ids if homekit_exposes(filt, eid)}
        out.append({"bridge": f"bridge_{n}", "mode": options.get("mode"), "exposed": exposed})
    return out


def our_camera_entities(hass, entry) -> dict[str, str]:
    """entity_id -> unique_id for this entry's camera entities. unique_id is the
    reliable link to a camera: it embeds the device id, while the entity id is
    built from the baby's name and may have been renamed."""
    try:
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(hass)
        return {
            e.entity_id: str(e.unique_id or "")
            for e in er.async_entries_for_config_entry(registry, entry.entry_id)
            if e.entity_id.startswith("camera.")
        }
    except Exception:  # noqa: BLE001
        return {}


# ── verdicts ─────────────────────────────────────────────────────────────────


def _handshake_verdicts(report: dict, no_reply: int, no_grant: int, ever_streamed: bool) -> list[str]:
    """Advice for a camera session that could not be set up at all.

    go2rtc.log lines do not say WHICH camera failed, so a single-camera install
    names it and a multi-camera one says "a camera".
    """
    cams = report["cameras"]
    who = next(iter(cams)) if len(cams) == 1 else "A camera"
    ip = next(iter(cams.values())).get("camera_ip") if len(cams) == 1 else None
    out = []
    if no_reply:
        where = f" ({ip})" if ip else ""
        if ever_streamed:
            out.append(
                f"{who}{where} failed to answer the connection probe {no_reply} time(s), but streamed at "
                "other times — an intermittent network path (Wi-Fi drops, a mesh node hand-over, a "
                "firewall that sometimes loses its state) rather than a configuration problem."
            )
        else:
            out.append(
                f"{who}{where} never answered the connection probe ({no_reply} attempt(s) in go2rtc.log), "
                "so no video was ever received — this is the network path, not the video codec. The probe "
                "goes to the camera's UDP port 32761 but the camera answers from a DIFFERENT UDP port, so "
                "anything stateful between Home Assistant and the camera drops the answer as unrelated "
                "traffic. Check, in order: (1) the camera IP set in Configure is still the camera's "
                "current address; (2) if Home Assistant runs in a VM, use bridged networking, not NAT; "
                "(3) if they are on different subnets/VLANs, allow camera -> Home Assistant UDP as NEW "
                "traffic, not only established/related (that fixed issue #98); (4) as a test, put both "
                "on the same subnet."
            )
    if no_grant:
        out.append(
            f"{who} answered the connection probe but refused the session {no_grant} time(s). That is "
            "the camera, not the network: it limits how many sessions it grants and how fast. Close "
            "extra viewers (the Cubo app on several phones counts), wait a minute, and try again."
        )
    return out


def _protect_section(store: dict, options: dict, scrubber: Scrubber, streams: dict | None = None) -> dict:
    """UniFi Protect (ONVIF), per exposed camera: is it up, at which address,
    what has Protect asked, and which stream is Protect really pulling."""
    group = store.get("onvif")
    if not options.get(OPT_PROTECT_ENABLED):
        return {"enabled": False}
    if group is None:
        return {"enabled": True, "running": False, "start_error": "service not created", "cameras": []}
    # Which streams Protect's media server is actually pulling. It locks in the
    # address it got at adoption, so a camera adopted before the fixed stream
    # existed (2.6.37/38) keeps pulling the old one until it is re-adopted.
    pulling = sorted(
        name
        for name, info in (streams or {}).items()
        if any("www.ui.com" in str(c.get("user_agent") or "") for c in (info or {}).get("consumers") or [])
    )
    cameras = []
    for dev, service in (getattr(group, "services", None) or {}).items():
        cameras.append(
            {
                "camera": scrubber.alias_of(dev),
                **service.stats(),
                "expected_stream": protect_stream_name(dev, options),
                "protect_pulling": [name for name in pulling if name.endswith(dev)],
            }
        )
    return {
        "enabled": True,
        "running": any(c.get("running") for c in cameras),
        "start_error": getattr(group, "start_error", None),
        "cameras": cameras,
        "protect_pulling": pulling,
    }


def _protect_camera_verdicts(protect_cam: dict, report: dict, log_codecs: set) -> list[str]:
    out: list[str] = []
    alias = protect_cam.get("camera")
    if not protect_cam.get("running"):
        out.append(
            f"UniFi Protect support for {alias} is on but not running: "
            f"{protect_cam.get('start_error') or 'unknown reason'}."
        )
        return out
    address = protect_cam.get("address")
    if not protect_cam.get("discovery_bound"):
        out.append(
            f"UniFi Protect cannot find {alias} by itself here (auto-discovery could not start: "
            f"{protect_cam.get('discovery_error') or 'unknown'}). Add it by address instead: in Protect, "
            f"UniFi Devices → ? → Try Advanced Adoption → {address}."
        )
    cam = (report.get("cameras") or {}).get(alias) or {}
    live = (cam.get("streams") or {}).get("combined", {}).get("video_codec") or []
    hevc = any(c.upper() in ("H265", "HEVC") for c in live) or (
        len(report.get("cameras") or {}) == 1 and "hevc" in log_codecs
    )
    if hevc and not cam.get("h264_transcode"):
        # Seen live: Protect adopts HEVC, labels it H.265 (read over ONVIF
        # Media2) and passes it to viewers unconverted.
        out.append(
            f"{alias} is shown to UniFi Protect as H.265 (HEVC). Protect passes H.265 to your phone or "
            "browser without converting it, so a viewer that cannot decode H.265 shows no picture. If that "
            "happens, turn on 'Transcode these cameras to H.264' for it."
        )
    expected = protect_cam.get("expected_stream")
    stale = [name for name in protect_cam.get("protect_pulling") or [] if name != expected]
    if expected and stale:
        out.append(
            f"UniFi Protect is pulling {', '.join(stale)} instead of {expected}. Protect keeps the stream "
            f"address it was given when the camera was adopted, so {alias} still uses an old one. Remove "
            "it in Protect and adopt it again, once — after that, changing 'Transcode these cameras "
            "to H.264' takes effect on its own."
        )
    unhandled = protect_cam.get("unhandled_operations") or {}
    if unhandled:
        out.append(
            f"UniFi Protect asked {alias} for ONVIF operations this integration does not answer yet: "
            + ", ".join(sorted(unhandled))
            + ". If Protect will not add or stream the camera, please report these."
        )
    if protect_cam.get("auth_failures"):
        out.append(
            f"{protect_cam['auth_failures']} request(s) to the UniFi Protect service of {alias} were refused "
            "for a missing or wrong username/password. Protect must use exactly the credentials set in Configure."
        )
    return out


def _protect_verdicts(report: dict, log_codecs: set) -> list[str]:
    protect = report.get("unifi_protect") or {}
    if not protect.get("enabled"):
        return []
    cams = protect.get("cameras") or []
    if not cams:
        return [f"UniFi Protect support is on but not running: {protect.get('start_error') or 'no camera selected'}."]
    out: list[str] = []
    for protect_cam in cams:
        out += _protect_camera_verdicts(protect_cam, report, log_codecs)
    return out


def _transcode_verdicts(report: dict) -> list[str]:
    """A conversion that cannot keep up with the camera (#85).

    go2rtc.log does not say which stream a progress line belongs to: the camera
    is named only when it is the one camera with the H.264 transcode on and no
    other conversion (the RTSP timestamp) is configured.
    """
    slow = ((report.get("go2rtc_log") or {}).get("transcode") or {}).get("slow")
    if not slow:
        return []
    cams = report.get("cameras") or {}
    transcoding = [alias for alias, cam in cams.items() if cam.get("h264_transcode")]
    stamped = (report.get("options") or {}).get("rtsp_timestamp_cameras") or []
    named = len(transcoding) == 1 and not stamped
    who = f"The H.264 conversion of {transcoding[0]}" if named else "A video conversion (ffmpeg) on this machine"
    elapsed = slow["elapsed_s"]
    if elapsed is None:  # ffmpeg before 7.x prints no elapsed=; speed is video time / wall time
        elapsed = slow["video_time_s"] / slow["speed"] if slow["speed"] > 0 else slow["video_time_s"]
    behind = max(0.0, elapsed - slow["video_time_s"])
    text = (
        f"{who} runs slower than real time: {slow['speed']:.2f}x, {slow['video_time_s']:.0f} s of video in "
        f"{elapsed:.0f} s, so the picture was {behind:.0f} s behind and falling further. HomeKit gives up on a "
        "stream like that ('No Response'), and every viewer of the conversion lags. This machine does not have "
        "the processor for it."
    )
    if named and cams[transcoding[0]].get("h264_resolution") != "720p":
        text += " Set 'Size of the H.264 transcode' to 720p (Configure), which needs far less."
    else:
        text += " Close other viewers that convert at the same time, and check what else keeps the processor busy."
    return [text]


def verdicts(report: dict) -> list[str]:
    """Plain-language conclusions. Each rule states one thing that is true of
    this box and matters for HomeKit / HA's stream player."""
    out: list[str] = []
    if not report["go2rtc"]["running"]:
        out.append("The streaming engine (go2rtc) is not running, so no camera can stream at all.")
    if not report["debug_logs"]:
        out.append(
            "Debug logs are OFF, so go2rtc.log holds no stream details. Turn on 'Enable debug logs' "
            "(Configure), reproduce the problem, then download diagnostics again."
        )
    log = report.get("go2rtc_log") or {}
    log_codecs = set(log.get("camera_video_codecs") or {})
    handshake = log.get("handshake_failures") or {}
    no_reply = handshake.get("no_discovery_reply", 0)
    no_grant = handshake.get("answered_but_no_grant", 0)
    out += _handshake_verdicts(report, no_reply, no_grant, bool(log_codecs))
    out += _transcode_verdicts(report)
    out += _protect_verdicts(report, log_codecs)
    for alias, cam in report["cameras"].items():
        live = cam["streams"]["combined"].get("video_codec") or []
        hevc = any(c.upper() in ("H265", "HEVC") for c in live) or (
            len(report["cameras"]) == 1 and "hevc" in log_codecs
        )
        if hevc and not cam["h264_transcode"]:
            out.append(
                f"{alias} sends HEVC (H.265) and 'Transcode these cameras to H.264' is OFF for it. "
                "HomeKit and Home Assistant's stream player cannot decode HEVC — the session sets up "
                "and shows nothing ('No Response'). Turn the option on for this camera."
            )
        if hevc and cam["h264_transcode"]:
            out.append(
                f"{alias} sends HEVC and its H.264 transcode is ON: HomeKit is handed "
                f"{cam['stream_handed_out']}, which should be H.264."
            )
        # Only when nothing else explains it. With a failed handshake in the log
        # this advice is wrong — the user already opened the live view, and it
        # failed; telling them to do it again sent #107 round in a circle.
        if not live and not log_codecs and not (no_reply or no_grant):
            out.append(
                f"No video codec is known yet for {alias}. With debug logs on, open its live view for "
                "~15 seconds, then download diagnostics again."
            )
        for hit in (cam.get("homekit") or {}).get("exposed_in") or []:
            cfg = hit.get("entity_config") or {}
            if cfg.get("stream_source"):
                out.append(
                    f"HomeKit {hit['bridge']} has its own stream_source for {hit['entity']}, so it bypasses "
                    "the integration's stream choice (and the H.264 transcode): "
                    f"{cfg['stream_source']}"
                )
    return out


# ── entry point ──────────────────────────────────────────────────────────────


def configured_and_account_cameras(data: dict) -> tuple[list[dict], list[dict]]:
    """(cameras this entry streams, every camera the account can see).

    `all_cameras` is the whole account — including cameras the user never added,
    which have no streams and must not be reported as if they were broken. Only
    `cameras` is reported; everything is scrubbed. A legacy single-camera entry
    keeps its one camera at the top of entry.data.
    """
    configured = list(data.get("cameras") or [])
    if not configured and data.get("device_id"):
        configured = [{"device_id": data["device_id"], "baby_name": data.get("baby_name", "")}]
    seen = {c.get("device_id") for c in configured}
    account = configured + [c for c in (data.get("all_cameras") or []) if c.get("device_id") not in seen]
    return configured, account


async def async_get_config_entry_diagnostics(hass, entry) -> dict:
    data = dict(entry.data or {})
    cameras, account_cameras = configured_and_account_cameras(data)
    options = dict(entry.options or {})
    # Every top-level string in entry.data is login material (username, password,
    # tokens, device uuid) — none of it belongs in the report, and all of it must
    # be scrubbed wherever it might surface. Configured cameras first, so the
    # cameras actually reported are camera_1, camera_2, …
    login_values = [v for v in data.values() if isinstance(v, str)]
    scrubber = Scrubber(
        account_cameras,
        secrets=login_values + [options.get("nvr_password") or "", options.get(OPT_PROTECT_PASSWORD) or ""],
    )
    store = (hass.data.get(DOMAIN) or {}).get(entry.entry_id) or {}
    manager = store.get("go2rtc")
    running = bool(manager is not None and getattr(manager, "is_running", False))
    rtsp_port, api_port = effective_ports(hass, entry.entry_id)
    debug_logs = bool(options.get("enable_debug_logs"))

    base = os.path.dirname(__file__)

    def _read_version() -> str | None:
        try:
            with open(os.path.join(base, "manifest.json"), encoding="utf-8") as fh:
                return json.load(fh).get("version")
        except (OSError, ValueError):
            return None

    version = await hass.async_add_executor_job(_read_version)
    lines = await hass.async_add_executor_job(read_log_lines, os.path.join(base, "bin", "go2rtc.log"))
    streams = await fetch_streams(hass, api_port) if running else None

    our_entities = our_camera_entities(hass, entry)
    homekit = homekit_facts(hass, list(our_entities))
    calls = store.get("stream_source_calls") or {}

    report_cams = {}
    for cam in cameras:
        dev = cam.get("device_id") or ""
        alias = scrubber.alias_of(dev)
        mine = [eid for eid, uid in our_entities.items() if dev and dev in uid]
        transcoded = dev in (options.get("h264_cameras") or [])
        report_cams[alias] = {
            "h264_transcode": transcoded,
            "h264_resolution": h264_resolution(options) if transcoded else None,
            "camera_ip": options.get(f"camera_ip_{dev}") or cam.get("camera_ip") or None,
            "stream_handed_out": live_stream_name(dev, options),
            "recent_stream_requests": calls.get(dev) or [],
            "streams": {
                "combined": stream_facts(streams, f"cuboai_combined_{dev}"),
                "h264": stream_facts(streams, f"cuboai_h264_{dev}"),
            },
            "homekit": {
                "bridges": len(homekit),
                "exposed_in": [
                    {"bridge": b["bridge"], "entity": eid, "entity_config": b["exposed"][eid]}
                    for b in homekit
                    for eid in mine
                    if eid in b["exposed"]
                ],
            },
        }

    report = {
        "integration_version": version,
        "go2rtc": {"running": running, "api_port": api_port, "rtsp_port": rtsp_port},
        "debug_logs": debug_logs,
        "homekit_integration_loaded": bool(homekit),
        "cameras": report_cams,
        "options": options,
        "go2rtc_log": log_facts(lines, scrubber),
        "unifi_protect": _protect_section(store, options, scrubber, streams),
    }
    report["verdicts"] = verdicts(report)
    # One pass over everything, last, so no section escapes it: options,
    # HomeKit config, verdict text and log lines all get the same substitution.
    return scrubber.obj(report)
