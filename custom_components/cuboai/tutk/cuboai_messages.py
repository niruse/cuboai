"""
cuboai_messages.py — CuboAI Kalay AV-IOCTL application layer.

All message formats verified by Frida live capture on Android app v2.23.2
against a real CuboAI camera.

CONNECTION FACTS (confirmed by Frida):
  - Device UID:     license_id field from /user/cameras REST response
  - Account:        dev_admin_id  (e.g. "admin@YOUR_ACCOUNT")
  - Password:       dev_admin_pwd
  - security_mode:  0  (NON-SECURE — camera accepts plain connections)
  - auth_type:      0
  - No DTLS/PSK required

IOTC SESSION KEEPALIVE (plaintext, no library needed):
  RECV 16 bytes: 03 00 00 00 <session_id LE4> <timestamp LE4> 00 00 00 00
  SEND 24 bytes: 02 00 00 00 <session_id LE4> 01 00 00 00 00 00 00 00 <ts LE4> c8 00 00 00
"""
from __future__ import annotations
import struct
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# IOCTL type codes
# ---------------------------------------------------------------------------

# Video/audio stream
IOTYPE_USER_IPCAM_SETRESOLUTION         = 255
IOTYPE_USER_IPCAM_START                 = 511
IOTYPE_USER_IPCAM_AUDIOSTART            = 768

# Device info
IOTYPE_USER_GET_CONNECTED_USER_REQ      = 2320
IOTYPE_USER_GET_CONNECTED_USER_RESP     = 2321
IOTYPE_USER_SLEEP_SAFETY_STATUS_REQ     = 2336
IOTYPE_USER_SLEEP_SAFETY_STATUS_RESP    = 2337
IOTYPE_USER_GET_PRIVACY_MODE_REQ        = 2344
IOTYPE_USER_GET_PRIVACY_MODE_RESP       = 2345

# Lullaby
IOTYPE_USER_GET_LULLABY_INFO_REQ        = 2404   # get song list
IOTYPE_USER_GET_LULLABY_INFO_RESP       = 2405
IOTYPE_USER_SET_LULLABY_ACTION_REQ      = 2434   # play / stop
IOTYPE_USER_SET_LULLABY_ACTION_RESP     = 2435
IOTYPE_USER_GET_LULLABY_VOL_DURATION_REQ  = 2436  # current song + status
IOTYPE_USER_GET_LULLABY_VOL_DURATION_RESP = 2437
IOTYPE_USER_GET_LULLABY_SCHEDULE_REQ    = 2440
IOTYPE_USER_GET_LULLABY_SCHEDULE_RESP   = 2441

# Account
IOTYPE_USER_SET_ACCOUNT_INFO_REQ        = 2456
IOTYPE_USER_SET_ACCOUNT_INFO_RESP       = 2457

# Night light / light style
IOTYPE_USER_GET_NIGHT_LIGHT_ON_OFF_REQ  = 4352
IOTYPE_USER_GET_NIGHT_LIGHT_ON_OFF_RESP = 4353
IOTYPE_USER_SET_NIGHT_LIGHT_ON_OFF_REQ  = 4354
IOTYPE_USER_SET_NIGHT_LIGHT_ON_OFF_RESP = 4355
IOTYPE_USER_GET_LIGHT_STYLE_REQ         = 4366
IOTYPE_USER_GET_LIGHT_STYLE_RESP        = 4367
IOTYPE_USER_SET_LIGHT_STYLE_REQ         = 4368   # brightness
IOTYPE_USER_SET_LIGHT_STYLE_RESP        = 4369

# Status light (LED indicator on camera body)
IOTYPE_USER_GET_STATUS_LIGHT_ON_OFF_REQ  = 4362
IOTYPE_USER_GET_STATUS_LIGHT_ON_OFF_RESP = 4363
IOTYPE_USER_SET_STATUS_LIGHT_ON_OFF_REQ  = 4364
IOTYPE_USER_SET_STATUS_LIGHT_ON_OFF_RESP = 4365

# Hardware control (temp, humidity, all-in-one)
IOTYPE_USER_GET_HW_CONTROL_REQ          = 4384
IOTYPE_USER_GET_HW_CONTROL_RESP         = 4385
IOTYPE_USER_SET_HW_CONTROL_REQ          = 4386
IOTYPE_USER_SET_HW_CONTROL_RESP         = 4387

# Temperature/humidity dedicated
IOTYPE_USER_GET_TEMP_HUMIDITY_REQ       = 4372
IOTYPE_USER_GET_TEMP_HUMIDITY_RESP      = 4373

# Lullaby schedule/list
IOTYPE_USER_GET_LULLABY_SCHEDULES_REQ   = 4866
IOTYPE_USER_GET_LULLABY_SCHEDULES_RESP  = 4867
IOTYPE_USER_GET_LULLABY_LIST_REQ        = 4876
IOTYPE_USER_GET_LULLABY_LIST_RESP       = 4877


# ---------------------------------------------------------------------------
# Request builders  →  (type_code, payload_bytes)
# ---------------------------------------------------------------------------

def build_get_hw_control() -> tuple[int, bytes]:
    """GET_HW_CONTROL_REQ — returns temp, humidity, night light state, wifi etc."""
    return IOTYPE_USER_GET_HW_CONTROL_REQ, struct.pack('<i', 0) + b'\x00' * 4

def build_get_temp_humidity() -> tuple[int, bytes]:
    """GET_TEMP_HUMIDITY_REQ — dedicated temp/humidity only."""
    return IOTYPE_USER_GET_TEMP_HUMIDITY_REQ, struct.pack('<i', 0) + b'\x00' * 4

def build_get_lullaby_vol_duration() -> tuple[int, bytes]:
    """GET_LULLABY_VOL_DURATION_REQ — returns current song UUID and play state."""
    return IOTYPE_USER_GET_LULLABY_VOL_DURATION_REQ, b'\x00' * 8

def build_get_light_style() -> tuple[int, bytes]:
    """GET_LIGHT_STYLE_REQ — returns current brightness level (0-100)."""
    return IOTYPE_USER_GET_LIGHT_STYLE_REQ, struct.pack('<i', 0) + b'\x00' * 4

def build_get_night_light() -> tuple[int, bytes]:
    """GET_NIGHT_LIGHT_ON_OFF_REQ."""
    return IOTYPE_USER_GET_NIGHT_LIGHT_ON_OFF_REQ, struct.pack('<i', 0) + b'\x00' * 4

def build_set_night_light(on: bool, correlation_id: int = 0) -> tuple[int, bytes]:
    """SET_NIGHT_LIGHT_ON_OFF_REQ.
    Confirmed format from live capture: id(4) + on_off(4) + reserved(4)
    """
    return IOTYPE_USER_SET_NIGHT_LIGHT_ON_OFF_REQ, struct.pack('<III', correlation_id, 1 if on else 0, 0)

def build_set_light_style_brightness(brightness: int, correlation_id: int = 0) -> tuple[int, bytes]:
    """SET_LIGHT_STYLE_REQ — set night light brightness (0-100).
    Confirmed from live capture: brightness integer sits at byte offset 20 in 164-byte payload.
    Other fields (night style, status style) are zero for a brightness-only change.
    """
    payload = bytearray(164)
    struct.pack_into('<I', payload, 0, correlation_id)
    struct.pack_into('<I', payload, 20, brightness)
    return IOTYPE_USER_SET_LIGHT_STYLE_REQ, bytes(payload)

def build_set_lullaby_play(song_uuid: str, correlation_id: int = 0) -> tuple[int, bytes]:
    """SET_LULLABY_ACTION_REQ — play a song.
      offset 0:  correlation_id (4 bytes LE)
      offset 4:  song_uuid (null-terminated ASCII, 36 bytes)
      offset 40: zeros (28 bytes)
      offset 68: action = 0x01 (play)
      rest:      zeros

    NOTE: the action byte is 0x01 = PLAY (switches the current song and sets
    is_playing=1) and 0x00 = STOP.
    """
    payload = bytearray(200)
    struct.pack_into('<I', payload, 0, correlation_id)
    uuid_bytes = song_uuid.encode('ascii')[:36]
    payload[4:4+len(uuid_bytes)] = uuid_bytes
    payload[68] = 0x01  # play (see NOTE above — 0x01 plays, 0x00 stops)
    return IOTYPE_USER_SET_LULLABY_ACTION_REQ, bytes(payload)

def build_set_lullaby_stop(song_uuid: str, correlation_id: int = 0) -> tuple[int, bytes]:
    """SET_LULLABY_ACTION_REQ — stop playback (same as play but offset 68 = 0x00).
    See build_set_lullaby_play NOTE: 0x00 = STOP (re-verified live, fw 3.0.1369)."""
    payload = bytearray(200)
    struct.pack_into('<I', payload, 0, correlation_id)
    uuid_bytes = song_uuid.encode('ascii')[:36]
    payload[4:4+len(uuid_bytes)] = uuid_bytes
    payload[68] = 0x00  # stop (see build_set_lullaby_play NOTE)
    return IOTYPE_USER_SET_LULLABY_ACTION_REQ, bytes(payload)

def build_get_lullaby_info() -> tuple[int, bytes]:
    """GET_LULLABY_INFO_REQ — fetch song list."""
    return IOTYPE_USER_GET_LULLABY_INFO_REQ, b'\x00' * 8


# ---------------------------------------------------------------------------
# Response parsers
# ---------------------------------------------------------------------------

@dataclass
class HWControl:
    """GET_HW_CONTROL response — 96 bytes (temp, humidity, night light, wifi, etc.)."""
    id: int
    mic_level: int
    speaker_level: int
    night_vision_control: int
    video_v_flip_control: int
    status_light_on_off: int
    night_light_on_off: int
    camera_angle: int
    stand_type: int
    temperature: float      # °C
    humidity: float         # %
    fw_version: str
    ssid: str
    wifi_quality: int       # %
    wifi_maxbitrate: int
    result: int
    dongle: int
    mat_ctrl: int
    lullaby_list_update: int

    SIZE = 96

    @classmethod
    def parse(cls, raw: bytes) -> 'HWControl':
        if len(raw) < cls.SIZE:
            raise ValueError(f"HWControl response too short: {len(raw)} < {cls.SIZE}")
        (id_, mic, speaker, night_vis, v_flip, status_light, night_light,
         cam_angle, stand_type) = struct.unpack_from('<iiiiiiiii', raw, 0)
        temp, humid = struct.unpack_from('<ff', raw, 36)
        fw   = raw[44:56].split(b'\x00', 1)[0].decode('ascii', 'replace')
        ssid = raw[56:72].split(b'\x00', 1)[0].decode('ascii', 'replace')
        wifi_q, wifi_mbr, result, dongle, mat_ctrl, lullaby_upd = struct.unpack_from('<iiiiii', raw, 72)
        return cls(id_, mic, speaker, night_vis, v_flip, status_light, night_light,
                   cam_angle, stand_type, temp, humid, fw, ssid,
                   wifi_q, wifi_mbr, result, dongle, mat_ctrl, lullaby_upd)

    @property
    def ok(self) -> bool:
        return self.result == 0

    @property
    def night_light_on(self) -> bool:
        return self.night_light_on_off == 1


@dataclass
class TempHumidity:
    """GET_TEMP_HUMIDITY response — 20 bytes."""
    id: int
    result: int
    temperature: float   # °C
    humidity: float      # %
    dongle: int

    SIZE = 20

    @classmethod
    def parse(cls, raw: bytes) -> 'TempHumidity':
        if len(raw) < cls.SIZE:
            raise ValueError(f"TempHumidity response too short: {len(raw)} < {cls.SIZE}")
        id_, result, temp, humid, dongle = struct.unpack_from('<iiffi', raw, 0)
        return cls(id_, result, temp, humid, dongle)

    @property
    def ok(self) -> bool:
        return self.result == 0


@dataclass
class NightLightStatus:
    """SET_NIGHT_LIGHT_ON_OFF response — 12 bytes.
    Confirmed: id(4) + on_off(4) + reserved(4)
    """
    id: int
    on_off: int   # 0=off 1=on
    reserved: int

    SIZE = 12

    @classmethod
    def parse(cls, raw: bytes) -> 'NightLightStatus':
        if len(raw) < cls.SIZE:
            raise ValueError(f"NightLightStatus too short: {len(raw)} < {cls.SIZE}")
        id_, on_off, reserved = struct.unpack_from('<III', raw, 0)
        return cls(id_, on_off, reserved)

    @property
    def is_on(self) -> bool:
        return self.on_off == 1


@dataclass
class LightStyle:
    """GET_LIGHT_STYLE_RESP (type 4367, 552 bytes).
    Brightness at byte offset 24 (int offset 6), range 0-100.
    Confirmed from live capture: brightness=4 when night light is on dim.
    """
    brightness: int   # 0-100

    SIZE = 552

    @classmethod
    def parse(cls, raw: bytes) -> 'LightStyle':
        if len(raw) < 28:
            raise ValueError(f"LightStyle too short: {len(raw)}")
        brightness = struct.unpack_from('<i', raw, 24)[0]
        return cls(brightness)


@dataclass
class LullabyVolDuration:
    """GET_LULLABY_VOL_DURATION_RESP — 204 bytes.
    Confirmed from live capture:
      offset 0:  id (4)
      offset 4:  result (4) — standard status/result word (0 on success), per the
                 universal SMsg*Resp layout {id@0, result@4, ...} in the APK
      offset 8:  current_song_uuid (null-terminated ASCII, 36 bytes)
      offset 72: playing flag (4) — 1=playing, 0=stopped
    Live capture showed song UUID = 'F55001F0-9D5A-4C09-B58C-896964CAE485' playing.
    """
    id: int
    current_song_uuid: str
    is_playing: bool

    SIZE = 204

    @classmethod
    def parse(cls, raw: bytes) -> 'LullabyVolDuration':
        if len(raw) < 76:
            raise ValueError(f"LullabyVolDuration too short: {len(raw)}")
        id_ = struct.unpack_from('<I', raw, 0)[0]
        uuid = raw[8:44].split(b'\x00', 1)[0].decode('ascii', 'replace')
        playing = struct.unpack_from('<I', raw, 72)[0] == 1 if len(raw) >= 76 else False
        return cls(id_, uuid, playing)


