"""Constants for the Jarvis Assistant integration."""

from __future__ import annotations

DOMAIN = "jarvis_assistant"

CONF_URL = "url"
CONF_TIMEOUT = "timeout"
CONF_MODEL = "model"
CONF_API_KEY = "api_key"
CONF_SYSTEM_PROMPT = "system_prompt"
CONF_USE_ASYNC = "use_async"
CONF_FAST_WINDOW = "fast_window"
CONF_MAX_WAIT = "max_wait"
CONF_RICH_ACKS = "rich_acks"
CONF_ACK_TEXT = "ack_text"
CONF_NOTIFY_TARGET = "notify_target"
CONF_DEVICE_CONTEXT_TTL = "device_context_ttl"

DEFAULT_URL = "http://192.168.1.100:8642"
DEFAULT_TIMEOUT = 60
DEFAULT_MODEL = "hermes-agent"
DEFAULT_SYSTEM_PROMPT = (
    "You are Jarvis, a smart home voice assistant. "
    "Keep responses short and natural for spoken output. "
    "You can control Home Assistant devices when the user asks about one or requests an action. "
    "When the user's utterance is a routine name (good night, good morning, bedtime) or states "
    "a routine already ran, do NOT call tools to verify or re-run it: the Home Assistant script "
    "executes the actions deterministically and guards against redundant commands. "
    "Just reply conversationally."
)
DEFAULT_USE_ASYNC = True
DEFAULT_FAST_WINDOW = 7
DEFAULT_MAX_WAIT = 240
DEFAULT_RICH_ACKS = True
DEFAULT_ACK_TEXT = "Okay, on it."
DEFAULT_NOTIFY_TARGET = ""
DEFAULT_DEVICE_CONTEXT_TTL = 600

# Added to the system prompt when the request came from a voice satellite.
VOICE_SYSTEM_PROMPT = (
    "This request came from a voice satellite and your answer will be spoken aloud. "
    "Reply in one or two short sentences, without markdown, lists or links."
)

# Ordered (regex, ack) pairs; the first case-insensitive match wins.
ACK_PATTERNS: list[tuple[str, str]] = [
    (r"\b(play|music|song|album|artist|podcast|spotify)\b", "Okay, getting that playing."),
    (r"\b(garage)\b", "On it — dealing with the garage."),
    (r"\b(light|lights|lamp)\b", "Working on the lights."),
    (r"\b(lock|locks|door)\b", "Checking the doors and locks."),
    (r"\b(weather|temperature|outside)\b", "Let me check."),
    (r"\b(vacuum)\b", "Sending the vacuum out."),
    (r"\b(morning|goodnight|bedtime|routine)\b", "Starting that up."),
]

FAILED_TEXT = "Sorry, that didn't work. Check the Home Assistant logs for details."
TOO_LONG_TEXT = "That's taking longer than expected, so I've stopped working on it."
NOTIFICATION_TITLE = "Jarvis"

# Maximum length of text spoken on a satellite; longer results are cut at a
# sentence boundary. Notifications always carry the full text.
MAX_SPOKEN_CHARS = 250

POLL_INTERVAL = 1.0

MAX_HISTORY_EXCHANGES = 10
MAX_TRACKED_CONVERSATIONS = 50

CHAT_COMPLETIONS_PATH = "/v1/chat/completions"
MODELS_PATH = "/v1/models"
RUNS_PATH = "/v1/runs"
