"""Dump the integration's setup and Configure forms to forms.json for the docs images.

The forms are built by the REAL config flow (with the test suite's Home
Assistant stubs), so the rendered screenshots always show the actual fields,
order, defaults, ranges and labels. Sample data only: two made-up cameras.

    python tools/docs-images/dump_forms.py
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import tests.conftest  # noqa: E402,F401  (installs the Home Assistant stubs)


class MultiSelect:
    """Stands in for cv.multi_select so the choices survive into the dump."""

    def __init__(self, choices):
        self.choices = dict(choices)

    def __call__(self, value):  # voluptuous compiles schema values as validators
        return value


# `import homeassistant.helpers.config_validation as cv` resolves through the
# attribute chain of the stubbed package, so patch it there as well.
_cv = sys.modules["homeassistant"].helpers.config_validation
_cv.multi_select = MultiSelect
sys.modules["homeassistant.helpers.config_validation"].multi_select = MultiSelect

import voluptuous as vol  # noqa: E402

from custom_components.cuboai import config_flow as cf  # noqa: E402

CAMERAS = [
    {"device_id": "CB02XXXXXXXX0001", "baby_name": "Baby"},
    {"device_id": "SW05XXXXXXXX0002", "baby_name": "Nursery"},
]
EN = json.loads((ROOT / "custom_components/cuboai/translations/en.json").read_text(encoding="utf-8"))
OUT = Path(__file__).resolve().parent / "forms.json"


def _kind(validator):
    """(type, extra) of one voluptuous validator."""
    if isinstance(validator, MultiSelect):
        return "multi", {"choices": validator.choices}
    if validator is bool:
        return "bool", {}
    if validator is str:
        return "text", {}
    if isinstance(validator, vol.All):
        rng = next((v for v in validator.validators if isinstance(v, vol.Range)), None)
        if rng is not None:
            return "int", {"min": rng.min, "max": rng.max}
        return "int", {}
    if isinstance(validator, vol.In):
        return "select", {"choices": dict(validator.container)}
    return "text", {}


def _fields(schema, labels, descriptions):
    fields = []
    for key, validator in schema.schema.items():
        name = str(key)
        default = key.default() if key.default is not vol.UNDEFINED else None
        suggested = (getattr(key, "description", None) or {}).get("suggested_value")
        kind, extra = _kind(validator)
        if "password" in name:
            kind = "password"
        if isinstance(default, MultiSelect):
            default = None
        fields.append(
            {
                "key": name,
                "label": labels.get(name) or name,
                "help": descriptions.get(name, ""),
                "type": kind,
                "default": default if default is not None else suggested,
                "required": isinstance(key, vol.Required),
                **extra,
            }
        )
    return fields


def _step(section, step):
    s = EN[section]["step"][step]
    return s.get("title", ""), s.get("description", ""), s.get("data", {}), s.get("data_description", {})


async def main():
    hass = MagicMock()
    hass.data = {}

    async def _run(func, *args):
        return func(*args)

    hass.async_add_executor_job = _run
    hass.config_entries.async_entries = lambda domain: []

    forms = {}
    with (
        patch.object(cf, "setup_file_logger", MagicMock()),
        patch("custom_components.cuboai.utils.find_available_port", return_value=8557),
    ):
        # Login and MFA: the step schemas are module constants.
        title, desc, labels, helps = _step("config", "user")
        forms["setup-login"] = {"title": title, "description": desc, "fields": _fields(cf.AUTH_SCHEMA, labels, helps)}
        title, desc, labels, helps = _step("config", "mfa")
        forms["setup-mfa"] = {
            "title": title,
            "description": desc.replace("{mfa_type}", "authenticator app"),
            "fields": _fields(cf.MFA_SCHEMA, labels, helps),
        }

        # Select cameras: the multi-select the step builds.
        title, desc, labels, helps = _step("config", "select_cameras")
        choices = {c["device_id"]: f"{c['baby_name']} ({c['device_id']})" for c in CAMERAS}
        cams_schema = vol.Schema({vol.Required("cameras", default=list(choices)): MultiSelect(choices)})
        forms["setup-cameras"] = {"title": title, "description": desc, "fields": _fields(cams_schema, labels, helps)}

        # First options step, through the real flow.
        flow = cf.CuboAIConfigFlow()
        flow.hass = hass
        flow._auth_data = {"username": "parent@example.com", "cameras": CAMERAS}
        flow.async_show_form = lambda **kw: kw
        form = await flow.async_step_config()
        title, desc, labels, helps = _step("config", "config")
        forms["setup-options"] = {
            "title": title,
            "description": desc,
            "fields": _fields(form["data_schema"], labels, helps),
        }

        # Configure, through the real options flow.
        entry = MagicMock(
            entry_id="entryA",
            options={"history_sensors": True},
            data={"cameras": CAMERAS, "all_cameras": CAMERAS},
        )
        options_flow = cf.CuboAIOptionsFlowHandler()
        options_flow.hass, options_flow.config_entry = hass, entry
        options_flow.async_show_form = lambda **kw: kw
        form = await options_flow.async_step_init()
        title, desc, labels, helps = _step("options", "init")
        forms["configure"] = {
            "title": title,
            "description": desc,
            "fields": _fields(form["data_schema"], labels, helps),
        }

    OUT.write_text(json.dumps(forms, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for name, f in forms.items():
        raw = [x["key"] for x in f["fields"] if x["label"] == x["key"]]
        print(f"{name}: {len(f['fields'])} fields; unlabelled: {raw or 'none'}")


if __name__ == "__main__":
    asyncio.run(main())
