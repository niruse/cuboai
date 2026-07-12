import json
import logging
import os

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class CuboMediaLibrary:
    def __init__(self, hass: HomeAssistant):
        self.hass = hass
        self._path = hass.config.path(".storage", "cuboai_media.json")
        self._data = {"custom_songs": [], "playlists": [], "settings": {}}

    def _load(self):
        if os.path.exists(self._path):
            try:
                with open(self._path, encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception as e:
                _LOGGER.error(f"Failed to load CuboAI media library: {e}")

    def _save(self):
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
        except Exception as e:
            _LOGGER.error(f"Failed to save CuboAI media library: {e}")

    def update_custom_songs(self, songs):
        old = self._data.get("custom_songs", []) or []
        _LOGGER.info(f"Updating custom songs. Old: {len(old)} New: {len(songs)}")
        # Safety guard: never let a stale client wipe the whole library.
        # Deleting the last song (1 -> 0) is allowed; replacing many songs
        # with an empty list is almost certainly a stale browser/cache bug
        # (this is how the library was wiped on 2026-07-10).
        if not songs and len(old) > 1:
            _LOGGER.warning(
                "Refusing to replace %d custom songs with an empty list (possible stale client state). Ignoring save.",
                len(old),
            )
            return
        self._data["custom_songs"] = songs
        self._save()
        self.hass.loop.call_soon_threadsafe(self._update_sensor)

    def update_playlists(self, playlists):
        old = self._data.get("playlists", []) or []
        if not playlists and len(old) > 1:
            _LOGGER.warning(
                "Refusing to replace %d playlists with an empty list (possible stale client state). Ignoring save.",
                len(old),
            )
            return
        self._data["playlists"] = playlists
        self._save()
        self.hass.loop.call_soon_threadsafe(self._update_sensor)

    def update_settings(self, settings):
        """Per-camera card settings (shuffle/repeat, ...) shared across devices."""
        self._data["settings"] = settings
        self._save()
        self.hass.loop.call_soon_threadsafe(self._update_sensor)

    def get_data(self):
        return self._data

    def _update_sensor(self):
        import time

        from homeassistant.helpers.dispatcher import async_dispatcher_send

        self.hass.data["cuboai_media_library_update_time"] = time.time()
        async_dispatcher_send(self.hass, "cuboai_media_library_updated")

    def init_sensor(self):
        self._update_sensor()


async def async_setup_services(hass: HomeAssistant):
    if "cuboai_media_library_instance" in hass.data:
        return

    library = CuboMediaLibrary(hass)

    # Run the blocking I/O _load in an executor and wait for it so the
    # library is populated before the sensor first reads it.
    await hass.async_add_executor_job(library._load)

    hass.data["cuboai_media_library_instance"] = library
    library.init_sensor()

    async def handle_save_custom_songs(call):
        songs = call.data.get("songs", [])
        await hass.async_add_executor_job(library.update_custom_songs, songs)

    async def handle_save_playlists(call):
        playlists = call.data.get("playlists", [])
        await hass.async_add_executor_job(library.update_playlists, playlists)

    async def handle_save_settings(call):
        settings = call.data.get("settings", {})
        await hass.async_add_executor_job(library.update_settings, settings)

    hass.services.async_register("cuboai", "save_custom_songs", handle_save_custom_songs)
    hass.services.async_register("cuboai", "save_playlists", handle_save_playlists)
    hass.services.async_register("cuboai", "save_settings", handle_save_settings)
