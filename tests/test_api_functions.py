"""Tests for CuboAI API functions.

These tests cover token storage, API calls, and utility functions.
"""

import os
import tempfile
from unittest.mock import MagicMock, patch

# Import the actual functions from the cuboai module
from custom_components.cuboai.api import cuboai_functions


class TestSetTokenPaths:
    """Test token path configuration."""

    def test_sets_access_token_path(self):
        """set_token_paths should set ACCESS_TOKEN_FILE."""
        cuboai_functions.set_token_paths("/test/config")
        assert "cuboai_access_token.json" in cuboai_functions.ACCESS_TOKEN_FILE

    def test_sets_refresh_token_path(self):
        """set_token_paths should set REFRESH_TOKEN_FILE."""
        cuboai_functions.set_token_paths("/test/config")
        assert "cuboai_refresh_token.json" in cuboai_functions.REFRESH_TOKEN_FILE

    def test_handles_trailing_slash(self):
        """Should handle paths with or without trailing slash."""
        cuboai_functions.set_token_paths("/test/config/")
        # os.path.join handles this correctly
        assert "cuboai_access_token.json" in cuboai_functions.ACCESS_TOKEN_FILE


class TestGetAccessTokenPath:
    """Test access token path resolution with fallback."""

    def setup_method(self):
        """Reset token paths before each test."""
        cuboai_functions.ACCESS_TOKEN_FILE = None
        cuboai_functions.REFRESH_TOKEN_FILE = None

    def test_returns_portable_path_when_exists(self):
        """When portable path exists, return it."""
        cuboai_functions.ACCESS_TOKEN_FILE = "/portable/path/token.json"

        with patch("os.path.exists") as mock_exists:
            mock_exists.return_value = True
            path = cuboai_functions._get_access_token_path()

        assert path == "/portable/path/token.json"

    def test_returns_legacy_path_when_portable_missing(self):
        """When portable doesn't exist but legacy does, return legacy."""
        cuboai_functions.ACCESS_TOKEN_FILE = "/portable/path/token.json"

        def mock_exists(p):
            return p == cuboai_functions.LEGACY_ACCESS_TOKEN_FILE

        with patch("os.path.exists", side_effect=mock_exists):
            path = cuboai_functions._get_access_token_path()

        assert path == cuboai_functions.LEGACY_ACCESS_TOKEN_FILE

    def test_returns_portable_for_new_installation(self):
        """When neither exists, return portable path (for writing)."""
        cuboai_functions.ACCESS_TOKEN_FILE = "/portable/path/token.json"

        with patch("os.path.exists", return_value=False):
            path = cuboai_functions._get_access_token_path()

        assert path == "/portable/path/token.json"

    def test_returns_legacy_when_no_portable_configured(self):
        """When ACCESS_TOKEN_FILE is None, return legacy."""
        cuboai_functions.ACCESS_TOKEN_FILE = None

        with patch("os.path.exists", return_value=False):
            path = cuboai_functions._get_access_token_path()

        assert path == cuboai_functions.LEGACY_ACCESS_TOKEN_FILE


class TestGetRefreshTokenPath:
    """Test refresh token path resolution with fallback."""

    def setup_method(self):
        """Reset token paths before each test."""
        cuboai_functions.ACCESS_TOKEN_FILE = None
        cuboai_functions.REFRESH_TOKEN_FILE = None

    def test_returns_portable_path_when_exists(self):
        """When portable path exists, return it."""
        cuboai_functions.REFRESH_TOKEN_FILE = "/portable/path/refresh.json"

        with patch("os.path.exists") as mock_exists:
            mock_exists.return_value = True
            path = cuboai_functions._get_refresh_token_path()

        assert path == "/portable/path/refresh.json"

    def test_returns_legacy_when_portable_missing(self):
        """When portable doesn't exist but legacy does, return legacy."""
        cuboai_functions.REFRESH_TOKEN_FILE = "/portable/path/refresh.json"

        def mock_exists(p):
            return p == cuboai_functions.LEGACY_REFRESH_TOKEN_FILE

        with patch("os.path.exists", side_effect=mock_exists):
            path = cuboai_functions._get_refresh_token_path()

        assert path == cuboai_functions.LEGACY_REFRESH_TOKEN_FILE