# ---------------------------------------------------------------------------
# Transport-agnostic client
# ---------------------------------------------------------------------------

class CuboAIClient:
    """Wraps a TUTK AV-channel transport into typed CuboAI calls.

    `transport` must implement:
        ioctl(type_code: int, payload: bytes) -> tuple[int, bytes]
    returning (response_type_code, response_payload).

    See cuboai-wire-protocol.md for the connection sequence.
    """

    def __init__(self, transport):
        self.transport = transport

    def get_hw_control(self) -> HWControl:
        resp_type, data = self.transport.ioctl(*build_get_hw_control())
        if resp_type != IOTYPE_USER_GET_HW_CONTROL_RESP:
            raise ValueError(f"Unexpected response type {resp_type}")
        return HWControl.parse(data)

    def get_temp_humidity(self) -> TempHumidity:
        resp_type, data = self.transport.ioctl(*build_get_temp_humidity())
        if resp_type != IOTYPE_USER_GET_TEMP_HUMIDITY_RESP:
            raise ValueError(f"Unexpected response type {resp_type}")
        return TempHumidity.parse(data)

    def get_lullaby_status(self) -> LullabyVolDuration:
        resp_type, data = self.transport.ioctl(*build_get_lullaby_vol_duration())
        if resp_type != IOTYPE_USER_GET_LULLABY_VOL_DURATION_RESP:
            raise ValueError(f"Unexpected response type {resp_type}")
        return LullabyVolDuration.parse(data)

    def get_cry_detect_status(self) -> dict:
        resp_type, data = self.transport.ioctl(*build_get_cry_detect())
        return parse_cry_detection(data)

    def get_sleep_safety_status(self) -> dict:
        resp_type, data = self.transport.ioctl(*build_get_sleep_safety_setting())
        return parse_sleep_safety_setting(data)

    def get_cough_status(self) -> dict:
        resp_type, data = self.transport.ioctl(*build_get_cough_setting())
        return parse_cough_detection(data)

    def get_lightweight_status(self) -> dict:
        resp_type, data = self.transport.ioctl(*build_get_lightweight_status())
        return parse_lightweight_status(data)

    def get_sleep_mode(self) -> dict:
        resp_type, data = self.transport.ioctl(*build_get_sleep_mode())
        return parse_sleep_mode(data)

    def get_mat_info(self) -> dict:
        resp_type, data = self.transport.ioctl(*build_get_mat_info())
        return parse_mat_info(data)

    def get_smart_temp_info(self) -> dict:
        resp_type, data = self.transport.ioctl(*build_get_smart_temp_info())
        return parse_smart_temp_info(data)

    def get_session_stats(self) -> dict:
        resp_type, data = self.transport.ioctl(*build_get_session_stats())
        return parse_session_stats(data)

    def get_connected_users(self) -> dict:
        resp_type, data = self.transport.ioctl(*build_get_user_list())
        return parse_user_list(data)

    def get_wifi(self) -> dict:
        resp_type, data = self.transport.ioctl(*build_get_wifi())
        return parse_wifi(data)

    def get_hw_policy(self) -> dict:
        resp_type, data = self.transport.ioctl(*build_get_hw_policy())
        return parse_hw_policy(data)

    def get_smart_temp_config(self) -> dict:
        resp_type, data = self.transport.ioctl(*build_get_smart_temp_config())
        return parse_smart_temp_config(data)

    def set_night_light(self, on: bool) -> NightLightStatus:
        resp_type, data = self.transport.ioctl(*build_set_night_light(on))
        if resp_type != IOTYPE_USER_SET_NIGHT_LIGHT_ON_OFF_RESP:
            raise ValueError(f"Unexpected response type {resp_type}")
        return NightLightStatus.parse(data)

    def set_brightness(self, brightness: int) -> tuple[int, bytes]:
        """Set night light brightness 0-100."""
        return self.transport.ioctl(*build_set_light_style_brightness(brightness))

    def play_lullaby(self, song_uuid: str) -> tuple[int, bytes]:
        return self.transport.ioctl(*build_set_lullaby_play(song_uuid))

    def stop_lullaby(self, song_uuid: str) -> tuple[int, bytes]:
        return self.transport.ioctl(*build_set_lullaby_stop(song_uuid))


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    # Test HWControl parse with synthetic data matching live capture values
    buf = bytearray(100)
    struct.pack_into('<iiiiiiiii', buf, 0, 0, 2, 2, 0, 0, 0, 1, 0, 0)
    struct.pack_into('<ff', buf, 36, 31.0, 50.0)
    buf[44:53] = b'3.0.1369'
    buf[56:62] = b'MyWiFi'
    struct.pack_into('<iiiiii', buf, 72, 87, 0, 0, 0, 0, 0)
    hw = HWControl.parse(bytes(buf))
    assert abs(hw.temperature - 31.0) < 1e-5
    assert abs(hw.humidity - 50.0) < 1e-5
    assert hw.night_light_on
    assert hw.fw_version == '3.0.1369'
    assert hw.ssid == 'MyWiFi'
    print(f"HWControl OK: {hw.temperature}°C {hw.humidity}% NL={'ON' if hw.night_light_on else 'OFF'}")

    # Test NightLightStatus
    nl = NightLightStatus.parse(struct.pack('<III', 42, 1, 0))
    assert nl.is_on
    print(f"NightLightStatus OK: on={nl.is_on}")

    # Test LullabyVolDuration
    buf2 = bytearray(204)
    struct.pack_into('<I', buf2, 0, 99)
    uuid_test = b'F55001F0-9D5A-4C09-B58C-896964CAE485'
    buf2[8:8+len(uuid_test)] = uuid_test
    struct.pack_into('<I', buf2, 72, 1)
    lv = LullabyVolDuration.parse(bytes(buf2))
    assert lv.current_song_uuid == 'F55001F0-9D5A-4C09-B58C-896964CAE485'
    assert lv.is_playing
    print(f"LullabyVolDuration OK: uuid={lv.current_song_uuid} playing={lv.is_playing}")

    # Test request builders
    tc, p = build_set_night_light(True)
    assert tc == IOTYPE_USER_SET_NIGHT_LIGHT_ON_OFF_REQ
    assert struct.unpack_from('<I', p, 4)[0] == 1
    tc, p = build_set_night_light(False)
    assert struct.unpack_from('<I', p, 4)[0] == 0
    print("Night light builder OK")

    tc, p = build_set_lullaby_stop('F55001F0-9D5A-4C09-B58C-896964CAE485')
    assert p[68] == 0x00   # stop = 0x00
    tc, p = build_set_lullaby_play('F55001F0-9D5A-4C09-B58C-896964CAE485')
    assert p[68] == 0x01   # play = 0x01
    print("Lullaby action builder OK")

    tc, p = build_set_light_style_brightness(75)
    assert struct.unpack_from('<I', p, 20)[0] == 75
    print("Brightness builder OK")

    print("\nAll tests passed.")


# ---------------------------------------------------------------------------
# Lullaby volume / timer  (confirmed from live capture)
# ---------------------------------------------------------------------------

LULLABY_TIMER_REPEAT  = 0x0000   # infinite repeat
LULLABY_TIMER_30MIN   = 0x0708   # 30 minute sleep timer (1800s)
LULLABY_TIMER_60MIN   = 0x0e10   # 60 minute sleep timer (3600s)

def build_set_lullaby_vol_duration(volume: int, timer: int = LULLABY_TIMER_REPEAT,
                                   correlation_id: int = 0) -> tuple[int, bytes]:
    """SET_LULLABY_VOL_DURATION_REQ (type 2438, 140 bytes).

    Confirmed struct from live capture:
      offset 0:  correlation_id (4 bytes LE)
      offset 4:  timer_mode     (2 bytes LE)  0x0000=repeat, 0x0708=30min, 0x0e10=60min
      offset 6:  padding        (2 bytes)
      offset 8:  volume         (4 bytes LE, integer 0-100)
      offset 12: padding        (rest zeros)

    Args:
        volume: 0-100
        timer:  LULLABY_TIMER_REPEAT / LULLABY_TIMER_30MIN / LULLABY_TIMER_60MIN
    """
    payload = bytearray(140)
    struct.pack_into('<I', payload, 0, correlation_id)
    struct.pack_into('<H', payload, 4, timer)
    struct.pack_into('<I', payload, 8, volume)
    return 2438, bytes(payload)

IOTYPE_USER_SET_LULLABY_VOL_DURATION_REQ  = 2438
IOTYPE_USER_SET_LULLABY_VOL_DURATION_RESP = 2439


@dataclass
class LullabySchedule:
    """GET_LULLABY_SCHEDULE_RESP (type 2441, 144 bytes).

    Echoes current timer mode and volume after each SET.
    Confirmed struct:
      offset 8:  timer_mode (4 bytes LE)
      offset 12: volume     (4 bytes LE, integer 0-100)
      offset 16: playing    (4 bytes LE, 0=stopped 1=playing)
    """
    timer_mode: int
    volume: int
    playing: bool

    SIZE = 144

    @classmethod
    def parse(cls, raw: bytes) -> 'LullabySchedule':
        if len(raw) < 20:
            raise ValueError(f"LullabySchedule too short: {len(raw)}")
        timer = struct.unpack_from('<I', raw, 8)[0]
        vol   = struct.unpack_from('<I', raw, 12)[0]
        play  = struct.unpack_from('<I', raw, 16)[0]
        return cls(timer, vol, bool(play))

    @property
    def timer_name(self) -> str:
        return {
            LULLABY_TIMER_REPEAT: 'repeat',
            LULLABY_TIMER_30MIN:  '30min',
            LULLABY_TIMER_60MIN:  '60min',
        }.get(self.timer_mode, f'unknown_0x{self.timer_mode:04x}')


# ---------------------------------------------------------------------------
# Lullaby catalog  (extracted from LullabyManager.java, app v2.23.2)
# UUID → (internal_key, display_name, category)
# ---------------------------------------------------------------------------

LULLABY_CATALOG: dict[str, tuple[str, str, str]] = {
    # Classical
    "A7062448-4837-11EC-81D3-0242AC130003": ("CLASSICAL_1", "Brahms' Lullaby",                  "classical"),
    "E23245A6-4837-11EC-81D3-0242AC130003": ("CLASSICAL_2", "Schubert's Serenade",              "classical"),
    "33899E9A-4833-11EC-81D3-0242AC130003": ("CLASSICAL_3", "Bach's Minuet",                    "classical"),
    "1A083594-4838-11EC-81D3-0242AC130003": ("CLASSICAL_4", "Mozart's Lullaby 1",               "classical"),
    "52D62CC8-4838-11EC-81D3-0242AC130003": ("CLASSICAL_5", "Mozart's Lullaby 2",               "classical"),
    "D844127D-194B-4A26-A54C-C69304410DAE": ("CLASSICAL_6", "Schumann's Träumerei",             "classical"),
    "E29EC2A3-52E0-42c6-829E-EFD9C7B38D1B": ("CLASSICAL_7", "Pachelbel's Canon",                "classical"),
    # Light
    "4C89C060-B51E-4E02-8C9C-32C3E03D023C": ("LIGHT_1",    "Gentle Music Box Melody",          "light"),
    "D505858E-413D-44B5-A3BF-42B84254B041": ("LIGHT_2",    "Bedtime",                          "light"),
    "963EA024-D752-4444-987E-4930193E5CD5": ("LIGHT_3",    "Floating",                         "light"),
    "1401152C-017E-4182-954B-8CD9399FD970": ("LIGHT_4",    "Whisper",                          "light"),
    "0F8DE839-CBB4-4E2B-934C-F9DDBC72316E": ("LIGHT_5",    "Moon",                             "light"),
    "54858078-165D-4DD9-873B-0C20FB29ECAA": ("LIGHT_6",    "Sleepywood",                       "light"),
    "11E87EFB-553C-41B6-9E13-E9DE288A3E73": ("LIGHT_7",    "Planet",                           "light"),
    # Lullabies
    "27BF885A-6B28-4B64-924B-43DFAE08D45F": ("LULLABY_1",  "Twinkle Twinkle Little Star",      "lullaby"),
    "C067C97C-DAD2-413B-B55C-A60437B58D04": ("LULLABY_2",  "Are You Sleeping",                 "lullaby"),
    "1130009C-7759-4CF4-92FA-27F446970240": ("LULLABY_3",  "Row Row Row Your Boat",            "lullaby"),
    "9A7BDC1A-639F-472A-9B0E-C660E24C34EE": ("LULLABY_4",  "Old MacDonald",                    "lullaby"),
    "6CB9C0BE-2F80-4918-9DD3-BFF01CFB1B20": ("LULLABY_5",  "Happy Birthday",                   "lullaby"),
    "2F0DC563-A6E6-453B-866A-F12905995FC8": ("LULLABY_6",  "Mary Had a Little Lamb",           "lullaby"),
    "35FEC095-DB68-4AA4-BD50-8D1969557232": ("LULLABY_7",  "My Bonnie Lies Over the Ocean",    "lullaby"),
    "7599BC29-ED69-4DBA-9091-DFAD143D25C5": ("LULLABY_8",  "Hush Little Baby",                 "lullaby"),
    "41037383-0EF9-4D79-A432-E47FB8A48D7D": ("LULLABY_9",  "Rock-a-bye Baby",                  "lullaby"),
    # Noise / nature
    "963B384F-689F-4669-90AD-A35ED9C4125C": ("NOISE_1",    "Birds",                            "noise"),
    "0B720264-E7E6-4050-8A81-27EDEC01E172": ("NOISE_2",    "Rain",                             "noise"),
    "CC2D07C1-86AA-482E-A7E2-2DF09A1E3B0F": ("NOISE_3",    "Morning Rain Forest",              "noise"),
    "DED63F0F-7129-4570-AFA5-C01BD163BC72": ("NOISE_4",    "Brown Noise",                      "noise"),
    "F55001F0-9D5A-4C09-B58C-896964CAE485": ("NOISE_5",    "White Noise",                      "noise"),
    "511EC5AD-5978-45EC-B052-7CDB27B5F15D": ("NOISE_6",    "Electric Fan",                     "noise"),
    "84D6933E-1FD2-4B5D-AE16-C156473DCB87": ("NOISE_7",    "Shh",                              "noise"),
    "E96A18B9-3E77-4AC4-8FB0-8CDB4817E9A2": ("NOISE_8",    "Pink Noise",                       "noise"),
    "BCDC0C97-D276-4F29-B126-8A56C65761CF": ("NOISE_9",    "Ocean",                            "noise"),
    "06455BE6-9D04-4DF8-9FCA-53EBFFF4B66E": ("NOISE_10",   "Stream",                           "noise"),
    "25CCB2AC-1105-4C1A-B9CB-EA877594E3CB": ("NOISE_11",   "Fireplace",                        "noise"),
}

