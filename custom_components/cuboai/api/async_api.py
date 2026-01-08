"""Async API functions for CuboAI using aiohttp.

This module provides async versions of all CuboAI API calls,
eliminating the need for async_add_executor_job wrappers.
"""

import aiohttp

# API Base URLs
MOBILE_API_BASE = "https://mobile-api.getcubo.com"
API_BASE = "https://api.getcubo.com/prod"


def _get_common_headers(access_token: str, user_agent: str) -> dict:
    """Get common headers for CuboAI API requests."""
    return {
        "User-Agent": user_agent or "okhttp/5.0.0-alpha.14",
        "Content-Type": "application/json",
        "x-cspp-authorization": f"Bearer {access_token}",
        "Accept-Encoding": "gzip",
    }


async def cubo_mobile_login(
    uuid: str, username: str, access_token: str, user_agent: str, session: aiohttp.ClientSession | None = None
) -> dict:
    """Login to CuboAI mobile API.

    Args:
        uuid: Mobile device UUID
        username: User's email/username
        access_token: Cognito access token
        user_agent: User agent string
        session: Optional aiohttp session (creates new one if not provided)

    Returns:
        Login response data
    """
    url = f"{MOBILE_API_BASE}/v2/user/login"
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

    close_session = session is None
    session = session or aiohttp.ClientSession()
    try:
        async with session.post(url, json=payload, headers=headers) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data["data"]
    finally:
        if close_session:
            await session.close()


async def refresh_cubo_token(refresh_token: str, user_agent: str, session: aiohttp.ClientSession | None = None) -> dict:
    """Refresh CuboAI access token.

    Args:
        refresh_token: Current refresh token
        user_agent: User agent string
        session: Optional aiohttp session

    Returns:
        Token response containing new access_token and optionally refresh_token
    """
    url = f"{MOBILE_API_BASE}/v1/oauth/token"
    headers = {
        "x-cb-authorization": "",
        "x-cspp-authorization": "",
        "x-refresh-authorization": f"Bearer {refresh_token}",
        "User-Agent": user_agent,
    }

    close_session = session is None
    session = session or aiohttp.ClientSession()
    try:
        async with session.post(url, headers=headers) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data.get("data", data)
    finally:
        if close_session:
            await session.close()


async def get_camera_profiles(
    access_token: str, user_agent: str, session: aiohttp.ClientSession | None = None
) -> dict[str, str]:
    """Get camera profiles (baby name -> device_id mapping).

    Args:
        access_token: CuboAI access token
        user_agent: User agent string
        session: Optional aiohttp session

    Returns:
        Dict mapping baby names to device IDs
    """
    import json

    url = f"{API_BASE}/user/cameras"
    headers = _get_common_headers(access_token, user_agent)

    close_session = session is None
    session = session or aiohttp.ClientSession()
    try:
        async with session.get(url, headers=headers) as resp:
            resp.raise_for_status()
            data = await resp.json()

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
    finally:
        if close_session:
            await session.close()


async def get_camera_profiles_raw(
    access_token: str, user_agent: str, session: aiohttp.ClientSession | None = None
) -> list:
    """Get raw camera profiles data.

    Args:
        access_token: CuboAI access token
        user_agent: User agent string
        session: Optional aiohttp session

    Returns:
        List of profile objects
    """
    url = f"{API_BASE}/user/cameras"
    headers = _get_common_headers(access_token, user_agent)

    close_session = session is None
    session = session or aiohttp.ClientSession()
    try:
        async with session.get(url, headers=headers) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data.get("profiles", [])
    finally:
        if close_session:
            await session.close()


def _normalize_alert(alert: dict) -> dict:
    """Normalize alert data structure."""
    import json

    params = alert.get("params")
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except Exception:
            pass
    return {
        "id": alert.get("id"),
        "device_id": alert.get("device_id"),
        "type": alert.get("type"),
        "ts": alert.get("ts"),
        "created": alert.get("created"),
        "image": alert.get("image"),
        "params": params,
        "profile": alert.get("profile"),
        "region": alert.get("region"),
    }


async def get_n_alerts_paged(
    device_id: str,
    access_token: str,
    user_agent: str,
    n: int = 5,
    hours_back: int = 12,
    session: aiohttp.ClientSession | None = None,
) -> list[dict]:
    """Fetch the latest N alerts for a specific device.

    Walks forward in time by advancing since to max(ts)+1 per page.

    Args:
        device_id: Camera device ID
        access_token: CuboAI access token
        user_agent: User agent string
        n: Number of alerts to fetch
        hours_back: How far back to look for alerts
        session: Optional aiohttp session

    Returns:
        List of normalized alert dicts, sorted by timestamp descending
    """
    import time

    now = int(time.time())
    since_ts = now - hours_back * 60 * 60
    url_base = f"{API_BASE}/timeline/alerts"

    headers = {
        "User-Agent": user_agent or "okhttp/5.0.0-alpha.14",
        "Content-Type": "application/json",
        "x-cspp-authorization": f"Bearer {access_token}",
        "Accept-Encoding": "identity",
        "Connection": "Keep-Alive",
    }

    close_session = session is None
    session = session or aiohttp.ClientSession()
    try:
        all_alerts_for_device = {}
        iters = 0
        max_iters = 100
        seen_progress = True

        while iters < max_iters and seen_progress:
            url = f"{url_base}?since={since_ts}"
            async with session.get(url, headers=headers) as resp:
                resp.raise_for_status()
                response_data = await resp.json()
                data = response_data.get("data", [])

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
                # Can break early once we have enough
                pass

        # Sort newest first, take N, normalize
        result = sorted(all_alerts_for_device.values(), key=lambda a: a.get("ts", 0), reverse=True)[:n]
        return [_normalize_alert(a) for a in result]
    finally:
        if close_session:
            await session.close()


