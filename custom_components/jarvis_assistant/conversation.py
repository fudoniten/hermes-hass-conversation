"""Jarvis Assistant conversation agent."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import logging
import time
from typing import Literal

import aiohttp

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import ulid as ulid_util

from .acks import select_ack
from .client import HermesClient, RunsUnsupportedError
from .const import (
    CONF_ACK_TEXT,
    CONF_API_KEY,
    CONF_DEVICE_CONTEXT_TTL,
    CONF_FAST_WINDOW,
    CONF_MAX_WAIT,
    CONF_MODEL,
    CONF_NOTIFY_TARGET,
    CONF_RICH_ACKS,
    CONF_SYSTEM_PROMPT,
    CONF_TIMEOUT,
    CONF_URL,
    CONF_USE_ASYNC,
    DEFAULT_ACK_TEXT,
    DEFAULT_DEVICE_CONTEXT_TTL,
    DEFAULT_FAST_WINDOW,
    DEFAULT_MAX_WAIT,
    DEFAULT_MODEL,
    DEFAULT_NOTIFY_TARGET,
    DEFAULT_RICH_ACKS,
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_TIMEOUT,
    DEFAULT_USE_ASYNC,
    FAILED_TEXT,
    MAX_HISTORY_EXCHANGES,
    MAX_TRACKED_CONVERSATIONS,
    TOO_LONG_TEXT,
    VOICE_SYSTEM_PROMPT,
)
from .delivery import async_deliver
from .runner import RunOutcome, async_stop_run, async_wait_for_run

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Register the Jarvis Assistant conversation entity."""
    async_add_entities([JarvisAssistantConversationEntity(entry)])


@dataclass(frozen=True)
class _Request:
    """What a background run needs to deliver its result later."""

    text: str
    history: list[dict[str, str]]
    device_key: str | None
    satellite_id: str | None
    device_id: str | None


