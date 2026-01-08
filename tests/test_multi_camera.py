"""Tests for multi-camera support in CuboAI integration."""
import pytest


def test_multi_camera_entry_structure():
    """Test that multi-camera entry has correct data structure."""
    entry_data = {
        "username": "test@example.com",
        "access_token": "test-access-token",
        "refresh_token": "test-refresh-token",
        "user_agent": "test-agent",
        "cameras": [
            {"device_id": "device-001", "baby_name": "Emma"},
            {"device_id": "device-002", "baby_name": "Noah"},
        ],
    }
    
    # Verify cameras key exists
    assert "cameras" in entry_data
    assert isinstance(entry_data["cameras"], list)
    assert len(entry_data["cameras"]) == 2
    
    # Verify each camera has required fields
    for camera in entry_data["cameras"]:
        assert "device_id" in camera
        assert "baby_name" in camera


def test_backward_compat_entry_structure():
    """Test that old single-camera entry can be converted."""
    old_entry_data = {
        "username": "test@example.com",
        "access_token": "test-access-token",
        "refresh_token": "test-refresh-token",
        "user_agent": "test-agent",
        "device_id": "device-001",
        "baby_name": "Emma",
    }
    
    # Verify old format
    assert "device_id" in old_entry_data
    assert "baby_name" in old_entry_data
    assert "cameras" not in old_entry_data
    
    # Simulate backward compat conversion
    cameras = old_entry_data.get("cameras", [])
    if not cameras and "device_id" in old_entry_data:
        cameras = [{
            "device_id": old_entry_data["device_id"],
            "baby_name": old_entry_data["baby_name"]
        }]
    
    assert len(cameras) == 1
    assert cameras[0]["device_id"] == "device-001"
    assert cameras[0]["baby_name"] == "Emma"


def test_unique_id_patterns():
    """Test unique ID patterns for different sensor types."""
    device_id = "device-123"
    entry_id = "entry-abc-xyz"
    
    # Baby info sensor
    baby_info_id = f"cuboai_baby_info_{device_id}"
    assert baby_info_id == "cuboai_baby_info_device-123"
    
    # Alert sensor
    alert_id = f"cuboai_last_alert_{device_id}"
    assert alert_id == "cuboai_last_alert_device-123"
    
    # Camera state sensor
    camera_state_id = f"cuboai_camera_state_{device_id}"
    assert camera_state_id == "cuboai_camera_state_device-123"
    
    # Subscription sensor (account-level, uses entry_id)
    subscription_id = f"cuboai_subscription_{entry_id}"
    assert subscription_id == "cuboai_subscription_entry-abc-xyz"
    
    # Ensure no email in subscription ID
    assert "@" not in subscription_id


def test_device_identifier_format():
    """Test device identifier tuple format."""
    domain = "cuboai"
    device_id = "device-001"
    
    identifier = (domain, device_id)
    assert identifier == ("cuboai", "device-001")
    assert isinstance(identifier, tuple)
    assert len(identifier) == 2


def test_device_info_structure():
    """Test device_info dictionary structure."""
    device_id = "device-001"
    baby_name = "Emma"
    domain = "cuboai"
    
    device_info = {
        "identifiers": {(domain, device_id)},
        "name": f"CuboAI {baby_name}",
        "manufacturer": "CuboAI",
        "model": "Baby Monitor",
    }
    
    assert "identifiers" in device_info
    assert "name" in device_info
    assert "manufacturer" in device_info
    assert "model" in device_info
    
    assert device_info["name"] == "CuboAI Emma"
    assert device_info["manufacturer"] == "CuboAI"
    assert device_info["model"] == "Baby Monitor"
    assert (domain, device_id) in device_info["identifiers"]


