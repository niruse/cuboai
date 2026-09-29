import logging
import random

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback

from .api import cuboai_functions as api
from .const import (
    DESIRED_ONVIF_PORT,
    DOMAIN,
    NOTIFY_ON_RESTART_DEFAULT,
    OPT_NOTIFY_ON_RESTART,
    OPT_PROTECT_CAMERA,
    OPT_PROTECT_CAMERAS,
    OPT_PROTECT_ENABLED,
    OPT_PROTECT_PASSWORD,
    OPT_PROTECT_PORT,
    OPT_PROTECT_PORTS,
    OPT_PROTECT_USERNAME,
    PROTECT_USERNAME_DEFAULT,
    assign_protect_ports,
    effective_onvif_ports,
    effective_ports,
    protect_camera_id,
    protect_camera_ids,
)

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

# Injected into the enable_debug_logs helper text via description_placeholders —
# hassfest forbids raw URLs inside strings.json/translations.
DOCS_DEBUG_URL = "https://github.com/niruse/cuboai#debug-logs"

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


def _protect_schema(options: dict, cameras: list[dict], full: bool) -> dict:
    """The UniFi Protect fields. `full` adds the camera picker and the port
    (Configure); the first-run step keeps to the switch and the credentials."""
    import homeassistant.helpers.config_validation as cv

    schema = {
        vol.Optional(OPT_PROTECT_ENABLED, default=options.get(OPT_PROTECT_ENABLED, False)): bool,
    }
    if full and cameras:
        choices = {c["device_id"]: f"{c.get('baby_name', 'Camera')} ({c['device_id']})" for c in cameras}
        schema[vol.Optional(OPT_PROTECT_CAMERAS, default=protect_camera_ids(options, cameras))] = cv.multi_select(
            choices
        )
    schema[vol.Optional(OPT_PROTECT_USERNAME, default=options.get(OPT_PROTECT_USERNAME, PROTECT_USERNAME_DEFAULT))] = (
        str
    )
    # suggested_value, not default: with a default, a cleared field could never
    # be saved (the same reason as nvr_password).
    schema[
        vol.Optional(OPT_PROTECT_PASSWORD, description={"suggested_value": options.get(OPT_PROTECT_PASSWORD, "")})
    ] = str
    if full:
        schema[vol.Optional(OPT_PROTECT_PORT, default=int(options.get(OPT_PROTECT_PORT, DESIRED_ONVIF_PORT)))] = (
            vol.All(vol.Coerce(int), vol.Range(min=1024, max=65535))
        )
    return schema


def _recorded_primary(previous: dict, merged: dict, cameras: list[dict]):
    """The camera that keeps the host's real MAC on the base port, recorded
    once and then never moved (const.protect_primary_id).

    Already recorded: kept. A v2.6.37–2.6.40 install that had Protect on keeps
    the camera those versions exposed (the one Protect adopted with that MAC).
    Otherwise the first camera now exposed.
    """
    if previous.get(OPT_PROTECT_CAMERA) is not None:
        return previous[OPT_PROTECT_CAMERA]
    if previous.get(OPT_PROTECT_ENABLED) and OPT_PROTECT_CAMERAS not in previous:
        return protect_camera_id(previous, cameras)
    exposed = protect_camera_ids(merged, cameras)
    return exposed[0] if exposed else None


