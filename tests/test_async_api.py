"""Tests for the async CuboAI API functions.

These tests cover the aiohttp-based async API calls.
"""

import pytest
from aioresponses import aioresponses

# Import the async API functions
from custom_components.cuboai.api import async_api


class TestCuboMobileLoginAsync:
    """Test async cubo_mobile_login."""

    @pytest.mark.asyncio
    async def test_sends_correct_request(self):
        """Test that login sends correct payload."""
        with aioresponses() as m:
            m.post(
                "https://mobile-api.getcubo.com/v2/user/login",
                payload={"data": {"token": "xyz"}},
            )

            result = await async_api.cubo_mobile_login(
                uuid="test-uuid",
                username="test@example.com",
                access_token="test-token",
                user_agent="TestAgent/1.0",
            )

            assert result == {"token": "xyz"}

    @pytest.mark.asyncio
    async def test_includes_authorization_header(self):
        """Test that login includes x-cb-authorization header."""
        with aioresponses() as m:
            m.post(
                "https://mobile-api.getcubo.com/v2/user/login",
                payload={"data": {"success": True}},
            )

            await async_api.cubo_mobile_login(
                uuid="test-uuid",
                username="test@example.com",
                access_token="my-access-token",
                user_agent="TestAgent/1.0",
            )

            # Verify request was made
            assert len(m.requests) == 1


class TestRefreshCuboTokenAsync:
    """Test async refresh_cubo_token."""

    @pytest.mark.asyncio
    async def test_sends_refresh_token_header(self):
        """Test that refresh sends token in header."""
        with aioresponses() as m:
            m.post(
                "https://mobile-api.getcubo.com/v1/oauth/token",
                payload={"data": {"access_token": "new-token"}},
            )

            result = await async_api.refresh_cubo_token(
                refresh_token="my-refresh-token",
                user_agent="TestAgent/1.0",
            )

            assert result == {"access_token": "new-token"}

    @pytest.mark.asyncio
    async def test_returns_full_response_when_no_data(self):
        """Test returns full response when no data field."""
        with aioresponses() as m:
            m.post(
                "https://mobile-api.getcubo.com/v1/oauth/token",
                payload={"access_token": "direct-token"},
            )

            result = await async_api.refresh_cubo_token(
                refresh_token="my-refresh-token",
                user_agent="TestAgent/1.0",
            )

            assert result == {"access_token": "direct-token"}


class TestGetCameraProfilesAsync:
    """Test async get_camera_profiles."""

    @pytest.mark.asyncio
    async def test_returns_device_map(self):
        """Test that camera profiles returns baby -> device_id mapping."""
        import json

        with aioresponses() as m:
            m.get(
                "https://api.getcubo.com/prod/user/cameras",
                payload={
                    "profiles": [
                        {
                            "device_id": "device-123",
                            "profile": json.dumps({"baby": "Emma"}),
                        },
                        {
                            "device_id": "device-456",
                            "profile": json.dumps({"baby": "Liam"}),
                        },
                    ]
                },
            )

            result = await async_api.get_camera_profiles(
                access_token="test-token",
                user_agent="TestAgent/1.0",
            )

            assert result == {"Emma": "device-123", "Liam": "device-456"}

    @pytest.mark.asyncio
    async def test_handles_empty_profiles(self):
        """Test handling of empty profiles list."""
        with aioresponses() as m:
            m.get(
                "https://api.getcubo.com/prod/user/cameras",
                payload={"profiles": []},
            )

            result = await async_api.get_camera_profiles(
                access_token="test-token",
                user_agent="TestAgent/1.0",
            )

            assert result == {}


class TestGetCameraProfilesRawAsync:
    """Test async get_camera_profiles_raw."""

    @pytest.mark.asyncio
    async def test_returns_raw_profiles(self):
        """Test that raw profiles returns full profile list."""
        profiles = [
            {"device_id": "dev-1", "profile": '{"baby": "Test"}'},
            {"device_id": "dev-2", "profile": '{"baby": "Test2"}'},
        ]
        with aioresponses() as m:
            m.get(
                "https://api.getcubo.com/prod/user/cameras",
                payload={"profiles": profiles},
            )

            result = await async_api.get_camera_profiles_raw(
                access_token="test-token",
                user_agent="TestAgent/1.0",
            )

            assert result == profiles


