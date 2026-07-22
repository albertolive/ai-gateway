"""Conversational replies to review-comment threads (CodeRabbit-style).

Triggered when someone replies inside a review thread the bot started.
Fetches the whole thread + the diff hunk, asks the gateway for a reply,
and posts it back into the same thread.

Loop/abuse protection:
- exits silently unless the thread ROOT was authored by the bot
- exits silently if the triggering comment is from any bot
- thread transcript is size-capped

Required env: GH_TOKEN, GITHUB_REPOSITORY, PR_NUMBER, COMMENT_ID
Optional env: provider API keys (>=1 required)
"""

import json
import os
import sys
import urllib.request

import gateway
import learnings

BOT_LOGINS = {"github-actions[bot]"}
CAP_TRANSCRIPT = 20000

REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string",
                  "description": "Markdown reply to post in the thread."},
        "learning": {
            "type": ["string", "null"],
            "description": "A durable, repo-wide guideline this exchange "
                           "revealed (e.g. 'X is intentional legacy "
                           "behavior, do not flag it'), phrased as an "
                           "instruction for future reviews. null if the "
                           "exchange contains nothing worth remembering.",
        },
    },
    "required": ["reply", "learning"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = (
    "You are the AI code reviewer continuing a discussion on a pull request "
    "review comment you made earlier. Read the diff hunk and the thread, "
    "then reply to the latest message.\n"
    "- Be direct and short. Concede when the human is right.\n"
    "- If they explain a constraint (legacy behavior, intentional choice), "
    "adjust your advice accordingly instead of repeating it.\n"
    "- If an updated fix helps, include a ```suggestion block only if it "
    "replaces exactly the originally commented lines; otherwise show a "
    "normal code block.\n"
    "- Also decide whether the human taught you a durable guideline that "
    "should shape FUTURE reviews of this repository (a convention, an "
    "intentional pattern, something to stop flagging). Only extract "
    "genuine, general rules — not one-off details of this PR."
)


def gh_get(url_path, token):
    req = urllib.request.Request(
        f"https://api.github.com{url_path}",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def gh_post(url_path, payload, token):
    req = urllib.request.Request(
        f"https://api.github.com{url_path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.status


def main():
    token = os.environ.get("GH_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    pr = os.environ.get("PR_NUMBER")
    comment_id = os.environ.get("COMMENT_ID")
    if not all([token, repo, pr, comment_id]):
        print("Missing required env")
        sys.exit(1)

    trigger = gh_get(f"/repos/{repo}/pulls/comments/{comment_id}", token)
    if trigger.get("user", {}).get("type") == "Bot" or \
       trigger.get("user", {}).get("login") in BOT_LOGINS:
        print("Trigger comment is from a bot; ignoring to avoid loops.")
        return

    root_id = trigger.get("in_reply_to_id") or trigger["id"]
    root = gh_get(f"/repos/{repo}/pulls/comments/{root_id}", token)
    if root.get("user", {}).get("login") not in BOT_LOGINS:
        print("Thread was not started by the AI reviewer; ignoring.")
        return

    # Collect the whole thread in order
    thread, page = [root], 1
    while page <= 5:
        batch = gh_get(f"/repos/{repo}/pulls/{pr}/comments"
                       f"?per_page=100&page={page}", token)
        if not batch:
            break
        thread += [c for c in batch if c.get("in_reply_to_id") == root_id]
        if len(batch) < 100:
            break
        page += 1
    thread.sort(key=lambda c: c.get("created_at", ""))

    transcript = "\n\n".join(
        f"**{c['user']['login']}"
        f"{' (you, the AI reviewer)' if c['user']['login'] in BOT_LOGINS else ''}"
        f":**\n{c['body']}"
        for c in thread)[:CAP_TRANSCRIPT]

    learned = learnings.get(repo, token)
    prompt = (
        (f"## Existing team learnings (already known)\n{learned}\n\n"
         if learned else "")
        + f"File: `{root.get('path')}`\n\n"
        f"Diff hunk under discussion:\n```diff\n{root.get('diff_hunk', '')}\n```\n\n"
        f"Thread:\n\n{transcript}\n\n"
        "Write your reply to the latest message, and extract a learning "
        "if (and only if) there is one."
    )

    data, provider = gateway.complete(prompt, system=SYSTEM_PROMPT,
                                      intent="code_review",
                                      schema=REPLY_SCHEMA,
                                      schema_name="thread_reply")
    text = (data.get("reply") or "").strip() or "Understood."
    saved = False
    learning = data.get("learning")
    if learning and str(learning).strip().lower() not in ("null", "none", ""):
        saved = learnings.add(repo, token, learning,
                              trigger.get("html_url", ""))
    body = text
    if saved:
        body += "\n\n_🧠 Noted — I'll apply this in future reviews " \
                "(see the \"AI Review Learnings\" issue)._"
    body += f"\n\n<sub>AI reply via free-tier gateway ({provider}).</sub>"
    status = gh_post(f"/repos/{repo}/pulls/{pr}/comments/{root_id}/replies",
                     {"body": body}, token)
    print(f"Thread reply posted, HTTP {status}")


if __name__ == "__main__":
    main()
