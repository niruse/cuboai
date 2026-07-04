"""Regression tests for local streaming and environmental readings."""

from custom_components.cuboai.downloader import _elf_machine
from custom_components.cuboai.local_validation import is_valid_humidity, is_valid_temperature


def _write_elf_header(path, machine: int) -> None:
    header = bytearray(20)
    header[:4] = b"\x7fELF"
    header[18:20] = machine.to_bytes(2, "little")
    path.write_bytes(header)


def test_elf_machine_detects_supported_architectures(tmp_path):
    """ELF architecture detection distinguishes amd64 and arm64 binaries."""
    amd64_binary = tmp_path / "go2rtc-amd64"
    arm64_binary = tmp_path / "go2rtc-arm64"
    _write_elf_header(amd64_binary, 62)
    _write_elf_header(arm64_binary, 183)

    assert _elf_machine(str(amd64_binary)) == 62
    assert _elf_machine(str(arm64_binary)) == 183


def test_elf_machine_rejects_non_elf_file(tmp_path):
    """Non-ELF downloads are not accepted as go2rtc binaries."""
    invalid_binary = tmp_path / "go2rtc-invalid"
    invalid_binary.write_text("not an ELF binary")

    assert _elf_machine(str(invalid_binary)) is None


def test_environmental_reading_validation():
    """Firmware marker values are rejected while normal readings are accepted."""
    assert is_valid_temperature(23.5)
    assert is_valid_humidity(56.0)

    assert not is_valid_temperature(float("nan"))
    assert not is_valid_humidity(1.65603530741865e31)
    assert not is_valid_humidity(4.23945038699174e33)
    assert not is_valid_humidity(None)
