"""Tests for scripts/review.py — diff parsing and comment validation."""

import review


# ---------------------------------------------------------------------------
# parse_diff
# ---------------------------------------------------------------------------

class TestParseDiff:
    def test_simple_addition(self):
        diff = (
            "--- a/src/main.py\n"
            "+++ b/src/main.py\n"
            "@@ -1,3 +1,4 @@\n"
            " import os\n"
            " import sys\n"
            "+import json\n"
            " \n"
        )
        annotated, added = review.parse_diff(diff)
        assert "src/main.py" in added
        assert 3 in added["src/main.py"]
        assert "[ADDED]" in annotated
        assert "src/main.py::3::[ADDED]:: import json" in annotated

    def test_context_lines_tracked(self):
        diff = (
            "--- a/src/main.py\n"
            "+++ b/src/main.py\n"
            "@@ -1,3 +1,4 @@\n"
            " import os\n"
            " import sys\n"
            "+import json\n"
            " \n"
        )
        annotated, added = review.parse_diff(diff)
        assert "[CONTEXT]" in annotated
        assert "src/main.py::1::[CONTEXT]:: import os" in annotated

    def test_multiple_files(self):
        diff = (
            "--- a/src/a.py\n"
            "+++ b/src/a.py\n"
            "@@ -1,2 +1,3 @@\n"
            " x = 1\n"
            "+y = 2\n"
            "--- a/src/b.py\n"
            "+++ b/src/b.py\n"
            "@@ -1,2 +1,3 @@\n"
            " a = 1\n"
            "+b = 2\n"
        )
        annotated, added = review.parse_diff(diff)
        assert "src/a.py" in added
        assert "src/b.py" in added
        assert 2 in added["src/a.py"]
        assert 2 in added["src/b.py"]

    def test_deleted_file_has_no_added_lines(self):
        diff = (
            "--- a/src/old.py\n"
            "+++ /dev/null\n"
            "@@ -1,3 +0,0 @@\n"
            "-def old():\n"
            "-    pass\n"
        )
        annotated, added = review.parse_diff(diff)
        assert "src/old.py" not in added or not added.get("src/old.py")

    def test_line_numbers_correct_after_hunk_header(self):
        diff = (
            "--- a/src/big.py\n"
            "+++ b/src/big.py\n"
            "@@ -50,3 +50,4 @@\n"
            "     line_50\n"
            "     line_51\n"
            "+new_line_52\n"
        )
        annotated, added = review.parse_diff(diff)
        # The hunk starts at line 50 in the new file; added line is 52
        assert 52 in added["src/big.py"]
        assert "src/big.py::52::[ADDED]:: new_line_52" in annotated

    def test_empty_diff(self):
        annotated, added = review.parse_diff("")
        assert annotated == ""
        assert added == {}

    def test_removed_lines_not_in_added(self):
        diff = (
            "--- a/src/main.py\n"
            "+++ b/src/main.py\n"
            "@@ -1,3 +1,2 @@\n"
            " import os\n"
            "-import sys\n"
            " \n"
        )
        annotated, added = review.parse_diff(diff)
        # No added lines
        assert not any(added.values())
        assert "[ADDED]" not in annotated


# ---------------------------------------------------------------------------
# validate_comments
# ---------------------------------------------------------------------------

class TestValidateComments:
    def test_valid_comment_kept(self):
        added = {"src/main.py": {5, 10, 15}}
        comments = [{"path": "src/main.py", "line": 10, "start_line": None,
                     "body": "This is a bug."}]
        valid = review.validate_comments(comments, added)
        assert len(valid) == 1
        assert valid[0]["path"] == "src/main.py"
        assert valid[0]["line"] == 10
        assert valid[0]["side"] == "RIGHT"

    def test_invalid_line_dropped(self):
        added = {"src/main.py": {5, 10}}
        comments = [{"path": "src/main.py", "line": 999, "start_line": None,
                     "body": "Bug on wrong line."}]
        valid = review.validate_comments(comments, added)
        assert len(valid) == 0

    def test_unknown_path_dropped(self):
        added = {"src/main.py": {5}}
        comments = [{"path": "other.py", "line": 5, "start_line": None,
                     "body": "Bug."}]
        valid = review.validate_comments(comments, added)
        assert len(valid) == 0

    def test_empty_body_dropped(self):
        added = {"src/main.py": {5}}
        comments = [{"path": "src/main.py", "line": 5, "start_line": None,
                     "body": "  "}]
        valid = review.validate_comments(comments, added)
        assert len(valid) == 0

    def test_multiline_comment_with_valid_start(self):
        added = {"src/main.py": {5, 6, 7, 8}}
        comments = [{"path": "src/main.py", "line": 8, "start_line": 5,
                     "body": "Multi-line issue."}]
        valid = review.validate_comments(comments, added)
        assert len(valid) == 1
        assert valid[0]["start_line"] == 5
        assert valid[0]["start_side"] == "RIGHT"

    def test_multiline_comment_with_invalid_start_drops_start(self):
        added = {"src/main.py": {5, 8}}
        comments = [{"path": "src/main.py", "line": 8, "start_line": 3,
                     "body": "Issue."}]
        valid = review.validate_comments(comments, added)
        # line 8 is valid, start_line 3 is not in added -> start_line dropped
        assert len(valid) == 1
        assert "start_line" not in valid[0]

    def test_max_10_comments(self):
        added = {"src/main.py": set(range(1, 101))}
        comments = [
            {"path": "src/main.py", "line": i, "start_line": None,
             "body": f"Issue {i}"}
            for i in range(1, 20)
        ]
        valid = review.validate_comments(comments, added)
        assert len(valid) == 10

    def test_missing_keys_dropped(self):
        added = {"src/main.py": {5}}
        comments = [
            {"line": 5, "start_line": None, "body": "No path"},
            {"path": "src/main.py", "start_line": None, "body": "No line"},
            {"path": "src/main.py", "line": "not-a-number", "start_line": None,
             "body": "Bad line type"},
        ]
        valid = review.validate_comments(comments, added)
        assert len(valid) == 0
