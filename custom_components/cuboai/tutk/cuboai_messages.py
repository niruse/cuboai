"""
cuboai_messages.py — CuboAI IOCTL message definitions, builders, and parsers.

This module defines the application-layer protocol used to control a CuboAI
baby monitor camera over a ThroughTek (TUTK) P2P connection. It contains:

  - IOCTL type code constants   (request/response pairs)
  - Request payload builders    (build_* functions → (type_code, bytes))
  - Response payload parsers    (dataclass parse() methods)
  - The lullaby song catalog    (34 songs, UUIDs extracted from app)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HOW THE PROTOCOL WORKS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The camera speaks TUTK Kalay P2P. Once a session is established (handled
by cuboai_tutk.py), control messages are sent via avSendIOCtrl() and
received via avRecvIOCtrl().

Each IOCTL has a request type code (even) and response type code (odd):

    Client → camera:  avSendIOCtrl(av_id, REQUEST_TYPE, payload, len)
    Camera → client:  avRecvIOCtrl(av_id, &resp_type, buf, buf_len, timeout_ms)

The response type is always request_type + 1. For example:
    GET_HW_CONTROL_REQ  = 4384  →  send
    GET_HW_CONTROL_RESP = 4385  ←  receive

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HOW IOCTL CODES WERE DISCOVERED
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

All codes were discovered by:
  1. Decompiling the CuboAI Android app (v2.23.2) with JADX
  2. Reading the decompiled Java source (getcubo/ directory)
  3. Hooking avSendIOCtrl() in libAVAPIs.so using Frida to observe live
     traffic while operating the app

Confirmed codes are marked # confirmed.
Codes discovered from source only (not Frida-verified) are marked # untested.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CONNECTION CREDENTIALS (how to obtain them)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The camera requires three pieces of information to connect:

  uid (device UID)
    The TUTK P2P UID for your specific camera. Found in the REST API response
    at GET /prod/user/cameras → field "license_id".
    Format: 20-character alphanumeric string, e.g. "YOUR_DEVICE_UID"

  account (dev_admin_id)
    A device-specific account identifier. Found in the same REST API response
    → field "dev_admin_id".
    Format: "admin@<hex_string>", e.g. "admin@YOUR_DEVICE_HEX"

  password (dev_admin_pwd)
    The device-specific password → field "dev_admin_pwd" in the REST response.

  These credentials are unique per camera and are NOT your CuboAI account
  login. They are provisioned when the camera is first paired.

  To retrieve them: intercept the CuboAI app's HTTPS traffic using a tool
  like mitmproxy, Charles Proxy, or Frida. The app calls:
    GET https://app-api.getcubo.com/prod/user/cameras
  with your account bearer token, and receives a JSON array of camera objects
  each containing uid, dev_admin_id, and dev_admin_pwd.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECURITY NOTE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

security_mode = 0 (NON-SECURE): The camera accepts connections without
DTLS or PSK encryption at the TUTK layer. The P2P session payload IS
still encrypted (the nl/No frame content is encrypted using a key derived
from the ECDH handshake with the relay server), but the camera does not
require additional DTLS on top. This is consistent across all tested
firmware versions (3.0.1369).
"""
from __future__ import annotations
import struct
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# IOCTL type codes (confirmed from decompiled AVIControlMSGType.java + live capture)
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
    Confirmed format from live capture:
      offset 0:  correlation_id (4 bytes LE)
      offset 4:  song_uuid (null-terminated ASCII, 36 bytes)
      offset 40: zeros (28 bytes)
      offset 68: action = 0x00 (play)
      rest:      zeros
    """
    payload = bytearray(200)
    struct.pack_into('<I', payload, 0, correlation_id)
    uuid_bytes = song_uuid.encode('ascii')[:36]
    payload[4:4+len(uuid_bytes)] = uuid_bytes
    payload[68] = 0x00  # play
    return IOTYPE_USER_SET_LULLABY_ACTION_REQ, bytes(payload)

def build_set_lullaby_stop(song_uuid: str, correlation_id: int = 0) -> tuple[int, bytes]:
    """SET_LULLABY_ACTION_REQ — stop playback.
    Confirmed: same as play but offset 68 = 0x01.
    """
    payload = bytearray(200)
    struct.pack_into('<I', payload, 0, correlation_id)
    uuid_bytes = song_uuid.encode('ascii')[:36]
    payload[4:4+len(uuid_bytes)] = uuid_bytes
    payload[68] = 0x01  # stop
    return IOTYPE_USER_SET_LULLABY_ACTION_REQ, bytes(payload)

def build_get_lullaby_info() -> tuple[int, bytes]:
    """GET_LULLABY_INFO_REQ — fetch song list."""
    return IOTYPE_USER_GET_LULLABY_INFO_REQ, b'\x00' * 8


# ---------------------------------------------------------------------------
# Response parsers
# ---------------------------------------------------------------------------

@dataclass
class HWControl:
    """SMsgAVIoctrlGetHWControlResp — 96 bytes.
    Confirmed live: temp=31.0°C, humidity=50.0%, night_light=1, fw=3.0.1369, ssid=YourSSID
    """
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
    """SMsgAVIoctrlGetTempHumidityResp — 20 bytes."""
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
      offset 4:  unknown (4)
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
    buf[56:62] = b'YourSSID'
    struct.pack_into('<iiiiii', buf, 72, 87, 0, 0, 0, 0, 0)
    hw = HWControl.parse(bytes(buf))
    assert abs(hw.temperature - 31.0) < 1e-5
    assert abs(hw.humidity - 50.0) < 1e-5
    assert hw.night_light_on
    assert hw.fw_version == '3.0.1369'
    assert hw.ssid == 'YourSSID'
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
    assert p[68] == 0x01
    tc, p = build_set_lullaby_play('F55001F0-9D5A-4C09-B58C-896964CAE485')
    assert p[68] == 0x00
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
IOTYPE_USER_SET_AUTO_CAPTURE_REQ        = 2370
IOTYPE_USER_SET_AUTO_CAPTURE_RESP       = 2371

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
