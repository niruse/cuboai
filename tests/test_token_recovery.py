"""Exercise authentication recovery without credentials or a live CuboAI account.

The repository's HA stubs are used; API responses and HA's entry storage are
faked at their boundaries. Real setup, coordinator and login methods run.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components import cuboai
from custom_components.cuboai import auth, config_flow
from custom_components.cuboai.coordinator import CuboAICoordinator


def _entry(account="account-a"):
    return SimpleNamespace(
        entry_id=account,
        unique_id=account,
        data={
            "uuid": account,
            "username": "parent@example.com",
            "access_token": "entry-access",
            "refresh_token": "entry-refresh",
            "user_agent": "test-agent",
            "cameras": [{"device_id": "camera-a", "baby_name": "Baby"}],
            "selected_camera_ids": ["camera-a"],
        },
        options={"download_images": False},
    )


def _hass():
    hass = MagicMock()
    hass.data = {}

    async def execute(func, *args):
        return func(*args)

    def update(entry, **kwargs):
        for key, value in kwargs.items():
            setattr(entry, key, value)

    hass.async_add_executor_job = execute
    hass.config_entries.async_update_entry.side_effect = update
    hass.config_entries.async_reload = AsyncMock()
    return hass


def _http_error(status):
    return aiohttp.ClientResponseError(MagicMock(real_url="https://example.invalid"), (), status=status)


@pytest.mark.parametrize("status", [401, 429, 500])
async def test_rejected_refresh_requires_signin_but_server_errors_remain_retryable(monkeypatch, status):
    entry, hass = _entry(), _hass()
    monkeypatch.setattr(auth, "refresh_cubo_token", AsyncMock(side_effect=_http_error(status)))
    error = ConfigEntryAuthFailed if status == 401 else aiohttp.ClientResponseError
    with pytest.raises(error):
        await auth.async_refresh_entry_tokens(hass, entry, "agent", None)
    hass.config_entries.async_update_entry.assert_not_called()
    assert entry.data["refresh_token"] == "entry-refresh"


async def test_timeout_does_not_invalidate_credentials(monkeypatch):
    entry, hass = _entry(), _hass()
    monkeypatch.setattr(auth, "refresh_cubo_token", AsyncMock(side_effect=TimeoutError))
    with pytest.raises(TimeoutError):
        await auth.async_refresh_entry_tokens(hass, entry, "agent", None)
    hass.config_entries.async_update_entry.assert_not_called()


async def test_missing_refresh_token_prompts_for_signin(monkeypatch):
    entry = _entry()
    entry.data.pop("refresh_token")
    refresh = AsyncMock()
    monkeypatch.setattr(auth, "refresh_cubo_token", refresh)
    with pytest.raises(ConfigEntryAuthFailed):
        await auth.async_refresh_entry_tokens(_hass(), entry, "agent", None)
    refresh.assert_not_called()


@pytest.mark.parametrize("response", [{}, {"access_token": ""}, {"access_token": "new", "refresh_token": None}, []])
async def test_incomplete_response_keeps_previous_pair(monkeypatch, response):
    hass = _hass()
    monkeypatch.setattr(auth, "refresh_cubo_token", AsyncMock(return_value=response))
    with pytest.raises(UpdateFailed):
        await auth.async_refresh_entry_tokens(hass, _entry(), "agent", None)
    hass.config_entries.async_update_entry.assert_not_called()


@pytest.mark.parametrize("rotated", [True, False])
async def test_refresh_persists_pair_and_retains_refresh_if_omitted(monkeypatch, rotated):
    entry, hass = _entry(), _hass()
    response = {"access_token": "new-access"}
    if rotated:
        response["refresh_token"] = "new-refresh"
    refresh = AsyncMock(return_value=response)
    monkeypatch.setattr(auth, "refresh_cubo_token", refresh)
    pair = await auth.async_refresh_entry_tokens(hass, entry, "agent", None)
    assert pair == ("new-access", "new-refresh" if rotated else "entry-refresh")
    assert (entry.data["access_token"], entry.data["refresh_token"]) == pair
    assert entry.data["selected_camera_ids"] == ["camera-a"]


async def test_two_accounts_refresh_independently(monkeypatch):
    first, second, hass = _entry(), _entry("account-b"), _hass()
    second.data["refresh_token"] = "second-refresh"

    async def renew(token, *args):
        await asyncio.sleep(0)
        return {"access_token": f"access-for-{token}", "refresh_token": f"next-{token}"}

    monkeypatch.setattr(auth, "refresh_cubo_token", renew)
    await asyncio.gather(
        auth.async_refresh_entry_tokens(hass, first, "agent", None),
        auth.async_refresh_entry_tokens(hass, second, "agent", None),
    )
    assert first.data["refresh_token"] == "next-entry-refresh"
    assert second.data["refresh_token"] == "next-second-refresh"


async def test_coordinator_propagates_reauth_and_uses_rotated_tokens(monkeypatch):
    entry, hass = _entry(), _hass()
    coord = CuboAICoordinator(hass, entry, "entry-access", "entry-refresh", "agent")
    coord._get_session = AsyncMock(return_value=None)
    coord._fetch_all = AsyncMock(side_effect=[_http_error(401), {"ok": True}])
    refresh = AsyncMock(return_value={"access_token": "renewed", "refresh_token": "rotated"})
    monkeypatch.setattr(auth, "refresh_cubo_token", refresh)
    assert await coord._async_update_data() == {"ok": True}
    assert coord._access_token == entry.data["access_token"] == "renewed"
    assert coord._refresh_token == entry.data["refresh_token"] == "rotated"
    refresh.side_effect = _http_error(401)
    coord._fetch_all.side_effect = _http_error(401)
    with pytest.raises(ConfigEntryAuthFailed):
        await coord._async_update_data()
    assert coord._fetch_all.await_count == 3  # No data retry after rejected renewal.


async def _prepare_setup(monkeypatch, tmp_path):
    hass = _hass()
    hass.config.path.side_effect = lambda *parts: str(tmp_path.joinpath(*parts))
    monkeypatch.setattr(cuboai, "async_ensure_dependencies", AsyncMock(return_value=True))
    monkeypatch.setattr(cuboai, "set_debug_logs_enabled", lambda *args: None)
    monkeypatch.setattr(cuboai, "_setup_component_logger", lambda *args: None)
    from custom_components.cuboai import media_library

    monkeypatch.setattr(media_library, "async_setup_services", AsyncMock())
    return hass


async def test_startup_ignores_stale_global_json_after_fresh_login(monkeypatch, tmp_path):
    for filename, key in (
        ("cuboai_access_token.json", "access_token"),
        ("cuboai_refresh_token.json", "refresh_token"),
    ):
        (tmp_path / filename).write_text(json.dumps({key: "stale"}))
    hass = await _prepare_setup(monkeypatch, tmp_path)
    entry = _entry()
    profiles = AsyncMock(return_value=entry.data["cameras"])
    monkeypatch.setattr(cuboai, "get_camera_profiles", profiles)
    refresh = AsyncMock()
    monkeypatch.setattr(auth, "refresh_cubo_token", refresh)

    class SetupReached(Exception):
        pass

    # Stop after the real startup authentication path, before stream/platform setup.
    monkeypatch.setattr(
        CuboAICoordinator, "async_config_entry_first_refresh", AsyncMock(side_effect=SetupReached), raising=False
    )
    with pytest.raises(SetupReached):
        await cuboai.async_setup_entry(hass, entry)
    assert profiles.call_args.args[0] == "entry-access"
    refresh.assert_not_called()
    assert json.loads((tmp_path / "cuboai_refresh_token.json").read_text())["refresh_token"] == "stale"


async def test_startup_does_not_swallow_rejected_refresh(monkeypatch, tmp_path):
    hass = await _prepare_setup(monkeypatch, tmp_path)
    monkeypatch.setattr(cuboai, "get_camera_profiles", AsyncMock(side_effect=_http_error(401)))
    monkeypatch.setattr(auth, "refresh_cubo_token", AsyncMock(side_effect=_http_error(401)))
    with pytest.raises(ConfigEntryAuthFailed):
        await cuboai.async_setup_entry(hass, _entry())


def _listener_hass(entry):
    hass = _hass()
    hass.data = {
        "cuboai": {
            entry.entry_id: {
                "options_snapshot": dict(entry.options),
                "data_snapshot": cuboai._non_auth_data(entry.data),
            }
        }
    }
    return hass


async def test_token_data_update_does_not_reload_but_option_change_does():
    entry = _entry()
    hass = _listener_hass(entry)
    entry.data = {**entry.data, "access_token": "renewed", "refresh_token": "rotated", "user_agent": "new-agent"}
    await cuboai.async_update_options(hass, entry)
    hass.config_entries.async_reload.assert_not_called()
    entry.options["download_images"] = True
    await cuboai.async_update_options(hass, entry)
    hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)


async def test_camera_selection_in_configure_still_reloads():
    """The camera selection is stored in entry DATA with the options unchanged.
    Skipping every data-only write (not just the session) would leave a new
    selection unapplied until the next Home Assistant restart."""
    entry = _entry()
    hass = _listener_hass(entry)
    entry.data = {**entry.data, "selected_camera_ids": [], "cameras": []}
    await cuboai.async_update_options(hass, entry)
    hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)


def test_reauth_uses_no_helpers_newer_than_the_declared_minimum():
    """hacs.json declares HA 2024.1; _get_reauth_entry() and
    async_update_reload_and_abort(data_updates=...) arrived in 2024.11."""
    import ast
    import pathlib

    root = pathlib.Path(config_flow.__file__).parents[2]
    minimum = tuple(int(p) for p in json.loads((root / "hacs.json").read_text())["homeassistant"].split(".")[:2])
    tree = ast.parse(pathlib.Path(config_flow.__file__).read_text(encoding="utf-8"))
    used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    used |= {n.arg for n in ast.walk(tree) if isinstance(n, ast.keyword) and n.arg}
    if minimum < (2024, 11):
        for helper in ("_get_reauth_entry", "data_updates", "async_update_reload_and_abort"):
            assert helper not in used, f"{helper} needs HA 2024.11, hacs.json declares {minimum}"


def _flow(entry):
    flow = config_flow.CuboAIConfigFlow()
    flow.hass = _hass()
    flow.context = {"source": "reauth", "entry_id": entry.entry_id}
    flow.hass.config_entries.async_get_entry = lambda entry_id: entry if entry_id == entry.entry_id else None
    flow.hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())
    flow.async_show_form = lambda **kw: {"type": "form", **kw}
    flow.async_abort = lambda **kw: {"type": "abort", **kw}
    return flow


async def test_reauth_form_prefills_the_account():
    entry = _entry()
    result = await _flow(entry).async_step_reauth(entry.data)
    assert result["step_id"] == "reauth_confirm"
    assert result["data_schema"]({"password": "x"})["username"] == "parent@example.com"


@pytest.mark.parametrize("mfa", [False, True])
@pytest.mark.parametrize("same_account", [False, True])
async def test_password_and_mfa_reauth_preserve_entry_and_reject_wrong_account(monkeypatch, mfa, same_account):
    entry = _entry()
    original_data, original_options = dict(entry.data), dict(entry.options)
    flow = _flow(entry)
    result = await flow.async_step_reauth(entry.data)
    assert result["step_id"] == "reauth_confirm"
    api = config_flow.api
    monkeypatch.setattr(api, "initiate_user_srp_auth", MagicMock(return_value=({}, None, None, {})))
    tokens = {"IdToken": "id-token", "AccessToken": "cognito-access"}
    monkeypatch.setattr(
        api,
        "respond_to_password_verifier",
        MagicMock(
            return_value=(
                {"challenge": "SMS_MFA", "session": "session", "username": "parent@example.com"} if mfa else tokens
            )
        ),
    )
    monkeypatch.setattr(api, "respond_to_mfa_challenge", MagicMock(return_value=tokens))
    monkeypatch.setattr(api, "decode_id_token", lambda token: "account-a" if same_account else "other-account")
    monkeypatch.setattr(
        api, "cubo_mobile_login", MagicMock(return_value={"access_token": "new-access", "refresh_token": "new-refresh"})
    )
    result = await flow.async_step_reauth_confirm({"username": "parent@example.com", "password": "dummy"})
    if mfa:
        assert result["step_id"] == "mfa"
        result = await flow.async_step_mfa({"mfa_code": "123456"})
    assert result["reason"] == ("reauth_successful" if same_account else "wrong_account")
    assert entry.entry_id == "account-a"
    assert entry.options == original_options
    assert entry.data["cameras"] == original_data["cameras"]
    assert entry.data["selected_camera_ids"] == original_data["selected_camera_ids"]
    if same_account:
        assert entry.data["refresh_token"] == "new-refresh"
        flow.hass.config_entries.async_reload.assert_called_once_with(entry.entry_id)
    else:
        assert entry.data == original_data
        flow.hass.config_entries.async_reload.assert_not_called()


async def test_bad_password_keeps_reauth_form_and_existing_credentials(monkeypatch):
    entry = _entry()
    flow = _flow(entry)
    await flow.async_step_reauth(entry.data)
    monkeypatch.setattr(
        config_flow.api, "initiate_user_srp_auth", MagicMock(side_effect=ValueError("NotAuthorizedException"))
    )
    result = await flow.async_step_reauth_confirm({"username": "parent@example.com", "password": "bad"})
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "auth_failed"}
    assert entry.data["refresh_token"] == "entry-refresh"
    flow.hass.config_entries.async_reload.assert_not_called()


async def test_startup_persists_rotation_even_when_profile_fetch_is_empty(monkeypatch, tmp_path):
    hass = await _prepare_setup(monkeypatch, tmp_path)
    entry = _entry()
    monkeypatch.setattr(cuboai, "get_camera_profiles", AsyncMock(side_effect=[_http_error(401), []]))
    monkeypatch.setattr(
        auth, "refresh_cubo_token", AsyncMock(return_value={"access_token": "new", "refresh_token": "rotated"})
    )

    class SetupReached(Exception):
        pass

    monkeypatch.setattr(
        CuboAICoordinator, "async_config_entry_first_refresh", AsyncMock(side_effect=SetupReached), raising=False
    )
    with pytest.raises(SetupReached):
        await cuboai.async_setup_entry(hass, entry)
    assert entry.data["access_token"] == "new"
    assert entry.data["refresh_token"] == "rotated"
    assert entry.data["selected_camera_ids"] == ["camera-a"]
