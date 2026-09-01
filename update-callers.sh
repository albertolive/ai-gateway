#!/usr/bin/env bash
# Update the gateway version tag in all fleet repos' caller workflows.
#
# You should rarely need this. Callers float on the major tag (@v1), so a normal
# release reaches the fleet with `git tag -f v1 && git push -f origin v1` and no
# per-repo edit. Run this only for a MAJOR bump (@v1 -> @v2), which by design does
# not arrive automatically, or to migrate repos still on an old exact tag.
#
# Usage:
#   ./update-callers.sh v2           # move the fleet to the v2 major line
#   ./update-callers.sh v2 --dry-run # show what would change without pushing
set -euo pipefail

NEW_TAG="${1:?Usage: ./update-callers.sh v2 [--dry-run]}"
DRY_RUN="${2:-}"
# Absolute: this script cd's into a temp workspace before reading the fleet file, so a
# relative path resolves against the wrong directory and the loop reads nothing.
FLEET_FILE="$(cd "$(dirname "$0")" && pwd)/fleet-repos.txt"

if ! gh auth status &>/dev/null; then
  echo "❌ gh CLI not authenticated. Run: gh auth login" >&2
  exit 1
fi

if [ ! -f "$FLEET_FILE" ]; then
  echo "❌ $FLEET_FILE not found." >&2
  exit 1
fi

# Verify the tag exists on the gateway repo before deploying
if ! gh api "repos/albertolive/ai-gateway/git/refs/tags/$NEW_TAG" &>/dev/null; then
  echo "❌ Tag $NEW_TAG does not exist on albertolive/ai-gateway." >&2
  echo "   Create it first: git tag $NEW_TAG && git push origin $NEW_TAG" >&2
  exit 1
fi

echo "🚀 Updating fleet to ai-gateway@$NEW_TAG..."
[ "$DRY_RUN" = "--dry-run" ] && echo "   (dry run — no pushes)"

WORKSPACE=$(mktemp -d)
trap 'rm -rf "$WORKSPACE"' EXIT
cd "$WORKSPACE"

updated=0
skipped=0

while IFS= read -r repo || [ -n "$repo" ]; do
  [[ "$repo" =~ ^[[:space:]]*# ]] && continue
  repo=$(echo "$repo" | tr -d '[:space:]')
  [[ -z "$repo" ]] && continue
  name=$(basename "$repo")

  echo -n "  $repo: "
  if ! gh repo clone "$repo" "$name" -- --depth 1 -q 2>/dev/null; then
    echo "skip (cannot clone)"
    ((skipped++)) || true
    continue
  fi

  cd "$name"
  wf=".github/workflows/ai-review.yml"
  if [ ! -f "$wf" ]; then
    echo "skip (no ai-review.yml — run deploy-callers.sh first)"
    cd .. && rm -rf "$name"
    ((skipped++)) || true
    continue
  fi

  # Replace any @vN, @vN.N, @vN.N.N or @main with the new tag.
  # \{1,\} not \+ : BSD sed (macOS) treats \+ in a basic regex as a LITERAL plus, so the
  # old pattern never matched, git saw no change, and the loop printed "already at <tag>".
  # Every run reported success and did nothing — which is why repos stayed on @v1.0.0.
  sed -i.bak "s|@v[0-9]\{1,\}\(\.[0-9]\{1,\}\)\{0,2\}|@$NEW_TAG|g; s|@main|@$NEW_TAG|g" "$wf"
  rm -f "$wf.bak"

  git add "$wf"
  if git diff --staged --quiet 2>/dev/null; then
    echo "already at $NEW_TAG"
    cd .. && rm -rf "$name"
    ((skipped++)) || true
  else
    if [ "$DRY_RUN" = "--dry-run" ]; then
      echo "would update to $NEW_TAG"
      cd .. && rm -rf "$name"
    else
      git commit -q -m "ci: bump ai-gateway caller to $NEW_TAG"
      if git push -q origin "$(git branch --show-current)" 2>/dev/null; then
        echo "✅ updated to $NEW_TAG"
        ((updated++)) || true
      else
        echo "❌ push failed"
      fi
      cd .. && rm -rf "$name"
    fi
  fi
done < "$FLEET_FILE"

echo
echo "Done: $updated updated, $skipped skipped."
[ "$DRY_RUN" = "--dry-run" ] && echo "(dry run — no changes pushed)"
echo
echo "Next: verify the new tag works by opening a test PR on one repo."
