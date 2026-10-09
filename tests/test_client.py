"""Tests for the Hermes HTTP client."""

from __future__ import annotations

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.hermes_assist.client import HermesClient, RunsUnsupportedError

URL = "http://hermes.local:8642"


async def test_start_run_payload(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(
        f"{URL}/v1/runs", status=202, json={"run_id": "run_1", "status": "started"}
    )
    client = HermesClient(async_get_clientsession(hass), URL + "/", "secret")

    history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    run_id = await client.start_run("hermes-agent", "play jazz", "be brief", history)

    assert run_id == "run_1"
    _, _, body, headers = aioclient_mock.mock_calls[0]
    assert body == {
        "model": "hermes-agent",
        "input": "play jazz",
        "instructions": "be brief",
        "conversation_history": history,
    }
    assert headers["Authorization"] == "Bearer secret"


async def test_start_run_omits_empty_history(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(f"{URL}/v1/runs", status=202, json={"run_id": "run_1"})
    client = HermesClient(async_get_clientsession(hass), URL, None)

    await client.start_run("m", "hi", "sys", [])

    _, _, body, headers = aioclient_mock.mock_calls[0]
    assert "conversation_history" not in body
    assert "Authorization" not in headers


@pytest.mark.parametrize("status", [404, 405, 501])
async def test_start_run_unsupported(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status: int
) -> None:
    aioclient_mock.post(f"{URL}/v1/runs", status=status)
    client = HermesClient(async_get_clientsession(hass), URL, None)

    with pytest.raises(RunsUnsupportedError):
        await client.start_run("m", "hi", "sys", [])


async def test_get_and_stop_run(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(
        f"{URL}/v1/runs/run_1", json={"status": "completed", "output": "Done."}
    )
    aioclient_mock.post(f"{URL}/v1/runs/run_1/stop", json={"status": "stopping"})
    client = HermesClient(async_get_clientsession(hass), URL, None)

    assert (await client.get_run("run_1"))["output"] == "Done."
    await client.stop_run("run_1")
    assert aioclient_mock.call_count == 2


async def test_chat_completion(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(
        f"{URL}/v1/chat/completions",
        json={"choices": [{"message": {"content": [{"type": "text", "text": " Hi "}]}}]},
    )
    client = HermesClient(async_get_clientsession(hass), URL, None)

    assert await client.chat_completion("m", [{"role": "user", "content": "x"}], 5) == "Hi"


async def test_iter_run_events_parses_sse(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    body = (
        b": keepalive\n\n"
        b'id: 0\ndata: {"event": "tool.started", "tool": "ha_call_service"}\n\n'
        b"data: not json\n\n"
        b'id: 1\ndata: {"event": "run.completed"}\n\n'
    )
    aioclient_mock.get(f"{URL}/v1/runs/run_1/events", content=body)
    client = HermesClient(async_get_clientsession(hass), URL, None)

    events = [event async for event in client.iter_run_events("run_1", 30)]

    assert [e["event"] for e in events] == ["tool.started", "run.completed"]