def get_song_name(uuid: str) -> str:
    """Return display name for a lullaby UUID, or the UUID itself if unknown."""
    entry = LULLABY_CATALOG.get(uuid.upper())
    return entry[1] if entry else uuid

def get_song_category(uuid: str) -> str:
    """Return category for a lullaby UUID."""
    entry = LULLABY_CATALOG.get(uuid.upper())
    return entry[2] if entry else "unknown"


# ---------------------------------------------------------------------------
# Additional IOCTL type codes from source analysis (getcubo.zip)
# ---------------------------------------------------------------------------

# Danger zone (premium)
IOTYPE_USER_GET_DANGER_ZONE_REQ          = 2312
IOTYPE_USER_GET_DANGER_ZONE_RESP         = 2313

# Cry detection
IOTYPE_USER_GET_CRY_DETECT_REQ          = 2324
IOTYPE_USER_GET_CRY_DETECT_RESP         = 2325
IOTYPE_USER_SET_CRY_DETECT_REQ          = 2326
IOTYPE_USER_SET_CRY_DETECT_RESP         = 2327

# Sleep safety settings
IOTYPE_USER_GET_SLEEP_SAFETY_SETTING_REQ  = 2330
IOTYPE_USER_GET_SLEEP_SAFETY_SETTING_RESP = 2331
IOTYPE_USER_SET_SLEEP_SAFETY_SETTING_REQ  = 2332
IOTYPE_USER_SET_SLEEP_SAFETY_SETTING_RESP = 2333

# Sleep safety status (live detection result)
IOTYPE_USER_GET_SLEEP_SAFETY_STATUS_REQ  = 2336
IOTYPE_USER_GET_SLEEP_SAFETY_STATUS_RESP = 2337

# Sleep mode / privacy mode (same IOCTL, confirmed from source)
# GET_PRIVACY_MODE_RESP and GET_SLEEP_MODE both use type 2344
IOTYPE_USER_GET_SLEEP_MODE_REQ          = 2344
IOTYPE_USER_GET_SLEEP_MODE_RESP         = 2345
IOTYPE_USER_SET_SLEEP_MODE_REQ          = 2346
IOTYPE_USER_SET_SLEEP_MODE_RESP         = 2347

# Detection zone
IOTYPE_USER_GET_DETECTION_ZONE_REQ      = 2352
IOTYPE_USER_GET_DETECTION_ZONE_RESP     = 2353

# Auto capture / snapshot
IOTYPE_USER_GET_AUTO_CAPTURE_REQ        = 2368
IOTYPE_USER_GET_AUTO_CAPTURE_RESP       = 2369
# NOTE: SET_AUTOSNAPSHOT is 2366/2367, NOT 2370/2371 (the GET pair is 2368/2369).
# Proven live: code 2370 is silently ignored; 2366 applies the mode change.
IOTYPE_USER_SET_AUTO_CAPTURE_REQ        = 2366
IOTYPE_USER_SET_AUTO_CAPTURE_RESP       = 2367

# Detection zone v2
IOTYPE_USER_GET_DETECTION_ZONE_V2_REQ   = 2380
IOTYPE_USER_GET_DETECTION_ZONE_V2_RESP  = 2381

# Firmware update
IOTYPE_USER_GET_UPDATE_INFO_REQ         = 2400
IOTYPE_USER_GET_UPDATE_INFO_RESP        = 2401

# Light way (sunrise/sunset ambient light feature)
IOTYPE_USER_GET_LIGHT_WAY_STATUS_REQ    = 2404
IOTYPE_USER_GET_LIGHT_WAY_STATUS_RESP   = 2405
IOTYPE_USER_GET_LIGHT_WAY_CONFIG_REQ    = 2406
IOTYPE_USER_GET_LIGHT_WAY_CONFIG_RESP   = 2407

# Cough detection
IOTYPE_USER_GET_COUGH_SETTING_REQ       = 2452
IOTYPE_USER_GET_COUGH_SETTING_RESP      = 2453
IOTYPE_USER_SET_COUGH_SETTING_REQ       = 2454
IOTYPE_USER_SET_COUGH_SETTING_RESP      = 2455

# Connected users
IOTYPE_USER_GET_CONNECTED_USER_REQ      = 2458
IOTYPE_USER_GET_CONNECTED_USER_RESP     = 2459

# Danger zone 2
IOTYPE_USER_GET_DANGER_ZONE_2_REQ       = 4612
IOTYPE_USER_GET_DANGER_ZONE_2_RESP      = 4613

# HW Policy (device capability flags)
IOTYPE_USER_GET_HW_POLICY_REQ           = 4378
IOTYPE_USER_GET_HW_POLICY_RESP          = 4379

# Smart temp (wearable thermometer accessory)
IOTYPE_USER_GET_SMART_TEMP_INFO_REQ     = 4876
IOTYPE_USER_GET_SMART_TEMP_INFO_RESP    = 4877
IOTYPE_USER_GET_SMART_TEMP_CONFIG_REQ   = 4880
IOTYPE_USER_GET_SMART_TEMP_CONFIG_RESP  = 4881


# ---------------------------------------------------------------------------
# Request builders for new features
# ---------------------------------------------------------------------------

def build_get_cry_detect() -> tuple[int, bytes]:
    """GET_CRY_DETECT_REQ (2324) — cry detection enabled/sensitivity."""
    return IOTYPE_USER_GET_CRY_DETECT_REQ, struct.pack('<i', 0) + b'\x00' * 4

def build_get_sleep_safety_status() -> tuple[int, bytes]:
    """GET_SLEEP_SAFETY_STATUS_REQ (2336) — live safe sleep detection state."""
    return IOTYPE_USER_GET_SLEEP_SAFETY_STATUS_REQ, struct.pack('<i', 0) + b'\x00' * 4

def build_get_sleep_safety_setting() -> tuple[int, bytes]:
    """GET_SLEEP_SAFETY_SETTING_REQ (2330) — safety_alert and cover_alert config."""
    return IOTYPE_USER_GET_SLEEP_SAFETY_SETTING_REQ, struct.pack('<i', 0) + b'\x00' * 4

def build_get_sleep_mode() -> tuple[int, bytes]:
    """GET_SLEEP_MODE_REQ (2344) — sleep mode / privacy mode state.
    Note: same IOCTL as privacy mode — confirmed from source.
    """
    return IOTYPE_USER_GET_SLEEP_MODE_REQ, struct.pack('<i', 0) + b'\x00' * 4

def build_set_sleep_mode(on: bool) -> tuple[int, bytes]:
    """SET_SLEEP_MODE_REQ (2346) — enable/disable sleep mode (= privacy mode).
    When on: camera feed is suspended, no video/audio transmitted.

    Payload confirmed via Frida (96 bytes):
      bytes 0-3:  unix timestamp LE
      bytes 4-87: zeros
      byte 88:    on/off flag (1=on, 0=off)
      bytes 89-95: zeros
    """
    import time as _t
    payload = bytearray(96)
    struct.pack_into('<I', payload, 0, int(_t.time()))
    payload[88] = 1 if on else 0
    return IOTYPE_USER_SET_SLEEP_MODE_REQ, bytes(payload)

def build_get_cough_setting() -> tuple[int, bytes]:
    """GET_COUGH_SETTING_REQ (2452)."""
    return IOTYPE_USER_GET_COUGH_SETTING_REQ, struct.pack('<i', 0) + b'\x00' * 4

def build_get_auto_capture() -> tuple[int, bytes]:
    """GET_AUTO_CAPTURE_REQ (2368) — auto snapshot settings."""
    return IOTYPE_USER_GET_AUTO_CAPTURE_REQ, struct.pack('<i', 0) + b'\x00' * 4

def build_get_connected_users() -> tuple[int, bytes]:
    """GET_CONNECTED_USER_REQ (2458) — how many clients are connected."""
    return IOTYPE_USER_GET_CONNECTED_USER_REQ, struct.pack('<i', 0) + b'\x00' * 4

def build_check_firmware_update() -> tuple[int, bytes]:
    """GET_UPDATE_INFO_REQ (2400) — current/latest firmware + update availability."""
    return IOTYPE_USER_GET_UPDATE_INFO_REQ, struct.pack('<i', 0) + b'\x00' * 4

def build_set_cry_detect(get_resp_bytes: bytes, *, enabled=None, sensitivity=None,
                         correlation_id: int = 0) -> tuple[int, bytes]:
    """SET_CRY_DETECT_REQ (2326) — read-modify-write of the 40-byte cry-detect struct.

    `get_resp_bytes` MUST be the raw GET_CRY_DETECT response. Every current value is
    echoed back unchanged; only the keyword fields you pass are modified.

    SET request wire layout (getSize()==40) — the SET struct shares the SAME field
    offsets as the GET response (the `result` word @4 is present-but-zero). A value
    written to @36 lands in the GET's cry_alert_sensitivity, and a value written to
    @32 lands in cry_alert.
        @0  id (I)
        @4  result (I, =0)
        @8  audio_filter_enable (I)
        @12 audio_filter_bypass_energy_level (I)
        @16 cry_criteria_hit_percentage (D, 8)
        @24 cry_criteria_dnn_confidence (D, 8)
        @32 cry_alert (I)                 <- enable flag (`enabled`)
        @36 cry_alert_sensitivity (I)     <- sensitivity (GET 0x24)

    The previous builder wrote enabled@4 / sensitivity@8 (the audio_filter words),
    which is why a sensitivity change was accepted-but-ignored.
    """
    payload = bytearray(40)
    payload[:40] = get_resp_bytes[:40]                  # echo full struct head raw
    struct.pack_into('<i', payload, 0, correlation_id)  # id
    struct.pack_into('<i', payload, 4, 0)               # result
    if enabled is not None:
        # cry_alert is a bitmask (bit0=enabled, bit1=AI); flip only bit0, keep bit1.
        cur = struct.unpack_from('<i', payload, 32)[0]
        cur = (cur & ~1) | (1 if enabled else 0)
        struct.pack_into('<i', payload, 32, cur)                   # cry_alert
    if sensitivity is not None:
        struct.pack_into('<i', payload, 36, int(sensitivity))      # cry_alert_sensitivity
    return IOTYPE_USER_SET_CRY_DETECT_REQ, bytes(payload)

def build_set_cough_setting(get_resp_bytes: bytes = None, *, enabled=None,
                            in_crib=None, sensitivity=None,
                            correlation_id: int = 0) -> tuple[int, bytes]:
    """SET_COUGH_SETTING_REQ (2454) — cough detection enable / mode / sensitivity.

    SET wire layout (getSize()==16):
        @0 id (I)   @4 enable (I, = the coughAlert BITMASK)   @8 sensitivity (I)
        @12 reserved[4]

    `coughAlert` (GET resp @8) is a BITMASK, not a bool:
        bit0 (&1) = cough alert enabled
        bit1 (&2) = "Only when baby is in crib" (cough-AI). Cleared = "Always Alert".
    So: 0=off, 1=on+Always Alert, 3=on+In-crib-only.

    Read-modify-write: pass `get_resp_bytes` = the raw GET_COUGH_SETTING response so
    untouched bits/sensitivity are preserved; pass `enabled`/`in_crib`/`sensitivity`
    to change them. With no get bytes it builds from zero (enabled defaults False)."""
    cur_mask = 0
    cur_sens = 0
    if get_resp_bytes is not None and len(get_resp_bytes) >= 16:
        cur_mask = struct.unpack_from('<I', get_resp_bytes, 8)[0]
        s = struct.unpack_from('<I', get_resp_bytes, 12)[0]
        # @12 is the 0x1a22 marker tail on this fw, not a real sensitivity — don't
        # echo garbage into the SET sensitivity field; only keep a sane 1-3 value.
        cur_sens = s if 1 <= s <= 3 else 0
    mask = cur_mask
    if enabled is not None:
        mask = (mask & ~1) | (1 if enabled else 0)
    if in_crib is not None:
        mask = (mask & ~2) | (2 if in_crib else 0)
    sens = cur_sens if sensitivity is None else int(sensitivity)
    payload = struct.pack('<III', correlation_id, mask, sens) + b'\x00' * 4
    return IOTYPE_USER_SET_COUGH_SETTING_REQ, payload


# ---------------------------------------------------------------------------
# GET response parsers  →  plain dicts
# ---------------------------------------------------------------------------
# Shared by both backends (pure cuboai_pure.TUTKDirectSession and native
# cuboai_tutk.TUTKSession) so their decoded output is identical by construction.
# Where the on-wire struct layout is confirmed (HW control, light style, lullaby)
# the fields are exact; for the detection/status IOCTLs whose C struct we have not
# yet fully decoded, the parser extracts the well-known leading fields (id @0, result @4)
# plus the most likely flag/level words and ALSO returns 'raw_hex' so the true
# layout stays inspectable. Tighten these as captures confirm offsets.

def _u32(raw: bytes, off: int):
    return struct.unpack_from('<I', raw, off)[0] if len(raw) >= off + 4 else None

def _i32(raw: bytes, off: int):
    return struct.unpack_from('<i', raw, off)[0] if len(raw) >= off + 4 else None

