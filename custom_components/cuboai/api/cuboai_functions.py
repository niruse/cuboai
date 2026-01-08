import base64
import hashlib
import hmac
import importlib
import json
import os
import subprocess
import sys
import time

import boto3
import jwt
import requests

from custom_components.cuboai.utils import log_to_file

# Legacy hardcoded paths (for backwards compatibility - will be removed in future release)
LEGACY_ACCESS_TOKEN_FILE = "/config/cuboai_access_token.json"
LEGACY_REFRESH_TOKEN_FILE = "/config/cuboai_refresh_token.json"

# New portable paths (set by __init__.py using hass.config.path())
ACCESS_TOKEN_FILE = None
REFRESH_TOKEN_FILE = None


def set_token_paths(config_path: str):
    """Set token file paths based on Home Assistant config directory.

    Called from __init__.py with hass.config.path() for portability.
    """
    global ACCESS_TOKEN_FILE, REFRESH_TOKEN_FILE
    import os

    ACCESS_TOKEN_FILE = os.path.join(config_path, "cuboai_access_token.json")
    REFRESH_TOKEN_FILE = os.path.join(config_path, "cuboai_refresh_token.json")
    log_to_file(f"Token paths set to: {ACCESS_TOKEN_FILE}, {REFRESH_TOKEN_FILE}")


def _get_access_token_path():
    """Get the access token file path, with legacy fallback."""
    import os

    if ACCESS_TOKEN_FILE and os.path.exists(ACCESS_TOKEN_FILE):
        return ACCESS_TOKEN_FILE
    # Backwards compatibility: check legacy location
    if os.path.exists(LEGACY_ACCESS_TOKEN_FILE):
        log_to_file(f"Using legacy access token path: {LEGACY_ACCESS_TOKEN_FILE}")
        return LEGACY_ACCESS_TOKEN_FILE
    # Return new path for writing (even if doesn't exist yet)
    return ACCESS_TOKEN_FILE or LEGACY_ACCESS_TOKEN_FILE


def _get_refresh_token_path():
    """Get the refresh token file path, with legacy fallback."""
    import os

    if REFRESH_TOKEN_FILE and os.path.exists(REFRESH_TOKEN_FILE):
        return REFRESH_TOKEN_FILE
    # Backwards compatibility: check legacy location
    if os.path.exists(LEGACY_REFRESH_TOKEN_FILE):
        log_to_file(f"Using legacy refresh token path: {LEGACY_REFRESH_TOKEN_FILE}")
        return LEGACY_REFRESH_TOKEN_FILE
    # Return new path for writing (even if doesn't exist yet)
    return REFRESH_TOKEN_FILE or LEGACY_REFRESH_TOKEN_FILE


# --- Access/Refresh Token Save/Load ---
def _atomic_write(path, payload: str):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(payload)
    os.replace(tmp, path)


def save_access_token(access_token):
    try:
        path = _get_access_token_path()
        _atomic_write(path, json.dumps({"access_token": access_token}))
        log_to_file(f"Access token saved to: {path}")
    except Exception as e:
        log_to_file(f"Failed to save access_token: {e}")


def save_refresh_token(refresh_token):
    try:
        path = _get_refresh_token_path()
        _atomic_write(path, json.dumps({"refresh_token": refresh_token}))
        log_to_file(f"Refresh token saved to: {path}")
    except Exception as e:
        log_to_file(f"Failed to save refresh_token: {e}")


def load_access_token():
    try:
        path = _get_access_token_path()
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            return data.get("access_token")
    except Exception:
        return None


def load_refresh_token():
    try:
        path = _get_refresh_token_path()
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            return data.get("refresh_token")
    except Exception:
        return None


