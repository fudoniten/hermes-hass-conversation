"""Follow a Hermes async run until it finishes, fails or runs out of time."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Literal

import aiohttp

from .client import TERMINAL_STATUSES, HermesClient
from .const import POLL_INTERVAL

_LOGGER = logging.getLogger(__name__)

# Consecutive failed status polls before giving up on a run.
MAX_POLL_ERRORS = 5


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

    On timeout the run is stopped on the Hermes side (best effort).
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait
    poll_errors = 0
    approval_logged = False

    while True:
        try:
            record = await client.get_run(run_id)
        except aiohttp.ClientResponseError as err:
            if err.status == 404:
                return RunOutcome("failed", error="run no longer known to Hermes")
            poll_errors += 1
            _LOGGER.debug("Polling run %s failed: HTTP %s", run_id, err.status)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            poll_errors += 1
            _LOGGER.debug("Polling run %s failed: %s", run_id, err)
        else:
            poll_errors = 0
            status = record.get("status")
            if status == "completed":
                return RunOutcome("completed", output=str(record.get("output") or ""))
            if status in TERMINAL_STATUSES:
                return RunOutcome("failed", error=str(record.get("error") or status))
            if status == "waiting_for_approval" and not approval_logged:
                approval_logged = True
                _LOGGER.warning(
                    "Hermes run %s is waiting for a tool approval; it will time out "
                    "unless approved on the Hermes side",
                    run_id,
                )

        if poll_errors >= MAX_POLL_ERRORS:
            return RunOutcome("failed", error="lost contact with Hermes while polling")

        if loop.time() >= deadline:
            await async_stop_run(client, run_id)
            return RunOutcome("timed_out", error=f"no result after {max_wait:.0f}s")

        await asyncio.sleep(POLL_INTERVAL)


async def async_stop_run(client: HermesClient, run_id: str) -> None:
    """Ask Hermes to stop a run, logging rather than raising on failure."""
    try:
        await client.stop_run(run_id)
    except (aiohttp.ClientError, TimeoutError) as err:
        _LOGGER.warning("Could not stop Hermes run %s: %s", run_id, err)
