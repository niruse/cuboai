import os
from datetime import datetime

# Default to legacy path, but can be updated dynamically
LOG_FILE_PATH = "/config/cuboai_last_alert_debug.log"


def set_log_path(config_path: str):
    """Set portable log path based on Home Assistant config directory."""
    global LOG_FILE_PATH
    LOG_FILE_PATH = os.path.join(config_path, "cuboai_last_alert_debug.log")


def log_to_file(msg):
    try:
        with open(LOG_FILE_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now()} - {msg}\n")
    except Exception:
        # No logger here, just ignore file errors to avoid recursion
        pass
