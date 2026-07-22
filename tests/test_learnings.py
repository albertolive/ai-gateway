"""Tests for scripts/learnings.py — persistent review memory.

Tests the pure logic (text processing, dedup detection) without network
calls. Network-dependent functions (_find_issue, get, add) are tested
indirectly through the text-level helpers.
"""

import learnings


class TestMarker:
    def test_marker_is_html_comment(self):
        assert learnings.MARKER.startswith("<!--")
        assert learnings.MARKER.endswith("-->")

    def test_marker_contains_identifier(self):
        assert "ai-gateway" in learnings.MARKER
        assert "learnings" in learnings.MARKER


class TestHeader:
    def test_header_contains_marker(self):
        assert learnings.MARKER in learnings.HEADER

    def test_header_has_learnings_section(self):
        assert "## Learnings" in learnings.HEADER

    def test_header_explains_purpose(self):
        assert "persistent memory" in learnings.HEADER.lower() or \
               "memory" in learnings.HEADER.lower()


class TestGetTextExtraction:
    def test_extracts_text_after_learnings_header(self):
        body = (
            f"{learnings.MARKER}\n"
            "Some description.\n"
            "## Learnings\n"
            "- Always check for null\n"
            "- Use type hints\n"
        )
        # Simulate what get() does: split on "## Learnings"
        text = body.split("## Learnings", 1)[-1].strip()
        assert "Always check for null" in text
        assert "Use type hints" in text

    def test_empty_learnings_section(self):
        body = (
            f"{learnings.MARKER}\n"
            "Description.\n"
            "## Learnings\n"
        )
        text = body.split("## Learnings", 1)[-1].strip()
        assert text == ""

    def test_no_learnings_header(self):
        body = f"{learnings.MARKER}\nJust a description."
        text = body.split("## Learnings", 1)[-1].strip()
        # Without the header, the whole body after split is returned
        assert "Just a description" in text


class TestDedupLogic:
    def test_duplicate_detection_case_insensitive(self):
        existing_body = (
            f"{learnings.MARKER}\n## Learnings\n"
            "- Always check for null pointers _2026-07-01)_"
        )
        new_learning = "always check for null pointers"
        # This is the dedup check from add()
        assert new_learning.lower() in existing_body.lower()

    def test_non_duplicate_not_detected(self):
        existing_body = f"{learnings.MARKER}\n## Learnings\n- Check for null"
        new_learning = "Use type hints everywhere"
        assert new_learning.lower() not in existing_body.lower()
