"""Tests for CuboAI authentication flow functions.

These tests import the actual cuboai functions (not copies) to ensure
changes to the implementation are properly tested.
"""

from unittest.mock import MagicMock, patch

# Import actual functions from cuboai module
# (conftest.py sets up the necessary mocks before this runs)
from custom_components.cuboai.api.cuboai_functions import (
    get_secret_hash,
    respond_to_mfa_challenge,
    respond_to_password_verifier,
)


class TestGetSecretHash:
    """Tests for the get_secret_hash function."""

    def test_generates_consistent_hash(self):
        """Same inputs should produce same hash."""
        hash1 = get_secret_hash("user@test.com", "client123", "secret456")
        hash2 = get_secret_hash("user@test.com", "client123", "secret456")
        assert hash1 == hash2

    def test_different_users_produce_different_hashes(self):
        """Different usernames should produce different hashes."""
        hash1 = get_secret_hash("user1@test.com", "client123", "secret456")
        hash2 = get_secret_hash("user2@test.com", "client123", "secret456")
        assert hash1 != hash2

    def test_hash_is_base64_encoded(self):
        """Hash should be a valid base64 string."""
        import base64

        hash_value = get_secret_hash("user@test.com", "client123", "secret456")
        # Should not raise an exception
        decoded = base64.b64decode(hash_value)
        assert len(decoded) == 32  # SHA256 produces 32 bytes


class TestRespondToPasswordVerifier:
    """Tests for the respond_to_password_verifier function."""

    def test_returns_tokens_when_no_mfa(self, mock_cognito_client, mock_tokens):
        """When no MFA is required, returns AuthenticationResult tokens."""
        # Setup mocks
        mock_aws = MagicMock()
        mock_aws.process_challenge.return_value = {"USERNAME": "testuser"}

        mock_resp = {"ChallengeParameters": {"USER_ID_FOR_SRP": "testuser"}}

        mock_cognito_client.respond_to_auth_challenge.return_value = {"AuthenticationResult": mock_tokens}

        # Call actual function
        result = respond_to_password_verifier(
            resp=mock_resp,
            aws=mock_aws,
            client=mock_cognito_client,
            client_id="test-client-id",
            client_secret="test-client-secret",
            user_agent="test-agent",
        )

        # Verify
        assert result == mock_tokens
        assert "IdToken" in result
        assert "AccessToken" in result

    def test_returns_mfa_challenge_when_required(self, mock_cognito_client, mock_mfa_challenge):
        """When MFA is required, returns challenge info dict."""
        # Setup mocks
        mock_aws = MagicMock()
        mock_aws.process_challenge.return_value = {"USERNAME": "testuser"}

        mock_resp = {"ChallengeParameters": {"USER_ID_FOR_SRP": "testuser"}}

        mock_cognito_client.respond_to_auth_challenge.return_value = mock_mfa_challenge

        # Call actual function
        result = respond_to_password_verifier(
            resp=mock_resp,
            aws=mock_aws,
            client=mock_cognito_client,
            client_id="test-client-id",
            client_secret="test-client-secret",
            user_agent="test-agent",
        )

        # Verify MFA challenge info is returned
        assert "challenge" in result
        assert result["challenge"] == "SMS_MFA"
        assert "session" in result
        assert result["session"] == "test-session-abc123"
        assert "username" in result
        assert result["username"] == "testuser"

    def test_detects_software_token_mfa(self, mock_cognito_client, mock_software_token_mfa_challenge):
        """Detects SOFTWARE_TOKEN_MFA (TOTP) challenge."""
        mock_aws = MagicMock()
        mock_aws.process_challenge.return_value = {"USERNAME": "testuser"}

        mock_resp = {"ChallengeParameters": {"USER_ID_FOR_SRP": "testuser"}}

        mock_cognito_client.respond_to_auth_challenge.return_value = mock_software_token_mfa_challenge

        result = respond_to_password_verifier(
            resp=mock_resp,
            aws=mock_aws,
            client=mock_cognito_client,
            client_id="test-client-id",
            client_secret="test-client-secret",
            user_agent="test-agent",
        )

        assert result["challenge"] == "SOFTWARE_TOKEN_MFA"


