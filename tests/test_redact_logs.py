"""Logs are cleaned by default (#130 attached a log with the account email,
account id and camera id). Every value here is invented."""

import logging
import os

import pytest

from custom_components.cuboai import redact
from custom_components.cuboai.go2rtc import _pump_log

UID = "ABCDEFGHJK1234567890"  # TUTK UID shape: 20 upper-case letters + digits
PASSWORD = "s3cret-Pass!"
EMAIL = "someone@example.com"
ACCOUNT_ID = "c0ffee00-1234-4abc-9def-0123456789ab"
JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJzb21lb25lIn0.c2lnbmF0dXJlLXN0dWZm"
CAM = "CB02AABBCCDDEEFF"
CAM_GEN1 = "0001ABCDEF012345"


def _exec_line():
    return (
        f'run pipe args=["env","CUBOAI_UID={UID}","CUBOAI_ACCOUNT=admin@{CAM}",'
        f'"CUBOAI_PASSWORD={PASSWORD}","CUBOAI_MUX_AUDIO=1"]'
    )


@pytest.mark.parametrize(
    "line, gone",
    [
        (_exec_line(), [UID, PASSWORD, f"admin@{CAM}"]),
        (f"exec:env CUBOAI_UID={UID} CUBOAI_ACCOUNT=admin CUBOAI_PASSWORD={PASSWORD}", [UID, PASSWORD]),
        (f"rtsp://nvr:{PASSWORD}@192.168.1.20/stream", [PASSWORD]),
        (f"Decoded UUID from ID token: {ACCOUNT_ID}", [ACCOUNT_ID]),
        (f"Profiles for account {ACCOUNT_ID}: 2 cameras", [ACCOUNT_ID]),
        (f"Authorization: Bearer {JWT}", [JWT]),
        (f'{{"access_token": "abc123def456", "password": "{PASSWORD}"}}', ["abc123def456", PASSWORD]),
        (f"refresh_token={JWT}&x=1", [JWT]),
        (f"Setup of config entry 'CuboAI ({EMAIL})' cancelled", [EMAIL]),
        (f"Starting _fetch_local_data for UID={UID}, IP=192.168.1.30", [UID]),
    ],
)
def test_secrets_are_removed(line, gone):
    for text in (redact.secrets(line), redact.log_line(line)):
        for value in gone:
            assert value not in text, (value, text)


def test_email_keeps_its_domain_and_first_letter():
    assert redact.secrets(f"CuboAI ({EMAIL})") == "CuboAI (s…@example.com)"


def test_camera_ids_stay_whole_in_secrets_but_shorten_in_log_lines():
    line = f"Stream pre-warm of cuboai_combined_{CAM_GEN1} failed; camera {CAM} ok"
    assert redact.secrets(line) == line  # go2rtc.log: diagnostics read it by camera id
    assert redact.log_line(line) == "Stream pre-warm of cuboai_combined_…2345 failed; camera …EEFF ok"


@pytest.mark.parametrize(
    "line",
    [
        "go2rtc API port 1985 is already in use — using port 1986 instead",
        "[health t=10s] fps 9.7 1.2Mbps | loss 1.3% recov 100% (req 38 rec 21)",
        "CUBOAI_MUX_AUDIO=1 CUBOAI_CAMERA_IP=192.168.1.30",
        "Token refresh response keys: ['AccessToken', 'ExpiresIn']",
        "CuboAI authentication failed: Incorrect username or password.",
        "Pure Python handshake failed — the camera never answered the discovery probe (no nO reply)",
        "stream_type=0x1b frame 0123456789ABCDEF",  # 16 hex that is not a camera id
    ],
)
def test_ordinary_lines_are_untouched(line):
    assert redact.log_line(line) == line


class _Collect(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append((record.getMessage(), self.format(record)))


@pytest.fixture
def collected():
    redact.install()
    handler = _Collect()
    handler.setFormatter(logging.Formatter("%(message)s"))
    loggers = [
        logging.getLogger(n)
        for n in ("custom_components.cuboai", "custom_components.cuboai.later_module", "homeassistant.other")
    ]
    for lg in loggers:
        lg.addHandler(handler)
        lg.setLevel(logging.DEBUG)
        lg.propagate = False
    yield handler
    for lg in loggers:
        lg.removeHandler(handler)
        lg.propagate = True


def test_the_integrations_records_are_cleaned_for_every_handler(collected):
    logging.getLogger("custom_components.cuboai.later_module").debug("UID=%s pw=%s", UID, PASSWORD)
    # Home Assistant logs this one itself, through the integration's logger.
    logging.getLogger("custom_components.cuboai").error(
        "Setup of config entry '%s' for cuboai integration cancelled", f"CuboAI ({EMAIL})"
    )
    joined = " ".join(m for m, _ in collected.records)
    for value in (UID, PASSWORD, EMAIL):
        assert value not in joined


def test_tracebacks_are_cleaned_too(collected):
    try:
        raise RuntimeError(f"connect rtsp://admin:{PASSWORD}@10.0.0.2 failed for {UID}")
    except RuntimeError:
        logging.getLogger("custom_components.cuboai.later_module").exception("boom")
    _message, formatted = collected.records[-1]
    assert "RuntimeError" in formatted and PASSWORD not in formatted and UID not in formatted


def test_other_integrations_are_left_alone(collected):
    logging.getLogger("homeassistant.other").warning("user %s", EMAIL)
    assert collected.records[-1][0] == f"user {EMAIL}"


def test_install_is_idempotent():
    redact.install()
    factory = logging.getLogRecordFactory()
    redact.install()
    assert logging.getLogRecordFactory() is factory


def test_go2rtc_output_is_cleaned_on_its_way_to_the_log(tmp_path):
    read_fd, write_fd = os.pipe()
    with os.fdopen(write_fd, "wb") as go2rtc:
        go2rtc.write((_exec_line() + "\n").encode())
        go2rtc.write(f"[streams] start producer url=exec:env CUBOAI_UID={UID} cuboai_combined_{CAM}\n".encode())
        go2rtc.write(b"[health] fps 9.9\n")
    log_path = tmp_path / "go2rtc.log"
    _pump_log(read_fd, open(log_path, "a"))
    text = log_path.read_text()
    assert UID not in text and PASSWORD not in text
    assert f"cuboai_combined_{CAM}" in text  # camera ids stay whole for diagnostics
    assert "[health] fps 9.9" in text and text.count("\n") == 3