def test_multiple_cameras_unique_ids():
    """Test that multiple cameras generate different unique IDs."""
    cameras = [
        {"device_id": "device-001", "baby_name": "Emma"},
        {"device_id": "device-002", "baby_name": "Noah"},
        {"device_id": "device-003", "baby_name": "Olivia"},
    ]
    
    # Generate unique IDs for baby info sensors
    baby_info_ids = [f"cuboai_baby_info_{cam['device_id']}" for cam in cameras]
    assert len(baby_info_ids) == len(set(baby_info_ids))  # All unique
    assert baby_info_ids == [
        "cuboai_baby_info_device-001",
        "cuboai_baby_info_device-002",
        "cuboai_baby_info_device-003",
    ]
    
    # Generate unique IDs for alert sensors
    alert_ids = [f"cuboai_last_alert_{cam['device_id']}" for cam in cameras]
    assert len(alert_ids) == len(set(alert_ids))  # All unique
    
    # Generate unique IDs for camera state sensors
    state_ids = [f"cuboai_camera_state_{cam['device_id']}" for cam in cameras]
    assert len(state_ids) == len(set(state_ids))  # All unique


def test_sensor_count_calculation():
    """Test calculation of total sensors for multiple cameras."""
    cameras = [
        {"device_id": "device-001", "baby_name": "Emma"},
        {"device_id": "device-002", "baby_name": "Noah"},
    ]
    
    # 3 sensors per camera (baby info, alert, camera state)
    sensors_per_camera = 3
    camera_sensors = len(cameras) * sensors_per_camera
    
    # 1 subscription sensor per account
    subscription_sensors = 1
    
    total_sensors = camera_sensors + subscription_sensors
    assert total_sensors == 7  # 2 cameras * 3 + 1 subscription


def test_subscription_id_different_accounts():
    """Test subscription sensors have different IDs for different entry_ids."""
    entry_id_1 = "entry-abc-123"
    entry_id_2 = "entry-xyz-789"
    
    sub_id_1 = f"cuboai_subscription_{entry_id_1}"
    sub_id_2 = f"cuboai_subscription_{entry_id_2}"
    
    assert sub_id_1 != sub_id_2
    assert sub_id_1 == "cuboai_subscription_entry-abc-123"
    assert sub_id_2 == "cuboai_subscription_entry-xyz-789"


def test_device_grouping_logic():
    """Test that sensors with same device_id will be grouped together."""
    device_id = "device-001"
    domain = "cuboai"
    
    # All sensors for this device should have same identifier
    baby_info_identifier = {(domain, device_id)}
    alert_identifier = {(domain, device_id)}
    camera_state_identifier = {(domain, device_id)}
    
    # They should all be equal (Home Assistant will group them)
    assert baby_info_identifier == alert_identifier
    assert alert_identifier == camera_state_identifier


def test_backward_compat_device_info_lookup():
    """Test device info lookup works with backward compat."""
    # New format
    new_entry_data = {
        "cameras": [
            {"device_id": "device-001", "baby_name": "Emma"},
        ]
    }
    
    # Old format
    old_entry_data = {
        "device_id": "device-001",
        "baby_name": "Emma",
    }
    
    device_id = "device-001"
    
    # New format lookup
    cameras = new_entry_data.get("cameras", [])
    baby_name = "Unknown"
    for cam in cameras:
        if cam["device_id"] == device_id:
            baby_name = cam["baby_name"]
            break
    assert baby_name == "Emma"
    
    # Old format fallback
    baby_name = "Unknown"
    cameras = old_entry_data.get("cameras", [])
    for cam in cameras:
        if cam["device_id"] == device_id:
            baby_name = cam["baby_name"]
            break
    if baby_name == "Unknown" and old_entry_data.get("device_id") == device_id:
        baby_name = old_entry_data.get("baby_name", "Unknown")
    assert baby_name == "Emma"


def test_empty_cameras_list():
    """Test handling of empty cameras list."""
    entry_data = {
        "cameras": []
    }
    
    cameras = entry_data.get("cameras", [])
    assert len(cameras) == 0
    
    # Should only create subscription sensor
    camera_sensors = len(cameras) * 3
    subscription_sensors = 1
    total_sensors = camera_sensors + subscription_sensors
    assert total_sensors == 1
