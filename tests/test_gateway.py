"""Tests for scripts/gateway.py — cascade loading and provider config."""

import json
import os
import tempfile

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
        cascades = gateway.load_cascades()
        assert "creative" in cascades
        models = [e["model"] for e in cascades["creative"]]
        assert models[0] == "google/gemma-4-26b-a4b-it:free"
        assert models[1] == "google/gemma-4-31b-it:free"
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
        assert len(cr) >= 6, "the free cascade must not have been shortened to reach paid sooner"

    def test_general_cascade_stays_free(self):
        # `general` is the default for any app that does not name a cascade, so a paid step here
        # would bill every unrelated caller.
        gen = gateway.load_cascades()["general"]
        assert all(e["key_env"] != "DEEPSEEK_API_KEY" for e in gen)
