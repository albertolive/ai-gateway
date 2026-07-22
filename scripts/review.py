"""AI pull-request reviewer with incremental reviews and live context.

Pipeline:
1. Incremental: find the last-reviewed SHA (hidden marker in the bot's
   previous review summary) and diff only prev..head. Full diff on first
   review or after a force-push.
2. Context: repo guidelines (.ai-review.md), dependency manifests,
   live llms.txt framework docs (see context.py), and cross-file impact
   analysis (see impact.py — finds references to changed symbols in
   files NOT in this PR, catching the #1 cross-file bug class).
3. Anti-hallucination: the diff is pre-annotated with absolute line
   numbers; every model comment is validated against real added lines.
4. Posting: inline review with committable ```suggestion blocks via the
   Reviews API; falls back to a plain summary comment on rejection.

Required env: GH_TOKEN, GITHUB_REPOSITORY, PR_NUMBER, COMMIT_SHA
Optional env: TARGET_DIR (PR head checkout, enables context+incremental),
              PR_DIFF_PATH (default pr_diff.txt), MAX_DIFF_CHARS,
              provider API keys (>=1 required)
"""

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

import context
import gateway
import impact
import learnings
import lint

MAX_DIFF_CHARS = int(os.environ.get("MAX_DIFF_CHARS", "200000"))
MARKER_RE = re.compile(r"ai-gateway:last_reviewed_sha=([0-9a-f]{7,40})")

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "Overall markdown review summary for the PR.",
        },
        "comments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "line": {"type": "integer"},
                    "start_line": {
                        "type": ["integer", "null"],
                        "description": "First line of a multi-line comment, "
                                       "or null for single-line.",
                    },
                    "body": {"type": "string"},
                },
                "required": ["path", "line", "start_line", "body"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "comments"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = (
    "You are a pragmatic senior software engineer doing an automated code "
    "review. Each diff line is annotated as `path::line::[TYPE]:: code`.\n"
    "Rules:\n"
    "- Only raise real issues: bugs, security flaws, data loss, race "
    "conditions, broken logic, clear performance problems. No style nits.\n"
    "- Judge framework API usage against the provided dependency versions "
    "and documentation, not your training-data assumptions.\n"
    "- Follow any repository review guidelines provided.\n"
    "- Only comment on lines marked [ADDED]. Copy the exact path and line "
    "integer from the annotation; never compute or guess line numbers.\n"
    "- Keep each comment short and actionable. When you can propose a "
    "drop-in fix, include a GitHub ```suggestion block containing ONLY the "
    "replacement for the commented line range.\n"
    "- If the changes look fine, return an empty comments array and say so "
    "in the summary.\n"
    "- At most 10 comments; pick the highest-impact issues."
)


def parse_diff(diff_text):
    """Return (annotated_text, {path: set(added_line_numbers)})."""
    out, added = [], {}
    path, new_line = None, 0
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
            added.setdefault(path, set())
            continue
        if line.startswith("+++ /dev/null"):
            path = None  # deleted file: nothing to comment on
            continue
        if line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            new_line = int(m.group(1)) if m else 0
            continue
        if path is None or line.startswith("---"):
            continue
        if line.startswith("+"):
            out.append(f"{path}::{new_line}::[ADDED]:: {line[1:]}")
            added[path].add(new_line)
            new_line += 1
        elif line.startswith(" "):
            out.append(f"{path}::{new_line}::[CONTEXT]:: {line[1:]}")
            new_line += 1
        # "-" lines don't exist in the new file: skip, don't advance counter
    return "\n".join(out), added


def validate_comments(comments, added):
    """Keep only comments that target real added lines."""
    valid = []
    for c in comments:
        try:
            path, line = c["path"], int(c["line"])
            body = str(c["body"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if not body or line not in added.get(path, set()):
            print(f"  drop invalid comment target {c.get('path')}:{c.get('line')}")
            continue
        entry = {"path": path, "line": line, "side": "RIGHT", "body": body}
        start = c.get("start_line")
        if isinstance(start, int) and start < line and start in added.get(path, set()):
            entry["start_line"] = start
            entry["start_side"] = "RIGHT"
        valid.append(entry)
    return valid[:10]


def gh_api(url_path, token, payload=None, method=None):
    req = urllib.request.Request(
        f"https://api.github.com{url_path}",
        data=json.dumps(payload).encode("utf-8") if payload else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
        method=method or ("POST" if payload else "GET"),
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8") or "null")


def last_reviewed_sha(repo, pr, token):
    """Newest bot review containing the hidden marker, or None."""
    found = None
    for page in range(1, 6):
        try:
            reviews = gh_api(f"/repos/{repo}/pulls/{pr}/reviews"
                             f"?per_page=100&page={page}", token)
        except Exception as e:
            print(f"  could not list reviews: {e}")
            return None
        if not reviews:
            break
        for r in reviews:  # reviews are oldest-first; keep the last match
            m = MARKER_RE.search(r.get("body") or "")
            if m:
                found = m.group(1)
        if len(reviews) < 100:
            break
    return found


def incremental_diff(target_dir, prev_sha, head_sha):
    """git diff prev..head, or None if unavailable (force-push, shallow)."""
    try:
        r = subprocess.run(
            ["git", "diff", f"{prev_sha}..{head_sha}"],
            cwd=target_dir, capture_output=True, text=True, timeout=120)
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None


def main():
    token = os.environ.get("GH_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    pr = os.environ.get("PR_NUMBER")
    sha = os.environ.get("COMMIT_SHA")
    if not all([token, repo, pr, sha]):
        print("Missing required env (GH_TOKEN, GITHUB_REPOSITORY, PR_NUMBER, COMMIT_SHA)")
        sys.exit(1)
    target_dir = os.environ.get("TARGET_DIR", "")

    # 1. Choose diff: incremental when possible, full otherwise
    diff, mode, prev = None, "full", None
    if target_dir and os.path.isdir(target_dir):
        prev = last_reviewed_sha(repo, pr, token)
        if prev and prev != sha:
            diff = incremental_diff(target_dir, prev, sha)
            if diff is not None:
                mode = f"incremental ({prev[:7]}..{sha[:7]})"
        elif prev == sha:
            print("Head already reviewed; nothing to do.")
            return
    if diff is None:
        diff_path = os.environ.get("PR_DIFF_PATH", "pr_diff.txt")
        if not os.path.exists(diff_path):
            print(f"{diff_path} not found")
            sys.exit(1)
        with open(diff_path, encoding="utf-8", errors="replace") as f:
            diff = f.read()
    print(f"Review mode: {mode}")

    annotated, added = parse_diff(diff)
    if not annotated.strip() or not any(added.values()):
        print("No added lines to review; skipping.")
        return
    truncated = False
    if len(annotated) > MAX_DIFF_CHARS:
        annotated = annotated[:MAX_DIFF_CHARS]
        truncated = True

    # 2. Context: guidelines, manifests, fresh docs, learnings, lint findings
    ctx = (context.gather(target_dir, diff)
           if target_dir and os.path.isdir(target_dir) else "")

    prompt_parts = []
    learned = learnings.get(repo, token)
    if learned:
        prompt_parts.append("## Team learnings from past reviews (always "
                            "apply these)\n\n" + learned)
    if ctx:
        prompt_parts.append(ctx)
    if target_dir and os.path.isdir(target_dir):
        findings = lint.analyze(target_dir, lint.changed_files(added))
        block = lint.to_prompt_block(findings, added)
        if block:
            prompt_parts.append(block)
        # Cross-file impact: find references to changed symbols in files
        # NOT in this PR — catches renamed/deleted functions still called
        # elsewhere (the biggest gap vs. commercial repo-indexing reviewers).
        impact_block = impact.build_context(target_dir, diff)
        if impact_block:
            prompt_parts.append(impact_block)
    prompt_parts.append("## Annotated changes to review\n\n" + annotated)
    if truncated:
        prompt_parts.append("[NOTE: diff truncated for size — mention this "
                            "in the summary]")
    if mode.startswith("incremental"):
        prompt_parts.append("[NOTE: this is an incremental review of only "
                            "the commits pushed since your last review]")
    prompt = "\n\n".join(prompt_parts)

    # 3. Model call through the cascade
    data, provider = gateway.complete(
        prompt, system=SYSTEM_PROMPT, intent="code_review",
        schema=REVIEW_SCHEMA, schema_name="pr_review",
    )

    comments = validate_comments(data.get("comments", []), added)
    summary = (data.get("summary") or "Automated review complete.").strip()
    if mode.startswith("incremental"):
        summary = f"_Incremental review: {mode.split(' ', 1)[1]}_\n\n" + summary
    summary += (f"\n\n---\n<sub>AI review via free-tier gateway ({provider})."
                f" May be wrong — verify before acting.</sub>"
                f"\n\n<!-- ai-gateway:last_reviewed_sha={sha} -->")

    # 4. Post inline review, fall back to plain comment
    review_payload = {"commit_id": sha, "body": summary, "event": "COMMENT",
                      "comments": comments}
    try:
        gh_api(f"/repos/{repo}/pulls/{pr}/reviews", token, review_payload)
        print(f"Inline review posted ({len(comments)} comments)")
        return
    except urllib.error.HTTPError as e:
        print(f"Inline review rejected (HTTP {e.code}): {e.read().decode()[:300]}")

    parts = [summary]
    for c in comments:
        loc = f"`{c['path']}:{c.get('start_line', c['line'])}"
        loc += f"-{c['line']}`" if "start_line" in c else "`"
        parts.append(f"**{loc}**\n{c['body']}")
    gh_api(f"/repos/{repo}/issues/{pr}/comments", token,
           {"body": "\n\n---\n\n".join(parts)})
    print("Fallback summary comment posted")


if __name__ == "__main__":
    main()
