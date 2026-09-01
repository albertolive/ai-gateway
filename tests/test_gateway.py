"""Tests for scripts/gateway.py — cascade loading and provider config."""

import json
import os
import tempfile

import pytest

import gateway


class TestLoadCascades:
    def test_loads_from_models_json(self):
        cascades = gateway.load_cascades()
        assert "code_review" in cascades
        assert "general" in cascades

    def test_code_review_has_ordered_entries(self):
        cascades = gateway.load_cascades()
        cr = cascades["code_review"]
        assert len(cr) >= 3
        # Each entry has required keys
        for e in cr:
            assert "name" in e
            assert "url" in e
            assert "key_env" in e
            assert "model" in e
            assert "structured" in e

    def test_general_cascade_openrouter_free_last(self):
        cascades = gateway.load_cascades()
        gen = cascades["general"]
        # openrouter/free should be the safety net (last), not first
        models = [e["model"] for e in gen]
        assert models[-1] == "openrouter/free"
        assert models[0] != "openrouter/free"

    def test_code_review_has_safety_net(self):
        cascades = gateway.load_cascades()
        cr = cascades["code_review"]
        models = [e["model"] for e in cr]
        assert "openrouter/free" in models

    def test_no_dead_gemini_2_0_model(self):
        # gemini-2.0-flash still resolves as a valid model ID (200 on
        # /v1beta/models/{id}) but carries 0 RPM/TPM/RPD quota on the free
        # tier as of July 2026 — confirmed dead via live 429s, not by
        # existence-check (see check_models.py note on this gap).
        cascades = gateway.load_cascades()
        for intent, entries in cascades.items():
            for e in entries:
                if "gemini" in e["name"]:
                    assert e["model"] != "gemini-2.0-flash"

    def test_gemini_has_same_provider_fallback_tier(self):
        # gemini-3.6-flash (5 RPM/20 RPD) is followed by gemini-3.5-flash-lite
        # (15 RPM/500 RPD) so a Gemini daily-cap exhaustion falls over to a
        # much larger same-provider quota before dropping to groq/openrouter.
        cascades = gateway.load_cascades()
        for intent in ("code_review", "general"):
            gemini_models = [e["model"] for e in cascades[intent] if e["name"].startswith("gemini/")]
            assert gemini_models == ["gemini-3.6-flash", "gemini-3.5-flash-lite"]

    def test_creative_cascade_exists_for_prose_use_cases(self):
        # For social-caption/article-generation prompts, not code review.
        # Distinct from code_review/general because ai-gateway's own
        # code_review picks (cohere/north-mini-code:free) return empty
        # content on caption-style prompts -- verified live.
        #
        # Leads with Groq: fastest generation (~1-5s) and its failure mode
        # is a fast explicit 429 with Retry-After, vs Gemini flash-lite
        # which was observed silently queueing generations for 40-90s+
        # (Aug 25, 2026 live probes). gpt-oss needs reasoning_effort=low
        # (its hidden reasoning otherwise eats caption-sized token budgets).
        cascades = gateway.load_cascades()
        assert "creative" in cascades
        models = [e["model"] for e in cascades["creative"]]
        assert models[0] == "openai/gpt-oss-120b"
        assert cascades["creative"][0]["extra"]["reasoning_effort"] == "low"
        assert "gemini-3.5-flash-lite" in models
        assert "google/gemma-4-26b-a4b-it:free" in models  # provider-diverse fallback
        assert "openrouter/free" in models  # safety net

    def test_no_dead_qwen_model(self):
        cascades = gateway.load_cascades()
        for intent, entries in cascades.items():
            for e in entries:
                assert e["model"] != "qwen/qwen3-coder:free"

    def test_deepseek_cheap_cascade(self):
        cascades = gateway.load_cascades()
        assert "deepseek_cheap" in cascades
        entry = cascades["deepseek_cheap"][0]
        assert entry["name"] == "deepseek/deepseek-v4-flash"
        assert entry["key_env"] == "DEEPSEEK_API_KEY"

    def test_custom_config(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                         delete=False) as f:
            json.dump({"cascades": {
                "test": [{"provider": "groq", "model": "test-model",
                          "structured": "json_object"}]
            }}, f)
            f.flush()
            try:
                cascades = gateway.load_cascades(f.name)
                assert "test" in cascades
                assert cascades["test"][0]["model"] == "test-model"
                assert cascades["test"][0]["key_env"] == "GROQ_API_KEY"
            finally:
                os.unlink(f.name)


