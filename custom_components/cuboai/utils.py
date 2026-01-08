from datetime import datetime


def log_to_file(msg):
    return
    try:
        with open("/config/cuboai_last_alert_debug.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now()} - {msg}\n")
    except Exception:
        # No logger here, just ignore file errors to avoid recursion
        pass