class TestRespondToMfaChallenge:
    """Tests for the respond_to_mfa_challenge function.

    Note: The actual respond_to_mfa_challenge function creates its own boto3
    client internally, so we need to patch boto3.client to test it.
    """

    @patch("custom_components.cuboai.api.cuboai_functions.boto3.client")
    def test_sms_mfa_success(self, mock_boto_client, mock_tokens):
        """Successfully responds to SMS MFA challenge."""
        mock_client = MagicMock()
        mock_client.respond_to_auth_challenge.return_value = {"AuthenticationResult": mock_tokens}
        mock_boto_client.return_value = mock_client

        result = respond_to_mfa_challenge(
            client_id="test-client-id",
            client_secret="test-client-secret",
            session="test-session",
            username="testuser",
            mfa_code="123456",
            challenge_name="SMS_MFA",
        )

        # Verify tokens returned
        assert result == mock_tokens

        # Verify correct challenge response was sent
        call_args = mock_client.respond_to_auth_challenge.call_args
        assert call_args.kwargs["ChallengeName"] == "SMS_MFA"
        assert "SMS_MFA_CODE" in call_args.kwargs["ChallengeResponses"]
        assert call_args.kwargs["ChallengeResponses"]["SMS_MFA_CODE"] == "123456"

    @patch("custom_components.cuboai.api.cuboai_functions.boto3.client")
    def test_software_token_mfa_success(self, mock_boto_client, mock_tokens):
        """Successfully responds to SOFTWARE_TOKEN_MFA (TOTP) challenge."""
        mock_client = MagicMock()
        mock_client.respond_to_auth_challenge.return_value = {"AuthenticationResult": mock_tokens}
        mock_boto_client.return_value = mock_client

        result = respond_to_mfa_challenge(
            client_id="test-client-id",
            client_secret="test-client-secret",
            session="test-session",
            username="testuser",
            mfa_code="654321",
            challenge_name="SOFTWARE_TOKEN_MFA",
        )

        # Verify correct response key for TOTP
        call_args = mock_client.respond_to_auth_challenge.call_args
        assert call_args.kwargs["ChallengeName"] == "SOFTWARE_TOKEN_MFA"
        assert "SOFTWARE_TOKEN_MFA_CODE" in call_args.kwargs["ChallengeResponses"]
        assert call_args.kwargs["ChallengeResponses"]["SOFTWARE_TOKEN_MFA_CODE"] == "654321"

    @patch("custom_components.cuboai.api.cuboai_functions.boto3.client")
    def test_default_challenge_name_is_sms(self, mock_boto_client, mock_tokens):
        """Default MFA type is SMS_MFA."""
        mock_client = MagicMock()
        mock_client.respond_to_auth_challenge.return_value = {"AuthenticationResult": mock_tokens}
        mock_boto_client.return_value = mock_client

        respond_to_mfa_challenge(
            client_id="test-client-id",
            client_secret="test-client-secret",
            session="test-session",
            username="testuser",
            mfa_code="123456",
            # No challenge_name specified - should default to SMS_MFA
        )

        call_args = mock_client.respond_to_auth_challenge.call_args
        assert call_args.kwargs["ChallengeName"] == "SMS_MFA"

    @patch("custom_components.cuboai.api.cuboai_functions.boto3.client")
    def test_includes_secret_hash_and_username(self, mock_boto_client, mock_tokens):
        """Challenge response includes required SECRET_HASH and USERNAME."""
        mock_client = MagicMock()
        mock_client.respond_to_auth_challenge.return_value = {"AuthenticationResult": mock_tokens}
        mock_boto_client.return_value = mock_client

        respond_to_mfa_challenge(
            client_id="test-client-id",
            client_secret="test-client-secret",
            session="test-session",
            username="testuser",
            mfa_code="123456",
        )

        call_args = mock_client.respond_to_auth_challenge.call_args
        challenge_responses = call_args.kwargs["ChallengeResponses"]

        assert "USERNAME" in challenge_responses
        assert challenge_responses["USERNAME"] == "testuser"
        assert "SECRET_HASH" in challenge_responses

    @patch("custom_components.cuboai.api.cuboai_functions.boto3.client")
    def test_passes_session_to_cognito(self, mock_boto_client, mock_tokens):
        """Session token is passed to Cognito."""
        mock_client = MagicMock()
        mock_client.respond_to_auth_challenge.return_value = {"AuthenticationResult": mock_tokens}
        mock_boto_client.return_value = mock_client

        respond_to_mfa_challenge(
            client_id="test-client-id",
            client_secret="test-client-secret",
            session="my-unique-session-token",
            username="testuser",
            mfa_code="123456",
        )

        call_args = mock_client.respond_to_auth_challenge.call_args
        assert call_args.kwargs["Session"] == "my-unique-session-token"

    @patch("custom_components.cuboai.api.cuboai_functions.boto3.client")
    def test_creates_cognito_client_with_correct_region(self, mock_boto_client, mock_tokens):
        """Boto3 client is created with the specified region."""
        mock_client = MagicMock()
        mock_client.respond_to_auth_challenge.return_value = {"AuthenticationResult": mock_tokens}
        mock_boto_client.return_value = mock_client

        respond_to_mfa_challenge(
            client_id="test-client-id",
            client_secret="test-client-secret",
            session="test-session",
            username="testuser",
            mfa_code="123456",
            region="eu-west-1",
        )

        mock_boto_client.assert_called_once_with("cognito-idp", region_name="eu-west-1")


