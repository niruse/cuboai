"""The diagnostics download (issue #85).

A HomeKit "No Response" survived five releases because the one question that
decides it — does HomeKit get the H.264 transcode, or the camera's native HEVC? —
could not be answered from the thread. Those facts lived in three places (the
integration's options, go2rtc's live state, go2rtc.log) plus HomeKit's own
per-entity config. The download gathers them and states the verdict.

It is also a file users attach to PUBLIC GitHub issues, so the redaction tests
matter as much as the verdict tests: no password, account, token, TUTK uid,
device id or baby's name may survive in any section.

Every test names the mutation it kills.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.cuboai import diagnostics as diag
from custom_components.cuboai.const import DOMAIN

DEV = "SW05AABBCCDD1122"
UID = "TUTKUID0000XYZ9"
BABY = "Mia Rose"
CAM_PW = "s3cretCamPass"
ACCOUNT = "someone@example.com"
LOGIN_PW = "LoginPw123"
TOKEN = "tok_eyJhbGciOiJIUzI1NiJ9abcdef"
NVR_PW = "nvrPw77"
ENTITY = "camera.cuboai_mia_rose"

LOG = [
    f'12:00:00.000 DBG [exec] run pipe args=["env","CUBOAI_UID={UID}","CUBOAI_ACCOUNT={ACCOUNT}",'
    f'"CUBOAI_PASSWORD={CAM_PW}","CUBOAI_MUX_AUDIO=1"]',
    "12:00:01.000 DBG [exec] FICENSUS n=57 idx=56 len=92341 comp=1 kind=video head=00000001 codec=hevc kf=1",
    f"12:00:02.000 DBG [rtsp] new consumer stream=cuboai_combined_{DEV}",
    "12:00:03.000 WRN [rtsp] error=461 Unsupported transport",
    "12:00:04.000 DBG [exec] [health t=10s] fps 10.0 1.2Mbps",
    f"12:00:05.000 DBG [rtsp] url rtsp://admin:{NVR_PW}@192.168.1.5:8557/cuboai_combined_{DEV}",
    "12:00:06.000 DBG [homekit] linked binary_sensor.cuboai_mia_rose_crying",
]

H265_LIVE = {
    f"cuboai_combined_{DEV}": {
        "producers": [{"medias": ["audio, recvonly, MPEG4-GENERIC/16000/1", "video, recvonly, H265"]}],
        "consumers": [{"user_agent": "Lavf62.3.100", "protocol": "rtsp", "format_name": "rtsp"}],
    },
}
H264_LIVE = {
    f"cuboai_combined_{DEV}": {"producers": [{"medias": ["video, recvonly, H264"]}], "consumers": []},
}


def _entry(options=None, data_extra=None, cameras=None):
    cams = cameras or [
        {"device_id": DEV, "uid": UID, "baby_name": BABY, "password": CAM_PW, "account": ACCOUNT},
    ]
    data = {"username": ACCOUNT, "password": LOGIN_PW, "access_token": TOKEN, "cameras": cams}
    data.update(data_extra or {})
    return SimpleNamespace(
        entry_id="entryA",
        data=data,
        options={"enable_debug_logs": True, "nvr_password": NVR_PW, "h264_cameras": [], **(options or {})},
    )


def _hass(running=True, homekit_entries=()):
    hass = MagicMock()
    manager = SimpleNamespace(is_running=running)
    hass.data = {DOMAIN: {"entryA": {"go2rtc": manager}}}

    async def _run(func, *args):
        return func(*args)

    hass.async_add_executor_job = AsyncMock(side_effect=_run)
    hass.config_entries.async_entries = lambda domain: list(homekit_entries) if domain == "homekit" else []
    return hass


async def _diagnose(hass, entry, streams=H265_LIVE, log=LOG):
    fetch = AsyncMock(return_value=streams)
    with (
        patch.object(diag, "fetch_streams", fetch),
        patch.object(diag, "read_log_lines", lambda path: list(log)),
        patch.object(diag, "our_camera_entities", lambda h, e: {ENTITY: f"cuboai_camera_{DEV}"}),
    ):
        report = await diag.async_get_config_entry_diagnostics(hass, entry)
    return report, fetch


def _homekit(entity_config=None, include=(ENTITY,)):
    return SimpleNamespace(
        options={
            "mode": "bridge",
            "filter": {"include_entities": list(include)},
            "entity_config": entity_config or {},
        }
    )


# =============================================================================
# Redaction — this file goes onto public issues
# =============================================================================


@pytest.mark.asyncio
async def test_nothing_personal_survives_anywhere_in_the_download():
    """Kill: any one redaction path removed (secret values, env regex, URL
    userinfo, e-mail, id aliasing, baby-name aliasing)."""
    hk = _homekit({ENTITY: {"stream_source": f"rtsp://u:{NVR_PW}@h/cuboai_combined_{DEV}", "name": BABY}})
    report, _ = await _diagnose(_hass(homekit_entries=[hk]), _entry())
    blob = json.dumps(report)

    for secret in (DEV, UID, CAM_PW, ACCOUNT, LOGIN_PW, TOKEN, NVR_PW, "Mia", "mia_rose", "Rose"):
        assert secret.lower() not in blob.lower(), f"{secret!r} leaked into the diagnostics download"


@pytest.mark.asyncio
async def test_camera_ids_become_readable_aliases_not_blanks():
    """Redaction must not destroy the evidence: stream names have to stay
    readable so we can see WHICH stream a client dialed.

    Kill: identifiers blanked instead of aliased."""
    report, _ = await _diagnose(_hass(), _entry())

    consumers = report["go2rtc_log"]["rtsp_consumers_by_stream"]
    assert consumers == {"cuboai_combined_camera_1": 1}
    assert "camera_1" in report["cameras"]
    assert report["cameras"]["camera_1"]["stream_handed_out"] == "cuboai_combined_camera_1"


@pytest.mark.asyncio
async def test_a_legacy_entry_aliases_its_device_id_instead_of_blanking_it():
    """Old single-camera entries keep device_id at the top of entry.data, next
    to the login. It is an identifier, not a secret.

    Kill: the identifier exclusion removed from the secret list."""
    report, _ = await _diagnose(_hass(), _entry(data_extra={"device_id": DEV, "baby_name": BABY}))

    assert "cuboai_combined_camera_1" in report["go2rtc_log"]["rtsp_consumers_by_stream"]


# Each redaction layer ALONE. The end-to-end test above proves only that the
# UNION of the layers works: they overlap by design (defence in depth), so any
# single layer could be broken and that test would still pass — found by
# mutation, where removing any one of these four left the suite green. Each
# input below is something only its own layer can catch.


def test_layer_exact_values_catches_a_secret_in_no_known_shape():
    """Kill: exact-value redaction removed."""
    s = diag.Scrubber([], secrets=["hunter2secret"])
    assert "hunter2secret" not in s.text("the password is hunter2secret, plain text")


def test_layer_env_regex_catches_a_value_it_was_never_told():
    """Kill: the CUBOAI_* regex removed."""
    out = diag.Scrubber([]).text("args CUBOAI_PASSWORD=neverSeenBefore next")
    assert "neverSeenBefore" not in out


def test_layer_url_userinfo_catches_unknown_credentials():
    """Kill: the URL userinfo regex removed."""
    out = diag.Scrubber([]).text("dial rtsp://bob:unknownPw9@10.0.0.2:8557/x")
    assert "bob" not in out and "unknownPw9" not in out


def test_layer_email_catches_an_address_it_was_never_told():
    """Kill: the e-mail regex removed."""
    assert "other.person@mail.org" not in diag.Scrubber([]).text("sent to other.person@mail.org today")


@pytest.mark.asyncio
async def test_login_material_is_scrubbed_where_it_surfaces():
    """The login's token and password match no pattern; only handing their
    values to the scrubber removes them. Kill: login values not passed in."""
    log = LOG + [f"12:00:07.000 DBG [cloud] refresh {TOKEN} with {LOGIN_PW}"]
    report, _ = await _diagnose(_hass(), _entry(), log=log)
    blob = json.dumps(report)
    assert TOKEN not in blob and LOGIN_PW not in blob


def test_a_short_baby_name_cannot_mangle_unrelated_text():
    """Names are matched on word boundaries: "Al" must not eat "Already".

    Kill: the boundary look-arounds removed."""
    s = diag.Scrubber([{"device_id": DEV, "baby_name": "Al"}])
    assert s.text("Already all good, Al is asleep") == "Already all good, camera_1 is asleep"


# =============================================================================
# The verdict #85 needs
# =============================================================================


@pytest.mark.asyncio
async def test_hevc_with_the_transcode_off_is_called_out():
    """THE #85 question. Kill: the HEVC-and-off verdict removed."""
    report, _ = await _diagnose(_hass(), _entry())

    hits = [v for v in report["verdicts"] if "sends HEVC" in v and "OFF" in v]
    assert hits, report["verdicts"]
    assert "No Response" in hits[0], "the verdict must connect the fact to the symptom"