class TestGetNAlertsPagedAsync:
    """Test async get_n_alerts_paged."""

    @pytest.mark.asyncio
    async def test_returns_normalized_alerts(self):
        """Test that alerts are normalized properly."""
        import re

        with aioresponses() as m:
            # Use pattern to match URL with any since parameter
            pattern = re.compile(r"^https://api\.getcubo\.com/prod/timeline/alerts\?since=\d+$")
            m.get(
                pattern,
                payload={
                    "data": [
                        {
                            "id": "alert-1",
                            "device_id": "device-123",
                            "type": "cry",
                            "ts": 1700000000,
                            "created": "2024-01-01",
                            "image": "http://example.com/img.jpg",
                            "params": '{"level": "high"}',
                            "profile": "baby-profile",
                            "region": "us-east-1",
                        }
                    ]
                },
                repeat=True,
            )

            result = await async_api.get_n_alerts_paged(
                device_id="device-123",
                access_token="test-token",
                user_agent="TestAgent/1.0",
                n=5,
                hours_back=12,
            )

            assert len(result) == 1
            assert result[0]["id"] == "alert-1"
            assert result[0]["type"] == "cry"
            assert result[0]["device_id"] == "device-123"

    @pytest.mark.asyncio
    async def test_filters_by_device_id(self):
        """Test that alerts are filtered by device_id."""
        import re

        with aioresponses() as m:
            pattern = re.compile(r"^https://api\.getcubo\.com/prod/timeline/alerts\?since=\d+$")
            m.get(
                pattern,
                payload={
                    "data": [
                        {"id": "alert-1", "device_id": "device-123", "ts": 1700000000},
                        {"id": "alert-2", "device_id": "other-device", "ts": 1700000001},
                    ]
                },
                repeat=True,
            )

            result = await async_api.get_n_alerts_paged(
                device_id="device-123",
                access_token="test-token",
                user_agent="TestAgent/1.0",
                n=5,
                hours_back=12,
            )

            assert len(result) == 1
            assert result[0]["device_id"] == "device-123"


class TestGetSubscriptionInfoAsync:
    """Test async get_subscription_info."""

    @pytest.mark.asyncio
    async def test_returns_subscription_data(self):
        """Test that subscription info is returned correctly."""
        with aioresponses() as m:
            m.get(
                "https://api.getcubo.com/prod/services/v1/subscribed",
                payload={
                    "result": [
                        {
                            "status": "active",
                            "kind": "premium",
                            "service_id": "svc-123",
                            "device_id": "dev-123",
                            "platform": "android",
                            "service_start_date": "2024-01-01",
                            "service_end_date": "2025-01-01",
                            "grace_period_stop_date": None,
                            "auto_renewal": True,
                            "note": None,
                            "created": "2024-01-01",
                            "order_id": "order-123",
                        }
                    ]
                },
            )

            result = await async_api.get_subscription_info(
                access_token="test-token",
                user_agent="TestAgent/1.0",
            )

            assert result["status"] == "active"
            assert result["kind"] == "premium"

    @pytest.mark.asyncio
    async def test_returns_none_for_empty_result(self):
        """Test returns None when no subscription."""
        with aioresponses() as m:
            m.get(
                "https://api.getcubo.com/prod/services/v1/subscribed",
                payload={"result": []},
            )

            result = await async_api.get_subscription_info(
                access_token="test-token",
                user_agent="TestAgent/1.0",
            )

            assert result is None


class TestGetCameraStateAsync:
    """Test async get_camera_state."""

    @pytest.mark.asyncio
    async def test_returns_camera_state(self):
        """Test that camera state is returned."""
        with aioresponses() as m:
            m.get(
                "https://api.getcubo.com/prod/camera/state?device_id=device-123",
                payload={"state": "online", "last_seen": 1700000000},
            )

            result = await async_api.get_camera_state(
                device_id="device-123",
                access_token="test-token",
                user_agent="TestAgent/1.0",
            )

            assert result["state"] == "online"


class TestNormalizeAlert:
    """Test _normalize_alert helper."""

    def test_normalizes_string_params(self):
        """Test that string params are parsed as JSON."""
        alert = {
            "id": "alert-1",
            "device_id": "dev-1",
            "type": "cry",
            "ts": 1700000000,
            "created": "2024-01-01",
            "image": "http://example.com/img.jpg",
            "params": '{"level": "high"}',
            "profile": "profile-1",
            "region": "us-east-1",
        }

        result = async_api._normalize_alert(alert)

        assert result["params"] == {"level": "high"}

    def test_keeps_dict_params(self):
        """Test that dict params stay as dict."""
        alert = {
            "id": "alert-1",
            "params": {"level": "high"},
        }

        result = async_api._normalize_alert(alert)

        assert result["params"] == {"level": "high"}

    def test_handles_invalid_json_params(self):
        """Test that invalid JSON params stay as string."""
        alert = {
            "id": "alert-1",
            "params": "not-valid-json",
        }

        result = async_api._normalize_alert(alert)

        assert result["params"] == "not-valid-json"