def _trim_marker(raw: bytes) -> bytes:
    """Strip the camera's 4-byte 0x1a22 'marker+timestamp' tail that rides on some
    IOCTL response payloads (e.g. 221a221a / 221a231a). The handoff documents this
    as real-but-unused struct tail; reading a field over it yields garbage."""
    if len(raw) >= 4 and raw[-4] == 0x22 and raw[-3] == 0x1a and raw[-1] == 0x1a:
        return raw[:-4]
    return raw

def _nonzero_words(raw: bytes) -> dict:
    """Map offset -> value for every non-zero LE u32 word (4-byte aligned)."""
    return {off: struct.unpack_from('<I', raw, off)[0]
            for off in range(0, len(raw) - 3, 4)
            if struct.unpack_from('<I', raw, off)[0] != 0}


def parse_hw_control(raw: bytes) -> dict:
    """GET_HW_CONTROL_RESP (4385) — exact layout."""
    hw = HWControl.parse(raw)
    # night_vision_control @12 (0=auto, 1=on/IR, 2=off)
    nv = {0: 'auto', 1: 'on', 2: 'off'}.get(hw.night_vision_control,
                                            f'mode {hw.night_vision_control}')
    return {
        'temp_c':          round(hw.temperature, 1),
        'humidity_pct':    round(hw.humidity, 1),
        'firmware':        hw.fw_version,
        'ssid':            hw.ssid,
        'wifi_strength':   hw.wifi_quality,
        'night_light_on':  hw.night_light_on,
        'status_light_on': bool(hw.status_light_on_off),
        'night_vision':    nv,
        'video_flip':      bool(hw.video_v_flip_control),   # vertical image flip
        'mic_level':       hw.mic_level,
        'speaker_level':   hw.speaker_level,
        # brightness is not carried in the HW-control struct — use get_light_style()
        'light_brightness': None,
    }


def parse_light_style(raw: bytes) -> dict:
    """GET_LIGHT_STYLE_RESP (4367). brightness @24 confirmed (LightStyle dataclass)."""
    def _style(base):
        if len(raw) < base + 20:
            return None
        return {'brightness': _i32(raw, base), 'pattern_id': _i32(raw, base + 4),
                'r': _i32(raw, base + 8), 'g': _i32(raw, base + 12), 'b': _i32(raw, base + 16)}
    return {
        'brightness':   _i32(raw, 24),
        'style':        _i32(raw, 0),     # correlation/id word; refine vs capture
        'warm_cool':    _i32(raw, 28),
        'status_light': _style(8),        # UNVERIFIED offsets (APK SMsgLIGHTSTYLE)
        'night_light':  _style(28),       # UNVERIFIED offsets
        'raw_len':      len(raw),
        'raw_hex':      raw[:64].hex(),
    }


def parse_sleep_safety(raw: bytes) -> dict:
    """GET_SLEEP_SAFETY_STATUS_RESP (2337) — live safe-sleep detection state.
    Wire order: id, result, status@8, remaining_time@12, duration@16.
    status 0 = no active detection / idle."""
    status = _i32(raw, 8)
    return {
        'status':         status,
        'active':         bool(status),
        'remaining_time': _u32(raw, 12),
        'duration':       _u32(raw, 16),
        'id':             _u32(raw, 0),
        'result':         _i32(raw, 4),
        'raw_len':        len(raw),
        'raw_hex':        raw[:24].hex(),
    }


def parse_sleep_mode(raw: bytes) -> dict:
    """GET_SLEEP_MODE_RESP / GET_PRIVACY_MODE_RESP (2345) — sleep/privacy mode.
    SET payload places the on/off flag late in the struct; on the GET response the
    enable flag is one of the leading words — expose candidates + raw."""
    return {
        'id':       _u32(raw, 0),
        'result':   _i32(raw, 4),
        'enabled':  bool(_u32(raw, 8)) if len(raw) >= 12 else None,
        'raw_len':  len(raw),
        'raw_hex':  raw[:64].hex(),
    }


def _parse_detection(raw: bytes) -> dict:
    """Shared best-effort parser for the cry/cough detection-setting responses.

    Exact struct not fully decoded. Empirically the config words sit AFTER a block of
    zero/reserved leading words and BEFORE the 0x1a22 marker tail (live cry response:
    value 3 @0x20, value 2 @0x24; live cough response: all-zero -> feature inactive).
    We strip the marker tail, collect the non-zero words, and report the feature as
    enabled when any config word is set, sensitivity from the first such word. The
    full non-zero map is returned as 'fields' (offset->value) so semantics stay
    inspectable, alongside raw_hex."""
    body = _trim_marker(raw)
    nz = _nonzero_words(body)
    # don't count a leading id word (offset 0) as a config field
    cfg = {off: v for off, v in nz.items() if off >= 8}
    vals = [v for _, v in sorted(cfg.items())]
    return {
        'enabled':     bool(vals),
        'sensitivity': vals[0] if vals else 0,
        'id':          _u32(raw, 0),
        'result':      _i32(raw, 4),
        'fields':      {hex(k): v for k, v in sorted(cfg.items())},
        'raw_len':     len(raw),
        'raw_hex':     raw[:64].hex(),
    }


# Sensitivity scale shared by cry + cough alerts. The int→label mapping is INVERTED:
#   1 → High
#   2 → Medium   (the firmware default)
#   3 → Low
SENSITIVITY_LABELS = {1: 'High', 2: 'Medium', 3: 'Low'}

def sensitivity_label(v) -> str:
    """Map a cry/cough sensitivity int (1=High, 2=Medium, 3=Low) to its app label."""
    return SENSITIVITY_LABELS.get(v, f'level {v}' if v is not None else 'unknown')


def parse_cry_detection(raw: bytes) -> dict:
    """GET_CRY_DETECT_RESP (2325) — cry detection enabled + sensitivity.
    Layout:
      id@0, result@4, audio_filter_enable@8, audio_filter_bypass_energy_level@12,
      cry_criteria_hit_percentage@16 (double,8), cry_criteria_dnn_confidence@24
      (double,8), cry_alert@32, cry_alert_sensitivity@36, model_version@40.

    `cry_alert`@32 is a BITMASK:
      bit0 (&1) = cry-alert enabled, bit1 (&2) = cry-AI mode enabled.
    `sensitivity` is cry_alert_sensitivity@36 (=0x24); 1=High, 2=Medium, 3=Low."""
    import struct as _s
    cry_alert = _u32(raw, 32) if len(raw) >= 36 else None
    sens      = _u32(raw, 36) if len(raw) >= 40 else None
    # hit_percentage@16 + dnn_confidence@24 are little-endian doubles (the model's firing
    # thresholds); bypass_energy_level@12 is the audio noise-filter threshold.
    hitpct = _s.unpack_from('<d', raw, 16)[0] if len(raw) >= 24 else None
    dnnconf = _s.unpack_from('<d', raw, 24)[0] if len(raw) >= 32 else None
    return {
        'enabled':              (bool(cry_alert & 1) if cry_alert is not None else None),
        'ai_enabled':           (bool(cry_alert & 2) if cry_alert is not None else None),
        'sensitivity':          sens,
        'sensitivity_label':    sensitivity_label(sens) if sens is not None else None,
        'cry_alert_raw':        cry_alert,
        'audio_filter_enable':  bool(_u32(raw, 8)) if len(raw) >= 12 else None,
        'audio_filter_bypass_energy_level': _u32(raw, 12) if len(raw) >= 16 else None,
        'hit_percentage':       hitpct,
        'dnn_confidence':       dnnconf,
        'model_version':        _u32(raw, 40) if len(raw) >= 44 else None,
        'id':                   _u32(raw, 0),
        'result':               _i32(raw, 4),
        'raw_len':              len(raw),
        'raw_hex':              raw[:48].hex(),
    }


def parse_cough_detection(raw: bytes) -> dict:
    """GET_COUGH_SETTING_RESP (2453) — cough detection enabled + mode + sensitivity.
    Layout:
      id@0, result@4, coughAlert@8, coughAlertSensitivity@12.

    `coughAlert`@8 is a BITMASK: bit0 (&1)=enabled, bit1 (&2)=AI mode =
    "Only when baby is in crib" (cleared = "Always Alert"). A `coughAlertSensitivity`@12
    field is declared but on this firmware that slot is overwritten by the camera's
    0x1a22 marker tail (the response is only id/result/coughAlert + marker), so
    sensitivity is reported only when @12 reads as a sane 1-3 (else None)."""
    mask = _u32(raw, 8) if len(raw) >= 12 else None
    sens = _u32(raw, 12) if len(raw) >= 16 else None
    if sens is None or not (1 <= sens <= 3):   # marker tail / not present on this fw
        sens = None
    in_crib = bool(mask & 2) if mask is not None else None
    return {
        'enabled':           (bool(mask & 1) if mask is not None else None),
        'in_crib_only':      in_crib,
        'mode':              ('in_crib' if in_crib else 'always') if mask is not None else None,
        'mode_desc':         ('Only when baby is in crib' if in_crib else 'Always Alert') if mask is not None else None,
        'sensitivity':       sens,
        'sensitivity_label': sensitivity_label(sens) if sens is not None else None,
        'cough_alert_raw':   mask,
        'id':                _u32(raw, 0),
        'result':            _i32(raw, 4),
        'raw_len':           len(raw),
        'raw_hex':           raw[:16].hex(),
    }


def _extract_versions(raw: bytes) -> list:
    """Pull ASCII version-like tokens (digits/dots) out of a response buffer."""
    out, cur = [], bytearray()
    for b in raw:
        if 0x20 <= b < 0x7f and (chr(b).isdigit() or chr(b) in '.-_'):
            cur.append(b)
        else:
            if len(cur) >= 5 and b'.' in cur:
                out.append(cur.decode('ascii'))
            cur = bytearray()
    if len(cur) >= 5 and b'.' in cur:
        out.append(cur.decode('ascii'))
    return out


def parse_firmware_update(raw: bytes) -> dict:
    """GET_UPDATE_INFO_RESP (2401) — current/latest firmware + update availability."""
    versions = _extract_versions(raw)
    return {
        'id':               _u32(raw, 0),
        'result':           _i32(raw, 4),
        'update_available': bool(_u32(raw, 8)) if len(raw) >= 12 else None,
        'current_version':  versions[0] if len(versions) >= 1 else None,
        'latest_version':   versions[1] if len(versions) >= 2 else None,
        'versions':         versions,
        'raw_len':          len(raw),
        'raw_hex':          raw[:96].hex(),
    }


def parse_connected_users(raw: bytes) -> dict:
    """GET_CONNECTED_USER_RESP (2459) — connected client list.
    The leading word@8 is NOT a reliable count on this firmware (reads 0 even with one
    client connected). The real entries live further in as null-terminated ASCII: the
    account id/email plus a per-session UUID. We extract those tokens and report the
    number of distinct account tokens found as 'count'."""
    import re
    text = raw.split(b'\x00')
    accounts, uuids = [], []
    uuid_re = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
                         r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')
    for chunk in text:
        try:
            s = chunk.decode('ascii')
        except Exception:
            continue
        if uuid_re.match(s):
            uuids.append(s)
        elif '@' in s and '.' in s and len(s) >= 5:
            accounts.append(s)
    return {
        'count':     len(accounts) or (1 if uuids else 0),
        'accounts':  accounts,
        'session_uuids': uuids,
        'id':        _u32(raw, 0),
        'result':    _i32(raw, 4),
        'count_word8': _i32(raw, 8),   # raw leading word (unreliable, kept for reference)
        'raw_len':   len(raw),
    }


def parse_lullaby(raw: bytes) -> dict:
    """GET_LULLABY_VOL_DURATION_RESP (2437) — current song + play state.
    uuid @8 / playing @72 confirmed (LullabyVolDuration). volume/duration offsets
    not yet pinned — expose best-effort candidates + raw."""
    lv = LullabyVolDuration.parse(raw)
    # NOTE: this VOL_DURATION response carries the current song UUID (@8) and the
    # play flag (@72) but the volume slot reads 0 on this firmware — the live volume
    # is reported by GET_LULLABY_SCHEDULE (2441) @12 instead (see parse_lullaby_schedule).
    return {
        'current_sound': get_song_name(lv.current_song_uuid) if lv.current_song_uuid else None,
        'uuid':          lv.current_song_uuid,
        'is_playing':    lv.is_playing,
        'volume':        None,        # not in this response — use get_lullaby_schedule
        'raw_len':       len(raw),
        'raw_hex':       raw[:96].hex(),
    }


# ---------------------------------------------------------------------------
# Additional GET builders / parsers. Each responds using an 8-byte zero request
# payload (the canonical request size — a 4-byte payload yields false negatives).
# All are GET requests by construction; no SET code is sent. Where the C struct is
# not fully known the parser extracts the well-known leading fields and ALSO returns
# raw_hex so the layout stays inspectable, matching the convention of the parsers above.
# ---------------------------------------------------------------------------

def _f32(raw: bytes, off: int):
    return struct.unpack_from('<f', raw, off)[0] if len(raw) >= off + 4 else None


def build_get_status_light() -> tuple[int, bytes]:
    """GET_STATUS_LIGHT_ON_OFF_REQ (4362) — camera-body LED indicator state."""
    return IOTYPE_USER_GET_STATUS_LIGHT_ON_OFF_REQ, struct.pack('<i', 0) + b'\x00' * 4

def build_get_hw_policy() -> tuple[int, bytes]:
    """GET_HW_POLICY_REQ (4378) — device capability / policy flags."""
    return IOTYPE_USER_GET_HW_POLICY_REQ, struct.pack('<i', 0) + b'\x00' * 4

def build_get_smart_temp_config() -> tuple[int, bytes]:
    """GET_SMART_TEMP_CONFIG_REQ (4880) — wearable-thermometer alert thresholds."""
    return IOTYPE_USER_GET_SMART_TEMP_CONFIG_REQ, struct.pack('<i', 0) + b'\x00' * 4

def build_get_lullaby_schedule() -> tuple[int, bytes]:
    """GET_LULLABY_SCHEDULE_REQ (2440) — current lullaby timer/volume echo."""
    return IOTYPE_USER_GET_LULLABY_SCHEDULE_REQ, b'\x00' * 8

