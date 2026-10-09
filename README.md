# Hermes Assist for Home Assistant

A Home Assistant conversation agent for [Hermes Agent](https://github.com/NousResearch/hermes-agent)
that stays responsive when Hermes is slow.

Hermes can take well over a minute to finish a capable request ("play the latest
episode of that podcast on the media room speaker"). Hermes Assist starts each
request as an async run and waits a few seconds:

- **Hermes answers within the window** → you hear the answer, as with any agent.
- **Hermes is still working** → you hear a short acknowledgment ("Okay, getting
  that playing."), and the result is announced on the same voice satellite when
  it's ready. If the result is a question ("Pocket Casts or Spotify?"), the
  satellite listens for your answer without needing the wake word.

Based on [`sj-unit72/hass-hermes`](https://github.com/sj-unit72/hass-hermes) by
Serge Jespers (MIT). See [`docs/PLAN.md`](docs/PLAN.md) for the design.

## Requirements

- Home Assistant 2025.4 or newer
- A Hermes Agent API server reachable from Home Assistant. Async mode uses
  `POST /v1/runs`; against an older Hermes without it, the integration falls
  back to `POST /v1/chat/completions` automatically.

## Installation

**HACS:** HACS → ⋮ → Custom repositories → add this repository's URL with type
**Integration** → install **Hermes Assist** → restart Home Assistant. (HACS needs
the repository to be public.)

**Manual:** copy `custom_components/hermes_assist/` into
`/config/custom_components/` and restart Home Assistant.

Then:

1. Settings → Devices & services → Add integration → **Hermes Assist**. Enter the
   Hermes URL (for example `http://jarvis:8642`) and API key.
2. Settings → Voice assistants → your pipeline → **Conversation agent**: Hermes
   Assist.
3. In the same pipeline, turn on **Prefer handling commands locally**, so simple
   commands ("turn off the kitchen lights") are handled by Home Assistant in under
   a second and only the rest goes to Hermes.

Hermes Assist uses its own domain (`hermes_assist`), so it can be installed
alongside the original `hermes` integration while you compare them.

## Options

Settings → Devices & services → Hermes Assist → Configure.

| Option | Default | What it does |
|---|---|---|
| Use async runs | on | Off uses the original blocking chat completions call |
| Fast window | 7 s | How long to wait for a real answer before acknowledging. 0 always acknowledges |
| Maximum wait | 240 s | Slow requests still running after this are stopped, and you're told so |
| Request-specific acknowledgments | on | Picks an acknowledgment from the request (music, lights, locks…); off always uses the default |
| Default acknowledgment | "Okay, on it." | |
| Notify service for results | empty | e.g. `mobile_app_pixel_11_pro`; also sends the full result to your phone |
| Keep follow-up context per device | 600 s | A new conversation on the same satellite within this time continues the previous one, so "now pause it" works after a late result |
| Sync timeout | 60 s | Only used when async runs are off or unavailable |
| Model, API key, system prompt | | As in the original integration |

The acknowledgment patterns live in `ACK_PATTERNS` in
`custom_components/hermes_assist/const.py`.

## How late results are delivered

1. **The satellite that asked:** `assist_satellite.announce`, or
   `assist_satellite.start_conversation` if the result ends with a question.
   Spoken text is stripped of markdown and cut to about 250 characters at a
   sentence boundary.
2. **Your phone**, if a notify service is set, with the full text.
3. **A persistent notification** when the request failed or timed out, or when
   neither of the above was possible (for example a request typed in the chat
   panel).

## Limitations

- If Home Assistant restarts, or you change Hermes Assist's options, while a slow
  request is running, that request is stopped and its result is not delivered.
- A Hermes run waiting for a tool approval will hit the maximum wait unless it's
  approved on the Hermes side.
- Conversation history is kept in memory (last 10 exchanges per conversation).

## Troubleshooting

Turn on Hermes Assist's logs in `configuration.yaml` and restart:

```yaml
logger:
  default: warning
  logs:
    custom_components.hermes_assist: info   # use debug for raw run records and events
```

Each request then logs its whole life, for example:

```
Hermes run run_ab12 started in 0.1s for 'turn off the den lights' (0 history messages, device assist_satellite.den)
Hermes run run_ab12 is running after 0.1s (last event: none)
Hermes run run_ab12: tool ha_call_service started at 3.2s: light.turn_off den
Hermes run run_ab12: tool ha_call_service finished in 0.4s at 3.6s: ...
Hermes run run_ab12 is completed after 5.0s (last event: tool.completed)
Hermes run run_ab12 answered within the fast window (5.0s)
```

What to look for when a request is slow or times out:

- **`is waiting for a tool approval`** — Hermes paused the run until someone
  approves a tool call. `/v1/runs` runs wait for an answer (or Hermes' approval
  timeout), unlike the blocking chat completions call. The log line shows what
  needs approving; adjust Hermes' approval settings for that tool.
- **A tool that started but never finished**, or one with a long duration —
  the slow part is inside Hermes or the service it calls.
- **`gave up after …`** — the summary names the last status and event seen.
- **`Polling Hermes run … failed`** — Home Assistant can't reach Hermes' status
  endpoint.
- **A run that stays `queued`** — Hermes hasn't started it; check the Hermes
  gateway logs.

## Development

```bash
pip install -r requirements_test.txt hassil home-assistant-intents
pytest
```
