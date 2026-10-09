"""Thin HTTP client for the Hermes Agent API server."""

from __future__ import annotations

from typing import Any

import aiohttp

from .const import CHAT_COMPLETIONS_PATH, RUNS_PATH

RUN_REQUEST_TIMEOUT = 15

# Statuses after which a run will not change again.
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})


class RunsUnsupportedError(Exception):
    """The Hermes server has no /v1/runs endpoint."""


class HermesClient:
    """Calls Hermes' chat completions and runs endpoints."""

    def __init__(
        self, session: aiohttp.ClientSession, base_url: str, api_key: str | None
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._headers = {"Content-Type": "application/json"}
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"

    async def chat_completion(
        self, model: str, messages: list[dict[str, str]], timeout: float
    ) -> str:
        """Run a full turn synchronously and return the reply text."""
        payload: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        async with self._session.post(
            self._base_url + CHAT_COMPLETIONS_PATH,
            json=payload,
            headers=self._headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            resp.raise_for_status()
            return extract_reply(await resp.json())

    async def start_run(
        self,
        model: str,
        text: str,
        instructions: str,
        history: list[dict[str, str]],
    ) -> str:
        """Start an async run and return its run_id.

        Hermes takes the new user message as `input`, the system prompt as
        `instructions`, and prior turns as `conversation_history`.
        """
        payload: dict[str, Any] = {
            "model": model,
            "input": text,
            "instructions": instructions,
        }
        if history:
            payload["conversation_history"] = history
        async with self._session.post(
            self._base_url + RUNS_PATH,
            json=payload,
            headers=self._headers,
            timeout=aiohttp.ClientTimeout(total=RUN_REQUEST_TIMEOUT),
        ) as resp:
            if resp.status in (404, 405, 501):
                raise RunsUnsupportedError(f"HTTP {resp.status}")
            resp.raise_for_status()
            data = await resp.json()
        run_id = data.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("no run_id in response")
        return run_id

    async def get_run(self, run_id: str) -> dict[str, Any]:
        """Return the run's current status record."""
        async with self._session.get(
            f"{self._base_url}{RUNS_PATH}/{run_id}",
            headers=self._headers,
            timeout=aiohttp.ClientTimeout(total=RUN_REQUEST_TIMEOUT),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        if not isinstance(data, dict):
            raise ValueError("unrecognized run status shape")
        return data

    async def stop_run(self, run_id: str) -> None:
        """Ask Hermes to interrupt a run."""
        async with self._session.post(
            f"{self._base_url}{RUNS_PATH}/{run_id}/stop",
            headers=self._headers,
            timeout=aiohttp.ClientTimeout(total=RUN_REQUEST_TIMEOUT),
        ) as resp:
            resp.raise_for_status()


def extract_reply(data: dict[str, Any]) -> str:
    """Pull the assistant text out of an OpenAI-format response."""
    choices = data.get("choices") or []
    if not choices:
        raise ValueError("no choices in response")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip() or "(empty response)"
    if isinstance(content, list):
        parts = [
            p.get("text", "")
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        ]
        joined = "".join(parts).strip()
        return joined or "(empty response)"
    raise ValueError("unrecognized content shape")
