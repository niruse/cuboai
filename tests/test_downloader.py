"""Tests for go2rtc binary architecture validation (issue #80 regression).

The repo used to ship an ARM64 go2rtc binary and the downloader skipped
re-downloading whenever a file existed, so x86_64 hosts kept a binary that
could never start. _binary_matches_arch validates the ELF e_machine so a
wrong-arch binary self-heals.
"""

import os
import struct
import tempfile

from custom_components.cuboai import downloader


def _write_elf(path, e_machine: int):
    # Minimal 20-byte ELF header: magic + padding + e_machine at offset 18
    header = bytearray(20)
    header[0:4] = b"\x7fELF"
    struct.pack_into("<H", header, 18, e_machine)
    with open(path, "wb") as f:
        f.write(bytes(header))


class TestBinaryMatchesArch:
    def test_amd64_binary_matches_amd64(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "go2rtc")
            _write_elf(p, 62)  # EM_X86_64
            assert downloader._binary_matches_arch(p, "amd64") is True

    def test_arm64_binary_does_not_match_amd64(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "go2rtc")
            _write_elf(p, 183)  # EM_AARCH64
            assert downloader._binary_matches_arch(p, "amd64") is False

    def test_arm64_binary_matches_arm64(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "go2rtc")
            _write_elf(p, 183)
            assert downloader._binary_matches_arch(p, "arm64") is True

    def test_non_elf_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "go2rtc")
            with open(p, "wb") as f:
                f.write(b"#!/bin/sh\necho not elf\n")
            assert downloader._binary_matches_arch(p, "amd64") is False

    def test_missing_file_is_rejected(self):
        assert downloader._binary_matches_arch("/no/such/go2rtc", "amd64") is False

    def test_unknown_arch_is_permissive(self):
        # Unknown host arch: don't fight it, accept whatever exists
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "go2rtc")
            _write_elf(p, 62)
            assert downloader._binary_matches_arch(p, "sparc") is True