@pytest.mark.asyncio
async def test_an_h264_camera_gets_no_hevc_verdict():
    """A CB02 is native H.264; telling its owner to transcode would be wrong.

    Kill: the codec check dropped (verdict always fires)."""
    report, _ = await _diagnose(_hass(), _entry(), streams=H264_LIVE, log=[LOG[2]])

    assert not any("HEVC" in v for v in report["verdicts"]), report["verdicts"]


@pytest.mark.asyncio
async def test_hevc_with_the_transcode_on_says_what_homekit_is_handed():
    """Kill: the transcode-ON branch removed or inverted."""
    report, _ = await _diagnose(_hass(), _entry(options={"h264_cameras": [DEV]}))

    assert not any("OFF" in v for v in report["verdicts"]), report["verdicts"]
    assert any("cuboai_h264_camera_1" in v for v in report["verdicts"]), report["verdicts"]
    assert report["cameras"]["camera_1"]["h264_transcode"] is True


@pytest.mark.asyncio
async def test_the_log_supplies_the_codec_when_the_stream_is_idle():
    """HomeKit's session is gone by the time anyone downloads this; the log
    still knows what the camera sent.

    Kill: the go2rtc.log codec fallback removed."""
    report, _ = await _diagnose(_hass(), _entry(), streams={})

    assert report["go2rtc_log"]["video_codecs_seen"] == {"hevc": 1}
    assert any("sends HEVC" in v for v in report["verdicts"]), report["verdicts"]


