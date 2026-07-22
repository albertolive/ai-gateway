"""Persistent review memory, CodeRabbit-style, with zero infrastructure.

Learnings live in a plain GitHub issue in the target repo (found via a
hidden marker in its body). Humans can read, edit, or delete any learning
by editing the issue — the store is fully transparent.

Write path: when someone corrects the bot in a review thread, reply.py
extracts a durable guideline and appends it here.
Read path: review.py and reply.py prepend the learnings to every prompt.

Needs `issues: write` on the workflow. All calls best-effort.
"""

import datetime
import json
import urllib.request

MARKER = "<!-- ai-gateway:learnings -->"
TITLE = "AI Review Learnings"
CAP_BODY = 20000

HEADER = (
    f"{MARKER}\n"
    "This issue is the AI reviewer's persistent memory for this repository. "
    "Each bullet below is applied to every future review. Edit or delete "
    "freely — the bot only ever appends.\n\n## Learnings\n"
)


def _api(repo, path, token, payload=None, method=None):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}{path}",
        data=json.dumps(payload).encode("utf-8") if payload else None,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "Content-Type": "application/json"},
        method=method or ("POST" if payload else "GET"))
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8") or "null")


def _find_issue(repo, token):
    """Return (number, body) of the learnings issue, or None."""
    for page in range(1, 4):
        try:
            issues = _api(repo, f"/issues?state=open&per_page=100&page={page}",
                          token)
        except Exception:
            return None
        if not issues:
            return None
        for i in issues:
            if "pull_request" not in i and MARKER in (i.get("body") or ""):
                return i["number"], i.get("body") or ""
        if len(issues) < 100:
            return None
    return None


def get(repo, token):
    """Learnings text for prompts, or empty string."""
    try:
        found = _find_issue(repo, token)
    except Exception:
        return ""
    if not found:
        return ""
    body = found[1]
    text = body.split("## Learnings", 1)[-1].strip()
    return text if text else ""


def add(repo, token, learning, source_url=""):
    """Append one learning bullet; create the issue on first use."""
    learning = " ".join(str(learning).split())[:500]
    if not learning:
        return False
    date = datetime.date.today().isoformat()
    bullet = f"- {learning} _({date}"
    bullet += f", [source]({source_url}))_" if source_url else ")_"
    try:
        found = _find_issue(repo, token)
        if found:
            number, body = found
            if learning.lower() in body.lower():
                print("  learnings: duplicate, skipped")
                return False
            new_body = (body.rstrip() + "\n" + bullet)[:CAP_BODY]
            _api(repo, f"/issues/{number}", token, {"body": new_body},
                 method="PATCH")
        else:
            _api(repo, "/issues", token,
                 {"title": TITLE, "body": HEADER + bullet})
        print(f"  learnings: saved -> {learning[:80]}")
        return True
    except Exception as e:
        print(f"  learnings: could not save ({e})")
        return False
