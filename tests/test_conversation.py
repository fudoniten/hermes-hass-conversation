"""Tests for the Hermes Assist conversation agent."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import aiohttp
import pytest

from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.hermes_assist.client import RunsUnsupportedError
from custom_components.hermes_assist.const import (
    CONF_FAST_WINDOW,
    CONF_MAX_WAIT,
    CONF_NOTIFY_TARGET,
    CONF_URL,
    CONF_USE_ASYNC,
    DOMAIN,
    FAILED_TEXT,
    TOO_LONG_TEXT,
)

AGENT_ID = "conversation.hermes_assist"
SATELLITE = "assist_satellite.kitchenette"


class FakeHermes:
    """Stands in for HermesClient; runs finish when the test says so."""

    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []
        self.stopped: list[str] = []
        self.sync_calls: list[list[dict[str, str]]] = []
        self.start_error: Exception | None = None
        self.records: dict[str, dict[str, Any]] = {}
        self.gates: dict[str, asyncio.Event] = {}
        self.default_record: dict[str, Any] = {"status": "running"}
        self.sync_reply = "Sync reply."

    async def start_run(self, model, text, instructions, history) -> str:
        if self.start_error is not None:
            raise self.start_error
        run_id = f"run_{len(self.started) + 1}"
        self.started.append(
            {"text": text, "instructions": instructions, "history": history}
        )
        return run_id

    async def get_run(self, run_id: str) -> dict[str, Any]:
        if (gate := self.gates.get(run_id)) is not None:
            await gate.wait()
        return self.records.get(run_id, self.default_record)

    async def stop_run(self, run_id: str) -> None:
        self.stopped.append(run_id)

    async def chat_completion(self, model, messages, timeout) -> str:
        self.sync_calls.append(messages)
        return self.sync_reply

    def finish(self, run_id: str, output: str) -> None:
        self.records[run_id] = {"status": "completed", "output": output}
        self.gates.setdefault(run_id, asyncio.Event()).set()

    def hold(self, run_id: str) -> None:
        self.gates[run_id] = asyncio.Event()


@pytest.fixture
def hermes() -> FakeHermes:
    fake = FakeHermes()
    with (
        patch(
            "custom_components.hermes_assist.conversation.HermesClient",
            return_value=fake,
        ),
        patch("custom_components.hermes_assist.runner.POLL_INTERVAL", 0.01),
    ):
        yield fake


@pytest.fixture
def announce(hass: HomeAssistant):
    return async_mock_service(hass, "assist_satellite", "announce")


@pytest.fixture
def start_conversation(hass: HomeAssistant):
    return async_mock_service(hass, "assist_satellite", "start_conversation")


@pytest.fixture
def persistent():
    with patch(
        "custom_components.hermes_assist.delivery.persistent_notification.async_create"
    ) as mock:
        yield mock


async def _setup(hass: HomeAssistant, **options: Any) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_URL: "http://hermes.local:8642"},
        options={CONF_FAST_WINDOW: 7, **options},
    )
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _ask(
    hass: HomeAssistant,
    text: str,
    *,
    conversation_id: str | None = None,
    satellite_id: str | None = SATELLITE,
) -> conversation.ConversationResult:
    return await conversation.async_converse(
        hass,
        text,
        conversation_id,
        Context(),
        language="en",
        agent_id=AGENT_ID,
        satellite_id=satellite_id,
    )


def _speech(result: conversation.ConversationResult) -> str:
    return result.response.speech["plain"]["speech"]


async def test_fast_answer_is_returned_directly(
    hass: HomeAssistant, hermes: FakeHermes, announce
) -> None:
    await _setup(hass)
    hermes.finish("run_1", "It's 21 degrees in the theater.")

    result = await _ask(hass, "what's the temperature in the theater?")
    await hass.async_block_till_done(wait_background_tasks=True)

    assert _speech(result) == "It's 21 degrees in the theater."
    assert announce == []
    assert "spoken aloud" in hermes.started[0]["instructions"]


async def test_slow_request_acks_then_announces(
    hass: HomeAssistant, hermes: FakeHermes, announce, persistent
) -> None:
    await _setup(hass, **{CONF_FAST_WINDOW: 0})
    hermes.hold("run_1")

    result = await _ask(hass, "play some Mozart in the media room")
    assert _speech(result) == "Okay, getting that playing."
    assert announce == []

    hermes.finish("run_1", "Mozart is playing in the media room.")
    await hass.async_block_till_done(wait_background_tasks=True)

    assert len(announce) == 1
    assert announce[0].data["entity_id"] == SATELLITE
    assert announce[0].data["message"] == "Mozart is playing in the media room."
    persistent.assert_not_called()


async def test_late_question_starts_conversation(
    hass: HomeAssistant, hermes: FakeHermes, announce, start_conversation
) -> None:
    await _setup(hass, **{CONF_FAST_WINDOW: 0})
    hermes.hold("run_1")

    await _ask(hass, "play the latest episode of that podcast")
    hermes.finish("run_1", "Pocket Casts or Spotify?")
    await hass.async_block_till_done(wait_background_tasks=True)

    assert announce == []
    assert start_conversation[0].data["start_message"] == "Pocket Casts or Spotify?"


async def test_runs_unsupported_falls_back_to_sync_once(
    hass: HomeAssistant, hermes: FakeHermes
) -> None:
    await _setup(hass)
    hermes.start_error = RunsUnsupportedError("HTTP 404")

    assert _speech(await _ask(hass, "hello")) == "Sync reply."
    hermes.start_error = AssertionError("should not try /v1/runs again")
    assert _speech(await _ask(hass, "hello again")) == "Sync reply."
    assert len(hermes.sync_calls) == 2


async def test_start_error_falls_back_for_this_request_only(
    hass: HomeAssistant, hermes: FakeHermes
) -> None:
    await _setup(hass)
    hermes.start_error = aiohttp.ClientConnectionError("refused")

    assert _speech(await _ask(hass, "hello")) == "Sync reply."

    hermes.start_error = None
    hermes.finish("run_1", "Async reply.")
    assert _speech(await _ask(hass, "hello again")) == "Async reply."


async def test_late_failure_is_delivered(
    hass: HomeAssistant, hermes: FakeHermes, announce, persistent
) -> None:
    await _setup(hass, **{CONF_FAST_WINDOW: 0})
    hermes.hold("run_1")

    await _ask(hass, "vacuum the kitchen")
    hermes.records["run_1"] = {"status": "failed", "error": "tool exploded"}
    hermes.gates["run_1"].set()
    await hass.async_block_till_done(wait_background_tasks=True)

    assert announce[0].data["message"] == FAILED_TEXT
    persistent.assert_called_once()
    assert persistent.call_args.args[1] == FAILED_TEXT


async def test_max_wait_stops_run(
    hass: HomeAssistant, hermes: FakeHermes, announce, persistent
) -> None:
    await _setup(hass, **{CONF_FAST_WINDOW: 0, CONF_MAX_WAIT: 0.05})

    assert _speech(await _ask(hass, "tell me a joke")) == "Okay, on it."
    await hass.async_block_till_done(wait_background_tasks=True)

    assert hermes.stopped == ["run_1"]
    assert announce[0].data["message"] == TOO_LONG_TEXT
    persistent.assert_called_once()


async def test_concurrent_requests_deliver_independently(
    hass: HomeAssistant, hermes: FakeHermes, announce
) -> None:
    await _setup(hass, **{CONF_FAST_WINDOW: 0})
    hermes.hold("run_1")
    hermes.hold("run_2")

    await _ask(hass, "turn on the lights", satellite_id="assist_satellite.den")
    await _ask(hass, "lock the doors", satellite_id="assist_satellite.office")
    hermes.finish("run_2", "Doors locked.")
    hermes.finish("run_1", "Lights on.")
    await hass.async_block_till_done(wait_background_tasks=True)

    delivered = {call.data["entity_id"]: call.data["message"] for call in announce}
    assert delivered == {
        "assist_satellite.den": "Lights on.",
        "assist_satellite.office": "Doors locked.",
    }


async def test_follow_up_on_same_device_keeps_context(
    hass: HomeAssistant, hermes: FakeHermes, announce
) -> None:
    await _setup(hass, **{CONF_FAST_WINDOW: 0})
    hermes.hold("run_1")

    await _ask(hass, "play the news podcast", conversation_id="conv-a")
    hermes.finish("run_1", "Playing the news.")
    await hass.async_block_till_done(wait_background_tasks=True)

    hermes.finish("run_2", "Paused.")
    await _ask(hass, "now pause it", conversation_id="conv-b")

    assert hermes.started[1]["history"] == [
        {"role": "user", "content": "play the news podcast"},
        {"role": "assistant", "content": "Playing the news."},
    ]


async def test_other_device_does_not_share_context(
    hass: HomeAssistant, hermes: FakeHermes
) -> None:
    await _setup(hass)
    hermes.finish("run_1", "Playing.")
    await _ask(hass, "play jazz", conversation_id="conv-a")

    hermes.finish("run_2", "Paused.")
    await _ask(
        hass, "pause", conversation_id="conv-b", satellite_id="assist_satellite.den"
    )

    assert hermes.started[1]["history"] == []


async def test_unload_stops_in_flight_runs(
    hass: HomeAssistant, hermes: FakeHermes, announce
) -> None:
    entry = await _setup(hass, **{CONF_FAST_WINDOW: 0})
    hermes.hold("run_1")
    await _ask(hass, "play something long")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert hermes.stopped == ["run_1"]
    assert announce == []


async def test_cancelled_request_stops_run(
    hass: HomeAssistant, hermes: FakeHermes, announce
) -> None:
    await _setup(hass)
    hermes.hold("run_1")

    request = hass.async_create_task(_ask(hass, "play something"))
    while not hermes.started:
        await asyncio.sleep(0)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    await hass.async_block_till_done(wait_background_tasks=True)

    assert hermes.stopped == ["run_1"]
    assert announce == []


async def test_use_async_off_uses_chat_completions(
    hass: HomeAssistant, hermes: FakeHermes
) -> None:
    await _setup(hass, **{CONF_USE_ASYNC: False})

    assert _speech(await _ask(hass, "hello")) == "Sync reply."
    assert hermes.started == []


async def test_notify_target_gets_full_text(
    hass: HomeAssistant, hermes: FakeHermes, persistent
) -> None:
    notify = async_mock_service(hass, "notify", "mobile_app_pixel_11_pro")
    await _setup(
        hass,
        **{CONF_FAST_WINDOW: 0, CONF_NOTIFY_TARGET: "notify.mobile_app_pixel_11_pro"},
    )
    hermes.hold("run_1")
    long_text = "Here is a long answer. " * 30

    await _ask(hass, "summarize my day", satellite_id=None)
    hermes.finish("run_1", long_text)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert notify[0].data["message"] == long_text.strip()
    persistent.assert_not_called()


async def test_no_satellite_or_notify_creates_persistent_notification(
    hass: HomeAssistant, hermes: FakeHermes, persistent
) -> None:
    await _setup(hass, **{CONF_FAST_WINDOW: 0})
    hermes.hold("run_1")

    await _ask(hass, "summarize my day", satellite_id=None)
    hermes.finish("run_1", "Busy day.")
    await hass.async_block_till_done(wait_background_tasks=True)

    persistent.assert_called_once()
    assert persistent.call_args.args[1] == "Busy day."
