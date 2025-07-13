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

# --- Ensure warrant is in sys.path ---
deps_path = "/config/deps/lib/python3.12/site-packages"
if deps_path not in sys.path:
    sys.path.append(deps_path)

try:
    from warrant.aws_srp import AWSSRP
except ImportError:
    raise ImportError(
        "warrant==0.6.1 is not installed. Run this once from Terminal:\n"
        "pip install --target /config/deps/lib/python3.12/site-packages --upgrade --no-deps warrant==0.6.1"
    )

# --- Utility ---
def get_secret_hash(username, client_id, client_secret):
    msg = username + client_id
    dig = hmac.new(client_secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(dig).decode()

# === Step 1: Initiate SRP ===
def initiate_user_srp_auth(username, password, pool_id, client_id, client_secret, user_agent, region="us-east-1"):
    client = boto3.client("cognito-idp", region_name=region)
    aws = AWSSRP(username=username, password=password, pool_id=pool_id, client_id=client_id, client=client)

    auth_params = aws.get_auth_params()
    auth_params["SECRET_HASH"] = get_secret_hash(username, client_id, client_secret)

    # boto3 does not use User-Agent for Cognito, but if you want to log it, you can do so here
    return client.initiate_auth(
        AuthFlow="USER_SRP_AUTH",
        AuthParameters=auth_params,
        ClientId=client_id
    ), aws, client

# === Step 2: Password Verifier ===
def respond_to_password_verifier(resp, aws, client, client_id, client_secret, user_agent):
    challenge_params = resp["ChallengeParameters"]
    challenge_responses = aws.process_challenge(challenge_params)
    username = challenge_params["USER_ID_FOR_SRP"]
    challenge_responses["SECRET_HASH"] = get_secret_hash(username, client_id, client_secret)
    # boto3 does not use User-Agent for Cognito, but if you want to log it, you can do so here
    return client.respond_to_auth_challenge(
        ClientId=client_id,
        ChallengeName="PASSWORD_VERIFIER",
        ChallengeResponses=challenge_responses
    )["AuthenticationResult"]

# === Step 3: Decode UUID from IdToken ===
def decode_id_token(id_token):
    return jwt.decode(id_token, options={"verify_signature": False}).get("sub")

# === Step 4: Cubo Mobile Login ===
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

# === Step 5: Refresh Cubo Token ===
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
    return response.json()

# --- Camera Profiles (baby name/device map and full list) ---
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
    return device_map  # dict of { baby_name: device_id }

def get_camera_profiles_raw(access_token, user_agent):
    """Fetches full camera profiles list (for attributes/etc)."""
    url = "https://api.getcubo.com/prod/user/cameras"
    headers = {
        "User-Agent": user_agent,
        "Content-Type": "application/json",
        "x-cspp-authorization": f"Bearer {access_token}"
    }
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json().get("profiles", [])

# --- Alerts (Notifications) ---
def get_recent_alerts(device_id, access_token, user_agent, hours=2, max_alerts=5):
    now = int(time.time())
    since_ts = now - hours * 60 * 60
    url = f"https://api.getcubo.com/prod/timeline/alerts?since={since_ts}"
    headers = {
        "User-Agent": user_agent,
        "Content-Type": "application/json",
        "x-cspp-authorization": f"Bearer {access_token}"
    }
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    alerts_data = resp.json().get("data", [])
    filtered = [a for a in alerts_data if a.get("device_id") == device_id]
    filtered_sorted = sorted(filtered, key=lambda a: a.get("ts", 0), reverse=True)
    return filtered_sorted[:max_alerts]

# --- Download image (alert photo) ---
def download_image(url, token, user_agent, save_dir, filename=None):
    """Downloads an image with authentication header and passed User-Agent."""
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
