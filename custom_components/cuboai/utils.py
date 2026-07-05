import os
import socket
from datetime import datetime

# Default to current directory, but can be updated dynamically
LOG_FILE_PATH = "cuboai_last_alert_debug.log"
DEBUG_LOGS_ENABLED = False


def set_log_path(config_path: str):
    """Set portable log path based on Home Assistant config directory."""
    global LOG_FILE_PATH
    LOG_FILE_PATH = os.path.join(config_path, "cuboai_last_alert_debug.log")


def set_debug_logs_enabled(enabled: bool):
    """Enable or disable debug file logging dynamically."""
    global DEBUG_LOGS_ENABLED
    DEBUG_LOGS_ENABLED = enabled


def log_to_file(msg):
    if not DEBUG_LOGS_ENABLED:
        return
    try:
        if os.path.exists(LOG_FILE_PATH) and os.path.getsize(LOG_FILE_PATH) > 2 * 1024 * 1024:
            backup_path = f"{LOG_FILE_PATH}.1"
            if os.path.exists(backup_path):
                os.remove(backup_path)
            os.rename(LOG_FILE_PATH, backup_path)
            
        with open(LOG_FILE_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now()} - {msg}\n")
    except Exception:
        pass


def find_available_port(start_port=8555, max_port=8600):
    """Find an available port for go2rtc RTSP."""
    for port in range(start_port, max_port):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(('127.0.0.1', port)) != 0:
                return port
    return start_port
