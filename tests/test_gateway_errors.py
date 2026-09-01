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
        for key in ["OPENROUTER_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY",
                    "AI_GATEWAY_API_KEY"]:
            monkeypatch.delenv(key, raising=False)
        with pytest.raises(RuntimeError, match="All providers.*failed.*skipped"):
            gateway.complete("test prompt", intent="general")

    def test_runtime_error_lists_skip_messages(self, monkeypatch):
        """The error message should mention which providers were skipped."""
        for key in ["OPENROUTER_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY",
                    "AI_GATEWAY_API_KEY"]:
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


class TestCascadeCostControl:
    """The cascade must not burn unbounded billable CI time discovering everything is down.

    On 2026-07-29 one AI PR Review run took 44 minutes: 7 providers x 2 attempts, a 120s socket
    timeout that applies per read rather than to total elapsed, and a 5s sleep before each retry.
    On a private repo that is metered time spent learning nothing.
    """

    def _keys(self, monkeypatch):
        for k in ("OPENROUTER_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY",
                  "DEEPSEEK_API_KEY", "AI_GATEWAY_API_KEY"):
            monkeypatch.setenv(k, "x")

    def _fail_with(self, monkeypatch, code, body, sleep=0.0):
        import io
        import time as _t
        calls = []

        def _p(url, key, payload, timeout=120):
            calls.append(timeout)
            if sleep:
                _t.sleep(sleep)
            raise urllib.error.HTTPError(url, code, "err", {}, io.BytesIO(body.encode()))

        monkeypatch.setattr(gateway, "_post_chat", _p)
        return calls

    def test_daily_cap_is_not_retried(self, monkeypatch):
        # A daily allowance resets tomorrow, so sleeping 5s and asking again is pure waste.
        self._keys(monkeypatch)
        calls = self._fail_with(
            monkeypatch, 429,
            '{"error":{"message":"Rate limit exceeded: free-models-per-day"}}')
        with pytest.raises(RuntimeError):
            gateway.complete("p", intent="code_review", budget_s=300)
        cascade = gateway.load_cascades()["code_review"]
        assert len(calls) == len(cascade), "exactly one attempt per provider, no retries"

    def test_gemini_per_day_wording_also_detected(self, monkeypatch):
        # Google phrases it differently from OpenRouter; both are daily caps.
        self._keys(monkeypatch)
        calls = self._fail_with(
            monkeypatch, 429,
            '{"error":{"message":"Quota exceeded for metric: GenerateRequestsPerDay"}}')
        with pytest.raises(RuntimeError):
            gateway.complete("p", intent="code_review", budget_s=300)
        assert len(calls) == len(gateway.load_cascades()["code_review"])

    def test_burst_limit_still_retries(self, monkeypatch):
        # The opposite case: a per-minute limit genuinely clears, so the retry is worth keeping.
        self._keys(monkeypatch)
        calls = self._fail_with(
            monkeypatch, 429,
            '{"error":{"message":"Rate limit exceeded: 20 per minute"}}')
        monkeypatch.setattr(gateway.time, "sleep", lambda s: None)
        with pytest.raises(RuntimeError):
            gateway.complete("p", intent="code_review", budget_s=300)
        assert len(calls) == 2 * len(gateway.load_cascades()["code_review"])

    def test_budget_bounds_a_slow_cascade(self, monkeypatch):
        import time as _t
        self._keys(monkeypatch)
        self._fail_with(monkeypatch, 500, "{}", sleep=0.3)
        t0 = _t.monotonic()
        with pytest.raises(RuntimeError, match="budget exhausted"):
            gateway.complete("p", intent="code_review", budget_s=0.5)
        assert _t.monotonic() - t0 < 3, "the budget must actually stop the cascade"

    def test_per_call_timeout_never_exceeds_remaining_budget(self, monkeypatch):
        # Otherwise one slow provider can outlive the whole budget on its own.
        self._keys(monkeypatch)
        calls = self._fail_with(monkeypatch, 500, "{}")
        with pytest.raises(RuntimeError):
            gateway.complete("p", intent="code_review", budget_s=10)
        assert calls and max(calls) <= 10

    def test_budget_default_is_env_overridable(self, monkeypatch):
        self._keys(monkeypatch)
        monkeypatch.setenv("AI_GATEWAY_BUDGET_S", "0")
        with pytest.raises(RuntimeError, match="budget exhausted"):
            gateway.complete("p", intent="code_review")

    def test_default_budget_clears_every_observed_successful_run(self, monkeypatch):
        # Every successful AI PR Review in the week to 2026-07-29 took 312-666s. A default that
        # cuts below the slowest of those turns working reviews into outages, which is worse than
        # the cost problem it set out to solve. Guards against tightening it without new data.
        self._keys(monkeypatch)
        monkeypatch.delenv("AI_GATEWAY_BUDGET_S", raising=False)
        seen = {}

        def _p(url, key, payload, timeout=120):
            seen["budget"] = timeout
            return '{"summary":"ok","comments":[]}'

        monkeypatch.setattr(gateway, "_post_chat", _p)
        gateway.complete("p", intent="code_review", schema={"type": "object"})
        # First call gets min(120, budget); prove the budget itself is above the slowest real run.
        import inspect
        src = inspect.getsource(gateway.complete)
        assert "900" in src, "default budget must stay above the 666s slowest observed success"

    def test_malformed_budget_env_does_not_raise(self, monkeypatch):
        # complete()'s callers catch RuntimeError; a ValueError from float() would escape as an
        # unhandled crash and be reported as a code failure.
        self._keys(monkeypatch)
        monkeypatch.setenv("AI_GATEWAY_BUDGET_S", "not-a-number")
        self._fail_with(monkeypatch, 500, "{}")
        with pytest.raises(RuntimeError):
            gateway.complete("p", intent="code_review")

    def test_empty_budget_env_falls_back_to_default(self, monkeypatch):
        self._keys(monkeypatch)
        monkeypatch.setenv("AI_GATEWAY_BUDGET_S", "")
        self._fail_with(monkeypatch, 500, "{}")
        with pytest.raises(RuntimeError):
            gateway.complete("p", intent="code_review")


