"""Tests for camera LAN-IP extraction/validation.

The old regex accepted things like 999.999.999.999 and version strings such
as "2.1.0.5", which then became the camera IP and broke LAN streaming.
_extract_lan_ip validates octets and requires a private-range address for the
loose whole-JSON fallback scan.
"""

from custom_components.cuboai.api.cuboai_functions import _extract_lan_ip


class TestExtractLanIp:
    def test_valid_private_candidate(self):
        assert _extract_lan_ip("192.168.240.85", "") == "192.168.240.85"

    def test_rejects_out_of_range_octets(self):
        assert _extract_lan_ip("999.999.999.999", "") is None

    def test_rejects_version_string_as_candidate(self):
        # a firmware version is not a private IP
        assert _extract_lan_ip("2.1.0.5", "") is None

    def test_fallback_finds_private_ip_in_json(self):
        blob = '{"fw":"2.0.2257","lan_ip":"192.168.1.50","x":"8.8.8.8"}'
        assert _extract_lan_ip(None, blob) == "192.168.1.50"

    def test_fallback_ignores_public_ip(self):
        # 8.8.8.8 is public -> not accepted as the camera's LAN IP
        assert _extract_lan_ip(None, '{"dns":"8.8.8.8"}') is None

    def test_fallback_ignores_version_in_json(self):
        assert _extract_lan_ip(None, '{"fw":"2.1.0.5"}') is None

    def test_rejects_loopback(self):
        assert _extract_lan_ip("127.0.0.1", "") is None

    def test_returns_none_for_empty(self):
        assert _extract_lan_ip(None, "") is None
