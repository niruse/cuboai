import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    # One global button across all config entries (same pattern as the
    # Cache YouTube/Spotify Songs switch).
    domain_data = hass.data.setdefault(DOMAIN, {})
    if not domain_data.get("_clear_cache_button_added"):
        domain_data["_clear_cache_button_added"] = True
        async_add_entities([CuboClearCacheButton()])


class CuboClearCacheButton(ButtonEntity):
    """Button that deletes all locally cached YouTube/Spotify songs."""

    def __init__(self):
        self._attr_has_entity_name = True
        self._attr_name = "Clear Song Cache"
        self._attr_unique_id = "cuboai_clear_youtube_cache"
        self._attr_icon = "mdi:delete-sweep"
        self._attr_entity_category = EntityCategory.CONFIG

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, "cuboai_media_library")},
            "name": "CuboAI Media Library",
            "manufacturer": "CuboAI",
            "model": "Media Library",
        }

    async def async_press(self) -> None:
        import shutil

        cache_dir = self.hass.config.path("www", "cuboai_cache")
        await self.hass.async_add_executor_job(lambda: shutil.rmtree(cache_dir, True))
        _LOGGER.info("CuboAI song cache cleared (%s)", cache_dir)
