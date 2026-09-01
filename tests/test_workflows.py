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
    """Gateway checkout should pin a versioned tag, not ref: main."""

    def test_gateway_checkout_pinned(self):
        wf_dir = os.path.abspath(_WORKFLOW_DIRS[0])
        for name in ["pr-review.yml", "pr-reply.yml", "llm-task.yml"]:
            path = os.path.join(wf_dir, name)
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as f:
                content = f.read()
            # Gateway checkout should pin a versioned release tag (ref: vX.Y.Z)
            if "repository:" in content and "ai-gateway" in content:
                assert re.search(r"ref: v\d+\.\d+\.\d+", content), \
                    f"{name}: gateway checkout should pin a versioned tag (ref: vX.Y.Z)"
                assert "ref: main" not in content, \
                    f"{name}: gateway checkout still uses ref: main"

    def test_all_pins_agree(self):
        """Every version pin in the repo must name the SAME release.

        This is the bug that made the fleet useless for months and nothing caught it:
        each reusable workflow fetches its OWN scripts by tag, so there are two pins per
        caller and they drifted. Callers asked for @v1.3.1, whose workflow said
        `ref: v1.2.0`, whose workflow said `ref: v1.0.0` — so 12 repos ran the very first
        release, with four dead providers and none of the fixes. Asserting mutual agreement
        works at any commit (unlike comparing against the newest git tag, which cannot
        exist yet on the commit that is about to be tagged).
        """
        repo_root = os.path.abspath(os.path.join(_WORKFLOW_DIRS[0], "..", ".."))
        wf_dir = os.path.abspath(_WORKFLOW_DIRS[0])
        pins = {}  # "where" -> tag

        for name in ["pr-review.yml", "pr-reply.yml", "llm-task.yml"]:
            path = os.path.join(wf_dir, name)
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as f:
                content = f.read()
            m = re.search(r"repository: \S*ai-gateway\s*\n\s*ref: (v\d+\.\d+\.\d+)",
                          content)
            if m:
                pins[f"{name} (gateway checkout ref)"] = m.group(1)

        script = os.path.join(repo_root, "deploy-callers.sh")
        if os.path.exists(script):
            with open(script, encoding="utf-8") as f:
                m = re.search(r'VERSION_TAG="(v\d+\.\d+\.\d+)"', f.read())
            if m:
                pins["deploy-callers.sh VERSION_TAG"] = m.group(1)

        tpl_dir = os.path.join(repo_root, "caller-templates")
        if os.path.isdir(tpl_dir):
            for name in sorted(os.listdir(tpl_dir)):
                if not name.endswith((".yml", ".yaml")):
                    continue
                with open(os.path.join(tpl_dir, name), encoding="utf-8") as f:
                    for i, line in enumerate(f, 1):
                        m = re.search(r"uses: \S*ai-gateway/\S+@(v\d+\.\d+\.\d+)", line)
                        if m:
                            pins[f"caller-templates/{name}:{i}"] = m.group(1)

        assert pins, "found no version pins to check — did the layout change?"
        distinct = sorted(set(pins.values()))
        assert len(distinct) == 1, (
            "version pins disagree, so callers will execute a different release than "
            f"they name: {distinct}\n"
            + "\n".join(f"  {v}  <- {k}" for k, v in sorted(pins.items()))
        )

    def test_pinned_ref_is_not_stale(self):
        """The scripts at the pinned tag must be the scripts on HEAD.

        Mutual agreement between pins is not enough: every pin said v1.2.0 while HEAD was
        v1.3.1, so the fleet consistently ran code that was two releases behind and no test
        objected. What callers actually execute is `scripts/` + `models.json` AT THE PINNED
        TAG, so that tree is what has to be current. Bump the pins (and re-tag) whenever
        either changes.
        """
        repo_root = os.path.abspath(os.path.join(_WORKFLOW_DIRS[0], "..", ".."))
        path = os.path.join(os.path.abspath(_WORKFLOW_DIRS[0]), "pr-review.yml")
        if not os.path.exists(path):
            pytest.skip("pr-review.yml not present")
        with open(path, encoding="utf-8") as f:
            m = re.search(r"repository: \S*ai-gateway\s*\n\s*ref: (v\d+\.\d+\.\d+)",
                          f.read())
        if not m:
            pytest.skip("no pinned gateway ref found")
        pinned = m.group(1)

        if subprocess.run(["git", "rev-parse", "--verify", f"{pinned}^{{commit}}"],
                          cwd=repo_root, capture_output=True).returncode != 0:
            pytest.skip(f"tag {pinned} not available (shallow clone without tags)")

        r = subprocess.run(
            ["git", "diff", "--name-only", pinned, "HEAD", "--", "scripts", "models.json"],
            cwd=repo_root, capture_output=True, text=True)
        assert r.returncode == 0, f"git diff failed: {r.stderr.strip()}"
        changed = [ln for ln in r.stdout.splitlines() if ln.strip()]
        assert not changed, (
            f"pinned ref {pinned} is stale: callers fetch their scripts from {pinned}, but "
            "these files have changed on HEAD, so the fleet runs old code:\n"
            + "\n".join(f"  {c}" for c in changed)
            + f"\nBump every pin to the next release and tag it (currently {pinned})."
        )
