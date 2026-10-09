"""Tests for finding the satellite that asked."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.jarvis_assistant.delivery import find_satellite


async def test_find_satellite_by_entity_id(hass: HomeAssistant) -> None:
    assert find_satellite(hass, "assist_satellite.den", None) == "assist_satellite.den"


async def test_find_satellite_by_device(hass: HomeAssistant) -> None:
    config_entry = MockConfigEntry(domain="esphome")
    config_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id, identifiers={("esphome", "pe-1")}
    )
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "media_player", "esphome", "pe-1-media", device_id=device.id
    )
    satellite = registry.async_get_or_create(
        "assist_satellite", "esphome", "pe-1-sat", device_id=device.id
    )

    assert find_satellite(hass, None, device.id) == satellite.entity_id


async def test_no_satellite(hass: HomeAssistant) -> None:
    assert find_satellite(hass, None, None) is None
    assert find_satellite(hass, None, "missing-device") is None