def build_get_light_way_config() -> tuple[int, bytes]:
    """GET_LIGHT_WAY_CONFIG_REQ (2406) — sunrise/sunset ambient-light config."""
    return IOTYPE_USER_GET_LIGHT_WAY_CONFIG_REQ, struct.pack('<i', 0) + b'\x00' * 4

def build_get_detection_zone_v2() -> tuple[int, bytes]:
    """GET_DETECTION_ZONE_V2_REQ (2380) — motion detection zone rectangle."""
    return IOTYPE_USER_GET_DETECTION_ZONE_V2_REQ, struct.pack('<i', 0) + b'\x00' * 4


def parse_temp_humidity(raw: bytes) -> dict:
    """GET_TEMP_HUMIDITY_RESP (4373) — dedicated temp/humidity.
    Example: temp@8 reliable (28.0 C, matches hw_control); the humidity slot @12 is
    overwritten by the camera's 0x1a22-style marker tail on this firmware, so it is
    reported only when it reads as a sane 0-100% (else None — use get_hw_control for
    humidity)."""
    temp = _f32(raw, 8)
    humid = _f32(raw, 12)
    # The humidity slot is overwritten by the camera's marker tail (a denormal ~1e-17)
    # on this firmware; a sub-1% reading is non-physical for this sensor -> treat as
    # absent. Use get_hw_control() for a real humidity value.
    if humid is None or not (1.0 <= humid <= 100.0):
        humid = None
    return {
        'temp_c':       round(temp, 1) if temp is not None else None,
        'humidity_pct': round(humid, 1) if humid is not None else None,
        'id':           _u32(raw, 0),
        'result':       _i32(raw, 4),
        'raw_len':      len(raw),
        'raw_hex':      raw[:24].hex(),
    }


def _parse_onoff(raw: bytes) -> dict:
    """Shared parser for the on/off light IOCTLs (night light 4353, status light
    4363). Live layout: id@0, reserved@4, on_off@8, 0x1a22 marker tail. Confirmed:
    night light @8=1 (on) and status light @8=0 (off) both match get_hw_control."""
    on = _u32(raw, 8)
    return {
        'on':      bool(on) if on is not None else None,
        'id':      _u32(raw, 0),
        'raw_len': len(raw),
        'raw_hex': raw[:16].hex(),
    }


def parse_night_light(raw: bytes) -> dict:
    """GET_NIGHT_LIGHT_ON_OFF_RESP (4353)."""
    return _parse_onoff(raw)


def parse_status_light(raw: bytes) -> dict:
    """GET_STATUS_LIGHT_ON_OFF_RESP (4363)."""
    return _parse_onoff(raw)


def parse_hw_policy(raw: bytes) -> dict:
    """GET_HW_POLICY_RESP (4379) — temperature/humidity comfort-alert thresholds.
    Wire order:
      id, result, temp_alert@8, temp_low@12, temp_high@16, humi_alert@20,
      humi_low@24, humi_high@28, dev_pull_alert@32, dev_pull_sens@36, dev_pull_count@40.
    (The firmware sends temp thresholds as plain integer °C even though the app field
    is declared float.) Live [1,19,24,1,40,70,0,1,1] = Temp 19–24°C, Humidity 40–70%."""
    return {
        'temp_alert':       bool(_u32(raw, 8)),
        'temp_low_c':       _u32(raw, 12),
        'temp_high_c':      _u32(raw, 16),
        'humi_alert':       bool(_u32(raw, 20)),
        'humi_low_pct':     _u32(raw, 24),
        'humi_high_pct':    _u32(raw, 28),
        'dev_pull_alert':   bool(_u32(raw, 32)),
        'dev_pull_sens':    _u32(raw, 36),
        'dev_pull_count':   _u32(raw, 40),
        'id':               _u32(raw, 0),
        'result':           _i32(raw, 4),
        'raw_len':          len(raw),
        'raw_hex':          raw[:48].hex(),
    }


def parse_sleep_safety_setting(raw: bytes) -> dict:
    """GET_SLEEP_SAFETY_SETTING_RESP (2331) — safe-sleep alert toggles.
    Wire order:
      id, result, safety_alert@8, cover_alert@12, safety_detection_sensitivity@16,
      baby_presence_alert@20.

    `safety_alert` and `cover_alert` are a MUTUALLY-EXCLUSIVE radio, NOT independent
    toggles:
      safety_alert=1, cover_alert=0 → "Covered Face and Rollover Alerts" (full)
      safety_alert=0, cover_alert=1 → "Covered Face Alerts Only"
      both 0                        → sleep-safety detection OFF
    `baby_presence_alert` is an independent baby-left/entered-crib alert."""
    safety = bool(_u32(raw, 8))  if len(raw) >= 12 else None
    cover  = bool(_u32(raw, 12)) if len(raw) >= 16 else None
    if safety and not cover:
        mode, mode_desc = 'face_and_rollover', 'Covered Face and Rollover Alerts'
    elif cover and not safety:
        mode, mode_desc = 'cover_only', 'Covered Face Alerts Only'
    elif safety is False and cover is False:
        mode, mode_desc = 'off', 'Off'
    else:
        mode, mode_desc = 'both', 'Covered Face + Rollover (both flags set)'
    return {
        'enabled':             bool(safety or cover),
        'safety_alert':        safety,
        'cover_alert':         cover,
        'mode':                mode,
        'mode_desc':           mode_desc,
        'sensitivity':         _u32(raw, 16)       if len(raw) >= 20 else None,
        'baby_presence_alert': bool(_u32(raw, 20)) if len(raw) >= 24 else None,
        'id':                  _u32(raw, 0),
        'result':              _i32(raw, 4),
        'raw_len':             len(raw),
        'raw_hex':             raw[:24].hex(),
    }


def parse_auto_capture(raw: bytes) -> dict:
    """GET_AUTO_CAPTURE_RESP (2369) — auto event-capture mode bitmask @8.
    bit0 = motion-triggered, bit1 = scheduled. Live mode=3 → motion + schedule."""
    val = _u32(raw, 8) or 0
    sources = []
    if val & 0x1: sources.append('motion')
    if val & 0x2: sources.append('schedule')
    return {
        'mode':    val,
        'enabled': bool(val),
        'motion':  bool(val & 0x1),
        'schedule':bool(val & 0x2),
        'desc':    ' + '.join(sources) if sources else 'disabled',
        'id':      _u32(raw, 0),
        'raw_len': len(raw),
        'raw_hex': raw[:12].hex(),
    }


def parse_smart_temp_config(raw: bytes) -> dict:
    """GET_SMART_TEMP_CONFIG_RESP (4881) — wearable thermometer alert thresholds.
    Example: enabled@8=1, high_temp f32@0x0c=37.2 C, low_temp f32@0x14=34.7 C."""
    return {
        'enabled':       bool(_u32(raw, 8)) if len(raw) >= 12 else None,
        'high_temp_c':   round(_f32(raw, 12), 1) if len(raw) >= 16 else None,
        'low_temp_c':    round(_f32(raw, 20), 1) if len(raw) >= 24 else None,
        'id':            _u32(raw, 0),
        'raw_len':       len(raw),
        'raw_hex':       raw[:32].hex(),
    }


def parse_lullaby_schedule(raw: bytes) -> dict:
    """GET_LULLABY_SCHEDULE_RESP (2441) — authoritative lullaby timer + volume echo.
    timer_mode@8, volume@12 (live volume@12=42). The @16 word is NOT a reliable play
    flag (reads 0 while the sound is actually playing — use get_lullaby's @72 for play
    state), so it is intentionally not surfaced here."""
    timer_mode = _u32(raw, 8) or 0
    timer_name = {LULLABY_TIMER_REPEAT: 'repeat',
                  LULLABY_TIMER_30MIN:  '30 min',
                  LULLABY_TIMER_60MIN:  '60 min'}.get(timer_mode, f'0x{timer_mode:04x}')
    return {
        'timer_mode': timer_mode,
        'timer':      timer_name,
        'volume':     _u32(raw, 12),
        'id':         _u32(raw, 0),
        'raw_len':    len(raw),
        'raw_hex':    raw[:32].hex(),
    }


def parse_light_way_config(raw: bytes) -> dict:
    """GET_LIGHT_WAY_CONFIG_RESP (2407) — sunrise/sunset ambient-light config.
    Struct not fully decoded; live carries leading config words (@8=5, @0x0c=1) and the
    device SSID string. Expose leading words + any ASCII tokens + raw."""
    asc = ''.join(chr(b) if 32 <= b < 127 else ' ' for b in raw[:128])
    tokens = [t for t in asc.split() if len(t) >= 4]
    return {
        'word8':   _u32(raw, 8),
        'word12':  _u32(raw, 12),
        'strings': tokens[:6],
        'id':      _u32(raw, 0),
        'raw_len': len(raw),
        'raw_hex': raw[:48].hex(),
    }


def parse_detection_zone_v2(raw: bytes) -> dict:
    """GET_DETECTION_ZONE_V2_RESP (2381) — normalized detection bounding box.
    Wire order:
      id, result, x_max@8, y_max@12, x_min@16, y_min@20, measurement@24.
    It is a (x_min,y_min)-(x_max,y_max) box in [0,1], NOT x/y/w/h. Example:
    x∈[0.22,0.70], y∈[0.37,0.89]. A degenerate/zero box = whole frame (not set)."""
    if len(raw) < 24:
        return {'configured': False, 'id': _u32(raw, 0), 'raw_len': len(raw)}
    x_max, y_max, x_min, y_min = (_f32(raw, 8), _f32(raw, 12),
                                  _f32(raw, 16), _f32(raw, 20))
    configured = (x_max - x_min) > 1e-4 and (y_max - y_min) > 1e-4
    return {
        'configured':  configured,
        'x_min':       round(x_min, 4),
        'x_max':       round(x_max, 4),
        'y_min':       round(y_min, 4),
        'y_max':       round(y_max, 4),
        'measurement': _u32(raw, 24),
        'id':          _u32(raw, 0),
        'result':      _i32(raw, 4),
        'raw_len':     len(raw),
        'raw_hex':     raw[:40].hex(),
    }


# ---------------------------------------------------------------------------
# Further batch of GET codes — each responds (8-byte zero request, the response
# io_type == req+1 with non-zero payload). The many GET codes that DON'T respond on
# this baby-monitor firmware (mic/speaker volume, wifi-AP list, videomode, streamctrl,
# motiondetect, environment, devinfo, capacity, system, osd, timezone, event/status
# index, lullaby_info, license_mode, BBcall, …) are intentionally NOT wired. Where a
# struct isn't fully known the parser exposes the leading id/result words plus raw_hex,
# matching the convention above.
# ---------------------------------------------------------------------------
IOTYPE_USER_IPCAM_LISTEVENT_REQ                 = 0x0318  # 792
IOTYPE_USER_IPCAM_LISTEVENT_RESP                = 0x0319  # 793
IOTYPE_USER_IPCAM_GET_DANGERZONE_REQ            = 0x0908  # 2312
IOTYPE_USER_IPCAM_GET_DANGERZONE_RESP           = 0x0909  # 2313
IOTYPE_USER_IPCAM_GET_DANGERZONE2_REQ           = 0x1204  # 4612
IOTYPE_USER_IPCAM_GET_DANGERZONE2_RESP          = 0x1205  # 4613
IOTYPE_USER_IPCAM_GET_WIFI_REQ                  = 0x090E  # 2318
IOTYPE_USER_IPCAM_GET_WIFI_RESP                 = 0x090F  # 2319
IOTYPE_USER_IPCAM_GET_DETECTION_ZONE_REQ        = 0x0930  # 2352
IOTYPE_USER_IPCAM_GET_DETECTION_ZONE_RESP       = 0x0931  # 2353
IOTYPE_USER_GET_MEDIAPROFILES_REQ               = 0x0948  # 2376
IOTYPE_USER_GET_MEDIAPROFILES_RESP              = 0x0949  # 2377
IOTYPE_USER_IPCAM_GET_LIGHTWEIGHT_STATUS_REQ    = 0x0964  # 2404
IOTYPE_USER_IPCAM_GET_LIGHTWEIGHT_STATUS_RESP   = 0x0965  # 2405
IOTYPE_USER_IPCAM_GET_LULLABY_SCHEDULES_REQ     = 0x098E  # 2446
IOTYPE_USER_IPCAM_GET_LULLABY_SCHEDULES_RESP    = 0x098F  # 2447
IOTYPE_USER_IPCAM_GET_LULLABY_SCHEDULE_ACTION_REQ  = 0x0992  # 2450
IOTYPE_USER_IPCAM_GET_LULLABY_SCHEDULE_ACTION_RESP = 0x0993  # 2451
IOTYPE_USER_IPCAM_GET_MAT_CONFIG_REQ            = 0x1302  # 4866
IOTYPE_USER_IPCAM_GET_MAT_CONFIG_RESP           = 0x1303  # 4867
IOTYPE_USER_IPCAM_GET_MAT_INFO_REQ              = 0x1304  # 4868
IOTYPE_USER_IPCAM_GET_MAT_INFO_RESP             = 0x1305  # 4869
IOTYPE_USER_IPCAM_GET_SMART_TEMP_INFO_REQ       = 0x130C  # 4876
IOTYPE_USER_IPCAM_GET_SMART_TEMP_INFO_RESP      = 0x130D  # 4877
IOTYPE_USER_IPCAM_FEATURE_SUPPORT_REQ           = 0x1316  # 4886
IOTYPE_USER_IPCAM_FEATURE_SUPPORT_RESP          = 0x1317  # 4887


def _get8(code: int) -> tuple[int, bytes]:
    """Canonical 8-byte zero GET request (4 bytes yields false negatives)."""
    return code, struct.pack('<i', 0) + b'\x00' * 4

