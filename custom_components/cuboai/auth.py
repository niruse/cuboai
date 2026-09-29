"""Account-scoped token renewal and Home Assistant authentication recovery."""

import aiohttp
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from .api.async_api import refresh_cubo_token


async def async_refresh_entry_tokens(hass, entry, user_agent, session):
    """Renew and persist a token pair on its owning entry, never shared files.

    A rejected refresh credential needs user action. Network/server failures
    remain retryable and must not destroy the existing credentials.
    """
    refresh_token = entry.data.get("refresh_token")
    if not refresh_token:
        raise ConfigEntryAuthFailed("CuboAI session is missing. Please sign in again.")
    try:
        response = await refresh_cubo_token(refresh_token, user_agent, session)
    except aiohttp.ClientResponseError as err:
        if err.status == 401:
            raise ConfigEntryAuthFailed("CuboAI session was rejected. Please sign in again.") from err
        raise

    access_token = response.get("access_token") if isinstance(response, dict) else None
    new_refresh = response.get("refresh_token", refresh_token) if isinstance(response, dict) else None
    if not isinstance(access_token, str) or not access_token or not isinstance(new_refresh, str) or not new_refresh:
        raise UpdateFailed("CuboAI returned an incomplete token response")
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "access_token": access_token, "refresh_token": new_refresh}
    )
    return access_token, new_refresh
