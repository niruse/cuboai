import os
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
        with open(LOG_FILE_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now()} - {msg}\n")
    except Exception:
        pass
