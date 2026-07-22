#!/usr/bin/env bash
# Bulk-deploy the AI review caller workflow to many repos via gh CLI.
# Works for personal and org repos alike.
#
# Prereqs:
#   gh auth login   (token must include the `workflow` scope:
#                    gh auth refresh -s workflow)
#   git config user.name / user.email set globally
#
# Usage: edit GATEWAY_OWNER + TARGET_REPOSITORIES, then ./deploy-callers.sh
set -euo pipefail

GATEWAY_OWNER="albertolive"
VERSION_TAG="v1.0.0"

TARGET_REPOSITORIES=(
  # "owner/repo-one"
  # "owner/repo-two"
)

CALLER_TEMPLATE=$(cat <<EOF
name: AI PR Review

on:
  pull_request:
    types: [opened, synchronize]
  pull_request_review_comment:
    types: [created]

jobs:
  review:
    if: github.event_name == 'pull_request'
    permissions:
      contents: read
      pull-requests: write
      issues: write
    uses: ${GATEWAY_OWNER}/ai-gateway/.github/workflows/pr-review.yml@${VERSION_TAG}
    secrets:
      OPENROUTER_API_KEY: \${{ secrets.OPENROUTER_API_KEY }}
      GEMINI_API_KEY: \${{ secrets.GEMINI_API_KEY }}
      GROQ_API_KEY: \${{ secrets.GROQ_API_KEY }}
      CONTEXT7_API_KEY: \${{ secrets.CONTEXT7_API_KEY }}

  reply:
    if: >
      github.event_name == 'pull_request_review_comment' &&
      github.event.comment.user.type != 'Bot' &&
      github.event.comment.in_reply_to_id != null
    permissions:
      contents: read
      pull-requests: write
      issues: write
    uses: ${GATEWAY_OWNER}/ai-gateway/.github/workflows/pr-reply.yml@${VERSION_TAG}
    secrets:
      OPENROUTER_API_KEY: \${{ secrets.OPENROUTER_API_KEY }}
      GEMINI_API_KEY: \${{ secrets.GEMINI_API_KEY }}
      GROQ_API_KEY: \${{ secrets.GROQ_API_KEY }}
EOF
)

if [ ${#TARGET_REPOSITORIES[@]} -eq 0 ]; then
  echo "Edit TARGET_REPOSITORIES first." >&2
  exit 1
fi
if ! gh auth status &>/dev/null; then
  echo "gh CLI not authenticated. Run: gh auth login" >&2
  exit 1
fi

WORKSPACE=$(mktemp -d)
trap 'rm -rf "$WORKSPACE"' EXIT
cd "$WORKSPACE"

for repo in "${TARGET_REPOSITORIES[@]}"; do
  echo "== $repo"
  name=$(basename "$repo")
  if ! gh repo clone "$repo" "$name" -- --depth 1 -q; then
    echo "   skip: cannot clone (check permissions)"
    continue
  fi
  (
    cd "$name"
    mkdir -p .github/workflows
    printf '%s\n' "$CALLER_TEMPLATE" > .github/workflows/ai-review.yml
    git add .github/workflows/ai-review.yml
    if git diff --staged --quiet; then
      echo "   already up to date"
    else
      git commit -q -m "ci: add centralized AI PR review caller"
      git push -q origin "$(git branch --show-current)"
      echo "   deployed"
    fi
  )
  rm -rf "$name"
done

echo "Done. Remember: each repo (or the org) needs the API-key secrets set."
echo "Per-repo example:"
echo '  gh secret set OPENROUTER_API_KEY -R owner/repo --body "sk-or-..."'