class TestProviders:
    def test_all_providers_have_url_and_key(self):
        for name, p in gateway.PROVIDERS.items():
            assert "url" in p, f"provider {name} missing url"
            assert "key_env" in p, f"provider {name} missing key_env"
            assert p["url"].startswith("https://"), \
                f"provider {name} url not https"

    def test_gemini_url_has_openai_compat_path(self):
        assert "/openai" in gateway.PROVIDERS["gemini"]["url"]

    def test_groq_url_has_openai_compat_path(self):
        assert "/openai" in gateway.PROVIDERS["groq"]["url"]

    def test_deepseek_provider_configured(self):
        assert gateway.PROVIDERS["deepseek"]["url"] == "https://api.deepseek.com"
        assert gateway.PROVIDERS["deepseek"]["key_env"] == "DEEPSEEK_API_KEY"

    def test_vercel_provider_configured(self):
        assert gateway.PROVIDERS["vercel"]["url"] == "https://ai-gateway.vercel.sh/v1"
        assert gateway.PROVIDERS["vercel"]["key_env"] == "AI_GATEWAY_API_KEY"

    def test_anthropic_provider_configured(self):
        assert gateway.PROVIDERS["anthropic"]["url"] == "https://api.anthropic.com/v1"
        assert gateway.PROVIDERS["anthropic"]["key_env"] == "ANTHROPIC_API_KEY"

    def test_openai_provider_configured(self):
        assert gateway.PROVIDERS["openai"]["url"] == "https://api.openai.com/v1"
        assert gateway.PROVIDERS["openai"]["key_env"] == "OPENAI_API_KEY"


class TestPaidTail:
    """The code_review cascade ends in a paid provider, reached only when the free tier is gone.

    Safe by construction rather than by policy: gateway.complete() skips any provider whose
    key_env is unset, so a repo that never passes DEEPSEEK_API_KEY never reaches this step and
    never spends. Passing the secret is the opt-in.
    """

    def test_deepseek_is_last_not_first(self):
        cr = gateway.load_cascades()["code_review"]
        keys = [e["key_env"] for e in cr]
        assert keys[-1] == "DEEPSEEK_API_KEY", "the paid step must be the LAST resort"
        assert keys.count("DEEPSEEK_API_KEY") == 1, "one paid step, not a paid cascade"

    def test_free_tier_still_tried_first(self):
        cr = gateway.load_cascades()["code_review"]
        # Everything before the paid tail is a free-tier provider.
        assert all(e["key_env"] != "DEEPSEEK_API_KEY" for e in cr[:-1])
        # 4 free providers across 3 distinct services (gemini, groq,
        # openrouter) — provider diversity, not entry count, is what
        # protects against a single free tier going down (Aug 2026: the
        # old cohere+poolside leads died together on openrouter).
        assert len(cr) >= 4
        # provider diversity via resolved names ("provider/model")
        assert len({e["name"].split("/")[0] for e in cr[:-1]}) >= 3

    def test_general_cascade_stays_free(self):
        # `general` is the default for any app that does not name a cascade, so a paid step here
        # would bill every unrelated caller.
        gen = gateway.load_cascades()["general"]
        assert all(e["key_env"] != "DEEPSEEK_API_KEY" for e in gen)


