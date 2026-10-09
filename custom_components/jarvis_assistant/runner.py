"""Follow a Hermes async run until it finishes, fails or runs out of time."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import logging
from typing import Any, Literal

import aiohttp

from .client import TERMINAL_STATUSES, HermesClient
from .const import POLL_INTERVAL

_LOGGER = logging.getLogger(__name__)

# Consecutive failed status polls before giving up on a run.
MAX_POLL_ERRORS = 5

# Longest tool preview / approval detail / raw record written to the log.
_MAX_LOGGED_CHARS = 300

# Envelope keys of an approval event; everything else describes what needs approving.
_APPROVAL_ENVELOPE_KEYS = frozenset({"event", "run_id", "timestamp", "choices", "seq"})


@dataclass(frozen=True)
class RunOutcome:
    """How a run ended."""

    status: Literal["completed", "failed", "timed_out"]
    output: str = ""
    error: str | None = None


async def async_wait_for_run(
    client: HermesClient, run_id: str, max_wait: float
) -> RunOutcome:
    """Poll a run until it reaches a terminal state or max_wait elapses.

    On timeout the run is stopped on the Hermes side (best effort). While the
    run is going, its event stream is logged so slow tools and pending
    approvals show up in the Home Assistant log.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    events_task = asyncio.create_task(
        _async_log_events(client, run_id, started, max_wait)
    )
    try:
        outcome, last = await _async_poll(client, run_id, started, max_wait)
    finally:
        events_task.cancel()
        with suppress(asyncio.CancelledError):
            await events_task

    elapsed = loop.time() - started
    if outcome.status == "completed":
        _LOGGER.info(
            "Hermes run %s completed after %.1fs%s",
            run_id,
            elapsed,
            _usage_suffix(last),
        )
    elif outcome.status == "timed_out":
        _LOGGER.warning(
            "Hermes run %s gave up after %.1fs; last status %s, last event %s%s",
            run_id,
            elapsed,
            last.get("status", "unknown"),
            last.get("last_event") or "none",
            f", pending approval: {_describe_approval(last['approval'])}"
            if last.get("status") == "waiting_for_approval" and last.get("approval")
            else "",
        )
    else:
        _LOGGER.warning(
            "Hermes run %s failed after %.1fs: %s", run_id, elapsed, outcome.error
        )
    return outcome


async def _async_poll(
    client: HermesClient, run_id: str, started: float, max_wait: float
) -> tuple[RunOutcome, dict[str, Any]]:
    """Poll until terminal or timed out; return the outcome and the last record seen."""
    loop = asyncio.get_running_loop()
    deadline = started + max_wait
    poll_errors = 0
    last: dict[str, Any] = {}
    logged_approval: Any = None

    while True:
        try:
            record = await client.get_run(run_id)
        except aiohttp.ClientResponseError as err:
            if err.status == 404:
                return RunOutcome("failed", error="run no longer known to Hermes"), last
            poll_errors += 1
            _LOGGER.warning(
                "Polling Hermes run %s failed (%d in a row): HTTP %s",
                run_id,
                poll_errors,
                err.status,
            )
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            poll_errors += 1
            _LOGGER.warning(
                "Polling Hermes run %s failed (%d in a row): %r", run_id, poll_errors, err
            )
        else:
            poll_errors = 0
            _LOGGER.debug("Hermes run %s record: %s", run_id, _truncate(repr(record)))
            status = record.get("status")
            if status != last.get("status"):
                _LOGGER.info(
                    "Hermes run %s is %s after %.1fs (last event: %s)",
                    run_id,
                    status,
                    loop.time() - started,
                    record.get("last_event") or "none",
                )
            last = record
            if status == "completed":
                return RunOutcome("completed", output=str(record.get("output") or "")), last
            if status in TERMINAL_STATUSES:
                return RunOutcome("failed", error=str(record.get("error") or status)), last
            approval = record.get("approval")
            if status == "waiting_for_approval" and approval != logged_approval:
                logged_approval = approval
                _LOGGER.warning(
                    "Hermes run %s is waiting for a tool approval: %s. Hermes holds "
                    "/v1/runs runs until the approval is answered or its approval "
                    "timeout expires",
                    run_id,
                    _describe_approval(approval),
                )

        if poll_errors >= MAX_POLL_ERRORS:
            return RunOutcome("failed", error="lost contact with Hermes while polling"), last

        if loop.time() >= deadline:
            await async_stop_run(client, run_id)
            return RunOutcome("timed_out", error=f"no result after {max_wait:.0f}s"), last

        await asyncio.sleep(POLL_INTERVAL)


async def _async_log_events(
    client: HermesClient, run_id: str, started: float, max_wait: float
) -> None:
    """Log tool activity from the run's event stream. Best effort: polling
    decides the outcome, so stream errors are only logged.
    """
    loop = asyncio.get_running_loop()
    try:
        async for event in client.iter_run_events(run_id, max_wait + 30):
            name = event.get("event")
            elapsed = loop.time() - started
            if name == "tool.started":
                _LOGGER.info(
                    "Hermes run %s: tool %s started at %.1fs: %s",
                    run_id,
                    event.get("tool"),
                    elapsed,
                    _truncate(str(event.get("preview") or "")),
                )
            elif name == "tool.completed":
                _LOGGER.info(
                    "Hermes run %s: tool %s finished in %.1fs%s at %.1fs: %s",
                    run_id,
                    event.get("tool"),
                    float(event.get("duration") or 0),
                    " with an error" if event.get("error") else "",
                    elapsed,
                    _truncate(str(event.get("preview") or "")),
                )
            elif name in ("subagent.start", "subagent.complete"):
                _LOGGER.info(
                    "Hermes run %s: %s at %.1fs: %s",
                    run_id,
                    name,
                    elapsed,
                    _truncate(str(event.get("goal") or event.get("summary") or "")),
                )
            else:
                _LOGGER.debug(
                    "Hermes run %s event at %.1fs: %s",
                    run_id,
                    elapsed,
                    _truncate(repr(event)),
                )
    except asyncio.CancelledError:
        raise
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        _LOGGER.debug("Event stream for Hermes run %s ended: %r", run_id, err)


async def async_stop_run(client: HermesClient, run_id: str) -> None:
    """Ask Hermes to stop a run, logging rather than raising on failure."""
    try:
        await client.stop_run(run_id)
    except (aiohttp.ClientError, TimeoutError) as err:
        _LOGGER.warning("Could not stop Hermes run %s: %s", run_id, err)
    else:
        _LOGGER.info("Asked Hermes to stop run %s", run_id)


def _describe_approval(approval: Any) -> str:
    """Summarize an approval request without its event envelope."""
    if not isinstance(approval, dict):
        return "no details"
    details = {k: v for k, v in approval.items() if k not in _APPROVAL_ENVELOPE_KEYS}
    return _truncate(repr(details)) if details else "no details"


def _usage_suffix(record: dict[str, Any]) -> str:
    usage = record.get("usage")
    if isinstance(usage, dict) and usage.get("total_tokens"):
        return f" ({usage['total_tokens']} tokens)"
    return ""


def _truncate(text: str) -> str:
    if len(text) <= _MAX_LOGGED_CHARS:
        return text
    return text[: _MAX_LOGGED_CHARS - 1] + "…"
