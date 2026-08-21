"""Tests for issue #94: accounts whose /user/cameras response has no baby profiles.

The reporter's iOS-app capture (Proxyman) showed the endpoint returning his brand-new
CuboAi 3 camera in `data` with full TUTK credentials — while `profiles` was an empty
array. The old parser iterated `profiles` and only joined `data` in, so such an
account produced zero cameras and the config flow died with "No cameras found for
account" despite a fully successful login.

Covers, for BOTH the sync (config-flow) and async (setup-time healing) paths:
- the reporter's exact response shape yields the camera, credentials intact
- profiles-present accounts are unchanged (baby name still comes from the profile)
- mixed accounts (device with profile + device without) yield both cameras
- the issue #84 dedupe guarantee survives the parser inversion
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from aioresponses import aioresponses

from custom_components.cuboai.api import async_api, cuboai_functions

# The reporter's captured response, redacted values substituted like-for-like.
REPORTER_RESPONSE = {
    "data": [
        {
            "device_id": "SW05TESTDEVICE01",
            "dev_admin_id": "admin@SW05TESTDEVICE01",
            "dev_admin_pwd": "test-dev-password",
            "created": "2026-08-13T09:47:51.000Z",
            "license_id": "TESTLICENSE01",
            "role": "admin",
            "user_id": "test-user-uuid",
            "tag": 5,
            "settings": None,
        }
    ],
    "profiles": [],
    "report_settings": [],
}


def _sync_fetch(payload):
    """Run the sync get_camera_profiles against a canned /user/cameras payload."""
    with (
        patch("custom_components.cuboai.api.cuboai_functions.requests.get") as mock_get,
        patch(
            "custom_components.cuboai.api.cuboai_functions.get_camera_state",
            return_value={"state": "online"},
        ),
    ):
        mock_get.return_value.json.return_value = payload
        mock_get.return_value.raise_for_status = MagicMock()
        return cuboai_functions.get_camera_profiles("token", "agent")


async def _async_fetch(payload, device_ids):
    """Run the async get_camera_profiles against a canned /user/cameras payload."""
    with aioresponses() as m:
        m.get(f"{async_api.API_BASE}/user/cameras", payload=payload)
        for dev in device_ids:
            m.get(
                f"{async_api.API_BASE}/camera/state?device_id={dev}",
                payload={"state": "online"},
                repeat=True,
            )
        return await async_api.get_camera_profiles("token", "agent")


class TestProfilesEmptyAccount:
    """The issue #94 regression pin: `data` is authoritative, `profiles` optional."""

    def test_sync_returns_camera_when_profiles_empty(self):
        cameras = _sync_fetch(REPORTER_RESPONSE)

        assert len(cameras) == 1
        cam = cameras[0]
        assert cam["device_id"] == "SW05TESTDEVICE01"
        assert cam["uid"] == "TESTLICENSE01"
        assert cam["account"] == "admin@SW05TESTDEVICE01"
        assert cam["password"] == "test-dev-password"
        assert cam["baby_name"] == "Unknown"

    @pytest.mark.asyncio
    async def test_async_returns_camera_when_profiles_empty(self):
        cameras = await _async_fetch(REPORTER_RESPONSE, ["SW05TESTDEVICE01"])

        assert len(cameras) == 1
        cam = cameras[0]
        assert cam["device_id"] == "SW05TESTDEVICE01"
        assert cam["uid"] == "TESTLICENSE01"
        assert cam["account"] == "admin@SW05TESTDEVICE01"
        assert cam["password"] == "test-dev-password"

    def test_sync_returns_camera_when_profiles_key_missing_entirely(self):
        payload = {"data": REPORTER_RESPONSE["data"]}
        cameras = _sync_fetch(payload)
        assert len(cameras) == 1
        assert cameras[0]["uid"] == "TESTLICENSE01"


class TestProfilesPresentUnchanged:
    """Existing accounts keep their baby names (entity ids must not shift)."""

    PAYLOAD = {
        "data": [
            {
                "device_id": "CB02TESTDEVICE01",
                "license_id": "UIDCB02",
                "dev_admin_id": "admin@CB02TESTDEVICE01",
                "dev_admin_pwd": "pwd-cb02",
            }
        ],
        "profiles": [
            {"device_id": "CB02TESTDEVICE01", "profile": json.dumps({"baby": "Dragon"})},
        ],
    }

    def test_sync_baby_name_from_profile(self):
        cameras = _sync_fetch(self.PAYLOAD)
        assert len(cameras) == 1
        assert cameras[0]["baby_name"] == "Dragon"
        assert cameras[0]["uid"] == "UIDCB02"

    @pytest.mark.asyncio
    async def test_async_baby_name_from_profile(self):
        cameras = await _async_fetch(self.PAYLOAD, ["CB02TESTDEVICE01"])
        assert len(cameras) == 1
        assert cameras[0]["baby_name"] == "Dragon"
        assert cameras[0]["password"] == "pwd-cb02"


