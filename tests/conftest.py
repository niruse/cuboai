"""Pytest configuration and fixtures for CuboAI tests.

This module sets up the necessary mocks for Home Assistant modules
so that cuboai components can be imported in tests.
"""

import datetime
import sys
from types import ModuleType
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
sys.modules["homeassistant.helpers.config_validation"] = MagicMock()


# Entity base classes and exceptions must be REAL objects, not MagicMock attributes:
# the platform modules subclass them (and combine several by multiple inheritance, so
# they must also be distinct classes), and `except HomeAssistantError` needs a real
# exception type. Registering them here rather than in individual test modules keeps
# it independent of the order pytest happens to import tests in — a stub installed by
# one test module was otherwise visible to, and could break, every later one.
def _ha_module(name, **attrs):
    """Ensure sys.modules[name] exists and carries `attrs`, without clobbering
    anything a module already there has defined."""
    mod = sys.modules.get(name)
    if not isinstance(mod, ModuleType):
        mod = ModuleType(name)
        sys.modules[name] = mod
    for key, value in attrs.items():
        if not hasattr(mod, key):
            setattr(mod, key, value)
    return mod


def _entity_base(name):
    return type(name, (), {"__init__": lambda self, *a, **k: None})


class _CoordinatorEntity:
    """Mirrors the one behaviour the platform modules rely on from the real base:
    `super().__init__(coordinator)` makes `self.coordinator` available."""

    def __init__(self, coordinator=None, context=None):
        self.coordinator = coordinator


class _DataUpdateCoordinator:
    def __init__(self, hass=None, logger=None, *, name=None, update_interval=None, **kwargs):
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_interval = update_interval


_ha_module("homeassistant.components")
_ha_module("homeassistant.components.switch", SwitchEntity=_entity_base("SwitchEntity"))
_ha_module("homeassistant.helpers.restore_state", RestoreEntity=_entity_base("RestoreEntity"))
_ha_module(
    "homeassistant.helpers.update_coordinator",
    CoordinatorEntity=_CoordinatorEntity,
    DataUpdateCoordinator=_DataUpdateCoordinator,
    UpdateFailed=type("UpdateFailed", (Exception,), {}),
)
_ha_module(
    "homeassistant.components.number",
    NumberEntity=_entity_base("NumberEntity"),
    RestoreNumber=_entity_base("RestoreNumber"),
)
_ha_module(
    "homeassistant.components.media_player",
    MediaPlayerEntity=_entity_base("MediaPlayerEntity"),
    MediaPlayerEntityFeature=MagicMock(),
    MediaPlayerState=MagicMock(),
    MediaType=MagicMock(),
    RepeatMode=MagicMock(),
)
_ha_module("homeassistant.helpers.entity_registry", async_get=MagicMock())
_ha_module("homeassistant.helpers.dispatcher", async_dispatcher_send=MagicMock(), async_dispatcher_connect=MagicMock())
_ha_module("homeassistant.exceptions", HomeAssistantError=RuntimeError)
_ha_module("homeassistant.util")
_ha_module("homeassistant.util.dt", utcnow=lambda: datetime.datetime.now(datetime.UTC))

# Mock cuboai utils to avoid file I/O during tests
_mock_utils = MagicMock()
_mock_utils.log_to_file = MagicMock()
sys.modules["custom_components.cuboai.utils"] = _mock_utils


# =============================================================================
# aiohttp >= 3.14 compatibility shim for aioresponses
# =============================================================================
# aiohttp 3.14 added a REQUIRED keyword-only 'stream_writer' argument to
# ClientResponse.__init__, and (with writer=None, which aioresponses passes)
# reads stream_writer.output_size from it. aioresponses <= 0.7.9 (the latest
# release) doesn't pass it, so every mocked request dies with
# "TypeError: ClientResponse.__init__() missing ... 'stream_writer'".
# Make the argument optional with a zero-size stub. Remove once aioresponses
# supports aiohttp >= 3.14.
import inspect

import aiohttp

_resp_init = aiohttp.ClientResponse.__init__
if "stream_writer" in inspect.signature(_resp_init).parameters:

    class _StubStreamWriter:
        output_size = 0

    def _resp_init_compat(self, *args, **kwargs):
        kwargs.setdefault("stream_writer", _StubStreamWriter())
        return _resp_init(self, *args, **kwargs)

    aiohttp.ClientResponse.__init__ = _resp_init_compat


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
