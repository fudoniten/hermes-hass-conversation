"""Shared fixtures for Hermes Assist tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow loading custom_components/hermes_assist in every test."""
    return
