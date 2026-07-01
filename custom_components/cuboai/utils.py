import os
from datetime import datetime

# Default to current directory, but can be updated dynamically
LOG_FILE_PATH = "cuboai_last_alert_debug.log"


def set_log_path(config_path: str):
    """Set portable log path based on Home Assistant config directory."""
    global LOG_FILE_PATH
    LOG_FILE_PATH = os.path.join(config_path, "cuboai_last_alert_debug.log")


# NOTE: REMEMBER TO DISABLE LOGGING (e.g. add `return` here) BEFORE PUSHING TO GIT
# so that users' disks don't get filled up with debug logs!
def log_to_file(msg):
    try:
        with open(LOG_FILE_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now()} - {msg}\n")
    except Exception:
        pass
