import os
import ctypes
from ctypes import CDLL, c_int, c_int32, c_uint, c_uint32, c_uint8, c_char_p, pointer, Structure, c_char, LittleEndianStructure, sizeof, create_string_buffer, byref
import struct
import logging
import time

_LOGGER = logging.getLogger(__name__)

CUBO_TUTK_KEY = "AQAAAAN7yV/npboXLftKdwIj2m0wxZoRsF+MziieFJRQkMaem+gQRw02JPVoDD0DiN4kWShIi8m5sJo05HcQB2vcnNvNWMEOldnidJsNFcTZrgzPgNveK8tnJoQ2U8JqS679vPW32NEC92Rh1uMJCy1G5qaPRBzdKoOWVGMbUhaW7EjIagyVuAfbE9eMbzGI5WM28jfFWFXFWykY3PcBdKq6+dAb"

# Opcodes
IOTYPE_USER_GET_NIGHT_LIGHT_ON_OFF_REQ = 4352
IOTYPE_USER_GET_NIGHT_LIGHT_ON_OFF_RESP = 4353
IOTYPE_USER_SET_NIGHT_LIGHT_ON_OFF_REQ = 4354
IOTYPE_USER_SET_NIGHT_LIGHT_ON_OFF_RESP = 4355

class TutkError(Exception):
    pass

def load_library() -> CDLL:
    paths = [
        # Alpine/HAOS specific library first
        os.path.join(os.path.dirname(__file__), "..", "libIOTCAPIs_ALL_alpine.so"),
        # Check in the component directory for standard glibc library
        os.path.join(os.path.dirname(__file__), "..", "libIOTCAPIs_ALL.so"),
        # Global paths
        "/usr/local/lib/libIOTCAPIs_ALL.so",
        "/usr/lib/libIOTCAPIs_ALL.so",
    ]
    for path in paths:
        if os.path.exists(path):
            _LOGGER.debug(f"Loading TUTK library from: {path}")
            try:
                return ctypes.cdll.LoadLibrary(path)
            except OSError as e:
                _LOGGER.error(f"Failed to load TUTK library {path}. This usually means your system is missing glibc/libc6-compat (common on Alpine/Home Assistant OS). Error: {e}")
                raise TutkError(f"Library load failed: {e}. If on Home Assistant OS/Alpine, please install libc6-compat or gcompat.")
    
    raise TutkError("Could not find libIOTCAPIs_ALL.so. Make sure it is compiled for x86_64/aarch64 and placed in the custom_components/cuboai folder.")