def build_get_event_list():             return _get8(IOTYPE_USER_IPCAM_LISTEVENT_REQ)
def build_get_wifi():                   return _get8(IOTYPE_USER_IPCAM_GET_WIFI_REQ)
def build_get_danger_zone():            return _get8(IOTYPE_USER_IPCAM_GET_DANGERZONE_REQ)
def build_get_danger_zone2():           return _get8(IOTYPE_USER_IPCAM_GET_DANGERZONE2_REQ)
def build_get_detection_zone():         return _get8(IOTYPE_USER_IPCAM_GET_DETECTION_ZONE_REQ)
def build_get_media_profiles():         return _get8(IOTYPE_USER_GET_MEDIAPROFILES_REQ)
def build_get_lightweight_status():     return _get8(IOTYPE_USER_IPCAM_GET_LIGHTWEIGHT_STATUS_REQ)
def build_get_lullaby_schedules():      return _get8(IOTYPE_USER_IPCAM_GET_LULLABY_SCHEDULES_REQ)
def build_get_lullaby_schedule_action():return _get8(IOTYPE_USER_IPCAM_GET_LULLABY_SCHEDULE_ACTION_REQ)
def build_get_mat_config():             return _get8(IOTYPE_USER_IPCAM_GET_MAT_CONFIG_REQ)
def build_get_mat_info():               return _get8(IOTYPE_USER_IPCAM_GET_MAT_INFO_REQ)
def build_get_smart_temp_info():        return _get8(IOTYPE_USER_IPCAM_GET_SMART_TEMP_INFO_REQ)
def build_get_feature_support():        return _get8(IOTYPE_USER_IPCAM_FEATURE_SUPPORT_REQ)

# ── Undocumented query endpoints — not referenced by the native SDK or app, yet the
#    camera answers them. REQ codes are even, RESP == REQ|1, payload = _get8. ──
IOTYPE_USER_GET_SESSION_STATS_REQ  = 0x0934   # 2356 — live per-session stream stats (JSON)
IOTYPE_USER_GET_SESSION_STATS_RESP = 0x0935   # 2357
IOTYPE_USER_GET_USER_LIST_REQ      = 0x0946   # 2374 — connected-users list (JSON)
IOTYPE_USER_GET_USER_LIST_RESP     = 0x0947   # 2375

def build_get_session_stats():          return _get8(IOTYPE_USER_GET_SESSION_STATS_REQ)
def build_get_user_list():              return _get8(IOTYPE_USER_GET_USER_LIST_REQ)


def _ascii_tokens(raw: bytes, minlen: int = 4, limit: int = 8) -> list:
    """Printable-ASCII tokens (NUL/control-delimited) in a response blob."""
    out, cur = [], bytearray()
    for b in raw:
        if 0x20 <= b < 0x7f:
            cur.append(b)
        else:
            if len(cur) >= minlen:
                out.append(cur.decode('ascii'))
            cur = bytearray()
    if len(cur) >= minlen:
        out.append(cur.decode('ascii'))
    return out[:limit]

def _blob_common(raw: bytes, hexlen: int = 64) -> dict:
    return {'id': _u32(raw, 0), 'result': _i32(raw, 4),
            'raw_len': len(raw), 'raw_hex': raw[:hexlen].hex()}

def parse_event_list(raw: bytes) -> dict:
    """LISTEVENT_RESP (793) — recent event-log header. The word@8 packs the TUTK
    list-event header bytes [channel, endflag, count, reserved]; the count byte
    (offset 10) is the number of event entries that follow."""
    count = raw[10] if len(raw) >= 11 else None
    return {
        'count':   count,
        'endflag': raw[9] if len(raw) >= 10 else None,
        'id':      _u32(raw, 0),
        'result':  _i32(raw, 4),
        'raw_len': len(raw),
        'raw_hex': raw[:24].hex(),
    }

def parse_wifi(raw: bytes) -> dict:
    """GET_WIFI_RESP (2319) — current Wi-Fi association. Carries the SSID (@8), the
    camera's LAN IP, and the camera's MAC as null-terminated ASCII tokens. Example:
    ssid 'MyWiFi', ip '192.0.2.10', mac 'aa:bb:cc:dd:ee:ff'."""
    import re
    toks = _ascii_tokens(raw, minlen=2, limit=20)
    ip = next((t for t in toks if re.fullmatch(r'\d{1,3}(\.\d{1,3}){3}', t)), None)
    mac = next((t for t in toks if re.fullmatch(r'([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', t)), None)
    ssid = _ascii_tokens(raw, minlen=1, limit=1)
    out = {
        'ssid':    ssid[0] if ssid else None,
        'ip':      ip,
        'mac':     mac,
        'id':      _u32(raw, 0),
        'result':  _i32(raw, 4),
        'raw_len': len(raw),
        'raw_hex': raw[:24].hex(),
    }
    # Connected-AP radio metrics — exact offsets from the SMsgAVIoctrlGetYunWifiResp([B) parser
    # (signed LE int32): channelNum@0x94, frequency@0x98 (MHz), quality@0x9c, strength@0xa0 (RSSI;
    # dBm if negative), noise@0xa4. May read 0 if the firmware doesn't populate them; the primary
    # signal metric remains get_hw_control.wifi_strength (0–100 %).
    if len(raw) >= 0xa8:
        out['channel']   = _i32(raw, 0x94)
        out['frequency'] = _i32(raw, 0x98)
        out['quality']   = _i32(raw, 0x9c)
        out['strength']  = _i32(raw, 0xa0)
        out['noise']     = _i32(raw, 0xa4)
    return out

def parse_danger_zone(raw: bytes) -> dict:
    """GET_DANGERZONE_RESP (2313) / GET_DANGERZONE2_RESP (4613) — danger-zone config.

    Wire layout: id@0, dzone_config@4, result@(end-8), reserved@(end-4).
    dzone_config = 2 × roi (v1 roi=500 B, v2 roi=200 B). Each roi starts with
    enable@0 (int) then type@4 then name@8 (64 B ASCII). So in the full buffer
    roi[0].enable is @4 and roi[0].name is @12 for BOTH v1 and v2. A configured zone
    has enable!=0 and a real ASCII name; an unset zone is all-zero or carries the
    placeholder tag '##dzone_name_default_tag##'."""
    enable = _u32(raw, 4)
    # roi[0].name is a 64-byte NUL-terminated ASCII field at @12.
    name = None
    if len(raw) >= 12 + 64:
        nm = raw[12:12 + 64].split(b'\x00', 1)[0]
        try:
            s = nm.decode('ascii')
        except Exception:
            s = ''
        if s and 'dzone_name_default' not in s and '##' not in s and s.isprintable():
            name = s
    # result lives at the END (id@0 + config + result@(len-8) + reserved@(len-4)),
    # NOT @8 (@8 is roi[0].type). Report only if it reads as a sane small int.
    res = _i32(raw, len(raw) - 8) if len(raw) >= 12 else None
    if res is not None and not (-16 <= res <= 16):
        res = None
    return {
        'configured': bool(enable) or bool(name),
        'enabled':    bool(enable) if enable is not None else None,
        'name':       name,
        'id':         _u32(raw, 0),
        'result':     res,
        'raw_len':    len(raw),
        'raw_hex':    raw[:48].hex(),
    }


# Danger-zone SET IOTYPE codes: GET 2312/2313, SET 2314/2315;
# v2 GET 4612/4613, SET 4614/4615.
IOTYPE_USER_IPCAM_SET_DANGERZONE_REQ   = 2314
IOTYPE_USER_IPCAM_SET_DANGERZONE_RESP  = 2315
IOTYPE_USER_IPCAM_SET_DANGERZONE2_REQ  = 4614
IOTYPE_USER_IPCAM_SET_DANGERZONE2_RESP = 4615

# roi byte stride and field offsets inside the full GET/SET buffer (v1).
_DZONE_V1_ROI_STRIDE = 500     # p2p_dzone_roi_t.getSize() = points(32) + 468
_DZONE_V2_ROI_STRIDE = 200     # p2p_dzone2_roi_t add-int #200
_DZONE_ROI_BASE      = 4       # config starts after id@0


def build_set_danger_zone(get_resp_bytes: bytes, *, enable=None, name=None,
                          points=None, roi_index: int = 0, version: int = 1,
                          correlation_id: int = 0) -> tuple[int, bytes]:
    """SET_DANGERZONE_REQ (2314, v1) / SET_DANGERZONE2_REQ (4614, v2) — byte-faithful
    read-modify-write of the danger-zone config.

    The GET response and the SET request share the SAME wire layout:
        id@0, dzone_config@4 (2 × roi), result@(end-8), reserved@(end-4).
    The enable/disable switch is exactly this RMW: GET the zone, flip
    roi[`roi_index`].enable, and send the whole buffer back. We echo `get_resp_bytes`
    unchanged and modify only what you pass:
        enable : 0/1 → roi.enable (int @ roi_base+0)
        name   : ASCII, written into roi.name (64 B @ roi_base+8, NUL-padded)
        points : iterable of 8 ints [x1,y1,x2,y2,x3,y3,x4,y4] → roi.points
                 (32 B @ roi_base+468 for v1). For a rectangle pass the 4 corners.

    NOTE: a brand-new zone also needs the rasterised `region` grid bitmap
    (roi @ +72, 18×22 cells for v1) which the app computes from the polygon; that
    rasterisation is NOT reproduced here. So this builder is reliable for
    enable/disable/rename and for echoing an existing zone's geometry; drawing a
    fresh polygon from scratch would also need the region grid. A same-value echo
    (no kwargs) is a safe no-op the camera round-trips (result==0)."""
    if not get_resp_bytes:
        raise ValueError("build_set_danger_zone needs the raw GET_DANGERZONE response")
    stride = _DZONE_V1_ROI_STRIDE if version == 1 else _DZONE_V2_ROI_STRIDE
    code = (IOTYPE_USER_IPCAM_SET_DANGERZONE_REQ if version == 1
            else IOTYPE_USER_IPCAM_SET_DANGERZONE2_REQ)
    payload = bytearray(get_resp_bytes)            # echo the whole buffer verbatim
    struct.pack_into('<i', payload, 0, correlation_id)       # id
    if len(payload) >= 12:                                   # zero the result word
        struct.pack_into('<i', payload, len(payload) - 8, 0)
    base = _DZONE_ROI_BASE + roi_index * stride
    if enable is not None and base + 4 <= len(payload):
        struct.pack_into('<i', payload, base, 1 if enable else 0)
    if name is not None and base + 8 + 64 <= len(payload):
        nm = name.encode('ascii', 'replace')[:63]
        payload[base + 8: base + 8 + 64] = nm + b'\x00' * (64 - len(nm))
    if points is not None and version == 1:
        pts = list(points)
        if len(pts) != 8:
            raise ValueError("points must be 8 ints [x1,y1,x2,y2,x3,y3,x4,y4]")
        poff = base + 468
        if poff + 32 <= len(payload):
            struct.pack_into('<8i', payload, poff, *[int(p) for p in pts])
    return code, bytes(payload)

def parse_detection_zone(raw: bytes) -> dict:
    """GET_DETECTION_ZONE_RESP (2353) — legacy v1 grid motion-detection zone.
    Example: word@4=0xffffffff, body all-zero = no v1 grid set (whole frame). The active
    zone is reported by the v2 normalized box (parse_detection_zone_v2)."""
    body = [_u32(raw, o) for o in range(8, min(len(raw), 28), 4)]
    return {
        'configured': any(body),
        'id':         _u32(raw, 0),
        'result':     _i32(raw, 4),
        'raw_len':    len(raw),
        'raw_hex':    raw[:28].hex(),
    }

# TUTK AV media codec ids (AVFRAMEINFO.codec_id)
_VIDEO_CODECS = {0x4E: 'H.264', 0x50: 'HEVC (H.265)', 0x4F: 'MJPEG'}
# CuboAI Gen3 device video profiles:
#   hd = fps 30, 1_200_000 bps, codec 0x50, 2560x1440 ; sd = 15, 800_000, ...
_GEN3_BITRATE_KBPS = {(2560, 1440, 30): 1200, (2560, 1440, 15): 800}

def parse_media_profiles(raw: bytes) -> dict:
    """GET_MEDIAPROFILES_RESP (2377) — encoder/stream profile (1000 B).
    Layout:
      codec_id@12 (0x50 = HEVC — NOT a bitrate), width@16, height@20, fps@24,
      gop@28, profile_count@36.  The per-profile bitrate is not in this header; the
      camera's HD profile target is 1.2 Mbps (Gen3 device profile)."""
    codec_id = _u32(raw, 12)
    width    = _u32(raw, 16)
    height   = _u32(raw, 20)
    fps      = _u32(raw, 24)
    gop      = _u32(raw, 28)
    return {
        'codec_id':     codec_id,
        'codec':        _VIDEO_CODECS.get(codec_id, f'0x{codec_id:02x}'),
        'width':        width,
        'height':       height,
        'fps':          fps,
        'gop':          gop,
        'profiles':     _u32(raw, 36),
        'bitrate_kbps': _GEN3_BITRATE_KBPS.get((width, height, fps)),
        'id':           _u32(raw, 0),
        'raw_len':      len(raw),
        'raw_hex':      raw[:40].hex(),
    }

def parse_lightweight_status(raw: bytes) -> dict:
    """GET_LIGHTWEIGHT_STATUS_RESP (2405) — compact combined status echo. Carries the
    current lullaby UUID (@12), temperature (f32 @88), humidity (f32 @92) and the
    firmware string. Example: Brown Noise, 26.0°C, 45.0%, fw 3.0.1369."""
    uuid = raw[12:48].split(b'\x00', 1)[0].decode('ascii', 'replace') if len(raw) >= 48 else ''
    temp  = _f32(raw, 88)
    humid = _f32(raw, 92)
    fw = next((t for t in _ascii_tokens(raw, minlen=5) if t.count('.') >= 2 and t[0].isdigit()), None)
    return {
        'count':        _u32(raw, 8),
        'lullaby_uuid': uuid or None,
        'lullaby_name': get_song_name(uuid) if uuid else None,
        'temp_c':       round(temp, 1)  if temp  and 0 < temp  < 60  else None,
        'humidity_pct': round(humid, 1) if humid and 0 < humid < 100 else None,
        'firmware':     fw,
        'id':           _u32(raw, 0),
        'raw_len':      len(raw),
        'raw_hex':      raw[:48].hex(),
    }

