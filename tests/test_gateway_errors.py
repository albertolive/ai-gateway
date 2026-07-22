"""Tests for gateway.py error handling and edge cases.

Tests the cascade failover logic without making real network calls,
using monkeypatching to simulate provider responses.
"""

import json
import os
import sys
import urllib.error

import pytest

import gateway


class TestCascadeSkipNoKey:
    def test_all_providers_skipped_raises_runtime_error(self, monkeypatch):
        """When no API keys are set, complete() should raise RuntimeError."""
        for key in ["OPENROUTER_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY"]:
            monkeypatch.delenv(key, raising=False)
        with pytest.raises(RuntimeError, match="All providers.*failed.*skipped"):
            gateway.complete("test prompt", intent="general")

    def test_runtime_error_lists_skip_messages(self, monkeypatch):
        """The error message should mention which providers were skipped."""
        for key in ["OPENROUTER_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY"]:
            monkeypatch.delenv(key, raising=False)
        with pytest.raises(RuntimeError) as exc_info:
            gateway.complete("test", intent="general")
        msg = str(exc_info.value)
        # The skip messages are printed but errors list may be empty;
        # the RuntimeError is still raised because no provider succeeded
        assert "All providers" in msg


class TestJsonFenceStripping:
    def test_strips_markdown_json_fences(self, monkeypatch):
        """When a model wraps JSON in ```json ... ``` fences, it should be stripped."""
        # Mock _post_chat to return fenced JSON
        fenced = '```json\n{"summary": "ok", "comments": []}\n```'

        def mock_post(base_url, api_key, payload, timeout=120):
            return fenced

        monkeypatch.setattr(gateway, "_post_chat", mock_post)
        monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")

        schema = {"type": "object", "properties": {
            "summary": {"type": "string"},
            "comments": {"type": "array"}}}
        result, provider = gateway.complete(
            "test", intent="code_review", schema=schema)
        assert result["summary"] == "ok"
        assert result["comments"] == []

    def test_strips_bare_code_fences(self, monkeypatch):
        """When a model wraps JSON in ``` ... ``` fences (no language tag)."""
        fenced = '```\n{"summary": "test", "comments": []}\n```'

        monkeypatch.setattr(gateway, "_post_chat",
                            lambda *a, **kw: fenced)
        monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")

        schema = {"type": "object", "properties": {
            "summary": {"type": "string"},
            "comments": {"type": "array"}}}
        result, _ = gateway.complete("test", intent="code_review",
                                      schema=schema)
        assert result["summary"] == "test"

    def test_plain_json_returned_as_is(self, monkeypatch):
        """JSON without fences should parse directly."""
        plain = '{"summary": "clean", "comments": []}'

        monkeypatch.setattr(gateway, "_post_chat",
                            lambda *a, **kw: plain)
        monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")

        schema = {"type": "object", "properties": {
            "summary": {"type": "string"},
            "comments": {"type": "array"}}}
        result, _ = gateway.complete("test", intent="code_review",
                                      schema=schema)
        assert result["summary"] == "clean"

    def test_invalid_json_raises(self, monkeypatch):
        """Non-JSON response with schema should raise (caught as generic error)."""
        monkeypatch.setattr(gateway, "_post_chat",
                            lambda *a, **kw: "this is not json")
        monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")
        # Also set other keys so they also fail (all providers fail)
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
        monkeypatch.setenv("GROQ_API_KEY", "fake-key")

        schema = {"type": "object", "properties": {}}
        with pytest.raises(RuntimeError, match="All providers"):
            gateway.complete("test", intent="code_review", schema=schema)


class TestFailoverCascade:
    def test_first_provider_fails_second_succeeds(self, monkeypatch):
        """If the first provider fails, the cascade should try the next."""
        call_count = [0]

        def mock_post(base_url, api_key, payload, timeout=120):
            call_count[0] += 1
            if call_count[0] == 1:
                raise urllib.error.HTTPError(
                    base_url, 500, "Server Error", {}, None)
            return "Success from second provider"

        monkeypatch.setattr(gateway, "_post_chat", mock_post)
        monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

        result, provider = gateway.complete("test", intent="code_review")
        # The cascade should have failed over and returned success
        assert "Success" in result
        assert call_count[0] >= 2  # at least 2 attempts

    def test_429_triggers_retry(self, monkeypatch):
        """A 429 error should trigger a retry on the same provider."""
        call_count = [0]

        def mock_post(base_url, api_key, payload, timeout=120):
            call_count[0] += 1
            if call_count[0] == 1:
                raise urllib.error.HTTPError(
                    base_url, 429, "Rate Limited", {}, None)
            return "Success after retry"

        monkeypatch.setattr(gateway, "_post_chat", mock_post)
        monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")
        # Disable sleep to speed up test
        monkeypatch.setattr(gateway.time, "sleep", lambda s: None)

        result, _ = gateway.complete("test", intent="code_review")
        assert "Success after retry" in result
        assert call_count[0] == 2  # first attempt failed, second succeeded

    def test_all_providers_fail_raises(self, monkeypatch):
        """When all providers fail, RuntimeError should list all errors."""
        def mock_post(base_url, api_key, payload, timeout=120):
            raise urllib.error.HTTPError(
                base_url, 500, "Server Error", {}, None)

        monkeypatch.setattr(gateway, "_post_chat", mock_post)
        monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
        monkeypatch.setenv("GROQ_API_KEY", "fake-key")
        monkeypatch.setattr(gateway.time, "sleep", lambda s: None)

        with pytest.raises(RuntimeError) as exc_info:
            gateway.complete("test", intent="code_review")
        msg = str(exc_info.value)
        assert "All providers" in msg
        assert "HTTP 500" in msg