@pytest.mark.asyncio
async def test_a_homekit_stream_source_override_is_called_out():
    """HomeKit's entity_config can name its own stream, bypassing the
    integration's choice and the transcode with it.

    Kill: the override verdict removed."""
    hk = _homekit({ENTITY: {"stream_source": f"rtsp://host:8557/cuboai_combined_{DEV}"}})
    report, _ = await _diagnose(_hass(homekit_entries=[hk]), _entry(options={"h264_cameras": [DEV]}))

    hits = [v for v in report["verdicts"] if "bypasses" in v]
    assert hits, report["verdicts"]
    assert "cuboai_combined_camera_1" in hits[0]


@pytest.mark.asyncio
async def test_an_unexposed_camera_raises_no_homekit_verdict():
    """Kill: the `exposed` check dropped from the override verdict."""
    hk = _homekit({ENTITY: {"stream_source": "rtsp://x/y"}}, include=("camera.something_else",))
    report, _ = await _diagnose(_hass(homekit_entries=[hk]), _entry())

    assert not any("bypasses" in v for v in report["verdicts"]), report["verdicts"]


@pytest.mark.asyncio
async def test_debug_logs_off_is_called_out():
    """Without debug logs go2rtc.log holds nothing; say so rather than let an
    empty log read as a healthy one. Kill: the debug-off verdict removed."""
    report, _ = await _diagnose(_hass(), _entry(options={"enable_debug_logs": False}))

    assert any("Debug logs are OFF" in v for v in report["verdicts"]), report["verdicts"]


@pytest.mark.asyncio
async def test_a_dead_engine_is_called_out_and_its_api_is_not_queried():
    """Kill: the not-running verdict removed, or the API queried regardless."""
    report, fetch = await _diagnose(_hass(running=False), _entry())

    assert any("not running" in v for v in report["verdicts"]), report["verdicts"]
    fetch.assert_not_called()


# =============================================================================
# Found by running it on a real box
# =============================================================================
# The first live download listed a camera the user never added, repeated each
# HomeKit row seven times (one per bridge, with nothing saying which), and could
# not see a camera exposed through a glob. Each is pinned below.

OTHER_DEV = "CB02FFEEDDCCBBAA"


@pytest.mark.asyncio
async def test_only_configured_cameras_are_reported_but_all_are_scrubbed():
    """`all_cameras` is the whole account. A camera never added has no streams
    and would read as broken. Kill: report built from all_cameras again, or the
    unconfigured camera left out of the scrubber."""
    configured = {"device_id": DEV, "uid": UID, "baby_name": BABY, "password": CAM_PW, "account": ACCOUNT}
    unadded = {"device_id": OTHER_DEV, "baby_name": "Noam"}
    entry = _entry(cameras=[configured], data_extra={"all_cameras": [configured, unadded]})
    log = LOG + [f"12:00:09.000 DBG [cloud] saw {OTHER_DEV} (Noam)"]
    report, _ = await _diagnose(_hass(), entry, log=log)

    assert list(report["cameras"]) == ["camera_1"], "a camera the user never added was reported"
    blob = json.dumps(report)
    assert OTHER_DEV not in blob and "Noam" not in blob, "the un-added camera was not scrubbed"


@pytest.mark.asyncio
async def test_a_legacy_single_camera_entry_is_still_reported():
    """Old entries have no `cameras` list, only device_id at the top.

    Kill: the legacy fallback in configured_and_account_cameras removed."""
    entry = _entry()
    entry.data = {"username": ACCOUNT, "password": LOGIN_PW, "device_id": DEV, "baby_name": BABY}
    report, _ = await _diagnose(_hass(), entry)

    assert list(report["cameras"]) == ["camera_1"]
    assert DEV not in json.dumps(report)


