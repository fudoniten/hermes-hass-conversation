"""Tests for the config and options flows."""

from __future__ import annotations

import pytest

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.selector import TextSelector
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.jarvis_assistant.const import (
    CONF_API_KEY,
    CONF_FAST_WINDOW,
    CONF_MODEL,
    CONF_SYSTEM_PROMPT,
    CONF_URL,
    CONF_USE_ASYNC,
    DEFAULT_FAST_WINDOW,
    DOMAIN,
)

URL = "http://hermes.local:8642"


@pytest.fixture(autouse=True)
async def setup_core(hass: HomeAssistant) -> None:
    """Creating an entry sets it up, which needs the conversation dependency."""
    assert await async_setup_component(hass, "homeassistant", {})


async def test_user_flow_creates_entry_with_async_defaults(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{URL}/v1/models", json={"data": []})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL + "/", CONF_API_KEY: " key "}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_URL: URL}
    assert result["options"][CONF_API_KEY] == "key"
    assert result["options"][CONF_USE_ASYNC] is True
    assert result["options"][CONF_FAST_WINDOW] == DEFAULT_FAST_WINDOW


async def test_user_flow_invalid_auth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{URL}/v1/models", status=401)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL}
    )

    assert result["errors"] == {"base": "invalid_auth"}


async def test_options_flow_keeps_existing_and_adds_new(hass: HomeAssistant) -> None:
    # An entry created before the async options existed.
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_URL: URL}, options={CONF_MODEL: "custom"}
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    defaults = {
        key.schema: key.default() for key in result["data_schema"].schema
    }
    assert defaults[CONF_MODEL] == "custom"
    assert defaults[CONF_FAST_WINDOW] == DEFAULT_FAST_WINDOW

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_FAST_WINDOW: 3}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_FAST_WINDOW] == 3
    assert entry.options[CONF_MODEL] == "custom"


async def test_system_prompt_is_multiline(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_URL: URL}, options={})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    validators = {key.schema: value for key, value in result["data_schema"].schema.items()}
    assert isinstance(validators[CONF_SYSTEM_PROMPT], TextSelector)
    assert validators[CONF_SYSTEM_PROMPT].config["multiline"] is True

    prompt = "You are Hermes.\n\nRooms:\n- den\n- kitchenette"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SYSTEM_PROMPT: prompt}
    )
    assert entry.options[CONF_SYSTEM_PROMPT] == prompt
