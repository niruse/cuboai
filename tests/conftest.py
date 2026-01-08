"""Pytest configuration and fixtures for CuboAI tests."""

from unittest.mock import MagicMock

import pytest


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