class TestMixedAccount:
    """One device with a profile plus one without: both must appear."""

    PAYLOAD = {
        "data": [
            {
                "device_id": "CB02TESTDEVICE01",
                "license_id": "UIDCB02",
                "dev_admin_id": "admin@CB02TESTDEVICE01",
                "dev_admin_pwd": "pwd-cb02",
            },
            {
                "device_id": "SW05TESTDEVICE01",
                "license_id": "UIDSW05",
                "dev_admin_id": "admin@SW05TESTDEVICE01",
                "dev_admin_pwd": "pwd-sw05",
            },
        ],
        "profiles": [
            {"device_id": "CB02TESTDEVICE01", "profile": json.dumps({"baby": "Dragon"})},
        ],
    }

    def test_sync_both_devices_returned(self):
        cameras = _sync_fetch(self.PAYLOAD)
        by_id = {c["device_id"]: c for c in cameras}
        assert set(by_id) == {"CB02TESTDEVICE01", "SW05TESTDEVICE01"}
        assert by_id["CB02TESTDEVICE01"]["baby_name"] == "Dragon"
        assert by_id["SW05TESTDEVICE01"]["baby_name"] == "Unknown"
        assert by_id["SW05TESTDEVICE01"]["uid"] == "UIDSW05"

    @pytest.mark.asyncio
    async def test_async_both_devices_returned(self):
        cameras = await _async_fetch(self.PAYLOAD, ["CB02TESTDEVICE01", "SW05TESTDEVICE01"])
        assert {c["device_id"] for c in cameras} == {"CB02TESTDEVICE01", "SW05TESTDEVICE01"}


class TestParserInvariants:
    """Behavior the inversion must not break."""

    def test_duplicate_profiles_still_collapse_to_newest(self):
        """The issue #84 guarantee, now provided by _paired_camera_sources."""
        payload = {
            "data": [
                {
                    "device_id": "CB02TESTDEVICE01",
                    "license_id": "UIDCB02",
                    "dev_admin_id": "admin",
                    "dev_admin_pwd": "pwd",
                }
            ],
            "profiles": [
                {"device_id": "CB02TESTDEVICE01", "profile": json.dumps({"baby": "Dragon"})},
                {"device_id": "CB02TESTDEVICE01", "profile": json.dumps({"baby": "Draco Room"})},
            ],
        }
        cameras = _sync_fetch(payload)
        assert len(cameras) == 1
        assert cameras[0]["baby_name"] == "Draco Room"

    def test_profile_without_data_item_is_kept(self):
        """A profile whose device is absent from `data` still yields a camera
        (empty credentials), exactly as the profiles-driven parser did."""
        payload = {
            "data": [],
            "profiles": [
                {"device_id": "ORPHANDEVICE0001", "profile": json.dumps({"baby": "Ghost"})},
            ],
        }
        cameras = _sync_fetch(payload)
        assert len(cameras) == 1
        assert cameras[0]["device_id"] == "ORPHANDEVICE0001"
        assert cameras[0]["baby_name"] == "Ghost"
        assert cameras[0]["uid"] == ""

    def test_duplicate_data_items_emit_once(self):
        payload = {
            "data": [
                {"device_id": "CB02TESTDEVICE01", "license_id": "UID-A"},
                {"device_id": "CB02TESTDEVICE01", "license_id": "UID-B"},
            ],
            "profiles": [],
        }
        cameras = _sync_fetch(payload)
        assert len(cameras) == 1
        assert cameras[0]["uid"] == "UID-A"

    def test_malformed_profile_json_does_not_lose_the_device(self):
        payload = {
            "data": [
                {
                    "device_id": "CB02TESTDEVICE01",
                    "license_id": "UIDCB02",
                    "dev_admin_id": "admin",
                    "dev_admin_pwd": "pwd",
                }
            ],
            "profiles": [
                {"device_id": "CB02TESTDEVICE01", "profile": "{not json"},
            ],
        }
        cameras = _sync_fetch(payload)
        assert len(cameras) == 1
        assert cameras[0]["baby_name"] == "Unknown"
        assert cameras[0]["uid"] == "UIDCB02"
