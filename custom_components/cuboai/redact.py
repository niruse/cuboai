"""What counts as a secret in a log line, in one place.

Log files get pasted into public issues (#130 attached its whole log: account
email, account id, camera id). So every log line the integration writes is
cleaned BEFORE it is written, by default, and diagnostics use the same patterns,
so the two can never drift apart.

Two levels:
- `secrets()`: passwords, tokens, the TUTK UID, the account email and account
  id. Used for go2rtc.log, which diagnostics parse by camera id, so camera ids
  stay whole there (diagnostics alias them as camera_N when exporting).
- `log_line()`: `secrets()` plus camera ids shortened to their last 4
  characters (`…B447`), so two cameras can still be told apart in the log.
  Used for every record of the integration's own loggers.
"""

from __future__ import annotations

import logging
import re

REDACTED = "**REDACTED**"
LOGGER_PREFIX = "custom_components.cuboai"

# The engine's credentials on the go2rtc exec line (`env CUBOAI_UID=… …`).
ENV_SECRET = re.compile(r"(CUBOAI_(?:UID|ACCOUNT|PASSWORD))=\S+")
# user:password@ in any URL (rtsp://user:pass@host, NVR / Protect / stream URLs).
URL_USERINFO = re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^/\s@:]+:[^/\s@]*@", re.IGNORECASE)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# A JWT (the cloud's access / id / refresh tokens): three base64url parts.
JWT = re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}")
# password=…, "token": "…", access_token: … and friends.
KEYED_SECRET = re.compile(
    r"(?i)((?:access_|refresh_|id_)?token|password|passwd|pwd|secret|(?<![a-z])pw)(\"?\s*[:=]\s*\"?)([^\s\"',;}&]+)"
)
# The account id (Cognito sub) and any other UUID.
UUID = re.compile(
    r"(?<![0-9a-fA-F-])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?![0-9a-fA-F-])"
)
# The TUTK UID: 20 upper-case letters and digits, with at least one of each.
TUTK_UID = re.compile(r"(?<![A-Za-z0-9])(?=[A-Z0-9]*[A-Z])(?=[A-Z0-9]*\d)[A-Z0-9]{20}(?![A-Za-z0-9])")
# A camera's device id: CB02… / CB03… or a first-generation 0001…, 16 hex digits.
# Underscores around it are fine (stream names: cuboai_combined_<id>).
DEVICE_ID = re.compile(r"(?<![A-Za-z0-9])(CB[0-9A-F]{14}|000[0-9A-F]{13})(?![A-Za-z0-9])")


def _email(match: re.Match) -> str:
    local, _, domain = match.group(0).partition("@")
    return f"{local[:1]}…@{domain}"


def secrets(text: str) -> str:
    """Remove credentials and personal ids; camera ids are left whole."""
    if not text:
        return text
    text = ENV_SECRET.sub(rf"\1={REDACTED}", text)
    text = URL_USERINFO.sub(rf"\1{REDACTED}@", text)
    text = JWT.sub(REDACTED, text)
    text = KEYED_SECRET.sub(rf"\1\2{REDACTED}", text)
    text = EMAIL.sub(_email, text)
    text = UUID.sub(REDACTED, text)
    return TUTK_UID.sub(REDACTED, text)


def log_line(text: str) -> str:
    """secrets(), and camera ids shortened to their last 4 characters."""
    return DEVICE_ID.sub(lambda m: "…" + m.group(1)[-4:], secrets(text))


def _clean_record(record: logging.LogRecord) -> None:
    try:
        message = record.getMessage()
    except Exception:  # noqa: BLE001 - a broken format string is not ours to fix
        return
    clean = log_line(message)
    if clean != message:
        record.msg, record.args = clean, None
    if record.exc_info and not record.exc_text:
        # Tracebacks carry URLs and arguments too; format it now, cleaned.
        record.exc_text = log_line(logging.Formatter().formatException(record.exc_info))
    elif record.exc_text:
        record.exc_text = log_line(record.exc_text)


_installed = False


def install() -> None:
    """Clean every record of the integration's loggers as it is created.

    A record factory rather than a filter: logger filters do not reach child
    loggers, and some modules create theirs only when first imported. This
    catches all of them, for every handler (home-assistant.log, the UI log, the
    debug trace file), including the lines Home Assistant itself logs through
    the integration's logger ("Setup of config entry 'CuboAI (…)' …").
    Idempotent; other integrations' records pass through untouched.
    """
    global _installed
    if _installed:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        if record.name == LOGGER_PREFIX or record.name.startswith(LOGGER_PREFIX + "."):
            _clean_record(record)
        return record

    logging.setLogRecordFactory(factory)
    _installed = True