@pytest.mark.asyncio
async def test_many_bridges_give_one_row_per_exposure_not_one_per_bridge():
    """Seven bridges produced fourteen identical-looking rows. Now: a count, and
    one row only where the camera is actually exposed, naming the bridge.

    Kill: the per-bridge listing restored, or the bridge id dropped."""
    bridges = [_homekit(include=("light.x",)) for _ in range(6)] + [_homekit()]
    report, _ = await _diagnose(_hass(homekit_entries=bridges), _entry())

    hk = report["cameras"]["camera_1"]["homekit"]
    assert hk["bridges"] == 7
    assert [row["bridge"] for row in hk["exposed_in"]] == ["bridge_7"]


@pytest.mark.parametrize(
    "filt, exposed",
    [
        ({}, True),  # no filter at all: HomeKit exposes everything
        ({"include_entity_globs": ["camera.cuboai_*"]}, True),  # the case the live run missed
        ({"exclude_entity_globs": ["camera.cuboai_*"]}, False),
        ({"include_domains": ["camera"]}, True),
        ({"include_domains": ["light"]}, False),
        ({"exclude_domains": ["camera"]}, False),
        ({"exclude_domains": ["camera"], "include_entities": [ENTITY]}, True),  # explicit include wins
        ({"include_domains": ["camera"], "exclude_entities": [ENTITY]}, False),
    ],
)
def test_homekit_exposure_follows_home_assistants_filter_precedence(filt, exposed):
    """Kill: glob matching removed, or explicit include no longer beating a
    domain exclude."""
    assert diag.homekit_exposes(filt, ENTITY) is exposed


# =============================================================================
# Robustness
# =============================================================================


def test_an_unreachable_api_is_reported_not_raised():
    """Kill: the None branch in stream_facts removed."""
    assert diag.stream_facts(None, "x") == {"state": "go2rtc API not reachable"}


def test_video_census_is_thinned_to_three_per_run():
    """FICENSUS lines alone would fill the key-event budget.

    Kill: _thin_census bypassed."""
    events = ["kind=video a"] * 10 + ["[stall] x"] + ["kind=video b"] * 10
    kept = diag._thin_census(events)
    assert kept.count("kind=video a") == 3 and kept.count("kind=video b") == 3
    assert "[stall] x" in kept


def _import_camera_platform():
    """camera.py needs HA's camera module. Install the same stand-ins
    test_issue_84 uses, with setdefault, so this test passes on its own and in
    any order rather than only when that file happens to run first."""
    import sys
    from types import ModuleType

    camera_mod = ModuleType("homeassistant.components.camera")
    camera_mod.Camera = type("Camera", (), {"__init__": lambda self, *a, **k: None})
    camera_mod.CameraEntityFeature = MagicMock()
    camera_mod.StreamType = MagicMock()
    coordinator_mod = ModuleType("homeassistant.helpers.update_coordinator")
    # Same behaviour as test_issue_84's stand-in (it sets self.coordinator), so
    # whichever file installs it first, the other gets what it expects.
    coordinator_mod.CoordinatorEntity = type(
        "CoordinatorEntity",
        (),
        {"__init__": lambda self, coordinator=None, *a, **k: setattr(self, "coordinator", coordinator)},
    )
    sys.modules.setdefault("homeassistant.components.camera", camera_mod)
    sys.modules.setdefault("homeassistant.helpers.update_coordinator", coordinator_mod)
    from custom_components.cuboai.camera import CuboLocalCamera

    return CuboLocalCamera


def test_the_camera_remembers_the_last_five_streams_it_handed_out():
    """HomeKit's session lasts ~25 s; this is how the download still knows
    which stream it got. Kill: the record removed, or the cap removed."""
    CuboLocalCamera = _import_camera_platform()

    fake = SimpleNamespace(
        hass=SimpleNamespace(data={}),
        coordinator=SimpleNamespace(config_entry=SimpleNamespace(entry_id="entryA")),
        _device_id=DEV,
    )
    for n in range(7):
        CuboLocalCamera._remember_stream_handed_out(fake, f"stream_{n}")

    calls = fake.hass.data[DOMAIN]["entryA"]["stream_source_calls"][DEV]
    assert [c["stream"] for c in calls] == [f"stream_{n}" for n in range(2, 7)]
    assert all(c["at"] for c in calls)


@pytest.mark.asyncio
async def test_remembered_streams_appear_in_the_download():
    """Kill: the store read dropped from the report."""
    hass = _hass()
    hass.data[DOMAIN]["entryA"]["stream_source_calls"] = {
        DEV: [{"stream": f"cuboai_combined_{DEV}", "at": "2026-09-22T10:00:00+00:00"}]
    }
    report, _ = await _diagnose(hass, _entry())

    assert report["cameras"]["camera_1"]["recent_stream_requests"] == [
        {"stream": "cuboai_combined_camera_1", "at": "2026-09-22T10:00:00+00:00"}
    ]
