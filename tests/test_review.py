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


# ---------------------------------------------------------------------------
# cascade exhaustion (provider outage) must not read as a code failure
# ---------------------------------------------------------------------------

class TestCascadeExhaustion:
    """An exhausted cascade is an outage, not a verdict on the diff.

    It used to exit 1, putting a red X on the PR labelled "AI PR Review — failed", which is
    indistinguishable at a glance from a review that found a real problem. meteo-brief #79 (the
    forum-free cutover) merged with that gate red because the whole free tier capped at once.
    """

    def _run(self, monkeypatch, raise_on_post=False):
        posted = {}

        def fake_complete(*a, **k):
            raise RuntimeError(
                "All providers in the cascade failed or were skipped:\n"
                "  - gemini/gemini-3.6-flash HTTP 429: quota\n"
                "  - groq/llama-3.3-70b-versatile HTTP 403: error code: 1010"
            )

        def fake_gh_api(path, token, payload=None, method=None):
            if raise_on_post:
                raise RuntimeError("GitHub rejected the comment")
            posted["path"], posted["body"] = path, (payload or {}).get("body", "")
            return {}

        monkeypatch.setattr(review.gateway, "complete", fake_complete)
        monkeypatch.setattr(review, "gh_api", fake_gh_api)
        monkeypatch.setattr(review, "last_reviewed_sha", lambda *a, **k: None)
        for k, v in {
            "GH_TOKEN": "t", "GITHUB_REPOSITORY": "o/r",
            "PR_NUMBER": "79", "COMMIT_SHA": "abc123", "TARGET_DIR": "",
        }.items():
            monkeypatch.setenv(k, v)
        return posted

    def test_exits_zero_and_explains_on_the_pr(self, monkeypatch, tmp_path, capsys):
        diff = tmp_path / "pr_diff.txt"
        diff.write_text("--- a/x.py\n+++ b/x.py\n@@ -1 +1,2 @@\n+import os\n")
        monkeypatch.setenv("PR_DIFF_PATH", str(diff))
        posted = self._run(monkeypatch)

        review.main()  # must NOT raise SystemExit

        assert "/issues/79/comments" in posted["path"]
        assert "did not run" in posted["body"]
        # The comment must never imply the code was looked at and found clean.
        assert "not been reviewed" in posted["body"]
        assert "outage" in posted["body"]
        # And the run is annotated so it is visible in the Actions UI too.
        assert "::warning" in capsys.readouterr().out

    def test_a_failed_comment_post_still_exits_zero(self, monkeypatch, tmp_path):
        # If GitHub also rejects the comment we must not turn an outage into a hard failure.
        diff = tmp_path / "pr_diff.txt"
        diff.write_text("--- a/x.py\n+++ b/x.py\n@@ -1 +1,2 @@\n+import os\n")
        monkeypatch.setenv("PR_DIFF_PATH", str(diff))
        self._run(monkeypatch, raise_on_post=True)
        review.main()