class TestFrontier:
    """Vercel AI Gateway is the PAID frontier tier, opt-in via AI_GATEWAY_API_KEY.

    The key is the consent: repos that never set AI_GATEWAY_API_KEY never reach Vercel and never
    spend. Frontier models live in their own `frontier` cascade only — the free cascades
    (code_review/general/creative) stay Vercel-free so the $0 path never bills.
    """

    def test_frontier_cascade_models(self):
        cascades = gateway.load_cascades()
        assert "frontier" in cascades
        f = cascades["frontier"]
        assert [e["model"] for e in f] == [
            "anthropic/claude-opus-5",
            "openai/gpt-5.6-sol",
            "google/gemini-3.1-pro-preview",
            "claude-opus-5",
            "gpt-5.6-sol",
        ]
        # Vercel first (one bill), then direct Anthropic/OpenAI keys as independent
        # fallback tiers: same token price, separate billing path.
        assert [e["key_env"] for e in f] == [
            "AI_GATEWAY_API_KEY",
            "AI_GATEWAY_API_KEY",
            "AI_GATEWAY_API_KEY",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
        ]
        assert [e["name"] for e in f] == [
            "vercel/anthropic/claude-opus-5",
            "vercel/openai/gpt-5.6-sol",
            "vercel/google/gemini-3.1-pro-preview",
            "anthropic/claude-opus-5",
            "openai/gpt-5.6-sol",
        ]

    def test_frontier_stays_out_of_free_cascades(self):
        # The free cascades must never bill: paid frontier keys (Vercel, Anthropic,
        # OpenAI) belong only to `frontier`.
        cascades = gateway.load_cascades()
        paid = ("AI_GATEWAY_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")
        for intent in ("code_review", "general", "creative", "deepseek_cheap"):
            assert all(e["key_env"] not in paid for e in cascades[intent]), intent


class TestProviderKeys:
    """A provider's key env var may hold several comma/whitespace-separated keys."""

    def test_parses_commas_and_whitespace(self, monkeypatch):
        monkeypatch.setenv("AI_GATEWAY_TEST_KEY", "  key1 , key2\nkey3\t")
        assert gateway._provider_keys("AI_GATEWAY_TEST_KEY") == ["key1", "key2", "key3"]

    def test_unset_key_returns_empty(self, monkeypatch):
        monkeypatch.delenv("AI_GATEWAY_TEST_KEY", raising=False)
        assert gateway._provider_keys("AI_GATEWAY_TEST_KEY") == []

    def test_single_key_unchanged(self, monkeypatch):
        monkeypatch.setenv("AI_GATEWAY_TEST_KEY", "onlyone")
        assert gateway._provider_keys("AI_GATEWAY_TEST_KEY") == ["onlyone"]


class TestVisionFlag:
    """Every cascade entry carries a vision capability; known text-only models are False."""

    def test_every_entry_has_vision_flag(self):
        cascades = gateway.load_cascades()
        for intent, entries in cascades.items():
            for e in entries:
                assert "vision" in e, f"{intent}/{e['name']} missing vision"

    def test_known_text_only_models_flagged_false(self):
        cascades = gateway.load_cascades()
        assert cascades["deepseek_cheap"][0]["vision"] is False
        assert cascades["general"][-2]["vision"] is False  # groq llama
        assert cascades["general"][0]["vision"] is True     # gemini


class TestMessagesForwarding:
    """complete(messages=...) forwards client messages verbatim (vision parts)."""

    def test_messages_forwarded_verbatim(self, monkeypatch):
        seen = {}

        def mock_post(base_url, api_key, payload, timeout=120):
            seen["payload"] = payload
            return "ok"

        monkeypatch.setattr(gateway, "_post_chat", mock_post)
        monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "what is this?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]}]
        gateway.complete("ignored", messages=msgs, intent="general")
        assert seen["payload"]["messages"] == msgs

    def test_vision_request_skips_visionless_providers(self, monkeypatch):
        calls = []
        monkeypatch.setattr(gateway, "_post_chat",
                            lambda *a, **kw: calls.append(1))
        monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "describe"},
            {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
        ]}]
        with pytest.raises(RuntimeError, match="no vision"):
            gateway.complete("ignored", messages=msgs, intent="deepseek_cheap")
        assert not calls, "a visionless provider must never be called"

    def test_vision_request_uses_vision_capable_provider(self, monkeypatch):
        seen = {}

        def mock_post(base_url, api_key, payload, timeout=120):
            seen["msgs"] = payload["messages"]
            return "ok"

        monkeypatch.setattr(gateway, "_post_chat", mock_post)
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "describe"},
            {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
        ]}]
        text, provider = gateway.complete("ignored", messages=msgs, intent="general")
        assert seen["msgs"] == msgs
        assert provider.startswith("gemini/")

    def test_json_object_mode_embeds_schema_in_system_message(self, monkeypatch):
        seen = {}

        def mock_post(base_url, api_key, payload, timeout=120):
            seen["payload"] = payload
            return '{"n": 1}'

        monkeypatch.setattr(gateway, "_post_chat", mock_post)
        monkeypatch.setenv("GROQ_API_KEY", "fake-key")
        # Clear shell-inherited keys so groq (json_object mode) is the first
        # provider actually reached in the `general` cascade.
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
        msgs = [{"role": "system", "content": "You are a counter."},
                {"role": "user", "content": "count"}]
        result, provider = gateway.complete("ignored", messages=msgs,
                                            intent="general", schema=schema)
        sent = seen["payload"]["messages"]
        assert sent[0]["role"] == "system"
        assert sent[0]["content"].startswith("You are a counter.")
        assert "JSON Schema" in sent[0]["content"]
        assert sent[1] == msgs[1]
        assert result == {"n": 1}

    def test_no_images_does_not_skip_visionless_provider(self, monkeypatch):
        calls = []
        monkeypatch.setattr(gateway, "_post_chat",
                            lambda *a, **kw: calls.append(1) or "ok")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
        gateway.complete("plain text", intent="deepseek_cheap")
        assert len(calls) == 1, "a text-only request may use a visionless provider"


