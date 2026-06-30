import logging
import os
import platform

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    RepeatMode,
)
from homeassistant.components.media_source import async_resolve_media, is_media_source_id
from homeassistant.components.media_player.browse_media import async_process_play_media_url
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the CuboAI media player."""
    domain_data = hass.data[DOMAIN][entry.entry_id]
    manager = domain_data.get("go2rtc") if isinstance(domain_data, dict) else domain_data
    coordinator = domain_data.get("coordinator") if isinstance(domain_data, dict) else None
    
    cameras = entry.data.get("cameras", [])
    if not cameras and "device_id" in entry.data:
        cameras = [{"device_id": entry.data["device_id"], "baby_name": entry.data["baby_name"]}]
        
    entities = []
    for cam in cameras:
        if manager:
            entities.append(CuboAIMediaPlayer(manager, cam, entry.options))
        if coordinator and "uid" in cam:
            entities.append(CuboLullabyPlayer(coordinator, cam, entry.options))
            
    if entities:
        async_add_entities(entities)


def _execute_lullaby_cmd(uid, account, password, camera_ip, cmd_type: str, song_uuid=None, volume=None, timer=None):
    """Synchronous function to control lullabies."""
    from .utils import log_to_file
    import platform
    import os
    log_to_file(f"[Lullaby] cmd={cmd_type} uuid={song_uuid} vol={volume} timer={timer} ip={camera_ip}")
    try:
        from .tutk.cuboai_session import get_session
        from .tutk.cuboai_messages import CuboAIClient, build_set_lullaby_vol_duration, LULLABY_CATALOG, LULLABY_TIMER_REPEAT
        
        arch = "x86_64" if platform.machine().lower() in ["x86_64", "amd64"] else "aarch64"
        lib_path = os.path.join(os.path.dirname(__file__), "libs", arch, "libIOTCAPIs_ALL.so")
        
        with get_session(uid, account, password, camera_ip=camera_ip if camera_ip else None, defer_stream_start=True, defer_video_start_late=True, auto_discover_lib=True) as sess:
            client = CuboAIClient(sess)
            if cmd_type == "play":
                if not song_uuid:
                    song_uuid = list(LULLABY_CATALOG.keys())[0]
                
                # IMPORTANT: Set vol/duration BEFORE playing, otherwise camera might ignore or immediately stop
                vol = int(volume) if volume is not None else 50
                timer_val = LULLABY_TIMER_REPEAT
                if timer and timer > 0:
                    timer_val = timer * 60
                
                log_to_file(f"[Lullaby] Setting vol={vol} timer={timer_val} BEFORE play")
                if hasattr(sess, "ioctl"):
                    sess.ioctl(*build_set_lullaby_vol_duration(vol, timer_val))
                else:
                    sess._cubo_set(build_set_lullaby_vol_duration(vol, timer_val)[1])
                
                log_to_file(f"[Lullaby] Playing song: {song_uuid}")
                resp = client.play_lullaby(song_uuid)
                log_to_file(f"[Lullaby] Play response: {resp}")
                
                import time
                time.sleep(1)
                ls = client.get_lullaby_status()
                log_to_file(f"[Lullaby] Status after play: playing={ls.is_playing}")
            elif cmd_type == "stop":
                # Must stop the EXACT uuid that is playing, fallback to default
                if not song_uuid:
                    song_uuid = list(LULLABY_CATALOG.keys())[0]
                log_to_file(f"[Lullaby] Stopping with uuid: {song_uuid}")
                resp = client.stop_lullaby(song_uuid)
                log_to_file(f"[Lullaby] Stop response: {resp}")
            elif cmd_type == "volume":
                vol = int(volume) if volume is not None else 50
                timer_val = LULLABY_TIMER_REPEAT
                if timer and timer > 0:
                    timer_val = timer * 60
                log_to_file(f"[Lullaby] Setting volume={vol} timer={timer_val}")
                if hasattr(sess, "ioctl"):
                    resp = sess.ioctl(*build_set_lullaby_vol_duration(vol, timer_val))
                else:
                    resp = sess._cubo_set(build_set_lullaby_vol_duration(vol, timer_val)[1])
    except Exception as e:
        log_to_file(f"[Lullaby] ERROR: {e}")

class CuboAIMediaPlayer(MediaPlayerEntity):
    """Media Player entity to send audio to the CuboAI camera."""
    
    def __init__(self, manager, cam, options):
        self._manager = manager
        self._cam = cam
        self._options = options
        self._device_id = cam.get("device_id", "")
        baby_name = cam.get("baby_name", "Camera")
        self._attr_name = f"{baby_name} Speaker"
        self._attr_unique_id = f"cuboai_speaker_{self._device_id}"
        self._attr_supported_features = (
            MediaPlayerEntityFeature.PLAY_MEDIA 
            | MediaPlayerEntityFeature.BROWSE_MEDIA
            | MediaPlayerEntityFeature.PLAY
            | MediaPlayerEntityFeature.STOP
            | MediaPlayerEntityFeature.VOLUME_SET
            | MediaPlayerEntityFeature.TURN_ON
            | MediaPlayerEntityFeature.TURN_OFF
            | MediaPlayerEntityFeature.REPEAT_SET
        )
        self._attr_state = MediaPlayerState.IDLE
        self._queue = []
        self._queue_task = None
        self._attr_volume_level = 0.5
        self._attr_repeat = RepeatMode.OFF

    @property
    def media_content_id(self):
        return getattr(self, "_attr_media_content_id", None)

    @property
    def media_title(self):
        return getattr(self, "_attr_media_title", None)

    @property
    def extra_state_attributes(self):
        return {
            "device_id": self._device_id,
            "uid": self._cam.get("uid", "")
        }

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._cam.get("device_id", ""))},
            "name": f"CuboAI {self._cam.get('baby_name', 'Camera')}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

    async def async_turn_off(self) -> None:
        await self.async_media_stop()

    async def async_set_repeat(self, repeat: RepeatMode) -> None:
        self._attr_repeat = repeat
        self.async_write_ha_state()

    async def async_browse_media(self, media_content_type=None, media_content_id=None):
        """Implement the websocket media browsing request."""
        from homeassistant.components import media_source
        return await media_source.async_browse_media(
            self.hass,
            media_content_id,
            content_filter=lambda item: item.media_content_type.startswith("audio/")
        )

    async def async_media_stop(self) -> None:
        """Stop playing media."""
        self._queue.clear()
        
        # Stop background task properly
        import asyncio
        task = getattr(self, "_queue_task", None)
        if task:
            task.cancel()
            
        if getattr(self, "_backchannel_proc", None):
            try:
                self._backchannel_proc.terminate()
            except Exception:
                pass
            self._backchannel_proc = None
            
        self._attr_state = MediaPlayerState.IDLE
        self.async_write_ha_state()

    async def async_play_media(self, media_type: str, media_id: str, **kwargs) -> None:
        """Send an audio file or TTS to the go2rtc speaker stream."""
        try:
            await self.hass.services.async_call("persistent_notification", "create", {"title": "CuboAI Debug", "message": f"async_play_media called with {media_id}"})
        except:
            pass
        try:
            from homeassistant.components.media_source import async_resolve_media, is_media_source_id
            from homeassistant.components.media_player.browse_media import async_process_play_media_url
            import logging
            _LOGGER = logging.getLogger(__name__)
            
            # First resolve media source if it's TTS or local media
            if is_media_source_id(media_id):
                sourced_media = await async_resolve_media(self.hass, media_id, self.entity_id)
                media_id = sourced_media.url

            # Now check if it's a lullaby (lullabies are raw strings like "CuboAI_Lullaby")
            is_lullaby = not media_id.startswith("http") and "youtube" not in media_id and "spotify" not in media_id and "ytsearch" not in media_id and not media_id.startswith("/")
            
            if not is_lullaby:
                media_id = async_process_play_media_url(self.hass, media_id)
            
            enqueue = kwargs.get("enqueue", "play")
            
            if enqueue == "add":
                self._queue.append(media_id)
                if self._attr_state != MediaPlayerState.PLAYING and not getattr(self, "_queue_task", None):
                    self._start_queue_loop()
            elif enqueue == "replace":
                await self.async_media_stop()
                self._queue.append(media_id)
                self._start_queue_loop()
            else:
                await self.async_media_stop()
                self._queue.append(media_id)
                self._start_queue_loop()
        except Exception as e:
            import traceback
            log_path = self.hass.config.path("ffmpeg_error.log")
            with open(log_path, 'a') as f:
                f.write(f"Exception in async_play_media:\n{traceback.format_exc()}\n")
            
    def _start_queue_loop(self):
        import asyncio
        task = getattr(self, "_queue_task", None)
        if task:
            task.cancel()
        self._queue_task = self.hass.async_create_task(self._queue_loop())

    async def _extract_media_url(self, media_id: str) -> str:
        """Extract YouTube or Spotify URL in the background."""
        try:
            await self.hass.services.async_call("persistent_notification", "create", {"title": "CuboAI Debug", "message": f"_extract_media_url called for {media_id}"})
        except:
            pass
        import logging
        _LOGGER = logging.getLogger(__name__)
        if "youtube.com" in media_id or "youtu.be" in media_id or "spotify.com" in media_id or media_id.startswith("ytsearch"):
            if "spotify.com" in media_id:
                try:
                    import urllib.request, re
                    req = urllib.request.Request(media_id, headers={'User-Agent': 'Mozilla/5.0'})
                    html = urllib.request.urlopen(req, timeout=5).read().decode('utf-8')
                    title_match = re.search(r'<title>(.*?)</title>', html)
                    if title_match:
                        raw_title = title_match.group(1)
                        clean_title = raw_title.split('|')[0].replace('- song and lyrics by', '').strip()
                        media_id = f"ytsearch1:{clean_title}"
                except Exception as e:
                    pass

            try:
                import yt_dlp
            except ImportError:
                import sys, subprocess
                subprocess.check_call([sys.executable, "-m", "pip", "install", "yt-dlp"])
                import yt_dlp
                
            import os
            def _extract_yt_url():
                ydl_opts = {'format': 'bestaudio/best', 'quiet': True, 'noplaylist': True}
                cookie_path = self.hass.config.path("cuboai_cookies.txt")
                if os.path.exists(cookie_path):
                    ydl_opts['cookiefile'] = cookie_path
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(media_id, download=False)
                    if 'entries' in info and len(info['entries']) > 0:
                        info = info['entries'][0]
                    return info.get("url", media_id)
            media_id = await self.hass.async_add_executor_job(_extract_yt_url)
            
        import urllib.parse
        parsed = urllib.parse.urlparse(media_id)
        if parsed.path.startswith("/api/") or parsed.path.startswith("/media/") or media_id.startswith("/"):
            media_id = f"http://127.0.0.1:8123{parsed.path}"
            if parsed.query:
                media_id += f"?{parsed.query}"
                
        return media_id

    async def _queue_loop(self):
        try:
            await self.hass.services.async_call("persistent_notification", "create", {"title": "CuboAI Debug", "message": f"_queue_loop started!"})
        except:
            pass
        import asyncio
        import os
        import logging
        _LOGGER = logging.getLogger(__name__)
        current_task = asyncio.current_task()
        try:
            while self._queue:
                timer_state = self.hass.states.get(f"number.cuboai_speaker_timer_{self._device_id}")
                timer_min = int(float(timer_state.state)) if timer_state and timer_state.state not in ('unknown', 'unavailable') else 0
                start_time = asyncio.get_event_loop().time()
                
                raw_media_id = self._queue.pop(0)
                
                self._attr_state = MediaPlayerState.PLAYING
                self._attr_media_content_id = raw_media_id
                self._attr_media_title = raw_media_id
                self.async_write_ha_state()
                
                is_lullaby = not raw_media_id.startswith("http") and "youtube" not in raw_media_id and "spotify" not in raw_media_id and "ytsearch" not in raw_media_id and not raw_media_id.startswith("/")
                
                if is_lullaby:
                    # It's a lullaby! Find the lullaby entity and trigger it natively
                    _LOGGER.info(f"Playing lullaby via backend queue: {raw_media_id}")
                    lullaby_entity_id = self.entity_id.replace("_speaker", "_lullaby")
                    if self.hass.states.get(lullaby_entity_id):
                        await self.hass.services.async_call("media_player", "select_source", {
                            "entity_id": lullaby_entity_id,
                            "source": raw_media_id
                        })
                        # Lullabies loop forever natively. We clear the queue because a playlist stops at a lullaby.
                        self._queue.clear()
                        # Wait an hour or until stopped
                        await asyncio.sleep(3600)
                    else:
                        _LOGGER.error("Lullaby entity not found")
                    continue
                
                # Standard audio stream processing
                try:
                    extracted_url = await self._extract_media_url(raw_media_id)
                except Exception as e:
                    import traceback
                    log_path = self.hass.config.path("ffmpeg_error.log")
                    with open(log_path, 'a') as f:
                        f.write(f"Exception in _extract_media_url:\n{traceback.format_exc()}\n")
                    continue
                
                script_path = os.path.join(os.path.dirname(__file__), "tutk", "cuboai_stream_backchannel.py")
                
                env = os.environ.copy()
                camera_ip = self._options.get(f"camera_ip_{self._device_id}", "") or self._cam.get("camera_ip", "")
                
                env["CUBOAI_UID"] = str(self._cam.get("uid") or "")
                env["CUBOAI_ACCOUNT"] = str(self._cam.get("account") or "")
                env["CUBOAI_PASSWORD"] = str(self._cam.get("password") or "")
                env["CUBOAI_CAMERA_IP"] = str(camera_ip or "")
                
                log_path = self.hass.config.path("ffmpeg_error.log")
                
                self._backchannel_proc = await asyncio.create_subprocess_exec(
                    "python3", script_path, extracted_url,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=open(log_path, 'a'),
                    env=env
                )
                
                if timer_min > 0:
                    try:
                        await asyncio.wait_for(self._backchannel_proc.wait(), timeout=timer_min * 60)
                    except asyncio.TimeoutError:
                        _LOGGER.info(f"Speaker sleep timer expired ({timer_min} min)")
                        self._queue.clear() # clear queue to stop playing
                        if getattr(self, "_backchannel_proc", None):
                            try:
                                self._backchannel_proc.terminate()
                            except Exception:
                                pass
                else:
                    await self._backchannel_proc.wait()
                    
                self._backchannel_proc = None
                
                if self._attr_repeat == RepeatMode.ONE:
                    self._queue.insert(0, raw_media_id)
                elif self._attr_repeat == RepeatMode.ALL:
                    self._queue.append(raw_media_id)
                
            # Queue empty
            if self._queue_task == current_task:
                self._attr_state = MediaPlayerState.IDLE
                self.async_write_ha_state()
                self._queue_task = None
            
        except asyncio.CancelledError:
            if self._queue_task == current_task:
                if getattr(self, "_backchannel_proc", None):
                    try:
                        self._backchannel_proc.terminate()
                    except:
                        pass
                self._attr_state = MediaPlayerState.IDLE
                self.async_write_ha_state()
                self._queue_task = None
        except Exception as e:
            import traceback
            log_path = self.hass.config.path("ffmpeg_error.log")
            with open(log_path, 'a') as f:
                f.write(f"Exception in _queue_loop:\n{traceback.format_exc()}\n")
            if self._queue_task == current_task:
                self._attr_state = MediaPlayerState.IDLE
                self.async_write_ha_state()
                self._queue_task = None

    async def async_media_play(self) -> None:
        """Play media (stub for Music Assistant compatibility)."""
        self._attr_state = MediaPlayerState.PLAYING
        self.async_write_ha_state()

    async def async_media_pause(self) -> None:
        """Pause media (stub)."""
        self._attr_state = MediaPlayerState.PAUSED
        self.async_write_ha_state()
        
    async def async_turn_on(self) -> None:
        """Turn on (stub)."""
        self._attr_state = MediaPlayerState.IDLE
        self.async_write_ha_state()
        
    async def async_turn_off(self) -> None:
        """Turn off (stub)."""
        self._attr_state = MediaPlayerState.OFF
        self.async_write_ha_state()


class CuboLullabyPlayer(CoordinatorEntity, MediaPlayerEntity):
    def __init__(self, coordinator, camera, options):
        super().__init__(coordinator)
        self._device_id = camera["device_id"]
        self._baby_name = camera["baby_name"]
        self._uid = camera["uid"]
        self._account = camera["account"]
        self._password = camera["password"]
        self._camera_ip = options.get(f"camera_ip_{self._device_id}") or camera.get("camera_ip")
        
        self._attr_name = f"{self._baby_name} Lullaby"
        self._attr_unique_id = f"cuboai_lullaby_{self._device_id}"
        self._attr_supported_features = (
            MediaPlayerEntityFeature.PLAY
            | MediaPlayerEntityFeature.STOP
            | MediaPlayerEntityFeature.PLAY_MEDIA
            | MediaPlayerEntityFeature.VOLUME_SET
            | MediaPlayerEntityFeature.SELECT_SOURCE
        )

        from .tutk.cuboai_messages import LULLABY_CATALOG
        self._catalog = LULLABY_CATALOG
        self._attr_source_list = sorted([v[1] for v in LULLABY_CATALOG.values()])

    @property
    def state(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        playing = cam.get("local", {}).get("lullaby_playing")
        if playing is True:
            return MediaPlayerState.PLAYING
        elif playing is False:
            return MediaPlayerState.IDLE
        return None

    @property
    def source(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        song_uuid = cam.get("local", {}).get("lullaby_song")
        if song_uuid and song_uuid.upper() in self._catalog:
            return self._catalog[song_uuid.upper()][1]
        return None

    @property
    def volume_level(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        vol = cam.get("local", {}).get("lullaby_volume")
        if vol is not None:
            return float(vol) / 100.0
        return 0.5  # Default 50% — returning None causes HA validation errors

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

    async def async_media_play(self):
        timer_state = self.hass.states.get(f"number.cuboai_lullaby_timer_{self._device_id}")
        timer = int(float(timer_state.state)) if timer_state and timer_state.state not in ('unknown', 'unavailable') else 0
        
        # Optimistic update
        if self.coordinator.data and "cameras" in self.coordinator.data:
            cam = self.coordinator.data["cameras"].get(self._device_id, {})
            if "local" in cam:
                cam["local"]["lullaby_playing"] = True
        self.async_write_ha_state()
        
        await self.hass.async_add_executor_job(
            _execute_lullaby_cmd, self._uid, self._account, self._password, self._camera_ip, "play", None, None, timer
        )
        try:
            await self.coordinator.async_request_refresh()
        except Exception:
            pass

    async def async_media_stop(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        current_uuid = cam.get("local", {}).get("lullaby_song")
        
        # Optimistic update
        if self.coordinator.data and "cameras" in self.coordinator.data:
            cam_data = self.coordinator.data["cameras"].get(self._device_id, {})
            if "local" in cam_data:
                cam_data["local"]["lullaby_playing"] = False
        self.async_write_ha_state()
        
        await self.hass.async_add_executor_job(
            _execute_lullaby_cmd, self._uid, self._account, self._password, self._camera_ip, "stop", current_uuid
        )
        try:
            await self.coordinator.async_request_refresh()
        except Exception:
            pass

    async def async_select_source(self, source: str):
        uuid = next((k for k, v in self._catalog.items() if v[1] == source), None)
        if uuid:
            timer_state = self.hass.states.get(f"number.cuboai_lullaby_timer_{self._device_id}")
            timer = int(float(timer_state.state)) if timer_state and timer_state.state not in ('unknown', 'unavailable') else 0
            
            # Optimistic update
            if self.coordinator.data and "cameras" in self.coordinator.data:
                cam = self.coordinator.data["cameras"].get(self._device_id, {})
                if "local" in cam:
                    cam["local"]["lullaby_song"] = uuid
                    cam["local"]["lullaby_playing"] = True
            self.async_write_ha_state()
            
            await self.hass.async_add_executor_job(
                _execute_lullaby_cmd, self._uid, self._account, self._password, self._camera_ip, "play", uuid, None, timer
            )
            try:
                await self.coordinator.async_request_refresh()
            except Exception:
                pass

    async def async_play_media(self, media_type: str, media_id: str, **kwargs):
        if media_type == MediaType.MUSIC:
            uuid = next((k for k, v in self._catalog.items() if v[1] == media_id), media_id)
            timer_state = self.hass.states.get(f"number.cuboai_lullaby_timer_{self._device_id}")
            timer = int(float(timer_state.state)) if timer_state and timer_state.state not in ('unknown', 'unavailable') else 0
            await self.hass.async_add_executor_job(
                _execute_lullaby_cmd, self._uid, self._account, self._password, self._camera_ip, "play", uuid, None, timer
            )
            try:
                await self.coordinator.async_request_refresh()
            except Exception:
                pass

    async def async_set_volume_level(self, volume: float):
        # HA volume is 0.0 to 1.0, API is 0 to 100
        vol_int = int(volume * 100)
        timer_state = self.hass.states.get(f"number.cuboai_lullaby_timer_{self._device_id}")
        timer = int(float(timer_state.state)) if timer_state and timer_state.state not in ('unknown', 'unavailable') else 0
        await self.hass.async_add_executor_job(
            _execute_lullaby_cmd, self._uid, self._account, self._password, self._camera_ip, "volume", None, vol_int, timer
        )
