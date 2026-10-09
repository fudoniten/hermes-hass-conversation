"""Tests for run polling and its diagnostic logging."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import patch

import pytest

from custom_components.jarvis_assistant.runner import async_wait_for_run


class ScriptedHermes:
    """Returns a scripted sequence of run records, repeating the last one."""

    def __init__(
        self, records: list[dict[str, Any]], events: list[dict[str, Any]] | None = None
    ) -> None:
        self.records = records
        self.events = events or []
        self.stopped: list[str] = []

    async def get_run(self, run_id: str) -> dict[str, Any]:
        if len(self.records) > 1:
            return self.records.pop(0)
        return self.records[0]

    async def iter_run_events(self, run_id: str, timeout: float):
        for event in self.events:
            yield event
        await asyncio.Event().wait()  # stream stays open until cancelled

    async def stop_run(self, run_id: str) -> None:
        self.stopped.append(run_id)


@pytest.fixture(autouse=True)
def fast_polling():
    with patch("custom_components.jarvis_assistant.runner.POLL_INTERVAL", 0.01):
        yield


async def test_logs_status_changes_and_tools(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="custom_components.jarvis_assistant")
    client = ScriptedHermes(
        [
            {"status": "queued"},
            {"status": "running", "last_event": "tool.started"},
            {"status": "running", "last_event": "tool.completed"},
            {"status": "completed", "output": "Lights off.", "usage": {"total_tokens": 1234}},
        ],
        events=[
            {"event": "tool.started", "tool": "ha_call_service", "preview": "light.turn_off"},
            {"event": "tool.completed", "tool": "ha_call_service", "duration": 0.42},
        ],
    )

    outcome = await async_wait_for_run(client, "run_1", 30)

    assert outcome.status == "completed"
    assert outcome.output == "Lights off."
    log = caplog.text
    assert "run_1 is queued" in log
    assert "run_1 is running" in log
    assert log.count("run_1 is running") == 1  # only on change
    assert "tool ha_call_service started" in log
    assert "tool ha_call_service finished in 0.4s" in log
    assert "completed after" in log and "(1234 tokens)" in log


async def test_logs_pending_approval_and_timeout_summary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="custom_components.jarvis_assistant")
    approval = {
        "event": "approval.request",
        "run_id": "run_1",
        "choices": ["once", "deny"],
        "command": "ha_call_service light.turn_off",
        "description": "Home Assistant service call",
    }
    client = ScriptedHermes(
        [
            {"status": "running"},
            {
                "status": "waiting_for_approval",
                "last_event": "approval.request",
                "approval": approval,
            },
        ]
    )

    outcome = await async_wait_for_run(client, "run_1", 0.1)

    assert outcome.status == "timed_out"
    assert client.stopped == ["run_1"]
    approval_logs = [
        r for r in caplog.records if "waiting for a tool approval" in r.getMessage()
    ]
    assert len(approval_logs) == 1  # logged once, not on every poll
    assert "Home Assistant service call" in approval_logs[0].getMessage()
    assert "'choices'" not in approval_logs[0].getMessage()
    summary = caplog.records[-1].getMessage()
    assert "gave up after" in summary
    assert "last status waiting_for_approval" in summary
    assert "pending approval" in summary


async def test_event_stream_errors_do_not_fail_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class BrokenStream(ScriptedHermes):
        async def iter_run_events(self, run_id: str, timeout: float):
            raise TimeoutError
            yield  # pragma: no cover

    client = BrokenStream([{"status": "completed", "output": "ok"}])

    assert (await async_wait_for_run(client, "run_1", 30)).output == "ok"


async def test_poll_errors_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    class Flaky(ScriptedHermes):
        calls = 0

        async def get_run(self, run_id: str) -> dict[str, Any]:
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError
            return {"status": "completed", "output": "ok"}

    outcome = await async_wait_for_run(Flaky([{}]), "run_1", 30)

    assert outcome.status == "completed"
    assert "Polling Hermes run run_1 failed (1 in a row)" in caplog.text