async def _protect_plan(hass, previous: dict, user_input: dict, cameras: list[dict], entry_id: str | None):
    """Validate the UniFi Protect fields and work out what to store.

    Returns (errors, stored): `stored` holds the recorded primary camera and
    the {camera: port} map, to be saved with the options (the options flow
    replaces ALL options, so bookkeeping not written here would be lost).
    """
    errors: dict = {}
    merged = {**previous, **user_input}
    stored = {
        OPT_PROTECT_CAMERA: _recorded_primary(previous, merged, cameras),
        OPT_PROTECT_PORTS: dict(previous.get(OPT_PROTECT_PORTS) or {}),
    }
    if not user_input.get(OPT_PROTECT_ENABLED):
        return errors, stored
    # Protect refuses to adopt a camera with an empty password.
    if not (user_input.get(OPT_PROTECT_PASSWORD) or "").strip():
        errors[OPT_PROTECT_PASSWORD] = "unifi_password_required"
    if OPT_PROTECT_CAMERAS in user_input and not user_input[OPT_PROTECT_CAMERAS]:
        errors[OPT_PROTECT_CAMERAS] = "unifi_protect_no_camera"
    # The base port and the MACs are per HOST: the primary camera of a second
    # CuboAI account would report the same MAC and Protect would merge them.
    others = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.entry_id != entry_id and (e.options or {}).get(OPT_PROTECT_ENABLED)
    ]
    if others:
        errors["base"] = "unifi_protect_other_entry"

    from .go2rtc import _port_bindable

    merged[OPT_PROTECT_CAMERA] = stored[OPT_PROTECT_CAMERA]
    held = set(effective_onvif_ports(hass, entry_id).values()) if entry_id else set()
    rtsp = user_input.get("rtsp_port")
    avoid = {int(rtsp)} if rtsp is not None else set()
    saved = {d: int(p) for d, p in (previous.get(OPT_PROTECT_PORTS) or {}).items()}
    try:
        base = int(merged.get(OPT_PROTECT_PORT) or DESIRED_ONVIF_PORT)
    except (TypeError, ValueError):
        base = DESIRED_ONVIF_PORT
    field = OPT_PROTECT_PORT if OPT_PROTECT_PORT in user_input else "base"
    if base in avoid:
        errors[field] = "onvif_port_in_use"
        return errors, stored
    # A camera that already has a port keeps it (Protect adopted ip:port); a
    # camera getting its first port skips any that cannot be bound.
    for _ in range(50):
        ports = assign_protect_ports(merged, cameras, avoid=avoid)
        retry = False
        for dev, port in ports.items():
            if port in held or await hass.async_add_executor_job(_port_bindable, port):
                continue
            if port == base:
                errors[field] = "onvif_port_in_use"
            elif saved.get(dev) == port:
                errors[OPT_PROTECT_CAMERAS if OPT_PROTECT_CAMERAS in user_input else "base"] = (
                    "onvif_camera_port_in_use"
                )
            else:
                avoid.add(port)
                retry = True
        if not retry:
            break
    stored[OPT_PROTECT_PORTS] = {**stored[OPT_PROTECT_PORTS], **ports}
    return errors, stored


class CuboAIConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    _reauth_entry = None

    async def async_step_reauth(self, entry_data):
        """Recover the existing account without recreating cameras or entities."""
        # context["entry_id"], not _get_reauth_entry(): that helper (and
        # async_update_reload_and_abort's data_updates) only exist from HA
        # 2024.11, and hacs.json still declares 2024.1.
        self._reauth_entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        """Reuse the password/MFA login, with a session-recovery explanation."""
        return await self.async_step_user(user_input)

    def _finish_reauth(self, uuid, username, data, user_agent):
        """Replace only authentication data after verifying the account."""
        entry = self._reauth_entry
        expected_uuid = entry.data.get("uuid")
        if expected_uuid:
            same_account = str(uuid) == str(expected_uuid)
        else:
            expected_username = str(entry.data.get("username", "")).strip().lower()
            same_account = bool(expected_username) and username.strip().lower() == expected_username
        if not same_account:
            return self.async_abort(reason="wrong_account")
        if not data.get("access_token") or not data.get("refresh_token"):
            raise ValueError("Incomplete CuboAI login response")
        self.hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                "uuid": uuid,
                "username": username,
                "access_token": data["access_token"],
                "refresh_token": data["refresh_token"],
                "user_agent": user_agent,
            },
        )
        # Only auth keys changed, which the update listener deliberately ignores,
        # so the reload is ours: it starts a failed entry or restarts a running
        # one on the new session.
        self.hass.async_create_task(self.hass.config_entries.async_reload(entry.entry_id))
        return self.async_abort(reason="reauth_successful")

    def _auth_schema(self):
        """The sign-in form; a re-sign-in pre-fills the account it must match."""
        if self._reauth_entry is None:
            return AUTH_SCHEMA
        return vol.Schema(
            {
                vol.Required("username", default=self._reauth_entry.data.get("username", "")): str,
                vol.Required("password"): str,
            }
        )

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

                if self._reauth_entry is not None:
                    return self._finish_reauth(uuid, user_input["username"], data, user_agent)

                # Fetch all cameras
                device_map = await self.hass.async_add_executor_job(api.get_camera_profiles, access_token, user_agent)

                if not device_map:
                    _LOGGER.error("No cameras found for account")
                    errors["base"] = "no_cameras"
                    return self.async_show_form(
                        step_id="reauth_confirm" if self._reauth_entry is not None else "user",
                        data_schema=self._auth_schema(),
                        errors=errors,
                    )

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

        return self.async_show_form(
            step_id="reauth_confirm" if self._reauth_entry is not None else "user",
            data_schema=self._auth_schema(),
            errors=errors,
        )

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

                if self._reauth_entry is not None:
                    return self._finish_reauth(uuid, self._username_input, data, self._user_agent)

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

        # One CuboAI ACCOUNT per config entry. Both auth paths (password and MFA)
        # funnel through this step, and this is the earliest point where the
        # account identity is known — so a duplicate is refused before the user
        # bothers picking cameras. A DIFFERENT account is still free to add a
        # second entry; only re-adding the same one is blocked, which otherwise
        # produced two entries whose entities collide on identical unique_ids.
        #
        # The id is the Cognito `sub` claim decoded from the ID token
        # (api.decode_id_token) — opaque and immutable, unlike the e-mail the
        # user types, which can vary by casing or alias.
        account_id = str(self._auth_data.get("uuid") or "").strip()
        if not account_id:
            account_id = str(self._auth_data.get("username", "")).strip().lower()
        if account_id:
            await self.async_set_unique_id(account_id)
            self._abort_if_unique_id_configured()

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
        errors = {}
        if user_input is not None:
            errors, stored = await _protect_plan(self.hass, {}, user_input, self._auth_data.get("cameras", []), None)
            if not errors:
                user_input.update(stored)
                user_input[OPT_PROTECT_PASSWORD] = user_input.get(OPT_PROTECT_PASSWORD) or ""
                return self.async_create_entry(
                    title=f"CuboAI ({self._auth_data['username']})",
                    data=self._auth_data,
                    options=user_input,
                )

        from .utils import find_available_port

        # Binds sockets to probe ports — keep it off the event loop. Re-shown
        # after an error, the user's own port stays in the field.
        default_port = (user_input or {}).get("rtsp_port") or await self.hass.async_add_executor_job(
            find_available_port
        )

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

        # UniFi Protect: the switch and the credentials here; which camera and
        # which port live in Configure (sensible defaults until then).
        schema.update(_protect_schema(user_input or {}, self._auth_data.get("cameras", []), full=False))

        return self.async_show_form(
            step_id="config",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={"docs_url": DOCS_DEBUG_URL},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return CuboAIOptionsFlowHandler()


class CuboAIOptionsFlowHandler(config_entries.OptionsFlow):
    def __init__(self):
        super().__init__()
        # self.config_entry is provided automatically by the base OptionsFlow class

    def _cameras_after(self, user_input: dict) -> list[dict]:
        """The cameras the entry will have once this form is saved (the camera
        picker in the same form can add or remove some)."""
        selected = user_input.get("cameras")
        if selected is None:
            return list(self.config_entry.data.get("cameras", []))
        all_cams = self.config_entry.data.get("all_cameras") or self.config_entry.data.get("cameras", [])
        return [c for c in all_cams if c["device_id"] in selected]

    async def async_step_init(self, user_input=None):
        setup_file_logger(self.hass)
        errors = {}
        if user_input is not None:
            # Validate a manually chosen RTSP port BEFORE saving: it is valid
            # if it's the port our own go2rtc currently holds, or if it can
            # actually be bound. Otherwise reject with a clear error instead
            # of silently self-healing to a different port at runtime.
            try:
                chosen_port = int(user_input.get("rtsp_port") or 0)
            except (TypeError, ValueError):
                chosen_port = 0
            current_port = effective_ports(self.hass, self.config_entry.entry_id)[0]
            if chosen_port and chosen_port != current_port:
                from .go2rtc import _port_bindable

                if not await self.hass.async_add_executor_job(_port_bindable, chosen_port):
                    errors["rtsp_port"] = "rtsp_port_in_use"
            protect_errors, protect_stored = await _protect_plan(
                self.hass,
                dict(self.config_entry.options),
                user_input,
                self._cameras_after(user_input),
                self.config_entry.entry_id,
            )
            errors.update(protect_errors)

        if user_input is not None and not errors:
            # Camera selection is stored in entry DATA (it defines which devices
            # exist), the rest are regular options.
            selected = user_input.pop("cameras", None)
            if selected is not None:
                all_cams = self.config_entry.data.get("all_cameras") or self.config_entry.data.get("cameras", [])
                new_data = dict(self.config_entry.data)
                new_data["selected_camera_ids"] = selected
                new_data["cameras"] = [c for c in all_cams if c["device_id"] in selected]
                self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)

            # NVR exposure: an empty password means NO authentication on the
            # RTSP listener (many NVRs — Hikvision/HiLook — only speak Digest
            # and reject go2rtc's Basic auth, so no-auth is the reliable path).
            # A non-empty password enables Basic auth. Either way the
            # ready-to-paste URL is on the "CuboAI WebRTC Stream" sensor.
            # A CLEARED password field arrives with the key absent (see the
            # suggested_value note in the schema) — store it explicitly as ""
            # so the old password can't survive anywhere downstream.
            user_input["nvr_password"] = user_input.get("nvr_password") or ""
            user_input[OPT_PROTECT_PASSWORD] = user_input.get(OPT_PROTECT_PASSWORD) or ""
            user_input.update(protect_stored)

            # The YouTube/Spotify cache is owned by the Cache YouTube Songs
            # switch entity (single source of truth, restored across restarts);
            # this checkbox is just a second way to flip it.
            cache_flag = user_input.pop("cache_youtube_songs", None)
            if cache_flag is not None:
                current = self.hass.data.get(DOMAIN, {}).get("youtube_cache_enabled", False)
                if cache_flag != current:
                    from homeassistant.helpers import entity_registry as er

                    ent_id = er.async_get(self.hass).async_get_entity_id("switch", DOMAIN, "cuboai_youtube_cache")
                    if ent_id:
                        await self.hass.services.async_call(
                            "switch",
                            "turn_on" if cache_flag else "turn_off",
                            {"entity_id": ent_id},
                            blocking=True,
                        )
                    else:
                        self.hass.data.setdefault(DOMAIN, {})["youtube_cache_enabled"] = cache_flag

            return self.async_create_entry(title="", data=user_input)

        cameras = self.config_entry.data.get("cameras", [])

        default_port = self.config_entry.options.get("rtsp_port", self.config_entry.data.get("rtsp_port"))
        if not default_port:
            # Do NOT probe for a free port here: our own go2rtc is already
            # running and holding the current RTSP port, so probing would
            # "suggest" a NEW port on every options save and silently move the
            # stream (breaking open streams). Show the port go2rtc ACTUALLY
            # bound (it self-heals conflicts at startup), falling back to the
            # historical default.
            default_port = effective_ports(self.hass, self.config_entry.entry_id)[0]

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
                "cache_youtube_songs",
                default=bool(self.hass.data.get(DOMAIN, {}).get("youtube_cache_enabled", False)),
            ): bool,
            vol.Optional(
                "enable_debug_logs",
                default=self.config_entry.options.get("enable_debug_logs", False),
            ): bool,
            vol.Optional(
                "history_sensors",
                default=self.config_entry.options.get("history_sensors", False),
            ): bool,
            # Defaults ON, unlike every other toggle here: it reports a failure
            # that used to be completely silent, and a crash you never hear
            # about is the reason a camera can stay dead all night.
            vol.Optional(
                OPT_NOTIFY_ON_RESTART,
                default=self.config_entry.options.get(OPT_NOTIFY_ON_RESTART, NOTIFY_ON_RESTART_DEFAULT),
            ): bool,
            vol.Required(
                "rtsp_port",
                default=default_port,
            ): vol.All(vol.Coerce(int), vol.Range(min=1024, max=65535)),
            vol.Optional(
                "nvr_enabled",
                default=self.config_entry.options.get("nvr_enabled", False),
            ): bool,
            vol.Optional(
                "nvr_username",
                default=self.config_entry.options.get("nvr_username", "cuboai"),
            ): str,
            # suggested_value (NOT default=) is critical here: with default=,
            # CLEARING the field makes the frontend omit the key and voluptuous
            # re-inserts the old password as the default — so an empty password
            # could never be saved and the NVR URL sensor kept the stale creds.
            # With suggested_value the field is still pre-filled, but clearing
            # it leaves the key absent and the submit handler stores "".
            vol.Optional(
                "nvr_password",
                description={"suggested_value": self.config_entry.options.get("nvr_password", "")},
            ): str,
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

        # H.265/HEVC cameras (e.g. Cubo 3 / SW05) can't be consumed by HomeKit or
        # HA's stream/HLS path — both are H.264-only, so their passthrough stream
        # fails with 'demuxing timed out' / HomeKit 'No Response' (#85). Checking
        # a camera here makes go2rtc transcode it to H.264. Leave native-H.264
        # cameras (Cubo 2 / CB02) unchecked to avoid needless CPU-heavy transcoding.
        h264_options = {c["device_id"]: f"{c.get('baby_name', 'Camera')} ({c['device_id']})" for c in cameras}
        schema[vol.Optional("h264_cameras", default=self.config_entry.options.get("h264_cameras", []))] = (
            cv.multi_select(h264_options)
        )

        # Burn the wall-clock time into the RTSP video image for the checked
        # cameras, so an NVR's recordings show when each frame was captured.
        # Opt-in: it forces a transcode of that camera's NVR stream (extra CPU
        # while the NVR is connected). Only the NVR URL is affected — the live
        # card and HomeKit keep the un-stamped passthrough stream. Same option
        # values as above (per-camera).
        schema[
            vol.Optional(
                "rtsp_timestamp_cameras",
                default=self.config_entry.options.get("rtsp_timestamp_cameras", []),
            )
        ] = cv.multi_select(h264_options)

        # UniFi Protect: present ONE camera as a third-party ONVIF camera
        # (onvif_server.py). Last in the form, as its own section.
        schema.update(_protect_schema(dict(self.config_entry.options), cameras, full=True))

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={"docs_url": DOCS_DEBUG_URL},
        )
