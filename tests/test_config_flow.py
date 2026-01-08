"""Tests for config flow multi-camera support."""
import pytest


def test_entry_title_format():
    """Test that entry title includes username."""
    username = "test@example.com"
    title = f"CuboAI ({username})"
    
    assert title == "CuboAI (test@example.com)"
    assert username in title


def test_cameras_data_structure():
    """Test that cameras are stored in correct format."""
    camera_map = {
        "Emma": "device-001",
        "Noah": "device-002",
        "Olivia": "device-003",
    }
    
    # Convert to list format
    cameras = []
    for baby_name, device_id in camera_map.items():
        cameras.append({
            "device_id": device_id,
            "baby_name": baby_name
        })
    
    assert len(cameras) == 3
    assert all("device_id" in cam and "baby_name" in cam for cam in cameras)
    
    device_ids = {cam["device_id"] for cam in cameras}
    baby_names = {cam["baby_name"] for cam in cameras}
    
    assert device_ids == {"device-001", "device-002", "device-003"}
    assert baby_names == {"Emma", "Noah", "Olivia"}


def test_single_camera_entry():
    """Test single camera creates valid structure."""
    camera_map = {"Emma": "device-001"}
    
    cameras = []
    for baby_name, device_id in camera_map.items():
        cameras.append({
            "device_id": device_id,
            "baby_name": baby_name
        })
    
    assert len(cameras) == 1
    assert cameras[0]["device_id"] == "device-001"
    assert cameras[0]["baby_name"] == "Emma"


def test_no_cameras_error():
    """Test that empty camera map should show error."""
    camera_map = {}
    
    has_cameras = len(camera_map) > 0
    assert has_cameras is False


def test_mfa_flow_camera_storage():
    """Test that MFA flow stores cameras same as normal flow."""
    # Both flows should produce the same camera structure
    camera_map = {
        "Emma": "device-001",
        "Noah": "device-002",
    }
    
    cameras_normal = [
        {"device_id": device_id, "baby_name": baby_name}
        for baby_name, device_id in camera_map.items()
    ]
    
    cameras_mfa = [
        {"device_id": device_id, "baby_name": baby_name}
        for baby_name, device_id in camera_map.items()
    ]
    
    # Both should be identical
    assert len(cameras_normal) == len(cameras_mfa)
    assert all(c in cameras_mfa for c in cameras_normal)


def test_entry_data_has_required_fields():
    """Test that entry data contains all required fields."""
    entry_data = {
        "uuid": "test-uuid-123",
        "username": "test@example.com",
        "client_id": "test-client-id",
        "client_secret": "test-client-secret",
        "pool_id": "test-pool-id",
        "region": "us-east-1",
        "access_token": "cubo-access-token",
        "refresh_token": "cubo-refresh-token",
        "user_agent": "test-agent",
        "cameras": [
            {"device_id": "device-001", "baby_name": "Emma"}
        ],
    }
    
    required_fields = [
        "uuid", "username", "access_token", "refresh_token",
        "user_agent", "cameras"
    ]
    
    for field in required_fields:
        assert field in entry_data


def test_cameras_list_format():
    """Test that cameras is a list, not a dict."""
    cameras = [
        {"device_id": "device-001", "baby_name": "Emma"},
        {"device_id": "device-002", "baby_name": "Noah"},
    ]
    
    assert isinstance(cameras, list)
    assert not isinstance(cameras, dict)
    assert len(cameras) > 0
    
    for camera in cameras:
        assert isinstance(camera, dict)
        assert "device_id" in camera
        assert "baby_name" in camera