def parse_lullaby_schedules(raw: bytes) -> dict:
    """GET_LULLABY_SCHEDULES_RESP (2447) — lullaby timer schedule table (1008 B).
    Per-entry struct not fully decoded -> expose header word + raw."""
    d = _blob_common(raw)
    scheds = []
    base = 8
    while base + 100 <= len(raw):
        en = _u32(raw, base)
        if en:
            name = raw[base + 4:base + 44].split(b'\x00', 1)[0].decode('ascii', 'replace')
            uuid = raw[base + 44:base + 88].split(b'\x00', 1)[0].decode('ascii', 'replace')
            scheds.append({
                'enable':       en,
                'name':         name,
                'uuid':         uuid,
                'sound':        (LULLABY_CATALOG.get(uuid) or (None, uuid, None))[1],
                'days_mask':    raw[base + 88],
                'start_hour':   raw[base + 89],
                'start_minute': raw[base + 90],
                'ai_autoplay':  bool(raw[base + 91]),
                'duration_sec': _u32(raw, base + 92),
                'created':      _u32(raw, base + 96),
            })
        base += 100
    d['schedules'] = scheds
    return d

def parse_lullaby_schedule_action(raw: bytes) -> dict:
    """GET_LULLABY_SCHEDULE_ACTION_RESP (2451) — active + upcoming scheduled lullaby
    (two schedule blocks, each {enable, name, ...}). The body is all-zero when no
    lullaby schedule is configured (enable=0)."""
    body = _trim_marker(raw)[8:]
    return {
        'has_schedule': any(body),
        'id':           _u32(raw, 0),
        'result':       _i32(raw, 4),
        'raw_len':      len(raw),
        'raw_hex':      raw[:48].hex(),
    }

def parse_mat_config(raw: bytes) -> dict:
    """GET_MAT_CONFIG_RESP (4867) — breathing-mat configuration. A paired mat carries
    a non-empty mat_address; here it is empty, so the mat is not paired. ai_mode and
    sensitivity are config defaults that exist whether or not a mat is connected."""
    addr = raw[12:30].split(b'\x00', 1)[0].decode('ascii', 'replace') if len(raw) >= 30 else ''
    addr = addr if all(32 <= ord(c) < 127 for c in addr) and len(addr) >= 6 else ''
    return {
        'paired':      bool(addr),
        'mat_address': addr or None,
        'id':          _u32(raw, 0),
        'result':      _i32(raw, 4),
        'raw_len':     len(raw),
        'raw_hex':     raw[:40].hex(),
    }

# Breathing-mat MAT_STATE_* + MAT_DETECT_STATE_* enums
_MAT_STATE = {0: 'none', 1: 'looking', 2: 'ready'}
_MAT_DETECT = {0: 'init', 1: 'measuring', 2: 'movement', 3: 'breathing', 4: 'no movement'}

def parse_mat_info(raw: bytes) -> dict:
    """GET_MAT_INFO_RESP (4869) — breathing-mat live state.
    Wire order: id, result, state@8, battery@12, irssi@16, detect_state@20, bpm@24,
    ai_mode@28, baby_state@32, ...
    state 0 (MAT_STATE_NONE) = no mat connected."""
    state = _u32(raw, 8) or 0
    connected = state != 0
    return {
        'connected':    connected,
        'state':        _MAT_STATE.get(state, f'state {state}'),
        'battery':      _u32(raw, 12) if connected else None,
        'bpm':          _u32(raw, 24) if connected else None,
        'detect_state': _MAT_DETECT.get(_u32(raw, 20)) if connected else None,
        'id':           _u32(raw, 0),
        'result':       _i32(raw, 4),
        'raw_len':      len(raw),
        'raw_hex':      raw[:40].hex(),
    }

def parse_smart_temp_info(raw: bytes) -> dict:
    """GET_SMART_TEMP_INFO_RESP (4877) — wearable-thermometer live reading.
    Wire order: id, result, smartTempInfo scan block, status, batteryLevel.
    batteryLevel reads 0xFFFFFFFF (-1) and the scan block is all-zero when no probe
    is paired. A paired probe reports a body temp f32 (~30-43°C) and a 0-100 battery."""
    battery = _u32(raw, 72)
    # scan for any plausible body-temperature float in the scan block
    temp = None
    for off in range(8, min(len(raw), 68), 4):
        f = _f32(raw, off)
        if f is not None and 25.0 <= f <= 45.0:
            temp = round(f, 1); break
    paired = temp is not None or (battery not in (None, 0xFFFFFFFF) and battery <= 100)
    return {
        'paired':   paired,
        'temp_c':   temp,
        'battery':  battery if (battery not in (None, 0xFFFFFFFF)) else None,
        'id':       _u32(raw, 0),
        'result':   _i32(raw, 4),
        'raw_len':  len(raw),
        'raw_hex':  raw[:24].hex(),
    }

def parse_feature_support(raw: bytes) -> dict:
    """FEATURE_SUPPORT_RESP (4887) — per-feature capability map (988 B). Live leading
    words [1,1,2,0,2,...] are per-feature support flags; exact feature ordering not
    not fully decoded -> expose the flag vector + raw for inspection."""
    d = _blob_common(raw)
    d['flags'] = [_u32(raw, o) for o in range(8, min(len(raw), 88), 4)]
    return d


def _extract_json(raw: bytes):
    """Pull the first {...} object out of a response blob, repairing the camera's bare
    (unquoted) dotted-quad IP values so json.loads accepts it. Returns dict or None."""
    import re, json
    m = re.search(rb'\{.*\}', raw, re.S)
    if not m:
        return None
    txt = m.group(0).decode('latin1', 'replace')
    txt = re.sub(r':\s*(\d{1,3}(?:\.\d{1,3}){3})\s*([,}\]])', r': "\1"\2', txt)  # quote bare IPs
    try:
        return json.loads(txt)
    except Exception:
        return None

def parse_session_stats(raw: bytes) -> dict:
    """GET_SESSION_STATS_RESP (0x0935) — UNDOCUMENTED. The camera's own per-session
    telemetry as embedded JSON: connection mode (lan/relay), NAT, client IP, and per-
    stream video/audio counters (frm_count, key_frm_count, resendBufferUsage,
    send_err_count, v_err code ring). Example: the audio frm_count advances ~15.7/s during a
    stream — a camera-side health channel to cross-check our own loss/recovery accounting
    (resendBufferUsage is the camera's resend-FIFO pressure). NOTE: video frm_count read 0
    in early probes (likely a session_id indexing quirk) — verify when extending."""
    j = _extract_json(raw)
    d = {'id': _u32(raw, 0), 'result': _i32(raw, 4), 'raw_len': len(raw)}
    if not j:
        d['raw_hex'] = raw[:32].hex()
        return d
    d['mode'], d['nat'], d['ip'] = j.get('mode'), j.get('nat'), j.get('ip')
    d['session_id'] = j.get('session_id')
    vf = (j.get('v_frame') or [{}])[0]
    af = (j.get('a_frame') or [{}])[0]
    d['video'] = {k: vf.get(k) for k in
                  ('frm_count', 'key_frm_count', 'resendBufferUsage', 'send_err_count')}
    d['video']['errors'] = [e for e in vf.get('v_err', []) if e.get('code')]
    d['audio'] = {k: af.get(k) for k in ('frm_count', 'send_err_count')}
    d['json'] = j
    return d

def parse_user_list(raw: bytes) -> dict:
    """GET_USER_LIST_RESP (0x0947) — UNDOCUMENTED. Connected client accounts as embedded
    JSON {"users":[...]}. More reliable than the documented GET_CONNECTED_USER (0x099a),
    which returns count 0 on this firmware."""
    j = _extract_json(raw)
    users = (j or {}).get('users', []) if j else []
    return {'users': users, 'count': len(set(users)), 'id': _u32(raw, 0),
            'result': _i32(raw, 4), 'raw_len': len(raw)}


# Maps each GET method to (builder, expected_resp_type, parser).
GET_METHODS = {
    # --- original 9 ---
    'get_hw_control':        (build_get_hw_control,        IOTYPE_USER_GET_HW_CONTROL_RESP,        parse_hw_control),
    'get_light_style':       (build_get_light_style,       IOTYPE_USER_GET_LIGHT_STYLE_RESP,       parse_light_style),
    'get_sleep_safety':      (build_get_sleep_safety_status, IOTYPE_USER_GET_SLEEP_SAFETY_STATUS_RESP, parse_sleep_safety),
    'get_sleep_mode':        (build_get_sleep_mode,         IOTYPE_USER_GET_SLEEP_MODE_RESP,        parse_sleep_mode),
    'get_lullaby':           (build_get_lullaby_vol_duration, IOTYPE_USER_GET_LULLABY_VOL_DURATION_RESP, parse_lullaby),
    'get_cry_detection':     (build_get_cry_detect,         IOTYPE_USER_GET_CRY_DETECT_RESP,        parse_cry_detection),
    'get_cough_detection':   (build_get_cough_setting,      IOTYPE_USER_GET_COUGH_SETTING_RESP,     parse_cough_detection),
    'check_firmware_update': (build_check_firmware_update,  IOTYPE_USER_GET_UPDATE_INFO_RESP,       parse_firmware_update),
    'get_connected_users':   (build_get_connected_users,    IOTYPE_USER_GET_CONNECTED_USER_RESP,    parse_connected_users),
    # --- additional GET controls ---
    'get_temp_humidity':       (build_get_temp_humidity,      IOTYPE_USER_GET_TEMP_HUMIDITY_RESP,       parse_temp_humidity),
    'get_night_light':         (build_get_night_light,        IOTYPE_USER_GET_NIGHT_LIGHT_ON_OFF_RESP,  parse_night_light),
    'get_status_light':        (build_get_status_light,       IOTYPE_USER_GET_STATUS_LIGHT_ON_OFF_RESP, parse_status_light),
    'get_hw_policy':           (build_get_hw_policy,          IOTYPE_USER_GET_HW_POLICY_RESP,           parse_hw_policy),
    'get_sleep_safety_setting':(build_get_sleep_safety_setting, IOTYPE_USER_GET_SLEEP_SAFETY_SETTING_RESP, parse_sleep_safety_setting),
    'get_auto_capture':        (build_get_auto_capture,       IOTYPE_USER_GET_AUTO_CAPTURE_RESP,        parse_auto_capture),
    'get_smart_temp_config':   (build_get_smart_temp_config,  IOTYPE_USER_GET_SMART_TEMP_CONFIG_RESP,   parse_smart_temp_config),
    'get_lullaby_schedule':    (build_get_lullaby_schedule,   IOTYPE_USER_GET_LULLABY_SCHEDULE_RESP,    parse_lullaby_schedule),
    'get_light_way_config':    (build_get_light_way_config,   IOTYPE_USER_GET_LIGHT_WAY_CONFIG_RESP,    parse_light_way_config),
    'get_detection_zone_v2':   (build_get_detection_zone_v2,  IOTYPE_USER_GET_DETECTION_ZONE_V2_RESP,   parse_detection_zone_v2),
    # --- further GET controls (codes that don't respond on this firmware — timezone,
    #     event/status index, lullaby_info, mic/speaker volume, wifi-list, videomode,
    #     environment, devinfo, … — are deliberately NOT wired). ---
    'get_event_list':            (build_get_event_list,            IOTYPE_USER_IPCAM_LISTEVENT_RESP,                parse_event_list),
    'get_wifi':                  (build_get_wifi,                  IOTYPE_USER_IPCAM_GET_WIFI_RESP,                 parse_wifi),
    'get_danger_zone':           (build_get_danger_zone,           IOTYPE_USER_IPCAM_GET_DANGERZONE_RESP,           parse_danger_zone),
    'get_danger_zone2':          (build_get_danger_zone2,          IOTYPE_USER_IPCAM_GET_DANGERZONE2_RESP,          parse_danger_zone),
    'get_detection_zone':        (build_get_detection_zone,        IOTYPE_USER_IPCAM_GET_DETECTION_ZONE_RESP,       parse_detection_zone),
    'get_media_profiles':        (build_get_media_profiles,        IOTYPE_USER_GET_MEDIAPROFILES_RESP,              parse_media_profiles),
    'get_lightweight_status':    (build_get_lightweight_status,    IOTYPE_USER_IPCAM_GET_LIGHTWEIGHT_STATUS_RESP,   parse_lightweight_status),
    'get_lullaby_schedules':     (build_get_lullaby_schedules,     IOTYPE_USER_IPCAM_GET_LULLABY_SCHEDULES_RESP,    parse_lullaby_schedules),
    'get_lullaby_schedule_action':(build_get_lullaby_schedule_action, IOTYPE_USER_IPCAM_GET_LULLABY_SCHEDULE_ACTION_RESP, parse_lullaby_schedule_action),
    'get_mat_config':            (build_get_mat_config,            IOTYPE_USER_IPCAM_GET_MAT_CONFIG_RESP,           parse_mat_config),
    'get_mat_info':              (build_get_mat_info,              IOTYPE_USER_IPCAM_GET_MAT_INFO_RESP,             parse_mat_info),
    'get_smart_temp_info':       (build_get_smart_temp_info,       IOTYPE_USER_IPCAM_GET_SMART_TEMP_INFO_RESP,      parse_smart_temp_info),
    'get_feature_support':       (build_get_feature_support,       IOTYPE_USER_IPCAM_FEATURE_SUPPORT_RESP,          parse_feature_support),
    # --- undocumented endpoints (not referenced by the native SDK/app, but the
    #     camera answers them). ---
    'get_session_stats':         (build_get_session_stats,         IOTYPE_USER_GET_SESSION_STATS_RESP,              parse_session_stats),
    'get_user_list':             (build_get_user_list,             IOTYPE_USER_GET_USER_LIST_RESP,                  parse_user_list),
}


