import base64
import hashlib
import hmac
import boto3
import requests
import jwt
import os
import json
import sys
import time
import glob
from datetime import datetime
from custom_components.cuboai.utils import log_to_file


ACCESS_TOKEN_FILE = "/config/cuboai_access_token.json"
REFRESH_TOKEN_FILE = "/config/cuboai_refresh_token.json"


# --- Access/Refresh Token Save/Load ---
def save_access_token(access_token):
    try:
        with open(ACCESS_TOKEN_FILE, "w", encoding="utf-8") as f:
            json.dump({"access_token": access_token}, f)
    except Exception as e:
        log_to_file(f"Failed to save access_token: {e}")


def load_access_token():
    try:
        with open(ACCESS_TOKEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("access_token")
    except Exception:
        return None


def save_refresh_token(refresh_token):
    try:
        with open(REFRESH_TOKEN_FILE, "w", encoding="utf-8") as f:
            json.dump({"refresh_token": refresh_token}, f)
    except Exception as e:
        log_to_file(f"Failed to save refresh_token: {e}")


def load_refresh_token():
    try:
        with open(REFRESH_TOKEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("refresh_token")
    except Exception:
        return None


# --- Cognito SRP Utilities ---
def ensure_warrant_installed():
    """
    Ensure 'warrant' can be imported regardless of Python version.
    Looks for the correct site-packages path inside /config/deps/lib/.
    """
    deps_path = f"/config/deps/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"

    paths_to_try = [deps_path]
    candidates = glob.glob("/config/deps/lib/python*/site-packages")
    for c in candidates:
        if c not in paths_to_try:
            paths_to_try.append(c)

    found = False
    for path in paths_to_try:
        if os.path.isdir(path):
            if path not in sys.path:
                sys.path.append(path)
            found = True
            break

    if not found:
        raise ImportError(
            "No valid deps path found under /config/deps/lib/. "
            "You may need to install warrant manually."
        )

    try:
        from warrant.aws_srp import AWSSRP  # noqa: F401
    except ImportError as e:
        raise ImportError(
            "warrant==0.6.1 is not installed. Run this from Terminal:\n"
            f"pip install --target {path} --upgrade --no-deps warrant==0.6.1"
        ) from e

    return True


# make sure it's available before import
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
    return client.initiate_auth(
        AuthFlow="USER_SRP_AUTH",
        AuthParameters=auth_params,
        ClientId=client_id
    ), aws, client


def respond_to_password_verifier(resp, aws, client, client_id, client_secret, user_agent):
    challenge_params = resp["ChallengeParameters"]
    challenge_responses = aws.process_challenge(challenge_params)
    username = challenge_params["USER_ID_FOR_SRP"]
    challenge_responses["SECRET_HASH"] = get_secret_hash(username, client_id, client_secret)
    return client.respond_to_auth_challenge(
        ClientId=client_id,
        ChallengeName="PASSWORD_VERIFIER",
        ChallengeResponses=challenge_responses
    )["AuthenticationResult"]


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
        "zone_name": "GMT"
    }
    headers = {
        "User-Agent": user_agent,
        "Accept-Encoding": "gzip",
        "Content-Type": "application/json; charset=UTF-8",
        "x-cb-authorization": f"Bearer {access_token}",
        "x-cspp-authorization": "",
        "x-refresh-authorization": ""
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
        "User-Agent": user_agent
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
        "x-cspp-authorization": f"Bearer {access_token}"
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
        "x-cspp-authorization": f"Bearer {access_token}"
    }
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json().get("profiles", [])


# --- Alerts ---
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


# --- Download image (alert photo) ---
def download_image(url, token, user_agent, save_dir, filename=None):
    if not os.path.exists(save_dir):
        os.makedirs(save_dir, exist_ok=True)
    if not filename:
        filename = url.split("/")[-1]
    save_path = os.path.join(save_dir, filename)
    headers = {
        "User-Agent": user_agent,
        "x-cspp-authorization": f"Bearer {token}",
        "Accept-Encoding": "gzip"
    }
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
        "x-cspp-authorization": f"Bearer {access_token}"
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
        "x-cspp-authorization": f"Bearer {access_token}"
    }
    response = requests.get(url_camera_state, headers=headers)
    response.raise_for_status()
    return response.json()