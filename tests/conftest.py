"""Shared pytest configuration — adds scripts/ to sys.path once.

This removes the need for repeated sys.path.insert boilerplate in every
test file.
"""
import os
import sys

import pytest

_scripts = os.path.join(os.path.dirname(__file__), "..", "scripts")
if _scripts not in sys.path:
    sys.path.insert(0, _scripts)

import gateway  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_circuit_breaker():
    gateway._circuit_state["failures"] = 0
    gateway._circuit_state["opened_at"] = 0.0
    yield
    gateway._circuit_state["failures"] = 0
    gateway._circuit_state["opened_at"] = 0.0