async def download_image(
    url: str,
    token: str,
    user_agent: str,
    save_dir: str,
    filename: str | None = None,
    session: aiohttp.ClientSession | None = None,
) -> str:
    """Download an alert image.

    Args:
        url: Image URL
        token: CuboAI access token
        user_agent: User agent string
        save_dir: Directory to save image
        filename: Optional filename (defaults to URL basename)
        session: Optional aiohttp session

    Returns:
        Path to saved image file
    """
    import os

    import aiofiles
    import aiofiles.os

    if not await aiofiles.os.path.exists(save_dir):
        await aiofiles.os.makedirs(save_dir, exist_ok=True)

    if not filename:
        filename = url.split("/")[-1]
    save_path = os.path.join(save_dir, filename)

    headers = {
        "User-Agent": user_agent,
        "x-cspp-authorization": f"Bearer {token}",
        "Accept-Encoding": "gzip",
    }

    close_session = session is None
    session = session or aiohttp.ClientSession()
    try:
        async with session.get(url, headers=headers) as resp:
            resp.raise_for_status()
            content = await resp.read()

        async with aiofiles.open(save_path, "wb") as f:
            await f.write(content)

        return save_path
    finally:
        if close_session:
            await session.close()


async def get_subscription_info(
    access_token: str, user_agent: str, session: aiohttp.ClientSession | None = None
) -> dict | None:
    """Get subscription information.

    Args:
        access_token: CuboAI access token
        user_agent: User agent string
        session: Optional aiohttp session

    Returns:
        Subscription info dict or None if no subscription
    """
    url = f"{API_BASE}/services/v1/subscribed"
    headers = _get_common_headers(access_token, user_agent)

    close_session = session is None
    session = session or aiohttp.ClientSession()
    try:
        async with session.get(url, headers=headers) as resp:
            resp.raise_for_status()
            data = await resp.json()

        result = data.get("result", [])
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
    finally:
        if close_session:
            await session.close()


async def get_camera_state(
    device_id: str, access_token: str, user_agent: str, session: aiohttp.ClientSession | None = None
) -> dict:
    """Get camera state (online/offline).

    Args:
        device_id: Camera device ID
        access_token: CuboAI access token
        user_agent: User agent string
        session: Optional aiohttp session

    Returns:
        Camera state response
    """
    url = f"{API_BASE}/camera/state?device_id={device_id}"
    headers = _get_common_headers(access_token, user_agent)

    close_session = session is None
    session = session or aiohttp.ClientSession()
    try:
        async with session.get(url, headers=headers) as resp:
            resp.raise_for_status()
            return await resp.json()
    finally:
        if close_session:
            await session.close()


async def get_camera_details(
    device_id: str, access_token: str, user_agent: str, session: aiohttp.ClientSession | None = None
) -> dict | None:
    """Get detailed camera information from /user/cameras endpoint.

    Extracts and combines data from the camera registration, profile,
    and report settings for a specific device.

    Args:
        device_id: Camera device ID
        access_token: CuboAI access token
        user_agent: User agent string
        session: Optional aiohttp session

    Returns:
        Dict with camera details or None if not found. Keys include:
        - device_id, license_id, created, role
        - baby_name, birth_date, gender, avatar_url
        - timezone, sleep_time, wakeup_time
        - alexa_enabled
    """
    import json

    url = f"{API_BASE}/user/cameras"
    headers = _get_common_headers(access_token, user_agent)

    close_session = session is None
    session = session or aiohttp.ClientSession()
    try:
        async with session.get(url, headers=headers) as resp:
            resp.raise_for_status()
            response_data = await resp.json()

        # Find the camera data
        camera_data = None
        for cam in response_data.get("data", []):
            if cam.get("device_id") == device_id:
                camera_data = cam
                break

        if not camera_data:
            return None

        # Find the profile data
        profile_data = {}
        for profile in response_data.get("profiles", []):
            if profile.get("device_id") == device_id:
                profile_json = profile.get("profile", "{}")
                try:
                    profile_data = json.loads(profile_json) if isinstance(profile_json, str) else profile_json
                except Exception:
                    profile_data = {}
                break

        # Find report settings
        report_settings = {}
        for settings in response_data.get("report_settings", []):
            if settings.get("device_id") == device_id:
                report_settings = settings
                break

        # Parse settings JSON from camera data
        settings = {}
        settings_json = camera_data.get("settings", "{}")
        try:
            settings = json.loads(settings_json) if isinstance(settings_json, str) else settings_json
        except Exception:
            settings = {}

        # Map gender: 0=male, 1=female
        gender_raw = profile_data.get("gender")
        gender = "male" if gender_raw == 0 else "female" if gender_raw == 1 else None

        return {
            # Camera registration info
            "device_id": device_id,
            "license_id": camera_data.get("license_id"),
            "created": camera_data.get("created"),
            "role": camera_data.get("role"),
            # Profile info
            "baby_name": profile_data.get("baby"),
            "birth_date": profile_data.get("birth"),
            "gender": gender,
            "avatar_url": profile_data.get("avatar"),
            # Settings
            "alexa_enabled": settings.get("alexa_enable", False),
            # Report settings
            "timezone": report_settings.get("time_zone"),
            "sleep_time": report_settings.get("sleep_time"),
            "wakeup_time": report_settings.get("wakeup_time"),
            "report_time": report_settings.get("report_time"),
            "gmt_offset": report_settings.get("gmt_offset"),
        }
    finally:
        if close_session:
            await session.close()