# --- Cognito SRP Utilities ---
def ensure_warrant_installed():
    """
    Ensure 'warrant' is installed and importable in Home Assistant's /config/deps path.
    If missing, automatically installs warrant==0.6.1 into the detected deps path.
    Creates the folder if it does not exist.
    """
    # Detect Python version
    py_version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    deps_root = f"/config/deps/lib/{py_version}"
    site_packages = os.path.join(deps_root, "site-packages")

    # Create folder if missing
    if not os.path.exists(site_packages):
        try:
            os.makedirs(site_packages, exist_ok=True)
            log_to_file(f"Created missing folder: {site_packages}")
        except Exception as e:
            raise ImportError(f"Failed to create deps folder {site_packages}: {e}")

    # Ensure path is in sys.path (insert at beginning so it has priority)
    if site_packages not in sys.path:
        sys.path.insert(0, site_packages)

    # Try importing
    try:
        from warrant.aws_srp import AWSSRP  # noqa: F401

        return True
    except ImportError:
        try:
            log_to_file("warrant not found, attempting auto-install warrant==0.6.1...")
            subprocess.check_call(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--no-cache-dir",
                    "--upgrade",
                    "--no-deps",
                    "--target",
                    site_packages,
                    "warrant==0.6.1",
                ]
            )
            importlib.invalidate_caches()
            from warrant.aws_srp import AWSSRP  # noqa: F401

            log_to_file("warrant successfully installed.")
            return True
        except Exception as e:
            raise ImportError(f"Failed to auto-install warrant==0.6.1 into {site_packages}: {e}")


# Make sure warrant is available before import
ensure_warrant_installed()
from warrant.aws_srp import AWSSRP


