"""Shared fixtures for Jarvis Assistant tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow loading custom_components/jarvis_assistant in every test."""
    return
