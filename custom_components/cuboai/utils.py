import logging
import logging.handlers
import os
import queue
import socket

# Default to current directory, but can be updated dynamically
LOG_FILE_PATH = "cuboai_last_alert_debug.log"
DEBUG_LOGS_ENABLED = False

# Dedicated debug-trace logger. log_to_file() only enqueues a LogRecord (safe
# from the event loop); a QueueListener thread performs the actual file I/O —
# the same pattern Home Assistant core uses for its own log file.
_TRACE_LOGGER = logging.getLogger("custom_components.cuboai.trace")
_TRACE_LOGGER.setLevel(logging.DEBUG)
_TRACE_LOGGER.propagate = False  # keep the high-volume trace out of home-assistant.log
_TRACE_LISTENER = None
_TRACE_QUEUE_HANDLER = None


def set_log_path(config_path: str):
    """Set portable log path based on Home Assistant config directory."""
    global LOG_FILE_PATH
    LOG_FILE_PATH = os.path.join(config_path, "cuboai_last_alert_debug.log")


def set_debug_logs_enabled(enabled: bool):
    """Enable or disable debug file logging dynamically."""
    global DEBUG_LOGS_ENABLED, _TRACE_LISTENER, _TRACE_QUEUE_HANDLER
    DEBUG_LOGS_ENABLED = enabled
    if enabled and _TRACE_LISTENER is None:
        try:
            file_handler = logging.handlers.RotatingFileHandler(
                LOG_FILE_PATH, maxBytes=2 * 1024 * 1024, backupCount=1, delay=True
            )
            file_handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
            log_queue = queue.SimpleQueue()
            _TRACE_QUEUE_HANDLER = logging.handlers.QueueHandler(log_queue)
            _TRACE_LOGGER.addHandler(_TRACE_QUEUE_HANDLER)
            _TRACE_LISTENER = logging.handlers.QueueListener(log_queue, file_handler)
            _TRACE_LISTENER.start()
        except Exception:
            pass
    elif not enabled and _TRACE_LISTENER is not None:
        try:
            _TRACE_LOGGER.removeHandler(_TRACE_QUEUE_HANDLER)
            _TRACE_LISTENER.stop()
        except Exception:
            pass
        _TRACE_LISTENER = None
        _TRACE_QUEUE_HANDLER = None


def log_to_file(msg):
    if not DEBUG_LOGS_ENABLED:
        return
    try:
        _TRACE_LOGGER.debug(msg)
    except Exception:
        pass


def retry_camera_command(description: str, attempts: int = 2, delay: float = 2.0):
    """Decorator for sync camera-command helpers (run in the executor).

    The camera rate-limits rapid session attempts, so a command issued while a
    coordinator poll or another command holds a session can fail transiently —
    retry once before surfacing a clean HomeAssistantError (instead of the
    generic "unknown error" toast a raw exception produces).
    """
    import functools
    import time

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_err = None
            for attempt in range(attempts):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_err = e
                    if attempt < attempts - 1:
                        log_to_file(f"{description} attempt {attempt + 1} failed, retrying: {e}")
                        time.sleep(delay)
            from homeassistant.exceptions import HomeAssistantError

            raise HomeAssistantError(
                f"{description} failed — the camera may be busy, try again in a few seconds ({last_err})"
            ) from last_err

        return wrapper

    return decorator


def find_available_port(start_port=8555, max_port=8600):
    """Find an available port for go2rtc RTSP.

    Binds sockets, so call from an executor when on the event loop.
    """
    # Ignore standard Home Assistant ports
    IGNORE_PORTS = {8123, 4357, 8554, 1984, 8443, 5683, 5353}
    for port in range(start_port, max_port):
        if port in IGNORE_PORTS:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                # Actually try to bind to verify if the port is free
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                pass
    return start_port
