# Plan: Hermes Assist — async Home Assistant conversation agent for Hermes

**Status:** v0.1.0 implemented (see `custom_components/hermes_assist/`). Revised
from the original "Async Responses + Instant Acks for hass-hermes" spec after
reviewing upstream v0.3.0.

**Goal:** Voice and chat requests through Home Assistant's Assist pipeline get a
real answer quickly when Hermes is fast, a short spoken acknowledgment when it is
slow, and the real result delivered back to the device that asked once Hermes
finishes. No changes to Hermes Agent itself.

---

## 1. Background

### The problem

[`sj-unit72/hass-hermes`](https://github.com/sj-unit72/hass-hermes) (v0.3.0,
domain `hermes`, MIT) calls Hermes' synchronous `POST /v1/chat/completions` and
waits up to `timeout` seconds (default 60). Capable requests ("play episode X of
podcast Y on a speaker you've never used") regularly take longer. The user stands
in silence for a minute and then hears "Hermes took too long to respond."

Hermes delivers exactly one message per turn, after all tool calls complete, so
interim output can't come from Hermes. The async behaviour must live in the HA
integration.

### What upstream looks like

About 370 lines total:

| File | Lines | Notes |
|---|---|---|
| `__init__.py` | 26 | Forwards to the `conversation` platform; **reloads the entry on any options change** |
| `config_flow.py` | 143 | URL + options (model, api_key, timeout, system_prompt) |
| `const.py` | 30 | Defaults, system prompt, history limits |
| `conversation.py` | 173 | `ConversationEntity`; per-`conversation_id` LRU history (10 exchanges, 50 conversations); sync POST; error strings |

There is **no `tests/` directory** upstream; tests are written from scratch here.

### Hermes API (already available)

- `POST /v1/runs` → `202 {"run_id": "...", "status": "started"}`
- `GET /v1/runs/{run_id}` → `{"status": "queued|running|completed|failed|cancelled", "output": "...", ...}`
- `GET /v1/runs/{run_id}/events` → SSE lifecycle events (not used in v1; polling is simpler)
- `POST /v1/runs/{run_id}/stop` → cancel
- Auth: `Authorization: Bearer <api_key>` (same key as chat/completions)

**Request body (resolved from Hermes' `gateway/platforms/api_server_runs.py`):**

- `input` — the new user message as a string, or a message list whose last
  entry is the user message and earlier entries become history.
- `instructions` — an ephemeral system prompt for this run.
- `conversation_history` — explicit prior `{role, content}` turns; takes
  precedence over everything else.
- `session_id` (body) or `X-Hermes-Session-Key` (header) — make Hermes load
  and persist history for a session on its side.
- Terminal statuses: `completed`, `failed`, `cancelled`, `interrupted`.
  Non-terminal: `queued`, `running`, `waiting_for_approval`, `stopping`.

v0.1.0 sends `input` (string) + `instructions` + `conversation_history` from the
integration's own history, so context handling is deterministic and identical to
the sync path. Hermes-side sessions are left for later (§4.3).

---

## 2. Decisions

| Decision | Choice | Why |
|---|---|---|
| Fork vs. fresh repo | **Fresh repo (this one), seeded from upstream code**, MIT attribution to Serge Jespers kept in `LICENSE` and README | Upstream is tiny; the new code will be several times its size and isn't going back upstream as small patches |
| Domain | **`hermes_assist`** (name "Hermes Assist") | Can be installed alongside the upstream `hermes` integration for A/B testing on one pipeline, then the old one removed. Cost: re-enter URL + API key once |
| Local intent handling | **Not implemented in the agent.** Use HA's built-in pipeline option **"Prefer handling commands locally"** | HA already does this per pipeline; doing it inside the agent was the riskiest item in the original spec and adds nothing |
| Late-result delivery | **Integration-side** via `assist_satellite.announce` / `assist_satellite.start_conversation` | Deterministic; doesn't rely on the model remembering to announce |
| Waiting on runs | Poll `GET /v1/runs/{id}` every 1 s | Simpler than SSE; good enough |
| Ack wording | Regex table in `const.py`, no classifier | Cheap, predictable, editable |
| Streaming the ack + result in one turn | **Not in v1** | Keeps the satellite busy for the whole run and still subject to pipeline limits; race-and-announce is more robust |

---

## 3. Request flow

```
Assist pipeline ─► HermesAssistEntity.async_process(user_input)
                    │
                    ├─ use_async off, or /v1/runs known-unsupported ─► legacy sync path (upstream behaviour)
                    │
                    ├─ POST /v1/runs  ──(non-202)──► log, mark unsupported if 404/501, legacy sync path
                    │
                    ├─ race: poll until completed/failed or fast_window elapses
                    │     ├─ completed in window ─► return real answer (normal reply), record history
                    │     ├─ failed in window    ─► return failure text
                    │     └─ window elapsed      ─► return ack text, hand run to background task
                    │
                    └─ background task (entry.async_create_background_task)
                          ├─ poll until terminal or max_wait
                          ├─ max_wait exceeded ─► POST /stop, deliver "taking too long" message
                          ├─ failed            ─► deliver "Sorry, that didn't work" message
                          └─ completed         ─► record history, deliver result (§6)
```

`fast_window = 0` means always ack immediately.

---

## 4. Context and follow-ups

This is the main gap in the original spec.

1. **History records real results, not acks.** On the ack path, record the user
   message immediately and append the assistant message when the background run
   completes. Never store the ack text as the assistant turn.
2. **Per-device context fallback.** After a late announcement, the user's next
   utterance ("now pause it") normally arrives with a new `conversation_id`.
   Maintain a second map keyed by satellite/device ID (`user_input.satellite_id`,
   else `user_input.device_id`) with a TTL (`device_context_ttl`, default 600 s).
   When a request arrives with an unknown `conversation_id` but a known, unexpired
   device key, continue that device's history.
3. **Hermes session ID (future).** Hermes accepts `session_id` /
   `X-Hermes-Session-Key`, which would let it keep per-device context (and its
   long-term memory) itself. Not used in v0.1.0: the local history plus
   `conversation_history` already covers follow-ups.
4. **Voice brevity.** When the request came from a satellite, add a system-prompt
   line telling Hermes the answer will be spoken and must be one or two sentences.

---

## 5. Acknowledgments

Ordered regex table in `const.py`; first case-insensitive match wins; fallback is
`ack_text` (default "Okay, on it.").

```python
ACK_PATTERNS: list[tuple[str, str]] = [
    (r"\b(play|music|song|album|artist|podcast|spotify)\b", "Okay, getting that playing."),
    (r"\b(garage)\b", "On it — dealing with the garage."),
    (r"\b(light|lights|lamp)\b", "Working on the lights."),
    (r"\b(lock|locks|door)\b", "Checking the doors and locks."),
    (r"\b(weather|temperature|outside)\b", "Let me check."),
    (r"\b(vacuum)\b", "Sending the vacuum out."),
    (r"\b(morning|goodnight|bedtime|routine)\b", "Starting that up."),
]
```

Option `rich_acks` (default on) — off means always use `ack_text`.

Acks are never retracted. If the run finishes one second after the ack, the
result is still delivered via §6; a slight overlap is acceptable.

---

## 6. Delivering late results

Resolve services at call time via `hass.services.async_call`; never cache them.

1. **The satellite that asked.** Find the `assist_satellite` entity for
   `user_input.satellite_id` (entity ID) or for `user_input.device_id` (look up
   the device's `assist_satellite` entity in the entity registry).
   - If the result **ends with a question** (`?` after stripping), call
     `assist_satellite.start_conversation` with the spoken text so the user can
     answer without the wake word.
   - Otherwise call `assist_satellite.announce`.
   - The spoken text is the voice-shortened result (below).
2. **Mobile app notification** if `notify_target` is set
   (e.g. `mobile_app_pixel_11_pro` → `notify.mobile_app_pixel_11_pro`), with the
   full text.
3. **Persistent notification** (`persistent_notification.create`, title "Hermes")
   **only** when the run failed/timed out, or when no satellite could be found and
   no `notify_target` is set. Not for every successful result.

**Voice shortening:** strip markdown, then truncate at a sentence boundary to
~250 characters. This is a backstop; §4.4 asks Hermes to keep spoken answers short
in the first place. Notifications always get the full text.

If a delivery service call raises, log it and continue to the next step.

---

## 7. Configuration

Config flow (setup): `url`, `api_key`. Options flow (all keep working on reload):

| Option | Type / default | Notes |
|---|---|---|
| `model` | str, `hermes-agent` | From upstream |
| `system_prompt` | str, upstream default | From upstream |
| `use_async` | bool, `True` | Off = exact upstream sync behaviour |
| `timeout` | int s, `60` | Relabel "Sync timeout"; used only on the sync path |
| `fast_window` | int s, `7`, range 0–30 | Race window; 0 = always ack |
| `max_wait` | int s, `240`, range 30–1800 | Hard cap for background runs |
| `rich_acks` | bool, `True` | |
| `ack_text` | str, `Okay, on it.` | |
| `notify_target` | str, `""` | Mobile app service suffix; empty = skip |
| `device_context_ttl` | int s, `600` | §4.2 |

---

## 8. Module layout

```
custom_components/hermes_assist/
  __init__.py        setup/unload, platform forwarding (no run logic here)
  manifest.json      domain hermes_assist, version 0.1.0, codeowners, dependencies: conversation, assist_satellite (after_dependencies)
  const.py           options, defaults, ACK_PATTERNS, paths
  config_flow.py     setup + options flow
  conversation.py    entity, history/device context, race orchestration
  client.py          thin aiohttp client: start_run, get_run, stop_run, chat_completions
  runner.py          race + background polling, max_wait, cancellation
  delivery.py        satellite lookup, announce/start_conversation, notify, persistent notification, voice shortening
  acks.py            ack selection
  strings.json, translations/en.json
tests/               pytest-homeassistant-custom-component
hacs.json
.github/workflows/   hassfest + HACS validation + pytest
```

### Lifecycle notes

- Background tasks via `entry.async_create_background_task`, tracked in an
  instance dict `{run_id: Task}`.
- On unload (including the reload upstream triggers on every options change):
  cancel tasks and best-effort `POST /stop` for in-flight runs. Document that
  changing options mid-run cancels it.
- HA restart mid-run: runs complete on the Hermes side and the result is lost on
  the HA side. Acceptable for v1; documented.
- `/v1/runs` 404/501 → remember "unsupported" for this entry's lifetime (no
  flapping); 401/5xx/network errors → sync fallback for this request only.

---

## 9. Implementation order

1. ~~Verify `/v1/runs` request schema~~ — resolved from Hermes' source (§1).
2. Seed from upstream: copy files, rename domain, keep MIT attribution, add
   `hacs.json`, CI.
3. `client.py` + `runner.py` with unit tests (fake Hermes via `aioclient_mock`).
4. Wire race into `conversation.py`; history rules (§4.1).
5. `delivery.py` + tests (service-call assertions).
6. Device context fallback + session ID (§4.2–4.3).
7. Options flow, strings, README.
8. Tag `v0.1.0` for HACS.

---

## 10. Testing

Automated (pytest):

1. Fast run completes inside window → real answer returned, no announce.
2. Slow run → ack returned; later `assist_satellite.announce` called on the right entity.
3. Late result ending in `?` → `assist_satellite.start_conversation` instead.
4. `rich_acks`: "play …" → music ack; "tell me a joke" → fallback.
5. `/v1/runs` 404 → sync fallback, and no second `/v1/runs` attempt afterward.
6. Run `failed` → failure delivered (satellite + persistent notification).
7. `max_wait` exceeded → `/stop` called, timeout message delivered.
8. Two concurrent requests from two devices → independent acks and deliveries.
9. Follow-up with new `conversation_id` from same device within TTL → previous history included.
10. Unload with an in-flight run → task cancelled, `/stop` called.
11. `use_async` off → upstream v0.3.0 request flow (chat completions). The one
    difference: the voice-brevity prompt (§4.4) is added on both paths.

Manual (on the Kitchenette PE, "Hey Jarvis"):

- Fast question ("what's the temperature in the theater?") → direct answer, no ack.
- Slow request ("play some Mozart in the media room") → ack, then announcement. Time both legs.
- Clarifying question from Hermes → satellite listens for the reply.
- "Now pause it" right after a late result → Hermes has context.
- Point at a Hermes build without `/v1/runs` → degrades to sync with no errors.
- Watch logs for info lines: run started / acked / delivered.

---

## 11. Installation (for the README)

- **HACS:** HACS → ⋮ → Custom repositories → add this repo URL, type
  "Integration" → install → restart HA. Requires the repo to be public and a
  tagged release.
- **Manual:** copy `custom_components/hermes_assist/` into
  `/config/custom_components/` and restart.
- Then: Settings → Devices & Services → Add Integration → Hermes Assist.
- Voice assistants → pipeline → Conversation agent: **Hermes Assist**; enable
  **"Prefer handling commands locally"**.

## 12. Out of scope

- Changes to Hermes Agent core.
- Hermes pushing results to HA itself (would need a Hermes delivery hook).
- Wake word / STT / TTS changes.
- Persisting in-flight runs across HA restarts.
