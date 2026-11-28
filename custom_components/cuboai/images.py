"""Alert photos: kept out of `www`, served only with a signed address.

Until v2.6.67 the photos were written to `<config>/www/cuboai_images/`. Home
Assistant serves everything under `www` at `/local/` to anyone who can reach
it, with no login, and a photo's name is built from the camera's serial
number, the alert time and three hex characters, so it could be guessed
(issue #139). These are pictures of a child's room.

They now live in `<config>/cuboai_images/`, which Home Assistant does not
serve. The integration serves them itself at
`/api/cuboai/alert_image/<file>?token=<signature>`. The signature is an HMAC
of the file name under a random key that never leaves this installation, so an
address is only known to someone who can already read the entity's attributes
in Home Assistant (a logged-in user). This is the same idea as the `token`
Home Assistant puts in a camera's `entity_picture`; an `<img>` tag cannot send
a login, so the address has to carry its own proof.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

IMAGES_FOLDER = "cuboai_images"
URL_BASE = "/api/cuboai/alert_image"
_DATA_KEY = f"{DOMAIN}_alert_images"
_KEY_FILE = "cuboai_alert_images.json"
#: A photo's name: letters, digits, "_", "-" and ".", ending in .jpg. No
#: slash can pass, and a name cannot start with a dot, so nothing outside the
#: folder can be asked for.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}\.jpg$")


class AlertImages:
    """Where the photos are, and the signed address of each."""

    def __init__(self, directory: str, key: bytes) -> None:
        self.directory = directory
        self._key = key

    def token(self, filename: str) -> str:
        return hmac.new(self._key, filename.encode("utf-8"), hashlib.sha256).hexdigest()[:32]

    def url(self, filename: str) -> str:
        return f"{URL_BASE}/{filename}?token={self.token(filename)}"

    def path_for(self, filename: str, token: str | None) -> str | None:
        """The file to send for a request, or None when it must be refused."""
        if not isinstance(filename, str) or not _SAFE_NAME.match(filename):
            return None
        if not isinstance(token, str) or not hmac.compare_digest(token, self.token(filename)):
            return None
        return os.path.join(self.directory, filename)


class AlertImageView(HomeAssistantView):
    """Serves one alert photo to a request that carries its signature."""

    url = URL_BASE + "/{filename}"
    name = "api:cuboai:alert_image"
    # The signature is the proof (see the module docstring): an <img> tag in a
    # dashboard cannot send Home Assistant's login.
    requires_auth = False

    def __init__(self, images: AlertImages) -> None:
        self._images = images

    async def get(self, request: web.Request, filename: str) -> web.StreamResponse:
        path = self._images.path_for(filename, request.query.get("token"))
        # One answer for "wrong signature" and "no such photo", so a guess
        # learns nothing about which photos exist.
        if path is None or not await request.app["hass"].async_add_executor_job(os.path.isfile, path):
            raise web.HTTPNotFound
        return web.FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})


def _load_or_create_key(path: str) -> bytes:
    try:
        with open(path, encoding="utf-8") as f:
            key = bytes.fromhex(json.load(f)["key"])
        if len(key) >= 32:
            return key
    except (OSError, ValueError, KeyError, TypeError):
        pass
    key = secrets.token_bytes(32)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"key": key.hex()}, f)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    return key


def migrate_public_images(old_dirs: list[str], new_dir: str) -> int:
    """Move photos left in a public `www` folder into `new_dir`.

    Returns how many were moved. The old folder is removed when nothing else is
    left in it. A photo already present in `new_dir` is kept, and the public
    copy deleted.
    """
    moved = 0
    seen: set[str] = set()
    for old in old_dirs:
        real = os.path.realpath(old)
        if real in seen or real == os.path.realpath(new_dir) or not os.path.isdir(old):
            continue
        seen.add(real)
        os.makedirs(new_dir, exist_ok=True)
        for name in os.listdir(old):
            src = os.path.join(old, name)
            if not (name.lower().endswith(".jpg") and os.path.isfile(src)):
                continue
            dst = os.path.join(new_dir, name)
            try:
                if os.path.exists(dst):
                    os.remove(src)
                else:
                    shutil.move(src, dst)
                moved += 1
            except OSError as err:
                _LOGGER.warning("Could not move the alert photo %s out of the public folder: %s", src, err)
        try:
            os.rmdir(old)  # only when empty
        except OSError:
            pass
    return moved


def _prepare(hass) -> AlertImages:
    directory = hass.config.path(IMAGES_FOLDER)
    key = _load_or_create_key(hass.config.path(".storage", _KEY_FILE))
    moved = migrate_public_images(
        [hass.config.path("www", IMAGES_FOLDER), f"/config/www/{IMAGES_FOLDER}"],
        directory,
    )
    if moved:
        _LOGGER.warning(
            "Moved %s alert photo(s) from www/%s, which Home Assistant serves without a login, to %s. "
            "The photo addresses in the alert attributes have changed (see the changelog).",
            moved,
            IMAGES_FOLDER,
            directory,
        )
    return AlertImages(directory, key)


async def async_setup_alert_images(hass) -> AlertImages:
    """Prepare the photo folder and its view once per Home Assistant run."""
    images = hass.data.get(_DATA_KEY)
    if isinstance(images, AlertImages):
        return images
    images = await hass.async_add_executor_job(_prepare, hass)
    # Another config entry may have finished first while this one was reading
    # the disk: one view per Home Assistant, so keep the first.
    existing = hass.data.get(_DATA_KEY)
    if isinstance(existing, AlertImages):
        return existing
    hass.http.register_view(AlertImageView(images))
    hass.data[_DATA_KEY] = images
    return images
