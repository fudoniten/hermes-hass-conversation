"""Tests for ack selection and speech shortening."""

from __future__ import annotations

from custom_components.jarvis_assistant.acks import select_ack
from custom_components.jarvis_assistant.delivery import shorten_for_speech


def test_rich_ack_matches_first_pattern() -> None:
    assert select_ack("Play some Mozart", True, "Okay.") == "Okay, getting that playing."
    assert select_ack("turn on the LIGHTS", True, "Okay.") == "Working on the lights."


def test_ack_falls_back() -> None:
    assert select_ack("tell me a joke", True, "Okay, on it.") == "Okay, on it."


def test_rich_acks_disabled() -> None:
    assert select_ack("play music", False, "Okay, on it.") == "Okay, on it."


def test_ack_needs_whole_word() -> None:
    assert select_ack("display the doorbell feed", True, "Fallback.") == "Fallback."


def test_shorten_strips_markdown() -> None:
    text = "## Result\n- **Playing** [Episode 12](https://x.y/z) on `media_player.den`"
    assert shorten_for_speech(text) == "Result Playing Episode 12 on media_player.den"


def test_shorten_cuts_at_sentence() -> None:
    text = "First sentence here. " + "Second sentence is much longer. " * 20
    assert shorten_for_speech(text, limit=60) == (
        "First sentence here. Second sentence is much longer."
    )


def test_shorten_cuts_long_single_sentence_at_word() -> None:
    text = "word " * 100
    short = shorten_for_speech(text, limit=22)
    assert short == "word word word word…"


def test_shorten_keeps_short_text() -> None:
    assert shorten_for_speech("Which speaker?") == "Which speaker?"
