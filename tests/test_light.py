"""Tests for the CuboAI light entity."""

import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Mock homeassistant before importing
sys.modules["homeassistant"] = MagicMock()
sys.modules["homeassistant.components"] = MagicMock()
sys.modules["homeassistant.components.light"] = MagicMock()
sys.modules["homeassistant.components.light"].ColorMode = MagicMock()
sys.modules["homeassistant.components.light"].LightEntity = object
sys.modules["homeassistant.helpers"] = MagicMock()
sys.modules["homeassistant.helpers.entity"] = MagicMock()
sys.modules["homeassistant.helpers.entity"].DeviceInfo = dict

from custom_components.cuboai.light import CuboNightLight
from custom_components.cuboai.const import DOMAIN


@pytest.fixture
def mock_tutk_client():
    """Mock the TutkClient."""
    with patch("custom_components.cuboai.light.TutkClient") as mock_client:
        mock_instance = MagicMock()
        mock_client.return_value = mock_instance
        yield mock_instance


class TestCuboNightLight:
    """Test the CuboNightLight entity."""

    def test_entity_properties(self):
        """Test basic properties of the light entity."""
        hass = MagicMock()
        light = CuboNightLight(
            hass=hass,
            baby_name="Emma",
            uid="device-123",
            license_id="lic-123",
            dev_admin_id="admin-1",
            dev_admin_pwd="pwd-1",
        )

        assert light._attr_name == "Night Light"
        assert light._attr_unique_id == "cuboai_nightlight_device-123"
        assert light.is_on is None
        
        device_info = light.device_info
        assert device_info["identifiers"] == {(DOMAIN, "device-123")}
        assert device_info["name"] == "CuboAI Emma"
        assert device_info["manufacturer"] == "CuboAI"

    @pytest.mark.asyncio
    async def test_async_turn_on(self, mock_tutk_client):
        """Test turning on the nightlight."""
        hass = MagicMock()
        light = CuboNightLight(
            hass=hass,
            baby_name="Emma",
            uid="device-123",
            license_id="lic-123",
            dev_admin_id="admin-1",
            dev_admin_pwd="pwd-1",
        )
        
        # Mock the run_in_executor to execute synchronously for the test
        async def _mock_run(executor, func):
            return func()
            
        with patch("asyncio.get_running_loop") as mock_loop:
            mock_loop_instance = MagicMock()
            mock_loop_instance.run_in_executor = AsyncMock(side_effect=_mock_run)
            mock_loop.return_value = mock_loop_instance
            
            mock_tutk_client.set_night_light_status.return_value = True
            
            await light.async_turn_on()
            
            assert light.is_on is True
            mock_tutk_client.connect.assert_called_once()
            mock_tutk_client.set_night_light_status.assert_called_once_with(True)
            mock_tutk_client.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_turn_off(self, mock_tutk_client):
        """Test turning off the nightlight."""
        hass = MagicMock()
        light = CuboNightLight(
            hass=hass,
            baby_name="Emma",
            uid="device-123",
            license_id="lic-123",
            dev_admin_id="admin-1",
            dev_admin_pwd="pwd-1",
        )
        
        # Mock the run_in_executor to execute synchronously for the test
        async def _mock_run(executor, func):
            return func()
            
        with patch("asyncio.get_running_loop") as mock_loop:
            mock_loop_instance = MagicMock()
            mock_loop_instance.run_in_executor = AsyncMock(side_effect=_mock_run)
            mock_loop.return_value = mock_loop_instance
            
            mock_tutk_client.set_night_light_status.return_value = True
            
            await light.async_turn_off()
            
            assert light.is_on is False
            mock_tutk_client.set_night_light_status.assert_called_once_with(False)

    @pytest.mark.asyncio
    async def test_async_update(self, mock_tutk_client):
        """Test fetching the state of the nightlight."""
        hass = MagicMock()
        light = CuboNightLight(
            hass=hass,
            baby_name="Emma",
            uid="device-123",
            license_id="lic-123",
            dev_admin_id="admin-1",
            dev_admin_pwd="pwd-1",
        )
        
        # Mock the run_in_executor to execute synchronously for the test
        async def _mock_run(executor, func):
            return func()
            
        with patch("asyncio.get_running_loop") as mock_loop:
            mock_loop_instance = MagicMock()
            mock_loop_instance.run_in_executor = AsyncMock(side_effect=_mock_run)
            mock_loop.return_value = mock_loop_instance
            
            mock_tutk_client.get_night_light_status.return_value = True
            
            await light.async_update()
            
            assert light.is_on is True
            mock_tutk_client.get_night_light_status.assert_called_once()
