"""Shared fixtures: run the integration against the synthetic simulated bus."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sim import SimBus  # noqa: E402

FAKE_PATH = "/dev/hidraw0"
FAKE_SERIAL = "SIM0000000001"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow loading custom_components/helvar510 in every test."""
    yield


@pytest.fixture
def sim():
    """Simulated 510 + DALI bus; every Dali510 instance talks to it."""
    from custom_components.helvar510 import dali510

    bus = SimBus()
    orig_init = dali510.Dali510.__init__

    def patched(self, path=None, opener=None):
        orig_init(self, path, opener=bus.opener)

    with (
        patch.object(dali510.Dali510, "__init__", patched),
        patch(
            "custom_components.helvar510.config_flow._device_choices",
            return_value={FAKE_PATH: f"{FAKE_PATH} - simulated 510"},
        ),
        patch(
            "custom_components.helvar510._serial_for", return_value=FAKE_SERIAL
        ),
    ):
        yield bus
    bus.close()
    bus._t.join(timeout=5)
