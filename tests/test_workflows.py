"""Tests for workflow YAML files — structural validation, security checks.

Validates that all .github/workflows/*.yml and caller-templates/*.yml files:
- Parse as valid YAML
- Have no remaining YOUR_GITHUB_USERNAME_OR_ORG placeholders
- Pin third-party actions to commit SHAs (not @main or @vN tags)
- Have least-privilege permissions blocks
- Have concurrency blocks where expected
- Use persist-credentials: false on checkout steps
"""

import os
import re

import yaml


_WORKFLOW_DIRS = [
    os.path.join(os.path.dirname(__file__), "..", ".github", "workflows"),
    os.path.join(os.path.dirname(__file__), "..", "caller-templates"),
]


def _all_workflow_files():
    files = []
    for d in _WORKFLOW_DIRS:
        d = os.path.abspath(d)
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f.endswith((".yml", ".yaml")):
                    files.append(os.path.join(d, f))
    return files


def _load_yaml(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


class TestWorkflowSyntax:
    def test_all_workflows_parse(self):
        for path in _all_workflow_files():
            data = _load_yaml(path)
            assert data is not None, f"{path} parsed to None"
            assert "name" in data, f"{path} missing 'name'"
            assert "jobs" in data, f"{path} missing 'jobs'"
            assert len(data["jobs"]) > 0, f"{path} has no jobs"

    def test_no_placeholder_remaining(self):
        for path in _all_workflow_files():
            with open(path, encoding="utf-8") as f:
                content = f.read()
            assert "YOUR_GITHUB_USERNAME_OR_ORG" not in content, \
                f"{path} still has placeholder"

    def test_reusable_workflows_use_workflow_call(self):
        """Reusable workflows must trigger on workflow_call."""
        reusable = ["pr-review.yml", "pr-reply.yml", "llm-task.yml"]
        wf_dir = os.path.abspath(_WORKFLOW_DIRS[0])
        for name in reusable:
            path = os.path.join(wf_dir, name)
            if not os.path.exists(path):
                continue
            data = _load_yaml(path)
            # YAML 1.1 parses 'on:' as boolean True, not string 'on'
            triggers = data.get("on") or data.get(True) or {}
            assert "workflow_call" in triggers, \
                f"{name} missing workflow_call trigger"


class TestSecurityPinning:
    """Third-party actions must be pinned to commit SHAs, not @main or @vN."""

    def test_actions_pinned_to_sha(self):
        # SHA pattern: 40 hex chars after @
        sha_re = re.compile(r"uses:\s*\S+@[0-9a-f]{40}")
        # Anti-pattern: uses: actions/checkout@v4 or @main
        loose_re = re.compile(r"uses:\s*\S+@(?:v\d+|main|master)\b")

        for path in _all_workflow_files():
            with open(path, encoding="utf-8") as f:
                content = f.read()
            # Find all uses: lines
            uses_lines = [l.strip() for l in content.splitlines()
                          if l.strip().startswith("uses:")]
            for line in uses_lines:
                # Skip local workflow references (uses: owner/repo/.github/...)
                if "/.github/" in line:
                    continue
                # Must have @SHA (40 hex chars) — not @vN or @main
                assert sha_re.search(line), \
                    f"{path}: action not SHA-pinned: {line}"
                assert not loose_re.search(line), \
                    f"{path}: action uses loose tag: {line}"

    def test_persist_credentials_false(self):
        """Checkout steps should set persist-credentials: false."""
        wf_dir = os.path.abspath(_WORKFLOW_DIRS[0])
        for name in os.listdir(wf_dir):
            if not name.endswith((".yml", ".yaml")):
                continue
            path = os.path.join(wf_dir, name)
            with open(path, encoding="utf-8") as f:
                content = f.read()
            # Every checkout step should have persist-credentials: false
            if "actions/checkout" in content:
                assert "persist-credentials: false" in content, \
                    f"{name}: checkout without persist-credentials: false"


class TestPermissions:
    def test_jobs_have_permissions(self):
        """Each job should have an explicit permissions block."""
        wf_dir = os.path.abspath(_WORKFLOW_DIRS[0])
        for name in ["pr-review.yml", "pr-reply.yml", "llm-task.yml",
                      "tests.yml"]:
            path = os.path.join(wf_dir, name)
            if not os.path.exists(path):
                continue
            data = _load_yaml(path)
            for job_name, job in data.get("jobs", {}).items():
                assert "permissions" in job, \
                    f"{name}/{job_name}: missing permissions block"

    def test_review_needs_pull_requests_write(self):
        path = os.path.abspath(
            os.path.join(_WORKFLOW_DIRS[0], "pr-review.yml"))
        if not os.path.exists(path):
            return
        data = _load_yaml(path)
        for job in data["jobs"].values():
            perms = job.get("permissions", {})
            assert perms.get("pull-requests") == "write", \
                "pr-review.yml: needs pull-requests: write"

    def test_review_needs_issues_write(self):
        """Review workflow needs issues: write for learnings memory."""
        path = os.path.abspath(
            os.path.join(_WORKFLOW_DIRS[0], "pr-review.yml"))
        if not os.path.exists(path):
            return
        data = _load_yaml(path)
        for job in data["jobs"].values():
            perms = job.get("permissions", {})
            assert perms.get("issues") == "write", \
                "pr-review.yml: needs issues: write for learnings"


class TestConcurrency:
    def test_review_has_concurrency(self):
        path = os.path.abspath(
            os.path.join(_WORKFLOW_DIRS[0], "pr-review.yml"))
        if not os.path.exists(path):
            return
        data = _load_yaml(path)
        assert "concurrency" in data, "pr-review.yml: missing concurrency"
        assert data["concurrency"].get("cancel-in-progress") is True, \
            "pr-review.yml: cancel-in-progress should be true"

    def test_reply_has_concurrency(self):
        path = os.path.abspath(
            os.path.join(_WORKFLOW_DIRS[0], "pr-reply.yml"))
        if not os.path.exists(path):
            return
        data = _load_yaml(path)
        assert "concurrency" in data, "pr-reply.yml: missing concurrency"

    def test_tests_has_concurrency(self):
        path = os.path.abspath(
            os.path.join(_WORKFLOW_DIRS[0], "tests.yml"))
        if not os.path.exists(path):
            return
        data = _load_yaml(path)
        assert "concurrency" in data, "tests.yml: missing concurrency"


class TestGatewayRefPinning:
    """Gateway checkout should use ref: v1.0.0, not ref: main."""

    def test_gateway_checkout_pinned(self):
        wf_dir = os.path.abspath(_WORKFLOW_DIRS[0])
        for name in ["pr-review.yml", "pr-reply.yml", "llm-task.yml"]:
            path = os.path.join(wf_dir, name)
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as f:
                content = f.read()
            # Gateway checkout should use ref: v1.0.0
            if "repository:" in content and "ai-gateway" in content:
                assert "ref: v1.0.0" in content, \
                    f"{name}: gateway checkout should use ref: v1.0.0"
                assert "ref: main" not in content, \
                    f"{name}: gateway checkout still uses ref: main"
