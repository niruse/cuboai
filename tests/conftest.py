"""Pytest configuration and fixtures for CuboAI tests.

This module sets up the necessary mocks for Home Assistant modules
so that cuboai components can be imported in tests.
"""

import sys
from unittest.mock import MagicMock

import pytest

# =============================================================================
# Mock Home Assistant modules BEFORE any cuboai imports
# This must happen at module level, before pytest collects tests
# =============================================================================

# Mock homeassistant core modules
sys.modules["homeassistant"] = MagicMock()
sys.modules["homeassistant.config_entries"] = MagicMock()
sys.modules["homeassistant.core"] = MagicMock()
sys.modules["homeassistant.const"] = MagicMock()
sys.modules["homeassistant.helpers"] = MagicMock()
sys.modules["homeassistant.helpers.entity"] = MagicMock()

# Mock cuboai utils to avoid file I/O during tests
_mock_utils = MagicMock()
_mock_utils.log_to_file = MagicMock()
sys.modules["custom_components.cuboai.utils"] = _mock_utils


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_cognito_client():
    """Create a mock boto3 cognito-idp client."""
    client = MagicMock()
    return client


@pytest.fixture
def mock_tokens():
    """Standard token response from Cognito."""
    return {
        "IdToken": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ0ZXN0LXV1aWQtMTIzNCJ9.test",
        "AccessToken": "test-access-token-12345",
        "RefreshToken": "test-refresh-token-67890",
    }


@pytest.fixture
def mock_mfa_challenge():
    """MFA challenge response from Cognito."""
    return {
        "ChallengeName": "SMS_MFA",
        "Session": "test-session-abc123",
        "ChallengeParameters": {
            "CODE_DELIVERY_DESTINATION": "+1******1234",
            "CODE_DELIVERY_DELIVERY_MEDIUM": "SMS",
        },
    }


@pytest.fixture
def mock_software_token_mfa_challenge():
    """Software token MFA challenge response."""
    return {
        "ChallengeName": "SOFTWARE_TOKEN_MFA",
        "Session": "test-session-totp-456",
        "ChallengeParameters": {},
    }


@pytest.fixture
def mock_cameras():
    """Sample multi-camera response."""
    return [
        {"device_id": "device-001", "baby_name": "Baby Emma"},
        {"device_id": "device-002", "baby_name": "Baby Noah"},
    ]
