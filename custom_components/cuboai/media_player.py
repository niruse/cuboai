import functools
import logging
import os
import sys

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    RepeatMode,
)
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .playback import playtime_expired as _playtime_expired
from .playback import should_loop_playback as _should_loop_playback
from .tutk.cuboai_messages import LULLABY_CATALOG

_LOGGER = logging.getLogger(__name__)


def _get_timer_minutes(hass, unique_id: str) -> int:
    """Read a CuboAI number-entity timer value (minutes) by its unique_id.

    Entity ids are derived from the entity NAME (e.g. "number.mia_lullaby_timer"),
    not the unique_id, so resolve through the entity registry — a hardcoded
    "number.cuboai_..." guess silently never matches.
    """
    try:
        registry = er.async_get(hass)
        entity_id = registry.async_get_entity_id("number", DOMAIN, unique_id)
        if not entity_id:
            return 0
        state = hass.states.get(entity_id)
        if state and state.state not in ("unknown", "unavailable"):
            return int(float(state.state))
    except Exception:
        pass
    return 0


def _internal_base_url(hass) -> str:
    """Base URL this HA instance is reachable at from localhost subprocesses."""
    try:
        from homeassistant.helpers.network import get_url

        return get_url(hass, allow_external=False, allow_ip=True).rstrip("/")
    except Exception:
        return "http://127.0.0.1:8123"


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


from .utils import retry_camera_command