class TestTokenSaveLoad:
    """Test token save/load functions with real file I/O."""

    def test_save_and_load_access_token(self):
        """Should save and load access token correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            token_path = os.path.join(tmpdir, "access_token.json")
            cuboai_functions.ACCESS_TOKEN_FILE = token_path

            with patch("os.path.exists", return_value=True):
                cuboai_functions.save_access_token("test-access-token-123")
                loaded = cuboai_functions.load_access_token()

            assert loaded == "test-access-token-123"

    def test_save_and_load_refresh_token(self):
        """Should save and load refresh token correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            token_path = os.path.join(tmpdir, "refresh_token.json")
            cuboai_functions.REFRESH_TOKEN_FILE = token_path

            with patch("os.path.exists", return_value=True):
                cuboai_functions.save_refresh_token("test-refresh-token-456")
                loaded = cuboai_functions.load_refresh_token()

            assert loaded == "test-refresh-token-456"

    def test_load_access_token_returns_none_on_missing_file(self):
        """Should return None when token file doesn't exist."""
        cuboai_functions.ACCESS_TOKEN_FILE = "/nonexistent/path/token.json"

        with patch("os.path.exists", return_value=False):
            result = cuboai_functions.load_access_token()

        assert result is None

    def test_load_refresh_token_returns_none_on_missing_file(self):
        """Should return None when token file doesn't exist."""
        cuboai_functions.REFRESH_TOKEN_FILE = "/nonexistent/path/token.json"

        with patch("os.path.exists", return_value=False):
            result = cuboai_functions.load_refresh_token()

        assert result is None


class TestDecodeIdToken:
    """Test JWT ID token decoding."""

    def test_decodes_sub_claim(self):
        """Should decode the 'sub' claim from ID token."""
        # Create a valid JWT structure (header.payload.signature)
        # Payload: {"sub": "user-uuid-12345"}
        import base64

        header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip("=")
        payload = base64.urlsafe_b64encode(b'{"sub":"user-uuid-12345"}').decode().rstrip("=")
        token = f"{header}.{payload}.fake-signature"

        result = cuboai_functions.decode_id_token(token)
        assert result == "user-uuid-12345"

    def test_returns_none_for_missing_sub(self):
        """Should return None if 'sub' claim is missing."""
        import base64

        header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip("=")
        payload = base64.urlsafe_b64encode(b'{"name":"test"}').decode().rstrip("=")
        token = f"{header}.{payload}.fake-signature"

        result = cuboai_functions.decode_id_token(token)
        assert result is None


class TestCuboMobileLogin:
    """Test CuboAI mobile login API call."""

    def test_sends_correct_request(self):
        """Should send correct request to CuboAI API."""
        with patch("custom_components.cuboai.api.cuboai_functions.requests.post") as mock_post:
            mock_post.return_value.json.return_value = {"data": {"token": "result"}}
            mock_post.return_value.raise_for_status = MagicMock()

            result = cuboai_functions.cubo_mobile_login(
                uuid="test-uuid",
                username="test@example.com",
                access_token="test-access-token",
                user_agent="Test-Agent",
            )

            mock_post.assert_called_once()
            call_args = mock_post.call_args
            assert call_args[0][0] == "https://mobile-api.getcubo.com/v2/user/login"
            assert "x-cb-authorization" in call_args[1]["headers"]

    def test_returns_data_field(self):
        """Should return the 'data' field from response."""
        with patch("custom_components.cuboai.api.cuboai_functions.requests.post") as mock_post:
            mock_post.return_value.json.return_value = {"data": {"access_token": "new-token"}}
            mock_post.return_value.raise_for_status = MagicMock()

            result = cuboai_functions.cubo_mobile_login(
                uuid="test-uuid",
                username="test@example.com",
                access_token="test-access-token",
                user_agent="Test-Agent",
            )

            assert result == {"access_token": "new-token"}


