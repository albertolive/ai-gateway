"""Generic LLM task runner for the gateway (release notes, triage, etc.).

Reads a prompt from PROMPT_FILE, runs it through the free-provider cascade,
writes the result to gateway_output.md, the job summary, and GITHUB_OUTPUT
(as `result`) so caller workflows can consume it.

Env: PROMPT_FILE (required), TASK_INTENT (default "general"),
     SYSTEM_PROMPT (optional), plus provider API keys.
"""

import os
import sys

import gateway


def main():
    prompt_file = os.environ.get("PROMPT_FILE", "prompt.txt")
    if not os.path.exists(prompt_file):
        print(f"Prompt file '{prompt_file}' not found")
        sys.exit(1)
    with open(prompt_file, encoding="utf-8") as f:
        prompt = f.read()
    if not prompt.strip():
        print("Prompt file is empty")
        sys.exit(1)

    result, provider = gateway.complete(
        prompt,
        system=os.environ.get("SYSTEM_PROMPT") or None,
        intent=os.environ.get("TASK_INTENT", "general"),
    )

    with open("gateway_output.md", "w", encoding="utf-8") as f:
        f.write(result)
    print(f"Output written via {provider} ({len(result)} chars)")

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write(f"## AI Gateway output ({provider})\n\n{result}\n")

    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a", encoding="utf-8") as f:
            # heredoc-style multiline output
            f.write(f"result<<GATEWAY_EOF\n{result}\nGATEWAY_EOF\n")
            f.write(f"provider={provider}\n")


if __name__ == "__main__":
    main()
