"""Deliver late Hermes results back to the user."""

from __future__ import annotations

import logging
import re

import voluptuous as vol

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from .const import MAX_SPOKEN_CHARS, NOTIFICATION_TITLE

_LOGGER = logging.getLogger(__name__)

SATELLITE_DOMAIN = "assist_satellite"

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


async def async_deliver(
    hass: HomeAssistant,
    text: str,
    *,
    satellite_id: str | None,
    device_id: str | None,
    notify_target: str,
    failed: bool,
) -> None:
    """Speak on the asking satellite, notify the phone, and fall back to a
    persistent notification when nothing else reached the user or the run failed.
    """
    spoken = False
    satellite = find_satellite(hass, satellite_id, device_id)
    if satellite is not None:
        spoken = await _async_speak(hass, satellite, shorten_for_speech(text))

    notified = False
    if notify_target:
        notified = await _async_notify(hass, notify_target, text)

    if failed or not (spoken or notified):
        persistent_notification.async_create(hass, text, title=NOTIFICATION_TITLE)


def find_satellite(
    hass: HomeAssistant, satellite_id: str | None, device_id: str | None
) -> str | None:
    """Return the assist_satellite entity that made the request, if any."""
    if satellite_id and satellite_id.startswith(f"{SATELLITE_DOMAIN}."):
        return satellite_id
    if device_id:
        registry = er.async_get(hass)
        for entry in er.async_entries_for_device(registry, device_id):
            if entry.domain == SATELLITE_DOMAIN:
                return entry.entity_id
    return None


async def _async_speak(hass: HomeAssistant, satellite: str, text: str) -> bool:
    """Announce on the satellite; if the text asks a question, listen for the answer."""
    if text.endswith("?"):
        service, data = "start_conversation", {"start_message": text}
    else:
        service, data = "announce", {"message": text}
    try:
        await hass.services.async_call(
            SATELLITE_DOMAIN,
            service,
            {"entity_id": satellite, **data},
            blocking=True,
        )
    except (HomeAssistantError, vol.Invalid) as err:
        _LOGGER.warning("Could not %s on %s: %s", service, satellite, err)
        return False
    _LOGGER.info("Delivered Hermes result to %s via %s", satellite, service)
    return True


async def _async_notify(hass: HomeAssistant, target: str, text: str) -> bool:
    """Send the full result to a notify service such as mobile_app_<phone>."""
    service = target.removeprefix("notify.")
    try:
        await hass.services.async_call(
            "notify",
            service,
            {"title": NOTIFICATION_TITLE, "message": text},
            blocking=True,
        )
    except (HomeAssistantError, vol.Invalid) as err:
        _LOGGER.warning("Could not notify %s: %s", service, err)
        return False
    _LOGGER.info("Delivered Hermes result via notify.%s", service)
    return True


def shorten_for_speech(text: str, limit: int = MAX_SPOKEN_CHARS) -> str:
    """Strip markdown and cut long text at a sentence boundary."""
    plain = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    plain = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", plain)
    plain = re.sub(r"^\s{0,3}(#{1,6}|[-*+]|\d+\.)\s+", "", plain, flags=re.MULTILINE)
    plain = re.sub(r"(\*\*|__|\*|`|~~)", "", plain)
    plain = re.sub(r"\s+", " ", plain).strip()
    if len(plain) <= limit:
        return plain

    kept = ""
    for sentence in _SENTENCE_END.split(plain):
        candidate = f"{kept} {sentence}".strip()
        if len(candidate) > limit:
            break
        kept = candidate
    if kept:
        return kept
    # The first sentence alone is too long: cut at a word boundary.
    return plain[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
