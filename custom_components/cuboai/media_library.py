import json
import logging
import os

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

class CuboMediaLibrary:
    def __init__(self, hass: HomeAssistant):
        self.hass = hass
        self._path = hass.config.path(".storage", "cuboai_media.json")
        self._data = {"custom_songs": [], "playlists": []}
        self._load()

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
        _LOGGER.warning(f'Updating custom songs. Old: {len(self._data.get("custom_songs", []))} New: {len(songs)}')
        _LOGGER.warning('Calling async_dispatcher_send soon...')
        self._data["custom_songs"] = songs
        self._save()
        self.hass.loop.call_soon_threadsafe(self._update_sensor)

    def update_playlists(self, playlists):
        self._data["playlists"] = playlists
        self._save()
        self.hass.loop.call_soon_threadsafe(self._update_sensor)

    def get_data(self):
        return self._data

    def _update_sensor(self):
        import time

        from homeassistant.helpers.dispatcher import async_dispatcher_send
        self.hass.data['cuboai_media_library_update_time'] = time.time()
        async_dispatcher_send(self.hass, "cuboai_media_library_updated")

    def init_sensor(self):
        self._update_sensor()

def async_setup_services(hass: HomeAssistant):
    if "cuboai_media_library_instance" in hass.data:
        return

    library = CuboMediaLibrary(hass)
    hass.data["cuboai_media_library_instance"] = library
    library.init_sensor()

    async def handle_save_custom_songs(call):
        songs = call.data.get("songs", [])
        await hass.async_add_executor_job(library.update_custom_songs, songs)

    async def handle_save_playlists(call):
        playlists = call.data.get("playlists", [])
        await hass.async_add_executor_job(library.update_playlists, playlists)

    hass.services.async_register("cuboai", "save_custom_songs", handle_save_custom_songs)
    hass.services.async_register("cuboai", "save_playlists", handle_save_playlists)