# ===========================================================================
# SET builders.  Every request wire layout below follows the request field WRITE
# ORDER (the serialiser order, which differs from an alphabetical field listing).
# Where the SET struct differs from the GET response
# struct (it usually does — e.g. HW_CONTROL, HW_POLICY, DETECTION_ZONE_V2 all
# reorder fields and drop the device-only fields), the builders take the RAW
# GET response bytes and do a byte-faithful read-modify-write: every field the
# camera reported is echoed back unchanged and only the requested fields are
# modified.  This is the safe way to flip one setting without disturbing the
# rest, and — for fields whose int/float wire encoding is ambiguous — it
# preserves the camera's own bytes exactly (a same-value SET is then a true
# no-op the camera will round-trip).
#
# Codes intentionally NOT built (hard-safety-forbidden or firmware-dead on this
# baby monitor — see SAFETY rules / [[cuboai-resolution-not-controllable]]):
#   SETWIFI, SETPASSWORD, SET_ACCOUNT_INFO, UPDATE/UPDATE_ACTION,
#   FORMATEXTSTORAGE, SET_TIMEZONE, SET_PUSH, SET_SYSTEM, SET_LICENSE_MODE,
#   SET_MIC/SPEAKER_VOLUME standalone IOCTLs (firmware-dead — use SET_HW_CONTROL
#   mic_level/speaker_level instead), SETMOTIONDETECT/SET_ENVIRONMENT/
#   SET_VIDEOMODE/SET_OSD (firmware-dead GET counterparts), SET_AUTOSNAPSHOT
#   (the SET struct is enable-only but GET_AUTO_CAPTURE reports a mode bitmask —
#   restore semantics are ambiguous, deferred).
# ===========================================================================

# --- extra IOTYPE codes referenced by the new builders ---
IOTYPE_USER_SET_SPEAKER_VOLUME_REQ        = 4360
IOTYPE_USER_SET_SPEAKER_VOLUME_RESP       = 4361
IOTYPE_USER_SET_MIC_VOLUME_REQ            = 4376
IOTYPE_USER_SET_MIC_VOLUME_RESP           = 4377
IOTYPE_USER_SET_HW_POLICY_REQ             = 4380
IOTYPE_USER_SET_HW_POLICY_RESP            = 4381
IOTYPE_USER_IPCAM_SET_DETECTION_ZONEV2_REQ  = 2382
IOTYPE_USER_IPCAM_SET_DETECTION_ZONEV2_RESP = 2383


def _flag(override, current) -> int:
    """0/1 flag: keep `current` (already 0/1) when no override is given."""
    return (1 if current else 0) if override is None else int(bool(override))

def _pick(override, current):
    return current if override is None else override


def build_set_hw_control(get_resp_bytes: bytes, *,
                         night_light_on=None, status_light_on=None,
                         night_vision_mode=None, video_v_flip=None,
                         mic_level=None, speaker_level=None,
                         camera_angle=None, stand_type=None) -> tuple[int, bytes]:
    """SET_HW_CONTROL_REQ (4386) — read-modify-write of the 96-byte HW-control struct.

    `get_resp_bytes` MUST be the raw GET_HW_CONTROL response (the bytes returned by
    ioctl(*build_get_hw_control())). Every current value is echoed back unchanged;
    only the keyword fields you pass are modified.

    SET request wire layout —
    and it DIFFERS from the GET response layout (GET has extra device-only fields):
        @0  id (I)                    @40 temperature (F, echoed raw)
        @4  result (I, =0)            @44 humidity (F, echoed raw)
        @8  mic_level (I)             @48 wifi_quality (I)
        @12 speaker_level (I)         @52 wifi_maxbitrate (I)
        @16 night_vision_control (I)  @56 fw_version[12] (echoed raw)
        @20 video_v_flip_control (I)  @68 ssid[16] (echoed raw)
        @24 status_light_on_off (I)   @84 reserved[12]
        @28 night_light_on_off (I)
        @32 camera_angle (I)
        @36 stand_type (I)

    night_vision_mode: 0=auto, 1=on (IR forced), 2=off.
    """
    hw = HWControl.parse(get_resp_bytes)
    payload = bytearray(96)
    struct.pack_into('<i', payload, 0,  hw.id)
    struct.pack_into('<i', payload, 4,  0)                                   # result
    struct.pack_into('<i', payload, 8,  _pick(mic_level, hw.mic_level))
    struct.pack_into('<i', payload, 12, _pick(speaker_level, hw.speaker_level))
    struct.pack_into('<i', payload, 16, _pick(night_vision_mode, hw.night_vision_control))
    struct.pack_into('<i', payload, 20, _pick(video_v_flip, hw.video_v_flip_control))
    struct.pack_into('<i', payload, 24, _flag(status_light_on, hw.status_light_on_off))
    struct.pack_into('<i', payload, 28, _flag(night_light_on, hw.night_light_on_off))
    struct.pack_into('<i', payload, 32, _pick(camera_angle, hw.camera_angle))
    struct.pack_into('<i', payload, 36, _pick(stand_type, hw.stand_type))
    payload[40:44] = get_resp_bytes[36:40]    # temperature f32 — echo raw (read-only)
    payload[44:48] = get_resp_bytes[40:44]    # humidity f32    — echo raw (read-only)
    struct.pack_into('<i', payload, 48, hw.wifi_quality)
    struct.pack_into('<i', payload, 52, hw.wifi_maxbitrate)
    payload[56:68] = get_resp_bytes[44:56]    # fw_version[12]  — echo raw
    payload[68:84] = get_resp_bytes[56:72]    # ssid[16]        — echo raw
    return IOTYPE_USER_SET_HW_CONTROL_REQ, bytes(payload)


def build_set_status_light(on: bool, correlation_id: int = 0) -> tuple[int, bytes]:
    """SET_STATUS_LIGHT_ON_OFF_REQ (4364) — camera-body LED indicator on/off.
    Wire layout: id(4) + on_off(4) + reserved(4). Mirrors the
    confirmed SET_NIGHT_LIGHT layout (on_off @4, not @8 as in the GET response)."""
    return IOTYPE_USER_SET_STATUS_LIGHT_ON_OFF_REQ, struct.pack('<III',
                                                                correlation_id,
                                                                1 if on else 0, 0)


def build_set_sleep_safety(safety_alert, cover_alert, sensitivity,
                           baby_presence_alert, correlation_id: int = 0) -> tuple[int, bytes]:
    """SET_SLEEP_SAFETY_SETTING_REQ (2332) — safe-sleep alert toggles.
    Wire layout (5 ints, no
    reserved): id, safety_alert, cover_alert, safety_detection_sensitivity,
    baby_presence_alert."""
    return IOTYPE_USER_SET_SLEEP_SAFETY_SETTING_REQ, struct.pack(
        '<iiiii', correlation_id, int(safety_alert), int(cover_alert),
        int(sensitivity), int(baby_presence_alert))


def build_set_detection_zone_v2(get_resp_bytes: bytes, *,
                                x_min=None, x_max=None, y_min=None, y_max=None,
                                measurement=None, correlation_id: int = 0) -> tuple[int, bytes]:
    """SET_DETECTION_ZONEV2_REQ (2382) — normalized motion detection box.
    Read-modify-write from the raw GET_DETECTION_ZONE_V2 response.

    GET resp layout: id@0, result@4, x_max@8, y_max@12, x_min@16, y_min@20, meas@24.
    SET req layout: id@0, x_max@4, y_max@8, x_min@12, y_min@16,
                                   measurement@20, reserved[60].
    Coordinates are floats in [0,1]. Unchanged coords are echoed byte-for-byte."""
    payload = bytearray(84)
    struct.pack_into('<i', payload, 0, correlation_id)
    payload[4:8]   = get_resp_bytes[8:12]    # x_max  (echo raw)
    payload[8:12]  = get_resp_bytes[12:16]   # y_max
    payload[12:16] = get_resp_bytes[16:20]   # x_min
    payload[16:20] = get_resp_bytes[20:24]   # y_min
    payload[20:24] = get_resp_bytes[24:28]   # measurement
    if x_max is not None: struct.pack_into('<f', payload, 4,  float(x_max))
    if y_max is not None: struct.pack_into('<f', payload, 8,  float(y_max))
    if x_min is not None: struct.pack_into('<f', payload, 12, float(x_min))
    if y_min is not None: struct.pack_into('<f', payload, 16, float(y_min))
    if measurement is not None: struct.pack_into('<i', payload, 20, int(measurement))
    return IOTYPE_USER_IPCAM_SET_DETECTION_ZONEV2_REQ, bytes(payload)


def build_set_hw_policy(get_resp_bytes: bytes, *,
                        temp_alert=None, temp_low=None, temp_high=None,
                        humi_alert=None, humi_low=None, humi_high=None,
                        dev_pull_alert=None, dev_pull_sensitivity=None,
                        dev_pull_count=None, version: int = 0,
                        correlation_id: int = 0) -> tuple[int, bytes]:
    """SET_HW_POLICY_REQ (4380) — temperature/humidity comfort-alert thresholds.
    Read-modify-write from the raw GET_HW_POLICY response.

    GET resp layout: id@0, result@4, temp_alert@8, temp_low@12, temp_high@16,
        humi_alert@20, humi_low@24, humi_high@28, dev_pull_alert@32,
        dev_pull_sens@36, dev_pull_count@40.
    SET req layout: id@0, temp_alert@4, temp_low@8, temp_high@12,
        humi_alert@16, humi_low@20, humi_high@24, dev_pull_alert@28,
        dev_pull_sens@32, dev_pull_count@36, version(byte)@40, reserved.

    The protocol carries temp thresholds as float, but the camera's GET response
    carries them as plain integers (live: 19/24 °C). We byte-faithfully echo the
    camera's own 9 config words so a same-value SET is a genuine no-op, and apply
    overrides as integers to match the observed wire encoding."""
    payload = bytearray(48)
    struct.pack_into('<i', payload, 0, correlation_id)
    payload[4:40] = get_resp_bytes[8:44]      # echo temp_alert..dev_pull_count raw
    for off, val in ((4, temp_alert), (8, temp_low), (12, temp_high),
                     (16, humi_alert), (20, humi_low), (24, humi_high),
                     (28, dev_pull_alert), (32, dev_pull_sensitivity),
                     (36, dev_pull_count)):
        if val is not None:
            struct.pack_into('<i', payload, off, int(val))
    payload[40] = int(version) & 0xff
    return IOTYPE_USER_SET_HW_POLICY_REQ, bytes(payload)


def build_set_auto_capture(mode: int, correlation_id: int = 0) -> tuple[int, bytes]:
    """SET_AUTO_CAPTURE_REQ (2366) — auto event-snapshot mode.
    Wire layout (size 8):
        @0 id (I)   @4 enable (I)
    `enable` is the same int the GET response reports @8 — a bitmask, NOT a bare
    bool (bit0=motion-triggered, bit1=scheduled; live=3 → motion+schedule). Pass the
    desired mode (0=off, 1=motion, 2=schedule, 3=both)."""
    return IOTYPE_USER_SET_AUTO_CAPTURE_REQ, struct.pack('<II', correlation_id, int(mode))


def build_set_lullaby_schedule(volume=None, duration=None, get_resp_bytes: bytes = None,
                               correlation_id: int = 0) -> tuple[int, bytes]:
    """Set the lullaby schedule volume / sleep-timer (SET_LULLABY_VOL_DURATION, 2438).

    The lullaby's live volume/timer is reported by GET_LULLABY_SCHEDULE (2441) @12/@8
    (see parse_lullaby_schedule) and is written by SET_LULLABY_VOL_DURATION (2438) —
    the dedicated SetLullabySchedule IOCTL (2442) carries the alarm-clock schedule
    table (action + SMsgLullabySchedule), which is out of scope here. This is a
    read-modify-write over the schedule echo: pass `get_resp_bytes` = the raw
    GET_LULLABY_SCHEDULE response so an omitted field is preserved.

    SetLullabyVolDuration layout (size 140): id@0, duration@4, volume@8,
    reserved[128]@12."""
    cur_timer, cur_vol = 0, 0
    if get_resp_bytes is not None and len(get_resp_bytes) >= 16:
        cur_timer = struct.unpack_from('<I', get_resp_bytes, 8)[0]
        cur_vol   = struct.unpack_from('<I', get_resp_bytes, 12)[0]
    vol = cur_vol if volume is None else int(volume)
    if duration is None:
        timer = cur_timer
    else:
        timer = LULLABY_TIMER_REPEAT if not duration else (int(duration) * 60)
    payload = bytearray(140)
    struct.pack_into('<I', payload, 0, correlation_id)
    struct.pack_into('<I', payload, 4, int(timer))   # duration / timer mode
    struct.pack_into('<I', payload, 8, int(vol))     # volume
    return IOTYPE_USER_SET_LULLABY_VOL_DURATION_REQ, bytes(payload)


def build_set_sleep_safety_setting(get_resp_bytes: bytes, *, safety_alert=None,
                                   cover_alert=None, sensitivity=None,
                                   baby_presence_alert=None,
                                   correlation_id: int = 0) -> tuple[int, bytes]:
    """SET_SLEEP_SAFETY_SETTING_REQ (2332) — read-modify-write convenience.

    `get_resp_bytes` MUST be the raw GET_SLEEP_SAFETY_SETTING response (layout
    id@0,result@4,safety_alert@8,cover_alert@12,sensitivity@16,baby_presence@20).
    Omitted fields are echoed from the current setting; only passed fields change.
    Delegates to build_set_sleep_safety (SET wire: id, safety_alert, cover_alert,
    safety_detection_sensitivity, baby_presence_alert — 5 ints, no reserved)."""
    cur = parse_sleep_safety_setting(get_resp_bytes)
    return build_set_sleep_safety(
        _flag(safety_alert,        cur.get('safety_alert')),
        _flag(cover_alert,         cur.get('cover_alert')),
        _pick(sensitivity,         cur.get('sensitivity') or 0),
        _flag(baby_presence_alert, cur.get('baby_presence_alert')),
        correlation_id=correlation_id)


def parse_set_result(raw: bytes) -> dict:
    """Generic SET response decode — every SMsgAVIoctrlSet*Resp is id@0, result@4
    (+ reserved). result==0 means the camera accepted the change."""
    return {'id': _u32(raw, 0), 'result': _i32(raw, 4),
            'ok': _i32(raw, 4) == 0, 'raw_len': len(raw), 'raw_hex': raw[:16].hex()}