def get_secret_hash(username, client_id, client_secret):
    msg = username + client_id
    dig = hmac.new(client_secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(dig).decode()


def initiate_user_srp_auth(username, password, pool_id, client_id, client_secret, user_agent, region="us-east-1"):
    client = boto3.client("cognito-idp", region_name=region)
    aws = AWSSRP(username=username, password=password, pool_id=pool_id, client_id=client_id, client=client)
    auth_params = aws.get_auth_params()
    auth_params["SECRET_HASH"] = get_secret_hash(username, client_id, client_secret)
    return client.initiate_auth(AuthFlow="USER_SRP_AUTH", AuthParameters=auth_params, ClientId=client_id), aws, client


def respond_to_password_verifier(resp, aws, client, client_id, client_secret, user_agent):
    challenge_params = resp["ChallengeParameters"]
    challenge_responses = aws.process_challenge(challenge_params)
    username = challenge_params["USER_ID_FOR_SRP"]
    challenge_responses["SECRET_HASH"] = get_secret_hash(username, client_id, client_secret)
    result = client.respond_to_auth_challenge(
        ClientId=client_id, ChallengeName="PASSWORD_VERIFIER", ChallengeResponses=challenge_responses
    )
    # Check if MFA is required
    if "ChallengeName" in result:
        return {
            "challenge": result["ChallengeName"],
            "session": result["Session"],
            "challenge_params": result.get("ChallengeParameters", {}),
            "username": username,
        }
    return result["AuthenticationResult"]


def respond_to_mfa_challenge(
    client_id, client_secret, session, username, mfa_code, challenge_name="SMS_MFA", region="us-east-1"
):
    """
    Respond to an MFA challenge (SMS_MFA or SOFTWARE_TOKEN_MFA).
    Creates its own boto3 client to avoid blocking calls in async context.
    Returns AuthenticationResult tokens on success.
    """
    # Create client inside executor job to avoid blocking the event loop
    client = boto3.client("cognito-idp", region_name=region)

    challenge_responses = {
        "USERNAME": username,
        "SECRET_HASH": get_secret_hash(username, client_id, client_secret),
    }
    # Different response key based on MFA type
    if challenge_name == "SOFTWARE_TOKEN_MFA":
        challenge_responses["SOFTWARE_TOKEN_MFA_CODE"] = mfa_code
    else:
        challenge_responses["SMS_MFA_CODE"] = mfa_code

    result = client.respond_to_auth_challenge(
        ClientId=client_id, ChallengeName=challenge_name, Session=session, ChallengeResponses=challenge_responses
    )
    return result["AuthenticationResult"]


def decode_id_token(id_token):
    return jwt.decode(id_token, options={"verify_signature": False}).get("sub")


# --- Cubo Mobile Login ---
def cubo_mobile_login(uuid, username, access_token, user_agent):
    url = "https://mobile-api.getcubo.com/v2/user/login"
    payload = {
        "version": "2396",
        "lang": "en",
        "mobile_uuid": uuid,
        "provider": "Yun",
        "push_token": "dummy-token",
        "timezone": 0,
        "tp": "Android",
        "uid_p": uuid,
        "uname_p": username,
        "device_model": "sdk_gphone64_x86_64",
        "zone_name": "GMT",
    }
    headers = {
        "User-Agent": user_agent,
        "Accept-Encoding": "gzip",
        "Content-Type": "application/json; charset=UTF-8",
        "x-cb-authorization": f"Bearer {access_token}",
        "x-cspp-authorization": "",
        "x-refresh-authorization": "",
    }
    response = requests.post(url, json=payload, headers=headers)
    response.raise_for_status()
    return response.json()["data"]


# --- Token Refresh ---
def refresh_cubo_token(refresh_token, user_agent):
    url = "https://mobile-api.getcubo.com/v1/oauth/token"
    headers = {
        "x-cb-authorization": "",
        "x-cspp-authorization": "",
        "x-refresh-authorization": f"Bearer {refresh_token}",
        "User-Agent": user_agent,
    }
    response = requests.post(url, headers=headers)
    response.raise_for_status()
    data = response.json()
    if "data" in data:
        return data["data"]
    return data


def refresh_access_token_only(refresh_token, user_agent):
    log_to_file(f"Refreshing CuboAI token with refresh_token: {refresh_token[:12]}...")
    disk_token = load_refresh_token() or refresh_token
    resp = refresh_cubo_token(disk_token, user_agent)
    log_to_file(f"Token refresh response: {json.dumps(resp, indent=2)}")
    access_token = resp.get("access_token")
    new_refresh_token = resp.get("refresh_token", disk_token)
    save_access_token(access_token)
    save_refresh_token(new_refresh_token)
    return access_token, new_refresh_token, resp


# --- Camera Profiles ---
def get_camera_profiles(access_token, user_agent):
    url = "https://api.getcubo.com/prod/user/cameras"
    headers = {
        "User-Agent": user_agent,
        "Content-Type": "application/json",
        "x-cspp-authorization": f"Bearer {access_token}",
    }
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    data = response.json()
    device_map = {}
    for profile in data.get("profiles", []):
        try:
            profile_data = json.loads(profile.get("profile", "{}"))
            baby_name = profile_data.get("baby", "Unknown")
            device_id = profile.get("device_id")
            device_map[baby_name] = device_id
        except Exception:
            continue
    return device_map


def get_camera_profiles_raw(access_token, user_agent):
    url = "https://api.getcubo.com/prod/user/cameras"
    headers = {
        "User-Agent": user_agent,
        "Content-Type": "application/json",
        "x-cspp-authorization": f"Bearer {access_token}",
    }
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json().get("profiles", [])


# --- Alerts ---
"""
def get_n_alerts_paged(device_id, access_token, user_agent, n=5, hours_back=12, max_per_call=20):
    now = int(time.time())
    since_ts = now - hours_back * 60 * 60
    url_base = "https://api.getcubo.com/prod/timeline/alerts"
    headers = {
        "User-Agent": user_agent,
        "Content-Type": "application/json",
        "x-cspp-authorization": f"Bearer {access_token}"
    }
    all_alerts = {}
    while True:
        url = f"{url_base}?since={since_ts}"
        resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        alerts_data = resp.json().get("data", [])
        filtered = [a for a in alerts_data if a.get("device_id") == device_id]
        if not filtered:
            break
        for alert in filtered:
            all_alerts[alert["id"]] = alert
        if len(all_alerts) >= n:
            break
        if len(filtered) < max_per_call:
            break
        oldest_ts = min(a["ts"] for a in filtered)
        since_ts = oldest_ts - 1
    return sorted(all_alerts.values(), key=lambda a: a.get('ts', 0), reverse=True)[:n]
"""
# --- Alerts helpers ---


def _normalize_alert(a):
    import json

    params = a.get("params")
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except Exception:
            pass
    return {
        "id": a.get("id"),
        "device_id": a.get("device_id"),
        "type": a.get("type"),
        "ts": a.get("ts"),
        "created": a.get("created"),
        "image": a.get("image"),
        "params": params,
        "profile": a.get("profile"),
        "region": a.get("region"),
    }


def get_n_alerts_paged(device_id, access_token, user_agent, n=5, hours_back=12):
    """
    Fetch the latest N alerts for a specific device_id.
    Walk forward in time by advancing since to max(ts)+1 per page.
    Return alerts sorted by ts desc and truncated to N.
    """
    import requests

    now = int(time.time())
    since_ts = now - hours_back * 60 * 60
    url_base = "https://api.getcubo.com/prod/timeline/alerts"

    headers = {
        "User-Agent": user_agent or "okhttp/5.0.0-alpha.14",
        "Content-Type": "application/json",
        "x-cspp-authorization": f"Bearer {access_token}",
        "Accept-Encoding": "identity",
        "Connection": "Keep-Alive",
    }

    all_alerts_for_device = {}
    iters = 0
    max_iters = 100
    seen_progress = True

    while iters < max_iters and seen_progress:
        url = f"{url_base}?since={since_ts}"
        resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json().get("data", [])

        if not data:
            break

        ts_list = []
        for a in data:
            ts = a.get("ts", 0)
            if ts:
                ts_list.append(ts)
            if a.get("device_id") == device_id:
                all_alerts_for_device[a["id"]] = a

        max_ts_in_page = max(ts_list) if ts_list else since_ts
        next_since = max_ts_in_page + 1

        seen_progress = next_since > since_ts
        since_ts = next_since
        iters += 1

        if len(all_alerts_for_device) >= n:
            # optional: you can break here to be quicker
            pass

    # newest first, take N, normalize
    result = sorted(all_alerts_for_device.values(), key=lambda a: a.get("ts", 0), reverse=True)[:n]
    return [_normalize_alert(a) for a in result]


# --- Download image (alert photo) ---
def download_image(url, token, user_agent, save_dir, filename=None):
    if not os.path.exists(save_dir):
        os.makedirs(save_dir, exist_ok=True)
    if not filename:
        filename = url.split("/")[-1]
    save_path = os.path.join(save_dir, filename)
    headers = {"User-Agent": user_agent, "x-cspp-authorization": f"Bearer {token}", "Accept-Encoding": "gzip"}
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    with open(save_path, "wb") as f:
        f.write(resp.content)
    return save_path


# --- Subscription Info ---
def get_subscription_info(access_token, user_agent):
    url = "https://api.getcubo.com/prod/services/v1/subscribed"
    headers = {
        "User-Agent": user_agent,
        "Content-Type": "application/json",
        "x-cspp-authorization": f"Bearer {access_token}",
    }
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    result = resp.json().get("result", [])
    if not result:
        return None
    sub = result[0]
    return {
        "status": sub.get("status"),
        "kind": sub.get("kind"),
        "service_id": sub.get("service_id"),
        "device_id": sub.get("device_id"),
        "platform": sub.get("platform"),
        "service_start_date": sub.get("service_start_date"),
        "service_end_date": sub.get("service_end_date"),
        "grace_period_stop_date": sub.get("grace_period_stop_date"),
        "auto_renewal": sub.get("auto_renewal"),
        "note": sub.get("note"),
        "created": sub.get("created"),
        "order_id": sub.get("order_id"),
    }


# --- Camera State (online/offline) ---
def get_camera_state(device_id, access_token, user_agent):
    url_camera_state = f"https://api.getcubo.com/prod/camera/state?device_id={device_id}"
    headers = {
        "User-Agent": user_agent,
        "Content-Type": "application/json",
        "x-cspp-authorization": f"Bearer {access_token}",
    }
    response = requests.get(url_camera_state, headers=headers)
    response.raise_for_status()
    return response.json()