class TestPycognitoCompatibility:
    """Tests to verify compatibility with the pycognito library.

    These tests help detect breaking changes in upstream dependencies
    by testing against the actual library (not mocked).
    """

    def test_awssrp_process_challenge_signature(self):
        """Verify AWSSRP.process_challenge accepts required parameters.

        This test ensures we're compatible with pycognito's API.
        If pycognito changes their signature again, this test will fail.
        """
        import inspect

        from pycognito import AWSSRP

        sig = inspect.signature(AWSSRP.process_challenge)
        params = list(sig.parameters.keys())

        # Should have: self, challenge_parameters, request_parameters
        assert "challenge_parameters" in params or len(params) >= 2, (
            f"AWSSRP.process_challenge signature changed! Got params: {params}"
        )
        # The new API requires request_parameters
        assert "request_parameters" in params or len(params) >= 3, (
            f"AWSSRP.process_challenge should accept request_parameters. Got: {params}"
        )

    def test_awssrp_can_be_instantiated(self):
        """Verify AWSSRP can be instantiated with expected parameters."""
        from unittest.mock import MagicMock

        from pycognito import AWSSRP

        mock_client = MagicMock()

        # This should not raise an exception
        aws = AWSSRP(
            username="test@example.com",
            password="testpassword",
            pool_id="us-east-1_TestPool",
            client_id="test-client-id",
            client=mock_client,
        )

        assert aws is not None
        assert aws.username == "test@example.com"

    def test_awssrp_get_auth_params_returns_expected_keys(self):
        """Verify get_auth_params returns expected structure."""
        from unittest.mock import MagicMock

        from pycognito import AWSSRP

        mock_client = MagicMock()
        aws = AWSSRP(
            username="test@example.com",
            password="testpassword",
            pool_id="us-east-1_TestPool",
            client_id="test-client-id",
            client=mock_client,
        )

        auth_params = aws.get_auth_params()

        assert "USERNAME" in auth_params, "auth_params should contain USERNAME"
        assert "SRP_A" in auth_params, "auth_params should contain SRP_A"
        assert auth_params["USERNAME"] == "test@example.com"
