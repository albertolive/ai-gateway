"""Static-analysis layer: deterministic findings fed into the AI review.

Mirrors what commercial reviewers do (CodeRabbit runs ~25 analyzers and
gives the LLM their output). We run whatever is available on the runner,
only on files changed in the PR:

- ruff       -> Python lint + common bug patterns (installed by workflow)
- shellcheck -> shell scripts (preinstalled on ubuntu runners)
- built-in secrets scan -> regex detection of committed credentials
  (always runs, stdlib-only)

Every tool is best-effort: missing tools are skipped, never fatal.
Output: list of {path, line, tool, code, message} dicts.
"""

import json
import os
import re
import shutil
import subprocess

SECRET_PATTERNS = [
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")),
    ("generic-secret-assign", re.compile(
        r"""(?i)\b(?:api_?key|secret|password|token)\b\s*[:=]\s*['"][^'"]{16,}['"]""")),
]

_TIMEOUT = 90


def _run(cmd, cwd):
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              timeout=_TIMEOUT)
    except Exception:
        return None


def _ruff(target_dir, files):
    py = [f for f in files if f.endswith(".py")]
    if not py or not shutil.which("ruff"):
        return []
    r = _run(["ruff", "check", "--output-format", "json", "--exit-zero", *py],
             target_dir)
    if not r or not r.stdout.strip():
        return []
    try:
        return [{"path": os.path.relpath(f["filename"], target_dir)
                 if os.path.isabs(f["filename"]) else f["filename"],
                 "line": f["location"]["row"], "tool": "ruff",
                 "code": f.get("code") or "", "message": f["message"]}
                for f in json.loads(r.stdout)]
    except (ValueError, KeyError, TypeError):
        return []


def _shellcheck(target_dir, files):
    sh = [f for f in files if f.endswith((".sh", ".bash"))]
    if not sh or not shutil.which("shellcheck"):
        return []
    r = _run(["shellcheck", "--format", "json", *sh], target_dir)
    if not r or not r.stdout.strip():
        return []
    try:
        return [{"path": f["file"], "line": f["line"], "tool": "shellcheck",
                 "code": f"SC{f.get('code', '')}", "message": f["message"]}
                for f in json.loads(r.stdout)]
    except (ValueError, KeyError, TypeError):
        return []


def _secrets(target_dir, files):
    findings = []
    for path in files:
        full = os.path.join(target_dir, path)
        try:
            with open(full, encoding="utf-8", errors="replace") as f:
                lines = f.read(500000).splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            for name, pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    findings.append({
                        "path": path, "line": i, "tool": "secrets-scan",
                        "code": name,
                        "message": f"Possible hardcoded credential ({name}). "
                                   "If real, revoke it and move to a secret "
                                   "store; committed keys must be rotated."})
                    break  # one finding per line is enough
    return findings


def changed_files(added):
    """File list from review.parse_diff's added-lines map."""
    return sorted(p for p, lines in added.items() if lines)


def analyze(target_dir, files, max_findings=40):
    """Run all available analyzers; return capped findings list."""
    findings = []
    for runner in (_secrets, _ruff, _shellcheck):
        try:
            findings += runner(target_dir, files)
        except Exception as e:
            print(f"  lint: {runner.__name__} failed: {e}")
    findings.sort(key=lambda f: (f["tool"] != "secrets-scan", f["path"], f["line"]))
    for f in findings[:max_findings]:
        print(f"  lint: {f['tool']} {f['path']}:{f['line']} {f['code']} {f['message'][:80]}")
    return findings[:max_findings]


def to_prompt_block(findings, added):
    """Render findings that hit changed lines (plus secrets anywhere)."""
    relevant = [f for f in findings
                if f["line"] in added.get(f["path"], set())
                or f["tool"] == "secrets-scan"]
    if not relevant:
        return ""
    lines = ["## Static analysis findings (deterministic tools — validate "
             "each, drop false positives, and fold real ones into your "
             "review instead of duplicating them)", ""]
    lines += [f"- {f['path']}:{f['line']} [{f['tool']}:{f['code']}] {f['message']}"
              for f in relevant]
    return "\n".join(lines)
