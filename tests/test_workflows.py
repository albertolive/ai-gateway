"""Tests for workflow YAML files — structural validation, security checks.

Validates that all .github/workflows/*.yml and caller-templates/*.yml files:
- Parse as valid YAML
- Have no remaining YOUR_GITHUB_USERNAME_OR_ORG placeholders
- Pin third-party actions to commit SHAs (not @main or @vN tags)
- Have least-privilege permissions blocks
- Have concurrency blocks where expected
- Use persist-credentials: false on checkout steps
"""

import json
import os
import re
import subprocess

import pytest
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
        """Checkout steps should set persist-credentials: false.

        Exception: model-watch.yml needs git push access for the bot PR,
        so it uses persist-credentials: false + explicit credential setup
        (verified by test_model_watch_has_credential_step instead).
        """
        wf_dir = os.path.abspath(_WORKFLOW_DIRS[0])
        for name in os.listdir(wf_dir):
            if not name.endswith((".yml", ".yaml")):
                continue
            if name == "model-watch.yml":
                continue  # exempt: has explicit credential step
            path = os.path.join(wf_dir, name)
            with open(path, encoding="utf-8") as f:
                content = f.read()
            if "actions/checkout" in content:
                assert "persist-credentials: false" in content, \
                    f"{name}: checkout without persist-credentials: false"

    def test_model_watch_has_credential_step(self):
        """model-watch.yml needs explicit git credentials for git push."""
        path = os.path.abspath(
            os.path.join(_WORKFLOW_DIRS[0], "model-watch.yml"))
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert "persist-credentials: false" in content, \
            "model-watch.yml: should still have persist-credentials: false"
        assert "git remote set-url" in content, \
            "model-watch.yml: needs explicit git credential configuration for push"
        assert "GH_TOKEN" in content, \
            "model-watch.yml: credential step should use GH_TOKEN"


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
    """The scripts a caller runs must be the workflow it asked for — structurally.

    History: every reusable workflow here fetches its own scripts, so each caller carried
    TWO pins. They drifted silently — callers asked for @v1.3.1, whose workflow checked out
    `ref: v1.2.0`, whose workflow checked out `ref: v1.0.0`. Twelve repos ran the first
    release for months (four dead providers, no wall-clock budget, no outage soft-fail) and
    no test objected, because each pin was individually valid.

    The fix is not a tighter assertion, it is removing the second pin: the workflow checks
    itself out at `job.workflow_sha`, which GitHub resolves to the exact commit already
    running. A value that is derived cannot disagree with itself, so callers can float on a
    major tag (@v1) and never be hand-bumped again.
    """

    _SELF_CHECKOUT = "${{ job.workflow_sha }}"

    def test_gateway_checkout_uses_workflow_sha(self):
        """No literal ref for the gateway checkout — that was the second pin."""
        wf_dir = os.path.abspath(_WORKFLOW_DIRS[0])
        checked = 0
        for name in ["pr-review.yml", "pr-reply.yml", "llm-task.yml"]:
            path = os.path.join(wf_dir, name)
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as f:
                content = f.read()
            m = re.search(r"repository: (.+)\n\s*ref: (.+)", content)
            if not m:
                continue
            checked += 1
            repo_val, ref_val = m.group(1).strip(), m.group(2).strip()
            assert "job.workflow_repository" in repo_val, (
                f"{name}: gateway checkout should use "
                "${{ job.workflow_repository }}, got: " + repo_val)
            assert ref_val == self._SELF_CHECKOUT, (
                f"{name}: gateway checkout must use {self._SELF_CHECKOUT} so the scripts "
                f"match the running workflow; a literal ref ({ref_val}) is a second pin "
                "that drifts — that is the v1.3.1 -> v1.2.0 -> v1.0.0 bug.")
        assert checked, "no gateway checkout found in any reusable workflow"

    def test_callers_float_on_major_tag(self):
        """Caller templates pin @vN, so a release never requires touching 12 repos."""
        repo_root = os.path.abspath(os.path.join(_WORKFLOW_DIRS[0], "..", ".."))
        tpl_dir = os.path.join(repo_root, "caller-templates")
        if not os.path.isdir(tpl_dir):
            pytest.skip("no caller-templates/")
        found = 0
        for name in sorted(os.listdir(tpl_dir)):
            if not name.endswith((".yml", ".yaml")):
                continue
            with open(os.path.join(tpl_dir, name), encoding="utf-8") as f:
                for i, line in enumerate(f, 1):
                    m = re.search(r"uses: \S*ai-gateway/\S+@(\S+)", line)
                    if not m:
                        continue
                    found += 1
                    ref = m.group(1)
                    assert re.fullmatch(r"v\d+", ref), (
                        f"caller-templates/{name}:{i}: callers should float on a major tag "
                        f"(@v1) so releases do not require a fleet-wide bump; got @{ref}")
        assert found, "no caller `uses:` lines found to check"

    def test_deploy_script_uses_major_tag(self):
        repo_root = os.path.abspath(os.path.join(_WORKFLOW_DIRS[0], "..", ".."))
        script = os.path.join(repo_root, "deploy-callers.sh")
        if not os.path.exists(script):
            pytest.skip("no deploy-callers.sh")
        with open(script, encoding="utf-8") as f:
            m = re.search(r'VERSION_TAG="(\S+)"', f.read())
        assert m, "deploy-callers.sh: no VERSION_TAG found"
        assert re.fullmatch(r"v\d+", m.group(1)), (
            "deploy-callers.sh: VERSION_TAG should be a floating major tag (v1), "
            f"matching the caller templates; got {m.group(1)}")

    def test_no_ref_main_anywhere(self):
        """`ref: main` in a reusable workflow is the supply-chain anti-pattern."""
        wf_dir = os.path.abspath(_WORKFLOW_DIRS[0])
        for name in sorted(os.listdir(wf_dir)):
            if not name.endswith((".yml", ".yaml")):
                continue
            with open(os.path.join(wf_dir, name), encoding="utf-8") as f:
                assert "ref: main" not in f.read(), f"{name}: uses ref: main"


