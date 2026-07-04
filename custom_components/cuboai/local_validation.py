"""Validation helpers for values received from the local camera protocol."""


def _is_number(value) -> bool:
    """Return whether value is a real numeric sensor value."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_valid_temperature(value) -> bool:
    """Return whether a temperature is physically plausible for the sensor."""
    return _is_number(value) and -40.0 <= value <= 100.0


def is_valid_humidity(value) -> bool:
    """Return whether a relative humidity reading is physically plausible."""
    return _is_number(value) and 1.0 <= value <= 100.0