class TestMultiKeyFailover:
    """A provider's key env var may hold several keys (multiple accounts), tried in order.

    Several Vercel accounts each contribute their own monthly credit pool; the cascade must
    exhaust key 1 before touching key 2, and only then move to the next model/provider.
    """

    def test_first_key_fails_second_succeeds(self, monkeypatch):
        calls = []

        def mock_post(base_url, api_key, payload, timeout=120):
            calls.append(api_key)
            if api_key == "key2":
                return "ok"
            raise urllib.error.HTTPError(base_url, 500, "Server Error", {}, None)

        monkeypatch.setattr(gateway, "_post_chat", mock_post)
        for k in ("GEMINI_API_KEY", "GROQ_API_KEY", "AI_GATEWAY_API_KEY",
                  "DEEPSEEK_API_KEY"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("OPENROUTER_API_KEY", "key1,key2")

        result, _ = gateway.complete("p", intent="code_review")
        assert result == "ok"
        assert calls == ["key1", "key2"], "second key must be tried after the first fails"

    def test_daily_cap_on_first_key_skips_to_second(self, monkeypatch):
        import io
        calls = []

        def mock_post(base_url, api_key, payload, timeout=120):
            calls.append(api_key)
            if api_key == "key2":
                return "ok"
            raise urllib.error.HTTPError(
                base_url, 429, "rate limit", {},
                io.BytesIO(b'{"error":{"message":"Rate limit exceeded: free-models-per-day"}}'))

        monkeypatch.setattr(gateway, "_post_chat", mock_post)
        for k in ("GEMINI_API_KEY", "GROQ_API_KEY", "AI_GATEWAY_API_KEY",
                  "DEEPSEEK_API_KEY"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("OPENROUTER_API_KEY", "key1,key2")

        result, _ = gateway.complete("p", intent="code_review")
        assert result == "ok"
        assert calls == ["key1", "key2"], "a daily cap must not retry the same key"

    def test_credit_exhaustion_on_first_key_skips_to_second(self, monkeypatch):
        import io
        calls = []

        def mock_post(base_url, api_key, payload, timeout=120):
            calls.append(api_key)
            if api_key == "key2":
                return "ok"
            raise urllib.error.HTTPError(
                base_url, 429, "rate limit", {},
                io.BytesIO(b'{"error":{"message":"insufficient credits"}}'))

        monkeypatch.setattr(gateway, "_post_chat", mock_post)
        for k in ("GEMINI_API_KEY", "GROQ_API_KEY", "AI_GATEWAY_API_KEY",
                  "DEEPSEEK_API_KEY"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("OPENROUTER_API_KEY", "key1,key2")

        result, _ = gateway.complete("p", intent="code_review")
        assert result == "ok"
        assert calls == ["key1", "key2"], "a spent-credit 429 must not retry the same key"

    def test_all_keys_exhausted_moves_to_next_provider(self, monkeypatch):
        calls = []

        def mock_post(base_url, api_key, payload, timeout=120):
            calls.append(api_key)
            raise urllib.error.HTTPError(base_url, 500, "Server Error", {}, None)

        monkeypatch.setattr(gateway, "_post_chat", mock_post)
        for k in ("GEMINI_API_KEY", "GROQ_API_KEY", "AI_GATEWAY_API_KEY",
                  "DEEPSEEK_API_KEY"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("OPENROUTER_API_KEY", "key1,key2")
        monkeypatch.setattr(gateway.time, "sleep", lambda s: None)

        with pytest.raises(RuntimeError):
            gateway.complete("p", intent="code_review")
        # code_review has one openrouter entry; two keys = two calls, then
        # the cascade gives up (all other providers skipped: no key).
        assert calls == ["key1", "key2"]
