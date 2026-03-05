import logging

from homeassistant.components.light import LightEntity, ColorMode
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN
from .api.tutk import TutkClient, TutkError

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the CuboAI light platform."""
    cameras = entry.data.get("cameras", [])
    
    tutk_supported = True
    try:
        from .api.tutk import load_library
        # Attempt to load the library once before creating entities
        load_library()
    except Exception as e:
        _LOGGER.error("Cannot enable Nightlight feature due to TUTK library error (Missing libc6-compat?): %s", e)
        tutk_supported = False
        
    entities = []
    for camera in cameras:
        uid = camera.get("device_id")
        user = camera.get("dev_admin_id")
        pwd = camera.get("dev_admin_pwd")
        license_id = camera.get("license_id")
        baby_name = camera.get("baby_name", "Unknown")
        
        # P2P requires the admin credentials extracted from the cloud API
        if tutk_supported and uid and user and pwd and license_id:
            entities.append(CuboNightLight(hass, baby_name, uid, license_id, user, pwd))
        elif not tutk_supported:
            _LOGGER.warning("Skipping nightlight for %s because TUTK library failed to load.", baby_name)
        else:
            _LOGGER.warning(
                "Skipping nightlight for %s because admin credentials or license_id are missing. "
                "Please re-authenticate the integration.", baby_name
            )

    if entities:
        async_add_entities(entities, update_before_add=True)

class CuboNightLight(LightEntity):
    """Representation of a CuboAI Night Light."""
    
    _attr_has_entity_name = True
    _attr_name = "Night Light"
    _attr_color_mode = ColorMode.ONOFF
    _attr_supported_color_modes = {ColorMode.ONOFF}

    def __init__(self, hass, baby_name, uid, license_id, dev_admin_id, dev_admin_pwd):
        """Initialize the light."""
        self.hass = hass
        self._baby_name = baby_name
        self._uid = uid
        self._license_id = license_id
        self._dev_admin_id = dev_admin_id
        self._dev_admin_pwd = dev_admin_pwd
        self._is_on: bool | None = None
        self._attr_unique_id = f"cuboai_nightlight_{uid}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information about this entity."""
        return {
            "identifiers": {(DOMAIN, self._uid)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

    @property
    def is_on(self) -> bool | None:
        """Return true if light is on."""
        return self._is_on

    async def _async_run_tutk_cmd(self, func, *args):
        def _run():
            client = TutkClient(self._uid, self._license_id, self._dev_admin_id, self._dev_admin_pwd)
            try:
                client.connect()
                return func(client, *args)
            finally:
                client.disconnect()

        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _run)

    async def async_turn_on(self, **kwargs):
        """Instruct the light to turn on."""
        success = await self._async_run_tutk_cmd(lambda c: c.set_night_light_status(True))
        if success:
            self._is_on = True

    async def async_turn_off(self, **kwargs):
        """Instruct the light to turn off."""
        success = await self._async_run_tutk_cmd(lambda c: c.set_night_light_status(False))
        if success:
            self._is_on = False

    async def async_update(self):
        """Fetch new state data for this light."""
        try:
            self._is_on = await self._async_run_tutk_cmd(lambda c: c.get_night_light_status())
        except Exception as e:
            _LOGGER.error("Failed to update CuboAI night light state: %s", e)
