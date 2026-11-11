"""Exercise the complete card bundle across custom-element registry swaps."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CARD = ROOT / "custom_components/cuboai/www/cuboai-card.js"
HARNESS = ROOT / "tests/card_registry_harness.js"
NAMES = ("cuboai-camera-card-editor", "cuboai-camera-card", "cuboai-timeline-card")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize(
    "scenario",
    [
        "startup",
        "ready_after_swap",
        "second_swap_after_recovery",
        "ready_before_swap",
        "late_swap_before_last_recheck",
        "constructor_fallback",
        "no_readiness",
        "existing",
        "partial",
        "repeat",
        "repeat_after_swap_before_ready",
        "too_late",
        "timer_error_retry",
        "readiness_error",
        *(f"sync_error:{name}" for name in NAMES),
        *(f"async_error:{name}" for name in NAMES),
    ],
)
def test_card_registry_recovery(scenario):
    result = subprocess.run(["node", str(HARNESS), str(CARD), scenario], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"{scenario}:\n{result.stdout}\n{result.stderr}"
