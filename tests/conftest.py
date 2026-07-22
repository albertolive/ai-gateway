"""Shared pytest configuration — adds scripts/ to sys.path once.

This removes the need for repeated sys.path.insert boilerplate in every
test file.
"""
import os
import sys

_scripts = os.path.join(os.path.dirname(__file__), "..", "scripts")
if _scripts not in sys.path:
    sys.path.insert(0, _scripts)