@retry_camera_command("Lullaby command")
def _execute_lullaby_cmd(uid, account, password, camera_ip, cmd_type: str, song_uuid=None, volume=None, timer=None):
    """Synchronous function to control lullabies."""

    from .utils import log_to_file

    log_to_file(f"[Lullaby] cmd={cmd_type} uuid={song_uuid} vol={volume} timer={timer} ip={camera_ip}")
    try:
        from .tutk.cuboai_messages import (
            LULLABY_CATALOG,
            LULLABY_TIMER_REPEAT,
            CuboAIClient,
            build_set_lullaby_vol_duration,
        )
        from .tutk.cuboai_session import get_session

        with get_session(
            uid,
            account,
            password,
            camera_ip=camera_ip if camera_ip else None,
            defer_stream_start=True,
            defer_video_start_late=True,
            auto_discover_lib=True,
        ) as sess:
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
        # Propagate so the retry decorator can retry and then surface a clean
        # HomeAssistantError instead of an optimistic "playing" state.
        _LOGGER.warning("Lullaby command '%s' failed: %s", cmd_type, e)
        raise


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
        return {"device_id": self._device_id, "uid": self._cam.get("uid", "")}

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

    async def async_set_volume_level(self, volume: float) -> None:
        """Store the speaker volume (applied to the next queued track).

        The entity advertises VOLUME_SET; without this override the base class
        raises NotImplementedError and the UI shows an 'unknown error' toast.
        """
        self._attr_volume_level = volume
        self.async_write_ha_state()

    async def async_set_repeat(self, repeat: RepeatMode) -> None:
        self._attr_repeat = repeat
        self.async_write_ha_state()

    async def async_browse_media(self, media_content_type=None, media_content_id=None):
        """Implement the websocket media browsing request."""
        from homeassistant.components import media_source

        return await media_source.async_browse_media(
            self.hass, media_content_id, content_filter=lambda item: item.media_content_type.startswith("audio/")
        )

    async def _shutdown_playback(self) -> None:
        """Stop the queue task, the backchannel subprocess, and any delegated lullaby."""
        self._queue.clear()

        task = getattr(self, "_queue_task", None)
        if task:
            task.cancel()

        if getattr(self, "_backchannel_proc", None):
            try:
                self._backchannel_proc.terminate()
            except Exception:
                pass
            self._backchannel_proc = None

        # If the queue delegated playback to the Lullaby entity, stop it too —
        # otherwise the camera keeps playing after the Speaker shows "idle".
        if getattr(self, "_delegated_lullaby", False):
            self._delegated_lullaby = False
            lullaby_entity_id = self.entity_id.replace("_speaker", "_lullaby")
            if self.hass.states.get(lullaby_entity_id):
                try:
                    await self.hass.services.async_call("media_player", "media_stop", {"entity_id": lullaby_entity_id})
                except Exception:
                    _LOGGER.exception("Failed to stop delegated lullaby")

    async def async_media_stop(self) -> None:
        """Stop playing media."""
        await self._shutdown_playback()
        self._attr_state = MediaPlayerState.IDLE
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Kill playback (task, subprocess, delegated lullaby) on removal/reload."""
        await self._shutdown_playback()
        self._queue_task = None

    async def async_play_media(self, media_type: str, media_id: str, **kwargs) -> None:
        """Send an audio file or TTS to the go2rtc speaker stream."""
        try:
            import logging

            from homeassistant.components.media_player.browse_media import async_process_play_media_url
            from homeassistant.components.media_source import async_resolve_media, is_media_source_id

            _LOGGER = logging.getLogger(__name__)

            # First resolve media source if it's TTS or local media
            if is_media_source_id(media_id):
                sourced_media = await async_resolve_media(self.hass, media_id, self.entity_id)
                media_id = sourced_media.url

            # Now check if it's a lullaby (lullabies are raw strings like "CuboAI_Lullaby")
            is_lullaby = (
                not media_id.startswith("http")
                and "youtube" not in media_id
                and "spotify" not in media_id
                and "ytsearch" not in media_id
                and not media_id.startswith("/")
            )

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
        except Exception:
            _LOGGER.exception("Exception in async_play_media:")

    def _start_queue_loop(self):
        task = getattr(self, "_queue_task", None)
        if task:
            task.cancel()
        self._queue_task = self.hass.async_create_task(self._queue_loop())

    async def _extract_media_url(self, media_id: str) -> str:
        """Extract YouTube or Spotify URL in the background."""

        base_url = _internal_base_url(self.hass)
        if (
            "youtube.com" in media_id
            or "youtu.be" in media_id
            or "spotify.com" in media_id
            or media_id.startswith("ytsearch")
        ):

            def _extract_yt_url():
                nonlocal media_id
                import glob
                import hashlib
                import os

                is_caching_enabled = self.hass.data.get("cuboai", {}).get("youtube_cache_enabled", False)
                cache_dir = self.hass.config.path("www", "cuboai_cache")

                # Key the cache by the ORIGINAL id (YouTube link, Spotify link or
                # ytsearch string) so a cached Spotify song replays straight from
                # disk without contacting spotify.com for the title again.
                cache_hash = hashlib.md5(media_id.encode()).hexdigest()

                if is_caching_enabled:
                    existing = glob.glob(os.path.join(cache_dir, f"{cache_hash}.*"))
                    if existing:
                        ext = existing[0].split(".")[-1]
                        return f"{base_url}/local/cuboai_cache/{cache_hash}.{ext}"
                    if not os.path.exists(cache_dir):
                        try:
                            os.makedirs(cache_dir, exist_ok=True)
                        except Exception:
                            pass

                # Spotify: yt-dlp can't download from Spotify, so resolve the track
                # title and search it on YouTube instead.
                if "spotify.com" in media_id:
                    try:
                        import re
                        import urllib.request

                        req = urllib.request.Request(media_id, headers={"User-Agent": "Mozilla/5.0"})
                        html = urllib.request.urlopen(req, timeout=5).read().decode("utf-8")
                        title_match = re.search(r"<title>(.*?)</title>", html)
                        if title_match:
                            raw_title = title_match.group(1)
                            clean_title = raw_title.split("|")[0].replace("- song and lyrics by", "").strip()
                            media_id = f"ytsearch1:{clean_title}"
                    except Exception:
                        pass

                try:
                    import yt_dlp
                except ImportError:
                    import subprocess
                    import sys

                    subprocess.check_call([sys.executable, "-m", "pip", "install", "yt-dlp"])
                    import yt_dlp

                ydl_opts = {"format": "bestaudio/best", "quiet": True, "noplaylist": True}
                if is_caching_enabled:
                    ydl_opts["outtmpl"] = os.path.join(cache_dir, f"{cache_hash}.%(ext)s")

                cookie_path = self.hass.config.path("cuboai_cookies.txt")
                if os.path.exists(cookie_path):
                    ydl_opts["cookiefile"] = cookie_path

                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(media_id, download=is_caching_enabled)
                        if is_caching_enabled:
                            existing = glob.glob(os.path.join(cache_dir, f"{cache_hash}.*"))
                            if existing:
                                ext = existing[0].split(".")[-1]
                                return f"{base_url}/local/cuboai_cache/{cache_hash}.{ext}"

                        if "entries" in info and len(info["entries"]) > 0:
                            info = info["entries"][0]
                        return info.get("url", media_id)
                except Exception as e:
                    import logging

                    _LOGGER = logging.getLogger(__name__)
                    _LOGGER.warning("yt-dlp extraction failed (%s), attempting automatic upgrade...", e)
                    import subprocess
                    import sys

                    try:
                        subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"])
                        _LOGGER.warning(
                            "yt-dlp was successfully upgraded! You must RESTART Home Assistant to apply the new version."
                        )
                    except Exception as upgrade_err:
                        _LOGGER.error("Failed to upgrade yt-dlp: %s", upgrade_err)
                    # We cannot hot-reload yt-dlp, so we just raise the original error for now
                    return None

            media_id = await self.hass.async_add_executor_job(_extract_yt_url)
            if not media_id:
                raise ValueError("Media URL extraction failed (yt-dlp returned nothing)")

        import urllib.parse

        parsed = urllib.parse.urlparse(media_id)
        if parsed.path.startswith("/api/") or parsed.path.startswith("/media/") or media_id.startswith("/"):
            media_id = f"{base_url}{parsed.path}"
            if parsed.query:
                media_id += f"?{parsed.query}"

        return media_id

    async def _queue_loop(self):
        import asyncio

        current_task = asyncio.current_task()
        loop = asyncio.get_running_loop()

        # Play Time is a TOTAL session budget measured from when playback
        # started. The value is RE-READ on every check (via _expired below), so
        # setting Play Time AFTER pressing Play — the natural flow — takes
        # effect within a few seconds instead of being ignored until the next
        # session.
        session_start = loop.time()
        # Tracks played this session (songs only) so Play Time can LOOP them:
        # "Play Time: 30 min" means play for 30 minutes, not "play once, max 30".
        session_tracks = []
        played_ok_since_refill = True

        def _timer_min():
            return _get_timer_minutes(self.hass, f"cuboai_speaker_timer_{self._device_id}")

        def _expired():
            return _playtime_expired(session_start, loop.time(), _timer_min())

        try:
            while True:
                if _expired():
                    _LOGGER.info("Speaker play time expired — stopping queue")
                    self._queue.clear()
                    break

                if not self._queue:
                    # Queue drained: while a Play Time is set, loop the session's
                    # songs until the budget is spent. The played_ok guard avoids
                    # a hot loop if every track fails to extract/play instantly.
                    if _should_loop_playback(_timer_min(), bool(session_tracks), _expired()) and played_ok_since_refill:
                        played_ok_since_refill = False
                        self._queue.extend(session_tracks)
                        continue
                    break

                raw_media_id = self._queue.pop(0)

                self._attr_state = MediaPlayerState.PLAYING
                self._attr_media_content_id = raw_media_id
                self._attr_media_title = raw_media_id
                self.async_write_ha_state()

                is_lullaby = (
                    not raw_media_id.startswith("http")
                    and "youtube" not in raw_media_id
                    and "spotify" not in raw_media_id
                    and "ytsearch" not in raw_media_id
                    and not raw_media_id.startswith("/")
                )

                if is_lullaby:
                    # It's a lullaby! Find the lullaby entity and trigger it natively
                    _LOGGER.info(f"Playing lullaby via backend queue: {raw_media_id}")
                    lullaby_entity_id = self.entity_id.replace("_speaker", "_lullaby")
                    if self.hass.states.get(lullaby_entity_id):
                        self._delegated_lullaby = True
                        await self.hass.services.async_call(
                            "media_player", "select_source", {"entity_id": lullaby_entity_id, "source": raw_media_id}
                        )
                        # Lullabies loop forever natively. We clear the queue because a playlist stops at a lullaby.
                        self._queue.clear()
                        # Delegated (card) lullabies follow the session's Play Time budget.
                        # Poll so a mid-session Play Time change (or a manual stop of the
                        # lullaby entity) is honoured within ~2 s.
                        while not _expired():
                            lull = self.hass.states.get(lullaby_entity_id)
                            if lull and lull.state != MediaPlayerState.PLAYING:
                                break  # stopped elsewhere
                            await asyncio.sleep(2)
                        if self._delegated_lullaby:
                            self._delegated_lullaby = False
                            try:
                                await self.hass.services.async_call(
                                    "media_player", "media_stop", {"entity_id": lullaby_entity_id}
                                )
                            except Exception:
                                pass
                    else:
                        _LOGGER.error("Lullaby entity not found")
                    continue

                # Standard audio stream processing
                try:
                    extracted_url = await self._extract_media_url(raw_media_id)
                except Exception:
                    _LOGGER.exception("Exception in _extract_media_url:")
                    continue

                script_path = os.path.join(os.path.dirname(__file__), "tutk", "cuboai_stream_backchannel.py")

                env = os.environ.copy()
                camera_ip = self._options.get(f"camera_ip_{self._device_id}", "") or self._cam.get("camera_ip", "")

                env["CUBOAI_UID"] = str(self._cam.get("uid") or "")
                env["CUBOAI_ACCOUNT"] = str(self._cam.get("account") or "")
                env["CUBOAI_PASSWORD"] = str(self._cam.get("password") or "")
                env["CUBOAI_CAMERA_IP"] = str(camera_ip or "")

                if self._options.get("enable_debug_logs", False):
                    # open() blocks — do it in the executor, and close our copy
                    # right after spawning (the child duplicates the fd).
                    log_path = self.hass.config.path("cuboai_debug.log")
                    stderr_dest = await self.hass.async_add_executor_job(functools.partial(open, log_path, "a"))
                else:
                    stderr_dest = asyncio.subprocess.DEVNULL

                try:
                    self._backchannel_proc = await asyncio.create_subprocess_exec(
                        sys.executable or "python3",
                        script_path,
                        extracted_url,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=stderr_dest,
                        env=env,
                    )
                finally:
                    if hasattr(stderr_dest, "close"):
                        stderr_dest.close()

                # Remember this song so Play Time can loop the session, and note
                # that real playback happened (clears the hot-loop guard).
                played_ok_since_refill = True
                if raw_media_id not in session_tracks:
                    session_tracks.append(raw_media_id)

                # Poll the track in short slices so a Play Time change mid-song
                # (or reaching the session budget) stops playback within ~5 s
                # instead of only between tracks.
                while True:
                    if _expired():
                        _LOGGER.info("Speaker play time expired — stopping playback")
                        self._queue.clear()
                        if getattr(self, "_backchannel_proc", None):
                            try:
                                self._backchannel_proc.terminate()
                            except Exception:
                                pass
                        break
                    try:
                        await asyncio.wait_for(self._backchannel_proc.wait(), timeout=5)
                        break  # track finished on its own
                    except TimeoutError:
                        continue  # re-check the budget and keep waiting

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
                    except Exception:
                        pass
                self._attr_state = MediaPlayerState.IDLE
                self.async_write_ha_state()
                self._queue_task = None
            raise
        except Exception:
            _LOGGER.exception("Exception in _queue_loop:")
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

    # ── Two timer mechanisms ──────────────────────────────────────────────
    # 1. NATIVE: playing from the entity controls (media_play / play_media,
    #    same as the CuboAI app) sends the Lullaby Timer to the camera, which
    #    enforces the duration itself (limited to the camera's options).
    # 2. CARD: the card plays via select_source; the camera runs in
    #    repeat-forever mode and Home Assistant sends the stop when the card's
    #    Play Time expires — any duration works. The scheduled stop only
    #    exists for card-initiated playback and double-checks the playing song
    #    before firing, so app/schedule playback is never touched.
    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        from homeassistant.helpers.dispatcher import async_dispatcher_connect

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, f"cuboai_lullaby_timer_changed_{self._device_id}", self._on_timer_changed
            )
        )

    async def async_will_remove_from_hass(self):
        self._cancel_scheduled_stop()

    @callback
    def _on_timer_changed(self, minutes):
        """Lullaby Timer changed: adjust a NATIVE playing lullaby on the camera.

        Card sessions (HA-owned scheduled stop) are governed by Play Time and
        ignore this; idle state just stores the value for the next play.
        """
        if getattr(self, "_stop_timer_task", None):
            return
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {}) if self.coordinator.data else {}
        if cam.get("local", {}).get("lullaby_playing"):
            _LOGGER.info("Lullaby timer changed to %s min while playing — updating camera", minutes)
            self.hass.async_create_task(self._push_native_timer(minutes))

    async def _push_native_timer(self, minutes):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {}) if self.coordinator.data else {}
        vol = cam.get("local", {}).get("lullaby_volume", 50)
        try:
            await self.hass.async_add_executor_job(
                _execute_lullaby_cmd,
                self._uid,
                self._account,
                self._password,
                self._camera_ip,
                "volume",
                None,
                vol,
                minutes,
            )
        except Exception:
            _LOGGER.exception("Failed to push lullaby timer to camera")

    def _cancel_scheduled_stop(self):
        import asyncio

        task = getattr(self, "_stop_timer_task", None)
        if task and task is not asyncio.current_task():
            task.cancel()
        self._stop_timer_task = None
        self._ha_started_uuid = None

    def _schedule_stop(self, minutes, started_uuid):
        self._cancel_scheduled_stop()
        if minutes and minutes > 0:
            self._ha_started_uuid = started_uuid
            self._stop_timer_task = self.hass.async_create_task(self._stop_after(minutes))

    async def _stop_after(self, minutes):
        import asyncio

        await asyncio.sleep(minutes * 60)
        started_uuid = getattr(self, "_ha_started_uuid", None)
        self._stop_timer_task = None

        # Only stop what WE started: if the app/schedule switched to another
        # song meanwhile (or playback already ended), leave it alone.
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {}) if self.coordinator.data else {}
        local = cam.get("local", {})
        if local.get("lullaby_playing") is False:
            _LOGGER.info("Lullaby timer expired (%s min) — playback already stopped", minutes)
            return
        current = (local.get("lullaby_song") or "").upper()
        if started_uuid and current and current != started_uuid.upper():
            _LOGGER.info(
                "Lullaby timer expired (%s min) but a different song is playing (started elsewhere) — not stopping",
                minutes,
            )
            return
        _LOGGER.info("Lullaby timer expired (%s min) — sending stop to camera", minutes)
        await self.async_media_stop()

    async def async_media_play(self):
        # NATIVE path (entity controls): the camera enforces the Lullaby Timer
        timer = _get_timer_minutes(self.hass, f"cuboai_lullaby_timer_{self._device_id}")
        self._cancel_scheduled_stop()

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
        self._cancel_scheduled_stop()
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
            # CARD path: the card plays lullabies via select_source and its
            # Play Time governs the duration (HA sends the stop — any value
            # works, unlike the camera's fixed native options).
            timer = _get_timer_minutes(self.hass, f"cuboai_speaker_timer_{self._device_id}")

            # Optimistic update
            if self.coordinator.data and "cameras" in self.coordinator.data:
                cam = self.coordinator.data["cameras"].get(self._device_id, {})
                if "local" in cam:
                    cam["local"]["lullaby_song"] = uuid
                    cam["local"]["lullaby_playing"] = True
            self.async_write_ha_state()

            # Camera plays in repeat-forever mode; HA enforces the timer.
            await self.hass.async_add_executor_job(
                _execute_lullaby_cmd,
                self._uid,
                self._account,
                self._password,
                self._camera_ip,
                "play",
                uuid,
                None,
                None,
            )
            self._schedule_stop(timer, uuid)
            try:
                await self.coordinator.async_request_refresh()
            except Exception:
                pass

    async def async_play_media(self, media_type: str, media_id: str, **kwargs):
        if media_type == MediaType.MUSIC:
            # NATIVE path (services/automations): camera enforces the Lullaby Timer
            uuid = next((k for k, v in self._catalog.items() if v[1] == media_id), media_id)
            timer = _get_timer_minutes(self.hass, f"cuboai_lullaby_timer_{self._device_id}")
            self._cancel_scheduled_stop()
            await self.hass.async_add_executor_job(
                _execute_lullaby_cmd,
                self._uid,
                self._account,
                self._password,
                self._camera_ip,
                "play",
                uuid,
                None,
                timer,
            )
            try:
                await self.coordinator.async_request_refresh()
            except Exception:
                pass

    async def async_set_volume_level(self, volume: float):
        # HA volume is 0.0 to 1.0, API is 0 to 100. Preserve the running
        # session's timer semantics: card sessions stay repeat-forever
        # (HA stops them), native sessions keep the camera-side Lullaby Timer.
        vol_int = int(volume * 100)
        if getattr(self, "_stop_timer_task", None):
            timer = None
        else:
            timer = _get_timer_minutes(self.hass, f"cuboai_lullaby_timer_{self._device_id}")
        await self.hass.async_add_executor_job(
            _execute_lullaby_cmd,
            self._uid,
            self._account,
            self._password,
            self._camera_ip,
            "volume",
            None,
            vol_int,
            timer,
        )
