#!/bin/bash
# Auto-commit hook: runs on Stop.
# Commits staged changes, pushes branch, and creates a PR if none exists.
set -euo pipefail

REPO=/home/sheldongomes/AIProjects/scoring-pipeline
cd "$REPO"

# Exit silently if nothing is staged
STAGED=$(git diff --staged --name-only 2>/dev/null)
[ -z "$STAGED" ] && exit 0

# Never commit directly to main — create a feature branch first
BRANCH=$(git branch --show-current)
if [ "$BRANCH" = "main" ] || [ "$BRANCH" = "master" ]; then
  if echo "$STAGED" | grep -qE '^tests?/'; then
    BTYPE="test"
  elif echo "$STAGED" | grep -qE '\.md$'; then
    BTYPE="docs"
  elif echo "$STAGED" | grep -qE '^scripts/'; then
    BTYPE="chore"
  elif echo "$STAGED" | grep -qE '^src/'; then
    BTYPE="feat"
  else
    BTYPE="chore"
  fi
  BRANCH="${BTYPE}/auto-$(date +%Y%m%d-%H%M)"
  git checkout -b "$BRANCH"
fi

# Lint Python files if any are staged
if echo "$STAGED" | grep -qE '\.py$'; then
  /home/sheldongomes/.local/bin/ruff check . 2>&1 || echo "[auto-commit] ruff found lint issues — fix before merging" >&2
fi

# Infer conventional commit type and scope from staged files
if echo "$STAGED" | grep -qE '^tests?/'; then
  CTYPE="test"
elif echo "$STAGED" | grep -qE '\.md$'; then
  CTYPE="docs"
elif echo "$STAGED" | grep -qE '^scripts/'; then
  CTYPE="chore"
elif echo "$STAGED" | grep -qE '^src/'; then
  CTYPE="feat"
else
  CTYPE="chore"
fi

FIRST=$(echo "$STAGED" | head -1)
SCOPE=$(echo "$FIRST" | cut -d/ -f1 | sed 's/\.[^.]*$//')
[ "$SCOPE" = "$FIRST" ] && SCOPE="root"

COUNT=$(echo "$STAGED" | wc -l | tr -d ' ')

git commit -m "${CTYPE}(${SCOPE}): auto-commit ${COUNT} file(s)" \
  -m "" \
  -m "Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"

# Push branch to origin
echo "[auto-commit] Pushing branch ${BRANCH} to origin..."
git push -u origin "$BRANCH" 2>&1 || {
  echo "[auto-commit] Push failed — check network/auth. Branch committed locally." >&2
  exit 0
}

# Create a PR if none exists for this branch
EXISTING_PR=$(gh pr list --head "$BRANCH" --json number --jq '.[0].number' 2>/dev/null || echo "")
if [ -z "$EXISTING_PR" ]; then
  echo "[auto-commit] Creating PR for branch ${BRANCH}..."
  # Build a summary of changed files for the PR body
  CHANGED_FILES=$(echo "$STAGED" | sed 's/^/- /' | head -10)
  COUNT_FILES=$(echo "$STAGED" | wc -l | tr -d ' ')

  gh pr create \
    --title "${CTYPE}(${SCOPE}): auto-commit ${COUNT_FILES} file(s)" \
    --body "$(cat <<EOF
## What changed
${CHANGED_FILES}

## Why
Auto-committed by Claude Code at end of session.

## Test plan
- [ ] \`ruff check .\` passes with zero warnings
- [ ] \`pytest tests/\` passes (if tests present)
- [ ] Manually verified pipeline output looks correct

## Schema / API changes
None (verify if scoring output format changed)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)" \
    --draft 2>&1 || echo "[auto-commit] PR creation failed — push succeeded, create PR manually." >&2
else
  echo "[auto-commit] PR #${EXISTING_PR} already exists for ${BRANCH} — skipping PR creation."
fi
