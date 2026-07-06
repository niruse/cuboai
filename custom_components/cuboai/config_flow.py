import logging
import random

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback

from .api import cuboai_functions as api
from .const import DOMAIN

# Dedicated file logger for CuboAI
_LOGGER = logging.getLogger(__name__)
_LOGGER.setLevel(logging.DEBUG)
_LOGGER_SETUP_DONE = False


def setup_file_logger(hass):
    """File logging for config flow is disabled. Use the enable_debug_logs option instead."""
    pass


AUTH_SCHEMA = vol.Schema(
    {
        vol.Required("username"): str,
        vol.Required("password"): str,
    }
)

MFA_SCHEMA = vol.Schema({vol.Required("mfa_code"): str})

CLIENT_ID = "1gvbkmngl920rtp6hlbp6057ue"
CLIENT_SECRET = "1ot7h8m3t83g0g4b7ais7ilcf12o44cvr9cbgad0t90kcpno56jr"
POOL_ID = "us-east-1_Wr7vffd5Y"
REGION = "us-east-1"


def generate_random_user_agent():
    android_version = f"{random.randint(8, 14)}.{random.randint(0, 3)}"
    sdk_device = random.choice(
        ["sdk_gphone64_x86_64", "sdk_gphone_x86", "Pixel_6_Pro", "Pixel_7", "Pixel_3a", "Nexus_6P"]
    )
    okhttp_version = f"{random.randint(4, 5)}.{random.randint(0, 2)}.0-alpha.{random.randint(1, 19)}"
    build = (
        f"{random.randint(100000, 999999)}-android{android_version.replace('.', '')}-9-00043-g383607d234da-ab10550364"
    )
    options = [
        f"aws-sdk-android/2.22.6 Linux/5.10.{random.randint(120, 199)}-{build} Dalvik/2.1.0/0 en_US DevcuboClient",
        f"okhttp/{okhttp_version} (Linux; Android {android_version}; {sdk_device})",
        f"Dalvik/2.1.0 (Linux; U; Android {android_version}; {sdk_device})",
        f"aws-sdk-android/2.22.6 (Linux; Android {android_version}; {sdk_device})",
    ]
    return random.choice(options)


class CuboAIConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        setup_file_logger(self.hass)
        errors = {}

        if user_input is not None:
            try:
                user_agent = generate_random_user_agent()
                _LOGGER.debug(f"Generated random User-Agent: {user_agent}")

                # Step 1: Initiate USER_SRP_AUTH
                resp, aws, client, auth_params = await self.hass.async_add_executor_job(
                    api.initiate_user_srp_auth,
                    user_input["username"],
                    user_input["password"],
                    POOL_ID,
                    CLIENT_ID,
                    CLIENT_SECRET,
                    user_agent,
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
                    user_agent,
                    auth_params,
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
                    api.cubo_mobile_login, uuid, user_input["username"], tokens["AccessToken"], user_agent
                )
                _LOGGER.debug("Cubo mobile login successful")

                access_token = data["access_token"]
                refresh_token = data["refresh_token"]

                _LOGGER.debug("Access token (first 20 chars): %s...", access_token[:20])
                _LOGGER.debug("Refresh token (first 20 chars): %s...", refresh_token[:20])

                # Fetch all cameras
                device_map = await self.hass.async_add_executor_job(api.get_camera_profiles, access_token, user_agent)

                if not device_map:
                    _LOGGER.error("No cameras found for account")
                    errors["base"] = "no_cameras"
                    return self.async_show_form(step_id="user", data_schema=AUTH_SCHEMA, errors=errors)

                # Store all cameras for setup
                cameras = device_map

                self._auth_data = {
                    "uuid": uuid,
                    "username": user_input["username"],
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "pool_id": POOL_ID,
                    "region": REGION,
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "user_agent": user_agent,
                    "cameras": cameras,
                }
                return await self.async_step_select_cameras()

            except Exception as e:
                error_str = str(e)
                if "SMS QUOTA" in error_str.upper() or "UserLambdaValidationException" in error_str:
                    _LOGGER.warning("CuboAI authentication failed: SMS Quota exceeded.")
                    errors["base"] = "sms_quota_exceeded"
                elif (
                    "NotAuthorizedException" in error_str
                    or "InvalidPasswordException" in error_str
                    or "UserNotFoundException" in error_str
                ):
                    _LOGGER.warning("CuboAI authentication failed: Incorrect username or password.")
                    errors["base"] = "auth_failed"
                elif "TooManyRequestsException" in error_str or "LimitExceededException" in error_str:
                    _LOGGER.warning("CuboAI authentication failed: Too many requests.")
                    errors["base"] = "too_many_requests"
                else:
                    _LOGGER.exception("CuboAI authentication failed: %s", e)
                    errors["base"] = "auth_failed"

        return self.async_show_form(step_id="user", data_schema=AUTH_SCHEMA, errors=errors)

    async def async_step_mfa(self, user_input=None):
        """Handle MFA code input step."""
        setup_file_logger(self.hass)
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
                    REGION,
                )
                _LOGGER.debug("MFA verification successful")

                # Continue with normal flow - decode ID token and login to Cubo
                uuid = api.decode_id_token(tokens["IdToken"])
                _LOGGER.debug("Decoded UUID from ID token: %s", uuid)

                data = await self.hass.async_add_executor_job(
                    api.cubo_mobile_login, uuid, self._username_input, tokens["AccessToken"], self._user_agent
                )
                _LOGGER.debug("Cubo mobile login successful after MFA")

                access_token = data["access_token"]
                refresh_token = data["refresh_token"]

                _LOGGER.debug("Access token (first 20 chars): %s...", access_token[:20])
                _LOGGER.debug("Refresh token (first 20 chars): %s...", refresh_token[:20])

                # Fetch all cameras
                device_map = await self.hass.async_add_executor_job(
                    api.get_camera_profiles, access_token, self._user_agent
                )

                if not device_map:
                    _LOGGER.error("No cameras found for account")
                    errors["base"] = "no_cameras"
                    return self.async_show_form(step_id="mfa", data_schema=MFA_SCHEMA, errors=errors)

                # Store all cameras for setup
                cameras = device_map

                self._auth_data = {
                    "uuid": uuid,
                    "username": self._username_input,
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "pool_id": POOL_ID,
                    "region": REGION,
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "user_agent": self._user_agent,
                    "cameras": cameras,
                }
                return await self.async_step_select_cameras()

            except Exception as e:
                error_str = str(e)
                if "CodeMismatchException" in error_str or "Invalid" in error_str:
                    _LOGGER.warning("MFA verification failed: Invalid code.")
                    errors["base"] = "invalid_mfa_code"
                elif "ExpiredCodeException" in error_str or "expired" in error_str.lower():
                    _LOGGER.warning("MFA verification failed: Code expired.")
                    errors["base"] = "mfa_code_expired"
                elif "SMS QUOTA" in error_str.upper() or "UserLambdaValidationException" in error_str:
                    _LOGGER.warning("MFA verification failed: SMS Quota exceeded.")
                    errors["base"] = "sms_quota_exceeded"
                elif "TooManyRequestsException" in error_str or "LimitExceededException" in error_str:
                    _LOGGER.warning("MFA verification failed: Too many requests.")
                    errors["base"] = "too_many_requests"
                else:
                    _LOGGER.exception("MFA verification failed: %s", e)
                    errors["base"] = "mfa_failed"

        # Determine hint text based on MFA type
        mfa_type = getattr(self, "_mfa_challenge", "SMS_MFA")
        description_placeholders = {"mfa_type": "authenticator app" if mfa_type == "SOFTWARE_TOKEN_MFA" else "SMS"}

        return self.async_show_form(
            step_id="mfa", data_schema=MFA_SCHEMA, errors=errors, description_placeholders=description_placeholders
        )

    async def async_step_select_cameras(self, user_input=None):
        """Let the user choose which of the account's cameras to add.

        Nothing is added automatically: all discovered cameras are listed
        (pre-checked) and only the ones the user confirms are set up. The
        full list is kept in the entry so unselected cameras can be added
        later from the Options flow.
        """
        import homeassistant.helpers.config_validation as cv

        all_cameras = self._auth_data.get("all_cameras") or self._auth_data.get("cameras", [])
        options_map = {
            cam["device_id"]: f"{cam.get('baby_name', 'Camera')} ({cam['device_id']})" for cam in all_cameras
        }
        errors = {}

        if user_input is not None:
            selected = user_input.get("cameras", [])
            if not selected:
                errors["base"] = "no_cameras_selected"
            else:
                self._auth_data["all_cameras"] = all_cameras
                self._auth_data["selected_camera_ids"] = selected
                self._auth_data["cameras"] = [c for c in all_cameras if c["device_id"] in selected]
                return await self.async_step_config()

        schema = vol.Schema({vol.Required("cameras", default=list(options_map)): cv.multi_select(options_map)})
        return self.async_show_form(step_id="select_cameras", data_schema=schema, errors=errors)

    async def async_step_config(self, user_input=None):
        """Handle configuration options step."""
        setup_file_logger(self.hass)
        if user_input is not None:
            return self.async_create_entry(
                title=f"CuboAI ({self._auth_data['username']})",
                data=self._auth_data,
                options=user_input,
            )

        from .utils import find_available_port

        # Binds sockets to probe ports — keep it off the event loop.
        default_port = await self.hass.async_add_executor_job(find_available_port)

        schema = {
            vol.Required("download_images", default=True): bool,
            vol.Optional("enable_debug_logs", default=False): bool,
            vol.Required("rtsp_port", default=default_port): vol.All(vol.Coerce(int), vol.Range(min=1024, max=65535)),
            vol.Required("alerts_count", default=5): vol.All(vol.Coerce(int), vol.Range(min=1, max=50)),
            vol.Required("max_saved_photos", default=10): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
            vol.Required("hours_back", default=12): vol.All(vol.Coerce(int), vol.Range(min=1, max=72)),
            vol.Required("update_interval", default=60): vol.All(vol.Coerce(int), vol.Range(min=15, max=300)),
        }

        # Add dynamic fields for each camera IP
        for cam in self._auth_data.get("cameras", []):
            dev_id = cam.get("device_id")
            key = f"camera_ip_{dev_id}"
            schema[vol.Optional(key, description={"suggested_value": ""})] = str

        return self.async_show_form(
            step_id="config",
            data_schema=vol.Schema(schema),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return CuboAIOptionsFlowHandler()


class CuboAIOptionsFlowHandler(config_entries.OptionsFlow):
    def __init__(self):
        super().__init__()
        # self.config_entry is provided automatically by the base OptionsFlow class

    async def async_step_init(self, user_input=None):
        setup_file_logger(self.hass)
        if user_input is not None:
            # Camera selection is stored in entry DATA (it defines which devices
            # exist), the rest are regular options.
            selected = user_input.pop("cameras", None)
            if selected is not None:
                all_cams = self.config_entry.data.get("all_cameras") or self.config_entry.data.get("cameras", [])
                new_data = dict(self.config_entry.data)
                new_data["selected_camera_ids"] = selected
                new_data["cameras"] = [c for c in all_cams if c["device_id"] in selected]
                self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)
            return self.async_create_entry(title="", data=user_input)

        cameras = self.config_entry.data.get("cameras", [])

        default_port = self.config_entry.options.get("rtsp_port", self.config_entry.data.get("rtsp_port"))
        if not default_port:
            # Do NOT probe for a free port here: our own go2rtc is already
            # running and holding the current RTSP port, so probing would
            # "suggest" a NEW port on every options save and silently move the
            # stream (breaking open streams). The integration has always
            # defaulted to 8555 — keep whatever is effectively in use.
            default_port = 8555

        import homeassistant.helpers.config_validation as cv

        # Camera picker: every camera on the account, with the currently
        # configured ones pre-checked. Unchecking removes a camera; checking a
        # new one adds it (nothing is added automatically at runtime).
        all_cameras = self.config_entry.data.get("all_cameras") or cameras
        camera_options = {c["device_id"]: f"{c.get('baby_name', 'Camera')} ({c['device_id']})" for c in all_cameras}
        currently_selected = [c["device_id"] for c in cameras if c["device_id"] in camera_options]

        schema = {
            vol.Required("cameras", default=currently_selected): cv.multi_select(camera_options),
            vol.Required(
                "download_images",
                default=self.config_entry.options.get(
                    "download_images", self.config_entry.data.get("download_images", True)
                ),
            ): bool,
            vol.Optional(
                "enable_debug_logs",
                default=self.config_entry.options.get("enable_debug_logs", False),
            ): bool,
            vol.Required(
                "rtsp_port",
                default=default_port,
            ): vol.All(vol.Coerce(int), vol.Range(min=1024, max=65535)),
            vol.Required(
                "alerts_count",
                default=self.config_entry.options.get("alerts_count", self.config_entry.data.get("alerts_count", 5)),
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=50)),
            vol.Required(
                "max_saved_photos",
                default=self.config_entry.options.get(
                    "max_saved_photos", self.config_entry.data.get("max_saved_photos", 10)
                ),
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
            vol.Required(
                "hours_back",
                default=self.config_entry.options.get("hours_back", self.config_entry.data.get("hours_back", 12)),
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=72)),
            vol.Required(
                "update_interval",
                default=self.config_entry.options.get(
                    "update_interval", self.config_entry.data.get("update_interval", 60)
                ),
            ): vol.All(vol.Coerce(int), vol.Range(min=15, max=300)),
        }

        for cam in cameras:
            dev_id = cam.get("device_id")
            key = f"camera_ip_{dev_id}"
            schema[vol.Optional(key, description={"suggested_value": self.config_entry.options.get(key, "")})] = str

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema),
        )
