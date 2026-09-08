"""One CuboAI ACCOUNT per config entry — adding the same one twice is refused.

Nothing stopped a duplicate add before: the flow never set a unique_id, so the
same account could be configured N times and every entry's entities collided on
identical unique_ids (`cuboai_<thing>_<device_id>` is derived from the camera,
not the entry).

A DIFFERENT account must still be allowed — v2.6.28 made two entries genuinely
independent at the go2rtc/port layer, and this guard must not undo that.

The identity is the Cognito `sub` claim (`api.decode_id_token` -> entry.data
["uuid"]), not the e-mail: an e-mail can be retyped with different casing or
reached through an alias, and would let a duplicate slip through.

NOTE these are the first tests in the repo that drive the real config flow. They
only work because conftest gives `homeassistant.config_entries` real ConfigFlow /
OptionsFlow bases — see the comment there.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from custom_components.cuboai import config_flow as cf


class _Aborted(Exception):
    """Stands in for HA's AbortFlow."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _flow(existing_ids=()):
    """A CuboAIConfigFlow with the unique-id machinery HA normally provides."""
    flow = cf.CuboAIConfigFlow()
    flow.hass = MagicMock()
    flow._set_id = None
    flow._existing = set(existing_ids)

    async def _set_unique_id(uid):
        flow._set_id = uid
        return None

    def _abort_if_configured(*a, **k):
        if flow._set_id in flow._existing:
            raise _Aborted("already_configured")

    flow.async_set_unique_id = _set_unique_id
    flow._abort_if_unique_id_configured = _abort_if_configured
    flow.async_show_form = lambda **kw: {"type": "form", **kw}
    flow.async_create_entry = lambda **kw: {"type": "entry", **kw}
    return flow


def _auth(uuid="cognito-sub-aaa", username="Parent@Example.com"):
    return {
        "uuid": uuid,
        "username": username,
        "cameras": [{"device_id": "DEV1", "baby_name": "Mia"}],
        "all_cameras": [{"device_id": "DEV1", "baby_name": "Mia"}],
    }


@pytest.mark.asyncio
async def test_same_account_added_twice_is_refused():
    flow = _flow(existing_ids={"cognito-sub-aaa"})
    flow._auth_data = _auth()
    with pytest.raises(_Aborted) as e:
        await flow.async_step_select_cameras()
    assert e.value.reason == "already_configured"


@pytest.mark.asyncio
async def test_a_different_account_is_still_allowed():
    """v2.6.28 made two entries safe; this guard must not block a real second one."""
    flow = _flow(existing_ids={"cognito-sub-aaa"})
    flow._auth_data = _auth(uuid="cognito-sub-bbb", username="other@example.com")
    result = await flow.async_step_select_cameras()
    assert result["type"] == "form"
    assert flow._set_id == "cognito-sub-bbb"


@pytest.mark.asyncio
async def test_identity_is_the_account_uuid_not_the_email():
    flow = _flow()
    flow._auth_data = _auth()
    await flow.async_step_select_cameras()
    assert flow._set_id == "cognito-sub-aaa"
    assert "@" not in flow._set_id


@pytest.mark.asyncio
async def test_falls_back_to_a_normalised_email_when_uuid_is_missing():
    """Defensive: an entry with no uuid still gets a stable-ish id, and the
    e-mail is lower-cased so casing alone cannot create a duplicate."""
    flow = _flow()
    flow._auth_data = _auth(uuid="")
    await flow.async_step_select_cameras()
    assert flow._set_id == "parent@example.com"


@pytest.mark.asyncio
async def test_the_guard_runs_before_the_camera_form():
    """Aborting only at async_step_config would waste the user's camera picking."""
    flow = _flow(existing_ids={"cognito-sub-aaa"})
    flow._auth_data = _auth()
    called = []
    flow.async_show_form = lambda **kw: called.append(kw) or {"type": "form"}
    with pytest.raises(_Aborted):
        await flow.async_step_select_cameras()
    assert not called, "the camera form was shown before the duplicate was refused"


# =============================================================================
# Backfill for entries created before the flow set a unique_id
# =============================================================================


def test_backfill_source_pins():
    """Two properties of the backfill in __init__.py that must not drift.

    It is a source pin because async_setup_entry needs the whole HA runtime;
    what matters here is (a) it only fills an EMPTY unique_id, and (b) it runs
    before add_update_listener — async_update_options derives changed_keys from
    OPTIONS only, so a unique_id-only write leaves that empty and its guard
    falls through to a full async_reload on every upgrade start.
    """
    src = Path("custom_components/cuboai/__init__.py").read_text(encoding="utf-8")
    flat = " ".join(src.split())
    assert 'if entry.unique_id is None and entry.data.get("uuid")' in flat
    assert 'async_update_entry(entry, unique_id=str(entry.data["uuid"]))' in flat
    # Anchor on the real registration CALL, not any prose mention of it.
    registration = "entry.async_on_unload(entry.add_update_listener("
    assert registration in src
    assert src.index("entry.unique_id is None") < src.index(registration), (
        "the unique_id backfill must run BEFORE add_update_listener or it triggers a reload"
    )


def test_the_abort_string_talks_about_the_account_not_a_camera():
    """The key predates this change and used to read 'This camera is already
    configured.', which is wrong for an account-level abort."""
    for path in (
        "custom_components/cuboai/strings.json",
        "custom_components/cuboai/translations/en.json",
    ):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        msg = data["config"]["abort"]["already_configured"]
        assert "account" in msg.lower(), f"{path}: {msg}"
        assert "camera" not in msg.lower(), f"{path}: {msg}"
