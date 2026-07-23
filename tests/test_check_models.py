"""Tests for scripts/check_models.py — model catalog checking logic."""

import check_models


class TestIsFree:
    def test_free_by_pricing(self):
        m = {"id": "test:free", "pricing": {"prompt": "0", "completion": "0"}}
        assert check_models.is_free(m) is True

    def test_paid_by_pricing(self):
        m = {"id": "test", "pricing": {"prompt": "0.001", "completion": "0.002"}}
        assert check_models.is_free(m) is False

    def test_free_by_suffix_when_pricing_missing(self):
        m = {"id": "test:free"}
        assert check_models.is_free(m) is True

    def test_paid_without_suffix(self):
        m = {"id": "test"}
        assert check_models.is_free(m) is False

    def test_free_suffix_with_nonzero_price_is_paid(self):
        m = {"id": "tricky:free", "pricing": {"prompt": "0.001", "completion": "0"}}
        assert check_models.is_free(m) is False

    def test_missing_pricing_and_no_suffix(self):
        m = {"id": "test-model"}
        assert check_models.is_free(m) is False


class TestRankCandidates:
    def test_excludes_pinned(self):
        catalog = {
            "a:free": {"id": "a:free", "pricing": {"prompt": "0", "completion": "0"},
                        "supported_parameters": []},
            "b:free": {"id": "b:free", "pricing": {"prompt": "0", "completion": "0"},
                        "supported_parameters": []},
        }
        pinned = {"a:free"}
        candidates = check_models.rank_candidates(catalog, pinned)
        ids = [c["id"] for c in candidates]
        assert "a:free" not in ids
        assert "b:free" in ids

    def test_excludes_auto_router(self):
        catalog = {
            "openrouter/free": {"id": "openrouter/free",
                                "pricing": {"prompt": "0", "completion": "0"},
                                "supported_parameters": []},
        }
        candidates = check_models.rank_candidates(catalog, set())
        ids = [c["id"] for c in candidates]
        assert "openrouter/free" not in ids

    def test_excludes_known_unsuitable(self):
        # A model that scores well on catalog metadata but was disproven live
        # (e.g. reasoning leak) shouldn't keep resurfacing as a candidate.
        catalog = {
            "nvidia/nemotron-3-super-120b-a12b:free": {
                "id": "nvidia/nemotron-3-super-120b-a12b:free",
                "pricing": {"prompt": "0", "completion": "0"},
                "supported_parameters": ["structured_outputs"],
            },
        }
        candidates = check_models.rank_candidates(catalog, set())
        ids = [c["id"] for c in candidates]
        assert "nvidia/nemotron-3-super-120b-a12b:free" not in ids

    def test_excludes_paid(self):
        catalog = {
            "paid:free": {"id": "paid:free",
                          "pricing": {"prompt": "0.001", "completion": "0"}},
        }
        candidates = check_models.rank_candidates(catalog, set())
        ids = [c["id"] for c in candidates]
        assert "paid:free" not in ids

    def test_coding_name_scores_higher(self):
        catalog = {
            "regular:free": {"id": "regular:free",
                             "pricing": {"prompt": "0", "completion": "0"},
                             "supported_parameters": [], "context_length": 100000},
            "amazing-coder:free": {"id": "amazing-coder:free",
                                   "pricing": {"prompt": "0", "completion": "0"},
                                   "supported_parameters": [],
                                   "context_length": 100000},
        }
        candidates = check_models.rank_candidates(catalog, set())
        assert candidates[0]["id"] == "amazing-coder:free"
        assert candidates[0]["score"] == 2

    def test_structured_outputs_adds_score(self):
        catalog = {
            "a:free": {"id": "a:free",
                       "pricing": {"prompt": "0", "completion": "0"},
                       "supported_parameters": ["structured_outputs"],
                       "context_length": 50000},
            "b:free": {"id": "b:free",
                       "pricing": {"prompt": "0", "completion": "0"},
                       "supported_parameters": [],
                       "context_length": 50000},
        }
        candidates = check_models.rank_candidates(catalog, set())
        assert candidates[0]["id"] == "a:free"
        assert candidates[0]["score"] == 1

    def test_context_length_tiebreak(self):
        catalog = {
            "a:free": {"id": "a:free",
                       "pricing": {"prompt": "0", "completion": "0"},
                       "supported_parameters": [],
                       "context_length": 200000},
            "b:free": {"id": "b:free",
                       "pricing": {"prompt": "0", "completion": "0"},
                       "supported_parameters": [],
                       "context_length": 100000},
        }
        candidates = check_models.rank_candidates(catalog, set())
        # Same score (0), tie broken by context_length desc
        assert candidates[0]["id"] == "a:free"

    def test_empty_catalog(self):
        candidates = check_models.rank_candidates({}, set())
        assert candidates == []


class TestSmokeTest:
    def test_passes_on_correct_json(self, monkeypatch):
        monkeypatch.setattr(check_models, "_post_chat_completion",
                            lambda *a, **k: '{"greeting": "hello", "count": 3}')
        passed, reason = check_models.smoke_test("m", "json_object", "key")
        assert passed is True
        assert reason == "ok"

    def test_strips_markdown_fence(self, monkeypatch):
        monkeypatch.setattr(check_models, "_post_chat_completion",
                            lambda *a, **k: '```json\n{"greeting": "hello", "count": 3}\n```')
        passed, _ = check_models.smoke_test("m", "json_object", "key")
        assert passed is True

    def test_fails_on_empty_response(self, monkeypatch):
        monkeypatch.setattr(check_models, "_post_chat_completion", lambda *a, **k: "")
        passed, reason = check_models.smoke_test("m", "json_object", "key")
        assert passed is False
        assert "empty" in reason

    def test_fails_on_reasoning_leak(self, monkeypatch):
        # The exact nemotron-3-super-120b-a12b failure mode.
        monkeypatch.setattr(
            check_models, "_post_chat_completion",
            lambda *a, **k: "We need to produce a JSON object with greeting and count...")
        passed, reason = check_models.smoke_test("m", "json_object", "key")
        assert passed is False
        assert "reasoning" in reason

    def test_fails_on_invalid_json(self, monkeypatch):
        monkeypatch.setattr(check_models, "_post_chat_completion",
                            lambda *a, **k: "not json at all")
        passed, reason = check_models.smoke_test("m", "json_object", "key")
        assert passed is False
        assert "invalid JSON" in reason

    def test_fails_on_ignored_instructions(self, monkeypatch):
        monkeypatch.setattr(check_models, "_post_chat_completion",
                            lambda *a, **k: '{"greeting": "hi", "count": 3}')
        passed, reason = check_models.smoke_test("m", "json_object", "key")
        assert passed is False
        assert "ignored instructions" in reason

    def test_fails_on_request_error(self, monkeypatch):
        def raise_err(*a, **k):
            raise ConnectionError("network down")
        monkeypatch.setattr(check_models, "_post_chat_completion", raise_err)
        passed, reason = check_models.smoke_test("m", "json_object", "key")
        assert passed is False
        assert "request failed" in reason