class TestRefreshCuboToken:
    """Test token refresh API call."""

    def test_sends_refresh_token_in_header(self):
        """Should send refresh token in x-refresh-authorization header."""
        with patch("custom_components.cuboai.api.cuboai_functions.requests.post") as mock_post:
            mock_post.return_value.json.return_value = {"data": {"access_token": "new"}}
            mock_post.return_value.raise_for_status = MagicMock()

            cuboai_functions.refresh_cubo_token("my-refresh-token", "Test-Agent")

            call_args = mock_post.call_args
            headers = call_args[1]["headers"]
            assert headers["x-refresh-authorization"] == "Bearer my-refresh-token"

    def test_returns_data_field_when_present(self):
        """Should return 'data' field if present."""
        with patch("custom_components.cuboai.api.cuboai_functions.requests.post") as mock_post:
            mock_post.return_value.json.return_value = {"data": {"access_token": "new-token"}}
            mock_post.return_value.raise_for_status = MagicMock()

            result = cuboai_functions.refresh_cubo_token("refresh", "agent")

            assert result == {"access_token": "new-token"}

    def test_returns_full_response_when_no_data_field(self):
        """Should return full response if no 'data' field."""
        with patch("custom_components.cuboai.api.cuboai_functions.requests.post") as mock_post:
            mock_post.return_value.json.return_value = {"access_token": "direct-token"}
            mock_post.return_value.raise_for_status = MagicMock()

            result = cuboai_functions.refresh_cubo_token("refresh", "agent")

            assert result == {"access_token": "direct-token"}


class TestGetCameraProfiles:
    """Test camera profiles API call."""

    def test_sends_correct_authorization(self):
        """Should send access token in x-cspp-authorization header."""
        with patch("custom_components.cuboai.api.cuboai_functions.requests.get") as mock_get:
            mock_get.return_value.json.return_value = {"profiles": []}
            mock_get.return_value.raise_for_status = MagicMock()

            cuboai_functions.get_camera_profiles("my-access-token", "Test-Agent")

            call_args = mock_get.call_args
            headers = call_args[1]["headers"]
            assert headers["x-cspp-authorization"] == "Bearer my-access-token"

    def test_calls_correct_endpoint(self):
        """Should call the cameras endpoint."""
        with patch("custom_components.cuboai.api.cuboai_functions.requests.get") as mock_get:
            mock_get.return_value.json.return_value = {"profiles": []}
            mock_get.return_value.raise_for_status = MagicMock()

            cuboai_functions.get_camera_profiles("token", "agent")

            call_args = mock_get.call_args
            assert call_args[0][0] == "https://api.getcubo.com/prod/user/cameras"


class TestGetSubscriptionInfo:
    """Test subscription info API call."""

    def test_calls_correct_endpoint(self):
        """Should call the subscription endpoint."""
        with patch("custom_components.cuboai.api.cuboai_functions.requests.get") as mock_get:
            mock_get.return_value.json.return_value = {"status": "active"}
            mock_get.return_value.raise_for_status = MagicMock()

            cuboai_functions.get_subscription_info("token", "agent")

            call_args = mock_get.call_args
            assert "subscribed" in call_args[0][0]


class TestGetCameraState:
    """Test camera state API call."""

    def test_includes_device_id_in_request(self):
        """Should include device_id in the request."""
        with patch("custom_components.cuboai.api.cuboai_functions.requests.get") as mock_get:
            mock_get.return_value.json.return_value = {"state": "online"}
            mock_get.return_value.raise_for_status = MagicMock()

            cuboai_functions.get_camera_state("device-123", "token", "agent")

            call_args = mock_get.call_args
            assert "device_id=device-123" in call_args[0][0]