class JarvisAssistantConversationEntity(conversation.ConversationEntity):
    """Sends utterances to Hermes, acknowledging slow requests and
    delivering their results when they finish.
    """

    _attr_has_entity_name = True
    _attr_name = "Jarvis Assistant"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = entry.entry_id
        self._history: OrderedDict[str, list[dict[str, str]]] = OrderedDict()
        # device key -> (conversation_id, last activity, monotonic seconds)
        self._device_context: dict[str, tuple[str, float]] = {}
        self._pending: dict[str, asyncio.Task[RunOutcome]] = {}
        self._runs_unsupported = False

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        return MATCH_ALL

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        conversation.async_set_agent(self.hass, self._entry, self)

    async def async_will_remove_from_hass(self) -> None:
        conversation.async_unset_agent(self.hass, self._entry)
        if self._pending:
            # Background tasks are cancelled when the entry unloads (including the
            # reload after an options change); stop the runs on the Hermes side too.
            client = self._client()
            await asyncio.gather(
                *(async_stop_run(client, run_id) for run_id in self._pending)
            )
            self._pending.clear()
        await super().async_will_remove_from_hass()

    def _option(self, key: str, default):
        return self._entry.options.get(key, default)

    def _client(self) -> HermesClient:
        return HermesClient(
            async_get_clientsession(self.hass),
            self._entry.data[CONF_URL],
            self._option(CONF_API_KEY, ""),
        )

    async def async_process(
        self, user_input: conversation.ConversationInput
    ) -> conversation.ConversationResult:
        """Answer directly if Hermes is quick, otherwise acknowledge and follow up."""
        conversation_id = user_input.conversation_id or ulid_util.ulid_now()
        device_key = user_input.satellite_id or user_input.device_id
        history = self._get_history(conversation_id, device_key)
        if device_key:
            self._device_context[device_key] = (conversation_id, time.monotonic())

        system_prompts = [self._option(CONF_SYSTEM_PROMPT, DEFAULT_SYSTEM_PROMPT)]
        if user_input.extra_system_prompt:
            system_prompts.append(user_input.extra_system_prompt)
        if device_key:
            system_prompts.append(VOICE_SYSTEM_PROMPT)

        reply: str | None = None
        if self._option(CONF_USE_ASYNC, DEFAULT_USE_ASYNC) and not self._runs_unsupported:
            request = _Request(
                text=user_input.text,
                history=history,
                device_key=device_key,
                satellite_id=user_input.satellite_id,
                device_id=user_input.device_id,
            )
            reply = await self._async_process_async(request, system_prompts)
        if reply is None:
            reply = await self._async_process_sync(user_input.text, history, system_prompts)

        response = intent.IntentResponse(language=user_input.language)
        response.async_set_speech(reply)
        return conversation.ConversationResult(
            response=response,
            conversation_id=conversation_id,
        )

    async def _async_process_async(
        self, request: _Request, system_prompts: list[str]
    ) -> str | None:
        """Race a /v1/runs run against the fast window.

        Returns the text to speak now, or None to fall back to the sync path.
        """
        client = self._client()
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            run_id = await client.start_run(
                self._option(CONF_MODEL, DEFAULT_MODEL),
                request.text,
                "\n\n".join(system_prompts),
                list(request.history),
            )
        except RunsUnsupportedError as err:
            _LOGGER.warning(
                "Hermes has no /v1/runs endpoint (%s); using chat completions from now on",
                err,
            )
            self._runs_unsupported = True
            return None
        except aiohttp.ClientResponseError as err:
            _LOGGER.warning("Starting Hermes run failed (HTTP %s); trying sync", err.status)
            return None
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.warning("Starting Hermes run failed (%s); trying sync", err)
            return None

        _LOGGER.info(
            "Hermes run %s started in %.1fs for %r (%d history messages, device %s)",
            run_id,
            loop.time() - started,
            request.text,
            len(request.history),
            request.device_key or "none",
        )
        task = self._entry.async_create_background_task(
            self.hass,
            async_wait_for_run(
                client, run_id, self._option(CONF_MAX_WAIT, DEFAULT_MAX_WAIT)
            ),
            f"{self.entity_id} run {run_id}",
        )
        try:
            done, _ = await asyncio.wait(
                {task}, timeout=self._option(CONF_FAST_WINDOW, DEFAULT_FAST_WINDOW)
            )
        except asyncio.CancelledError:
            # The pipeline gave up on us; nobody is left to deliver the result to.
            task.cancel()
            self._entry.async_create_background_task(
                self.hass, async_stop_run(client, run_id), f"{self.entity_id} stop {run_id}"
            )
            raise
        if task in done:
            _LOGGER.info(
                "Hermes run %s answered within the fast window (%.1fs)",
                run_id,
                loop.time() - started,
            )
            return self._finish(request, task.result(), run_id, user_recorded=False)

        # Too slow: acknowledge now and deliver the result when it arrives.
        request.history.append({"role": "user", "content": request.text})
        self._pending[run_id] = task
        self._entry.async_create_background_task(
            self.hass,
            self._async_deliver_late(run_id, task, request),
            f"{self.entity_id} deliver {run_id}",
        )
        ack = select_ack(
            request.text,
            self._option(CONF_RICH_ACKS, DEFAULT_RICH_ACKS),
            self._option(CONF_ACK_TEXT, DEFAULT_ACK_TEXT),
        )
        _LOGGER.info(
            "Hermes run %s still running after %.1fs; acknowledged with %r",
            run_id,
            loop.time() - started,
            ack,
        )
        return ack

    def _finish(
        self, request: _Request, outcome: RunOutcome, run_id: str, *, user_recorded: bool
    ) -> str:
        """Turn a finished run into reply text, recording successful exchanges.

        On the ack path the user's message is recorded up front, so follow-ups
        made while the run is still going see it; the reply is added here.
        """
        if outcome.status == "completed":
            reply = outcome.output.strip() or "(empty response)"
            if not user_recorded:
                request.history.append({"role": "user", "content": request.text})
            request.history.append({"role": "assistant", "content": reply})
            _trim_history(request.history)
            return reply
        _LOGGER.error("Hermes run %s %s: %s", run_id, outcome.status, outcome.error)
        return TOO_LONG_TEXT if outcome.status == "timed_out" else FAILED_TEXT

    async def _async_deliver_late(
        self, run_id: str, task: asyncio.Task[RunOutcome], request: _Request
    ) -> None:
        """Wait for an acknowledged run and deliver its result."""
        try:
            outcome = await task
        finally:
            self._pending.pop(run_id, None)
        text = self._finish(request, outcome, run_id, user_recorded=True)
        if request.device_key and request.device_key in self._device_context:
            conversation_id, _ = self._device_context[request.device_key]
            self._device_context[request.device_key] = (conversation_id, time.monotonic())
        await async_deliver(
            self.hass,
            text,
            satellite_id=request.satellite_id,
            device_id=request.device_id,
            notify_target=self._option(CONF_NOTIFY_TARGET, DEFAULT_NOTIFY_TARGET),
            failed=outcome.status != "completed",
        )
        _LOGGER.info("Hermes run %s delivered (%s)", run_id, outcome.status)

    async def _async_process_sync(
        self, text: str, history: list[dict[str, str]], system_prompts: list[str]
    ) -> str:
        """The original blocking chat completions path."""
        timeout = self._option(CONF_TIMEOUT, DEFAULT_TIMEOUT)
        messages = [{"role": "system", "content": p} for p in system_prompts]
        messages.extend(history)
        messages.append({"role": "user", "content": text})
        started = time.monotonic()
        try:
            reply = await self._client().chat_completion(
                self._option(CONF_MODEL, DEFAULT_MODEL), messages, timeout
            )
        except TimeoutError:
            _LOGGER.warning("Hermes timed out after %ss", timeout)
            return "Hermes took too long to respond."
        except aiohttp.ClientResponseError as err:
            _LOGGER.error("Hermes HTTP %s: %s", err.status, err.message)
            return f"Hermes returned an error ({err.status})."
        except aiohttp.ClientError as err:
            _LOGGER.error("Hermes unreachable: %s", err)
            return "Hermes is not reachable right now."
        except (KeyError, ValueError, TypeError) as err:
            _LOGGER.error("Malformed response from Hermes: %s", err)
            return "Hermes returned a malformed response."
        _LOGGER.info(
            "Hermes chat completion answered in %.1fs", time.monotonic() - started
        )
        history.append({"role": "user", "content": text})
        history.append({"role": "assistant", "content": reply})
        _trim_history(history)
        return reply

    def _get_history(
        self, conversation_id: str, device_key: str | None
    ) -> list[dict[str, str]]:
        """LRU-cached per-conversation message history (no system prompt).

        A new conversation from a device that spoke recently continues that
        device's history, so follow-ups to a late result keep their context.
        """
        if conversation_id in self._history:
            self._history.move_to_end(conversation_id)
            return self._history[conversation_id]
        history: list[dict[str, str]] | None = None
        if device_key and (previous := self._device_context.get(device_key)):
            previous_id, last_seen = previous
            ttl = self._option(CONF_DEVICE_CONTEXT_TTL, DEFAULT_DEVICE_CONTEXT_TTL)
            if time.monotonic() - last_seen <= ttl:
                history = self._history.get(previous_id)
        if history is None:
            history = []
        self._history[conversation_id] = history
        while len(self._history) > MAX_TRACKED_CONVERSATIONS:
            self._history.popitem(last=False)
        return history


def _trim_history(history: list[dict[str, str]]) -> None:
    """Cap history at MAX_HISTORY_EXCHANGES user/assistant pairs."""
    max_msgs = MAX_HISTORY_EXCHANGES * 2
    if len(history) > max_msgs:
        del history[: len(history) - max_msgs]
