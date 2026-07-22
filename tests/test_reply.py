"""Tests for reply.py — thread reply logic and loop protection.

Tests the pure logic: bot detection, thread filtering, and schema
validation. Network calls are not tested here.
"""

import reply


class TestBotLogins:
    def test_bot_logins_contains_github_actions(self):
        assert "github-actions[bot]" in reply.BOT_LOGINS

    def test_bot_logins_is_a_set(self):
        assert isinstance(reply.BOT_LOGINS, set)


class TestReplySchema:
    def test_schema_has_reply_field(self):
        assert "reply" in reply.REPLY_SCHEMA["properties"]
        assert reply.REPLY_SCHEMA["properties"]["reply"]["type"] == "string"

    def test_schema_has_learning_field(self):
        assert "learning" in reply.REPLY_SCHEMA["properties"]
        # learning can be string or null
        types = reply.REPLY_SCHEMA["properties"]["learning"]["type"]
        assert "string" in types
        assert "null" in types

    def test_required_fields(self):
        required = reply.REPLY_SCHEMA["required"]
        assert "reply" in required
        assert "learning" in required

    def test_additional_properties_false(self):
        assert reply.REPLY_SCHEMA.get("additionalProperties") is False


class TestSystemPrompt:
    def test_prompt_mentions_suggestion_blocks(self):
        assert "suggestion" in reply.SYSTEM_PROMPT.lower()

    def test_prompt_mentions_thread_context(self):
        assert "thread" in reply.SYSTEM_PROMPT.lower()

    def test_prompt_mentions_learning_extraction(self):
        assert "guideline" in reply.SYSTEM_PROMPT.lower() or \
               "learning" in reply.SYSTEM_PROMPT.lower()

    def test_prompt_says_be_direct(self):
        assert "direct" in reply.SYSTEM_PROMPT.lower()

    def test_prompt_says_concede(self):
        assert "concede" in reply.SYSTEM_PROMPT.lower()


class TestReviewSchema:
    """Tests for review.py's REVIEW_SCHEMA (imported from review module)."""

    def test_schema_has_summary_and_comments(self):
        import review
        assert "summary" in review.REVIEW_SCHEMA["properties"]
        assert "comments" in review.REVIEW_SCHEMA["properties"]

    def test_comment_has_path_line_body(self):
        import review
        comment_props = review.REVIEW_SCHEMA["properties"]["comments"]["items"]["properties"]
        assert "path" in comment_props
        assert "line" in comment_props
        assert "body" in comment_props
        assert "start_line" in comment_props

    def test_comment_required_fields(self):
        import review
        required = review.REVIEW_SCHEMA["properties"]["comments"]["items"]["required"]
        assert "path" in required
        assert "line" in required
        assert "body" in required
        assert "start_line" in required

    def test_additional_properties_false(self):
        import review
        assert review.REVIEW_SCHEMA.get("additionalProperties") is False
        assert review.REVIEW_SCHEMA["properties"]["comments"]["items"].get("additionalProperties") is False

    def test_start_line_allows_null(self):
        import review
        types = review.REVIEW_SCHEMA["properties"]["comments"]["items"]["properties"]["start_line"]["type"]
        assert "integer" in types
        assert "null" in types


class TestReviewSystemPrompt:
    def test_prompt_mentions_annotated_diff(self):
        import review
        assert "annotated" in review.SYSTEM_PROMPT.lower() or \
               "[ADDED]" in review.SYSTEM_PROMPT

    def test_prompt_mentions_suggestion_blocks(self):
        import review
        assert "suggestion" in review.SYSTEM_PROMPT.lower()

    def test_prompt_says_no_style_nits(self):
        import review
        assert "style" in review.SYSTEM_PROMPT.lower()
        assert "nit" in review.SYSTEM_PROMPT.lower()

    def test_prompt_has_comment_cap(self):
        import review
        assert "10 comments" in review.SYSTEM_PROMPT or \
               "10" in review.SYSTEM_PROMPT

    def test_prompt_mentions_addded_lines_only(self):
        import review
        assert "[ADDED]" in review.SYSTEM_PROMPT

    def test_prompt_says_copy_line_numbers(self):
        import review
        assert "line" in review.SYSTEM_PROMPT.lower()
        assert "copy" in review.SYSTEM_PROMPT.lower() or \
               "never compute" in review.SYSTEM_PROMPT.lower()