class TutkClient:
    def __init__(self, uid: str, license_id: str, admin_id: str, admin_pwd: str, region: int = 0):
        self.uid = uid
        self.license_id = license_id
        self.admin_id = admin_id.encode("ascii")
        self.admin_pwd = admin_pwd.encode("ascii")
        self.region = region
        self.lib = load_library()
        self.session_id = -1
        self.av_chan_id = -1
        self._initialize()

    def _initialize(self):
        try:
            ret = self.lib.TUTK_SDK_Set_License_Key(c_char_p(CUBO_TUTK_KEY.encode("ascii")))
            _LOGGER.debug(f"TUTK_SDK_Set_License_Key returned {ret}")
            if self.region is not None and hasattr(self.lib, "TUTK_SDK_Set_Region"):
                reg_ret = self.lib.TUTK_SDK_Set_Region(ctypes.c_int(self.region))
                _LOGGER.debug(f"TUTK_SDK_Set_Region({self.region}) returned {reg_ret}")
        except AttributeError:
            _LOGGER.warning("TUTK_SDK_Set_License_Key not found in shared library, ignoring license ID")

        # IOTC_Initialize2(0)
        ret = self.lib.IOTC_Initialize2(0)
        if ret < 0 and ret != -3: # -3 is IOTC_ER_ALREADY_INITIALIZED
            raise TutkError(f"IOTC_Initialize2 failed: {ret}")
        
        # avInitialize(1)
        ret = self.lib.avInitialize(1)
        if ret < 0 and ret != -20031: # -20031 is AV_ER_ALREADY_INITIALIZED
            raise TutkError(f"avInitialize failed: {ret}")

    def connect(self):
        _LOGGER.debug(f"Connecting to UID {self.uid}")
        session_id = self.lib.IOTC_Get_SessionID()
        if session_id < 0:
            raise TutkError(f"Failed to get session ID. Error: {session_id}")
            
        self.session_id = session_id
        
        # Setup timeouts (Cubo uses 20000)
        self.lib.IOTC_Setup_P2PConnection_Timeout(c_int(20000))
        self.lib.IOTC_Setup_LANConnection_Timeout(c_int(20000))
        
        # ACTUALLY pass the dynamic license_id as the UID parameter
        ret = self.lib.IOTC_Connect_ByUID_Parallel(c_char_p(self.license_id.encode("ascii")), c_int(self.session_id))
        if ret < 0:
            self.lib.IOTC_Session_Close(c_int(self.session_id))
            self.session_id = -1
            raise TutkError(f"Failed to connect by UID (Parallel). Error: {ret}")

        # Start AV client
        class St_AVClientStartInConfig(Structure):
            _fields_ = [
                ("cb", c_uint32),
                ("iotc_session_id", c_uint32),
                ("iotc_channel_id", c_uint8),
                ("timeout_sec", c_uint32),
                ("account_or_identity", c_char_p),
                ("password_or_token", c_char_p),
                ("resend", c_int32),
                ("security_mode", c_uint32),
                ("auth_type", c_uint32),
                ("sync_recv_data", c_int32),
            ]
            
        class St_AVClientStartOutConfig(Structure):
            _fields_ = [
                ("cb", c_uint32),
                ("server_type", c_uint32),
                ("resert_type", c_int32),
                ("two_way_streaming", c_int32),
                ("sync_recv_data", c_int32),
                ("security_mode", c_uint32),
            ]

        in_cfg = St_AVClientStartInConfig(
            cb=sizeof(St_AVClientStartInConfig),
            iotc_session_id=self.session_id,
            iotc_channel_id=0,
            timeout_sec=20,
            account_or_identity=self.admin_id,
            password_or_token=self.admin_pwd,
            resend=1,
            security_mode=0,
            auth_type=0,
            sync_recv_data=0
        )
        
        out_cfg = St_AVClientStartOutConfig(cb=sizeof(St_AVClientStartOutConfig))

        av_chan_id = self.lib.avClientStartEx(byref(in_cfg), byref(out_cfg))
        
        if av_chan_id < 0:
            self.lib.IOTC_Session_Close(c_int(self.session_id))
            self.session_id = -1
            raise TutkError(f"Failed to start AV client (EX). Error: {av_chan_id}")
            
        self.av_chan_id = av_chan_id
        _LOGGER.debug(f"Successfully connected AV channel to {self.uid}")

    def send_io_ctrl(self, ctrl_type: int, payload: bytes) -> bytes:
        if self.av_chan_id < 0:
            raise TutkError("AV channel not started")
            
        ret = self.lib.avSendIOCtrl(c_int(self.av_chan_id), c_int(ctrl_type), c_char_p(payload), c_int(len(payload)))
        if ret < 0:
            raise TutkError(f"avSendIOCtrl failed with code: {ret}")
            
        # Wait for response
        resp_type = c_uint(0)
        resp_buf = create_string_buffer(1024)
        
        # Give it a few tries to get the response
        for _ in range(50):
            ret = self.lib.avRecvIOCtrl(c_int(self.av_chan_id), byref(resp_type), resp_buf, c_int(1024), c_int(1000))
            if ret >= 0:
                _LOGGER.debug(f"Received IO ctrl response type: {resp_type.value} (expected {ctrl_type + 1})")
                if resp_type.value == ctrl_type + 1:
                    return resp_buf.raw[:ret]
            elif ret not in (-20011, -20014): # AV_ER_TIMEOUT, AV_ER_LOSED_THIS_FRAME
                raise TutkError(f"avRecvIOCtrl failed with code: {ret}")
            time.sleep(0.1)
            
        raise TutkError(f"Failed to receive IO ctrl response {ctrl_type+1} (Timeout)")

    def get_night_light_status(self) -> bool:
        # SMsgAVIoctrlGetNightLightOnOffReq doesn't seem to have a payload based on decompiled source analysis,
        # but let's check SMsgAVIoctrlGetNightLightOnOffReq just in case it requires an ID.
        # usually Get requests like this are empty or just an ID + reserved.
        # Actually CameraCommandFactory just instantiates and toBytes:
        # public CameraCommand getNightLightGetCommand() {
        #    return new CameraCommand(4352, new SMsgAVIoctrlGetNightLightOnOffReq().toBytes());
        # }
        # Assuming 8 bytes: id + reserved. We'll send 8 empty bytes or a dummy ID.
        payload = struct.pack("<ii", int(time.time()), 0)
        
        try:
            resp = self.send_io_ctrl(IOTYPE_USER_GET_NIGHT_LIGHT_ON_OFF_REQ, payload)
            if len(resp) >= 12:
                # SMsgAVIoctrlGetNightLightOnOffResp: id(4), result(4), on_off(4), reserved(4)
                # First 12 bytes are the id, result, and on_off state
                msg_id, result, on_off = struct.unpack("<iii", resp[:12])
                return on_off == 1
        except Exception as e:
            print(f"Error getting night light: {e}")
        return False
        
    def set_night_light_status(self, state: bool) -> bool:
        # SMsgAVIoctrlSetNightLightOnOffReq: id(4), on_off(4), reserved(4) -> 12 bytes
        on_off_val = 1 if state else 0
        payload = struct.pack("<iii", int(time.time()), on_off_val, 0)
        try:
            self.send_io_ctrl(IOTYPE_USER_SET_NIGHT_LIGHT_ON_OFF_REQ, payload)
            return True
        except Exception as e:
            print(f"Error setting night light: {e}")
        return False
        
    def disconnect(self):
        if self.av_chan_id >= 0:
            self.lib.avClientStop(c_int(self.av_chan_id))
            self.av_chan_id = -1
        if self.session_id >= 0:
            self.lib.IOTC_Session_Close(c_int(self.session_id))
            self.session_id = -1