class TestNullContent:
    """A provider answering with content=null must fail cleanly, not crash the attempt.

    Observed live on culturaCardedeu#212 (2026-08-25): openrouter/cohere/north-mini-code:free
    and openrouter/free both returned content=null, `["content"]` + `.strip()` raised
    AttributeError, and the log read "AttributeError: 'NoneType' object has no attribute
    'strip'" — indistinguishable from a dead provider.
    """

    def _resp(self, body):
        """Fake urlopen returning `body` as the JSON payload."""
        class FakeResp:
            def read(self):
                return json.dumps(body).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return lambda req, timeout=None: FakeResp()

    def test_null_content_raises_valueerror_not_attributeerror(self, monkeypatch):
        monkeypatch.setattr(gateway.urllib.request, "urlopen",
                            self._resp({"choices": [{"message": {"content": None}}]}))
        with pytest.raises(ValueError, match="empty content"):
            gateway._post_chat("https://x.test", "k", {})

    def test_reasoning_content_is_used_when_content_is_null(self, monkeypatch):
        monkeypatch.setattr(gateway.urllib.request, "urlopen", self._resp(
            {"choices": [{"message": {"content": None,
                                      "reasoning_content": '{"ok": true}'}}]}))
        assert gateway._post_chat("https://x.test", "k", {}) == '{"ok": true}'

    def test_empty_choices_raises_valueerror(self, monkeypatch):
        monkeypatch.setattr(gateway.urllib.request, "urlopen", self._resp({"choices": []}))
        with pytest.raises(ValueError, match="empty content"):
            gateway._post_chat("https://x.test", "k", {})

    def test_whitespace_only_content_raises_valueerror(self, monkeypatch):
        monkeypatch.setattr(gateway.urllib.request, "urlopen",
                            self._resp({"choices": [{"message": {"content": "  \n "}}]}))
        with pytest.raises(ValueError, match="empty content"):
            gateway._post_chat("https://x.test", "k", {})

    def test_normal_content_still_returned(self, monkeypatch):
        monkeypatch.setattr(gateway.urllib.request, "urlopen",
                            self._resp({"choices": [{"message": {"content": "hello"}}]}))
        assert gateway._post_chat("https://x.test", "k", {}) == "hello"

    def test_null_content_falls_through_to_next_provider(self, monkeypatch):
        """The whole point: one bad provider must not end the cascade."""
        calls = []

        def mock_post(base_url, api_key, payload, timeout=120):
            calls.append(base_url)
            if len(calls) == 1:
                raise ValueError("empty content in response: {}")
            return "recovered"

        monkeypatch.setattr(gateway, "_post_chat", mock_post)
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
        monkeypatch.setenv("GROQ_API_KEY", "fake-key")
        result, _ = gateway.complete("p", intent="code_review")
        assert result == "recovered"
        assert len(calls) == 2