class TestDocsDoNotCopyModelIds:
    """Docs must point at models.json, never restate it.

    The README used to carry a cascade table. Within weeks every model in it was dead
    (cohere/north-mini-code:free, poolside/laguna-m.1:free, gemini-2.0-flash,
    llama-3.3-70b-versatile all 404/403), and a note above it said the IDs were "verified,
    not assumed". Same failure as the version pins: two copies of a fast-moving fact, each
    internally consistent, nothing checking they agreed. Since free-tier model IDs rot every
    few weeks, the only stable doc is a pointer — so this fails if a doc names a model that
    models.json no longer pins, which is exactly when the doc has gone stale.
    """

    _DOCS = ["README.md", os.path.join("docs", "architecture.md")]

    def _repo_root(self):
        return os.path.abspath(os.path.join(_WORKFLOW_DIRS[0], "..", ".."))

    def _pinned_models(self):
        root = self._repo_root()
        with open(os.path.join(root, "models.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        return {e["model"] for c in cfg["cascades"].values() for e in c}

    def test_docs_name_no_unpinned_models(self):
        root = self._repo_root()
        pinned = self._pinned_models()
        # Match only things that really are model IDs: a known AI vendor namespace, a
        # `:free` suffix, or a family name carrying a version number. Vendors change far
        # more slowly than model IDs, so this list is stable in a way a model table is not.
        vendors = (r"openrouter|openai|google|anthropic|groq|deepseek|cohere|poolside"
                   r"|meta-llama|mistralai|nvidia|qwen|moonshotai|z-ai|minimax|vercel")
        families = (r"gpt|gemini|claude|llama|deepseek|kimi|glm|qwen|gemma|grok|minimax"
                    r"|mistral|nemotron|opus|sonnet|haiku")
        pattern = re.compile(
            r"`((?:" + vendors + r")/[a-z0-9][a-z0-9./:_-]*"          # openrouter/free
            r"|[a-z0-9][a-z0-9./_-]*:free"                            # anything :free
            r"|(?:" + families + r")[a-z0-9._-]*[0-9][a-z0-9._:-]*)`",  # gemini-2.0-flash
            re.I)
        offenders = []
        for rel in self._DOCS:
            path = os.path.join(root, rel)
            if not os.path.exists(path):
                continue
            for i, line in enumerate(open(path, encoding="utf-8"), 1):
                for m in pattern.finditer(line):
                    name = m.group(1)
                    if name in pinned:
                        continue
                    # Env vars, headers and paths are not model ids.
                    if name.endswith((".py", ".json", ".yml", ".md", ".sh", ".txt")):
                        continue
                    offenders.append(f"{rel}:{i}: `{name}`")
        assert not offenders, (
            "docs name model IDs that models.json does not pin — the doc is stale, or it "
            "copied a fact that belongs only in models.json:\n"
            + "\n".join(f"  {o}" for o in offenders)
            + "\nPrefer pointing at models.json over restating it."
        )

    def test_readme_points_at_models_json(self):
        root = self._repo_root()
        with open(os.path.join(root, "README.md"), encoding="utf-8") as f:
            content = f.read()
        assert "models.json" in content, (
            "README must point readers at models.json for the live cascade")
