#!/usr/bin/env bash
# Set or rotate AI gateway API key secrets across the entire fleet.
#
# Reads fleet-repos.txt for the repo list and ORGS below for org-level secrets.
# Idempotent: safe to re-run (overwrites existing values).
#
# Usage:
#   ./set-secrets.sh                    # interactive — prompts for each key
#   OPENROUTER_API_KEY=sk-... GEMINI_API_KEY=AIza... GROQ_API_KEY=gsk_... ./set-secrets.sh
#   ./set-secrets.sh --list             # show current secret status across fleet
#
# What it does:
#   1. Org-level secrets on Esdeveniments + twinfoundation-community (covers all org repos)
#   2. Repo-level secrets on each personal repo in fleet-repos.txt
#   3. Repo-level secrets on ai-gateway itself (for model-watch workflow)
#
# When to re-run:
#   - When you rotate an API key
#   - When you add new repos to fleet-repos.txt
#   - When you deploy the caller to a new repo for the first time
set -euo pipefail

GATEWAY_REPO="albertolive/ai-gateway"
ORGS=("Esdeveniments" "twinfoundation-community")
FLEET_FILE="$(dirname "$0")/fleet-repos.txt"

# Secrets to manage: name -> prompt label
declare -A SECRET_NAMES=(
  ["OPENROUTER_API_KEY"]="OpenRouter API key (sk-or-...)"
  ["GEMINI_API_KEY"]="Google AI Studio API key (AIza...)"
  ["GROQ_API_KEY"]="Groq API key (gsk_...)"
  ["CONTEXT7_API_KEY"]="Context7 API key (optional, press Enter to skip)"
)

if ! gh auth status &>/dev/null; then
  echo "❌ gh CLI not authenticated. Run: gh auth login" >&2
  exit 1
fi

if [ ! -f "$FLEET_FILE" ]; then
  echo "❌ $FLEET_FILE not found." >&2
  exit 1
fi

# --list mode: show which repos have which secrets
if [ "${1:-}" = "--list" ]; then
  echo "=== Org-level secrets ==="
  for org in "${ORGS[@]}"; do
    echo -n "$org: "
    gh secret list --org "$org" 2>&1 | grep -E 'OPENROUTER|GEMINI|GROQ|CONTEXT7' | tr '\n' ' ' || echo "(none or no access)"
    echo
  done
  echo
  echo "=== Repo-level secrets (personal repos) ==="
  while IFS= read -r repo; do
    [[ "$repo" =~ ^# ]] && continue
    [[ -z "$repo" ]] && continue
    repo=$(echo "$repo" | tr -d '[:space:]')
    # Skip org repos (they inherit org-level secrets)
    if [[ "$repo" == */* ]] && [[ " ${ORGS[*]} " == *" $(echo "$repo" | cut -d/ -f1) "* ]]; then
      continue
    fi
    echo -n "$repo: "
    gh secret list -R "$repo" 2>&1 | grep -E 'OPENROUTER|GEMINI|GROQ|CONTEXT7' | tr '\n' ' ' || echo "(none)"
    echo
  done < "$FLEET_FILE"
  echo
  echo -n "$GATEWAY_REPO: "
  gh secret list -R "$GATEWAY_REPO" 2>&1 | grep -E 'OPENROUTER|GEMINI|GROQ|CONTEXT7' | tr '\n' ' ' || echo "(none)"
  echo
  exit 0
fi

# Collect key values (from env vars or interactive prompt)
declare -A SECRET_VALUES
for name in "${!SECRET_NAMES[@]}"; do
  if [ -n "${!name:-}" ]; then
    SECRET_VALUES["$name"]="${!name}"
  else
    read -s -p "${SECRET_NAMES[$name]}: " value
    echo
    SECRET_VALUES["$name"]="$value"
  fi
done

echo
echo "🚀 Distributing secrets to fleet..."

# 1. Org-level secrets (covers all current + future org repos)
for org in "${ORGS[@]}"; do
  echo "  org: $org"
  for name in "${!SECRET_VALUES[@]}"; do
    value="${SECRET_VALUES[$name]}"
    [ -z "$value" ] && continue
    if gh secret set "$name" --org "$org" --body "$value" 2>/dev/null; then
      echo "    ✅ $name"
    else
      echo "    ⚠️  $name (may need org admin access)"
    fi
  done
done

# 2. Personal repos (per-repo, since personal accounts lack org-level secrets)
while IFS= read -r repo; do
  [[ "$repo" =~ ^# ]] && continue
  [[ -z "$repo" ]] && continue
  repo=$(echo "$repo" | tr -d '[:space:]')
  # Skip org repos — they're covered by org-level secrets above
  owner=$(echo "$repo" | cut -d/ -f1)
  if [[ " ${ORGS[*]} " == *" $owner "* ]]; then
    echo "  $repo (org repo — covered by org-level secrets)"
    continue
  fi
  echo "  repo: $repo"
  for name in "${!SECRET_VALUES[@]}"; do
    value="${SECRET_VALUES[$name]}"
    [ -z "$value" ] && continue
    if gh secret set "$name" -R "$repo" --body "$value" 2>/dev/null; then
      echo "    ✅ $name"
    else
      echo "    ⚠️  $name (check repo access)"
    fi
  done
done < "$FLEET_FILE"

# 3. ai-gateway itself (for model-watch workflow)
echo "  repo: $GATEWAY_REPO (model-watch)"
for name in "${!SECRET_VALUES[@]}"; do
  value="${SECRET_VALUES[$name]}"
  [ -z "$value" ] && continue
  if gh secret set "$name" -R "$GATEWAY_REPO" --body "$value" 2>/dev/null; then
    echo "    ✅ $name"
  else
    echo "    ⚠️  $name"
  fi
done

echo
echo "🎉 Done. Secrets distributed to:"
echo "   - $(echo "${ORGS[*]}" | wc -w | tr -d ' ') orgs (org-level, covers all current + future repos)"
echo "   - $(grep -v '^#' "$FLEET_FILE" | grep -c 'albertolive') personal repos"
echo "   - ai-gateway repo"
echo
echo "Verify with: ./set-secrets.sh --list"
