import logging
import random
import voluptuous as vol
from homeassistant import config_entries
from .const import DOMAIN
from .api import cuboai_functions as api

# Dedicated file logger for CuboAI
file_handler = logging.FileHandler('/config/cuboai_auth.log')
file_handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
_LOGGER = logging.getLogger(__name__)
_LOGGER.addHandler(file_handler)
_LOGGER.setLevel(logging.DEBUG)

AUTH_SCHEMA = vol.Schema({
    vol.Required("username"): str,
    vol.Required("password"): str
})

MFA_SCHEMA = vol.Schema({
    vol.Required("mfa_code"): str
})

CLIENT_ID = "1gvbkmngl920rtp6hlbp6057ue"
CLIENT_SECRET = "1ot7h8m3t83g0g4b7ais7ilcf12o44cvr9cbgad0t90kcpno56jr"
POOL_ID = "us-east-1_Wr7vffd5Y"
REGION = "us-east-1"

def generate_random_user_agent():
    android_version = f"{random.randint(8,14)}.{random.randint(0,3)}"
    sdk_int = random.randint(26, 34)
    sdk_device = random.choice([
        "sdk_gphone64_x86_64", "sdk_gphone_x86", "Pixel_6_Pro", "Pixel_7", "Pixel_3a", "Nexus_6P"
    ])
    okhttp_version = f"{random.randint(4,5)}.{random.randint(0,2)}.0-alpha.{random.randint(1,19)}"
    build = f"{random.randint(100000,999999)}-android{android_version.replace('.', '')}-9-00043-g383607d234da-ab10550364"
    options = [
        f"aws-sdk-android/2.22.6 Linux/5.10.{random.randint(120,199)}-{build} Dalvik/2.1.0/0 en_US DevcuboClient",
        f"okhttp/{okhttp_version} (Linux; Android {android_version}; {sdk_device})",
        f"Dalvik/2.1.0 (Linux; U; Android {android_version}; {sdk_device})",
        f"aws-sdk-android/2.22.6 (Linux; Android {android_version}; {sdk_device})"
    ]
    return random.choice(options)

class CuboAIConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}

        if user_input is not None:
            try:
                user_agent = generate_random_user_agent()
                _LOGGER.debug(f"Generated random User-Agent: {user_agent}")

                # Step 1: Initiate USER_SRP_AUTH
                resp, aws, client = await self.hass.async_add_executor_job(
                    api.initiate_user_srp_auth,
                    user_input["username"],
                    user_input["password"],
                    POOL_ID,
                    CLIENT_ID,
                    CLIENT_SECRET,
                    user_agent
                )
                _LOGGER.debug("USER_SRP_AUTH successful")

                # Step 2: Respond to PASSWORD_VERIFIER
                tokens = await self.hass.async_add_executor_job(
                    api.respond_to_password_verifier,
                    resp,
                    aws,
                    client,
                    CLIENT_ID,
                    CLIENT_SECRET,
                    user_agent
                )
                _LOGGER.debug("Password verifier responded successfully")

                # Check if MFA is required
                if isinstance(tokens, dict) and "challenge" in tokens:
                    _LOGGER.debug("MFA challenge detected: %s", tokens["challenge"])
                    # Store data for MFA step
                    self._mfa_session = tokens["session"]
                    self._mfa_challenge = tokens["challenge"]
                    self._mfa_username = tokens["username"]
                    self._user_agent = user_agent
                    self._username_input = user_input["username"]
                    return await self.async_step_mfa()

                # Step 3: Decode ID Token and login to Cubo
                uuid = api.decode_id_token(tokens["IdToken"])
                _LOGGER.debug("Decoded UUID from ID token: %s", uuid)

                data = await self.hass.async_add_executor_job(
                    api.cubo_mobile_login,
                    uuid,
                    user_input["username"],
                    tokens["AccessToken"],
                    user_agent
                )
                _LOGGER.debug("Cubo mobile login successful")

                access_token = data["access_token"]
                refresh_token = data["refresh_token"]

                _LOGGER.debug("Access token (first 20 chars): %s...", access_token[:20])
                _LOGGER.debug("Refresh token (first 20 chars): %s...", refresh_token[:20])

                # Store for use in next step
                self._uuid = uuid
                self._username = user_input["username"]
                self._access_token = access_token
                self._refresh_token = refresh_token
                self._user_agent = user_agent

                return await self.async_step_select_camera()

            except Exception as e:
                _LOGGER.exception("CuboAI authentication failed: %s", e)
                error_str = str(e)
                if "SMS QUOTA" in error_str.upper() or "UserLambdaValidationException" in error_str:
                    errors["base"] = "sms_quota_exceeded"
                elif "NotAuthorizedException" in error_str:
                    errors["base"] = "auth_failed"
                elif "UserNotFoundException" in error_str:
                    errors["base"] = "auth_failed"
                elif "InvalidPasswordException" in error_str:
                    errors["base"] = "auth_failed"
                elif "TooManyRequestsException" in error_str:
                    errors["base"] = "too_many_requests"
                elif "LimitExceededException" in error_str:
                    errors["base"] = "too_many_requests"
                else:
                    errors["base"] = "auth_failed"

        return self.async_show_form(
            step_id="user",
            data_schema=AUTH_SCHEMA,
            errors=errors
        )

    async def async_step_mfa(self, user_input=None):
        """Handle MFA code input step."""
        errors = {}

        if user_input is not None:
            try:
                mfa_code = user_input["mfa_code"].strip()
                _LOGGER.debug("Attempting MFA verification with code length: %d", len(mfa_code))

                tokens = await self.hass.async_add_executor_job(
                    api.respond_to_mfa_challenge,
                    CLIENT_ID,
                    CLIENT_SECRET,
                    self._mfa_session,
                    self._mfa_username,
                    mfa_code,
                    self._mfa_challenge,
                    REGION
                )
                _LOGGER.debug("MFA verification successful")

                # Continue with normal flow - decode ID token and login to Cubo
                uuid = api.decode_id_token(tokens["IdToken"])
                _LOGGER.debug("Decoded UUID from ID token: %s", uuid)

                data = await self.hass.async_add_executor_job(
                    api.cubo_mobile_login,
                    uuid,
                    self._username_input,
                    tokens["AccessToken"],
                    self._user_agent
                )
                _LOGGER.debug("Cubo mobile login successful after MFA")

                access_token = data["access_token"]
                refresh_token = data["refresh_token"]

                _LOGGER.debug("Access token (first 20 chars): %s...", access_token[:20])
                _LOGGER.debug("Refresh token (first 20 chars): %s...", refresh_token[:20])

                # Store for camera selection step
                self._uuid = uuid
                self._username = self._username_input
                self._access_token = access_token
                self._refresh_token = refresh_token

                return await self.async_step_select_camera()

            except Exception as e:
                _LOGGER.exception("MFA verification failed: %s", e)
                error_str = str(e)
                if "CodeMismatchException" in error_str or "Invalid" in error_str:
                    errors["base"] = "invalid_mfa_code"
                elif "ExpiredCodeException" in error_str or "expired" in error_str.lower():
                    errors["base"] = "mfa_code_expired"
                elif "SMS QUOTA" in error_str.upper() or "UserLambdaValidationException" in error_str:
                    errors["base"] = "sms_quota_exceeded"
                elif "TooManyRequestsException" in error_str or "LimitExceededException" in error_str:
                    errors["base"] = "too_many_requests"
                else:
                    errors["base"] = "mfa_failed"

        # Determine hint text based on MFA type
        mfa_type = getattr(self, "_mfa_challenge", "SMS_MFA")
        description_placeholders = {
            "mfa_type": "authenticator app" if mfa_type == "SOFTWARE_TOKEN_MFA" else "SMS"
        }

        return self.async_show_form(
            step_id="mfa",
            data_schema=MFA_SCHEMA,
            errors=errors,
            description_placeholders=description_placeholders
        )

    async def async_step_select_camera(self, user_input=None):
        errors = {}

        try:
            # Fetch camera list
            device_map = await self.hass.async_add_executor_job(
                api.get_camera_profiles,
                self._access_token,
                self._user_agent
            )

            if not device_map:
                _LOGGER.error("No cameras found for account")
                errors["base"] = "no_cameras"
                return self.async_show_form(
                    step_id="select_camera",
                    data_schema=vol.Schema({}),
                    errors=errors
                )

            if user_input is not None:
                selected_baby = user_input["camera"]
                selected_device = device_map[selected_baby]
                download_images = user_input.get("download_images", True)

                return self.async_create_entry(
                    title=f"{selected_baby} Camera",
                    data={
                        "uuid": self._uuid,
                        "username": self._username,
                        "client_id": CLIENT_ID,
                        "client_secret": CLIENT_SECRET,
                        "pool_id": POOL_ID,
                        "region": REGION,
                        "access_token": self._access_token,
                        "refresh_token": self._refresh_token,
                        "user_agent": self._user_agent,
                        "device_id": selected_device,
                        "baby_name": selected_baby,
                        "download_images": download_images
                    },
                )

            return self.async_show_form(
                step_id="select_camera",
                data_schema=vol.Schema({
                    vol.Required("camera"): vol.In(list(device_map.keys())),
                    vol.Optional("download_images", default=True): bool
                }),
                errors=errors
            )

        except Exception as e:
            _LOGGER.exception("Failed to fetch cameras: %s", e)
            errors["base"] = "camera_fetch_failed"
            return self.async_show_form(
                step_id="select_camera",
                data_schema=vol.Schema({}),
                errors=errors
            )

    @staticmethod
    def async_get_options_flow(config_entry):
        return CuboAIOptionsFlowHandler(config_entry)

class CuboAIOptionsFlowHandler(config_entries.OptionsFlow):
    def __init__(self, config_entry):
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data={
                "download_images": user_input.get("download_images", True)
            })

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional(
                    "download_images",
                    default=self.config_entry.options.get("download_images", self.config_entry.data.get("download_images", True))
                ): bool
            })
        )
