#!/bin/bash
# Auto-commit hook: runs on Stop, commits staged changes following dev-workflow.md conventions.
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
  else
    BTYPE="feat"
  fi
  git checkout -b "${BTYPE}/auto-$(date +%Y%m%d-%H%M)"
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
else
  CTYPE="feat"
fi

FIRST=$(echo "$STAGED" | head -1)
SCOPE=$(echo "$FIRST" | cut -d/ -f1 | sed 's/\.[^.]*$//')
[ "$SCOPE" = "$FIRST" ] && SCOPE="root"

COUNT=$(echo "$STAGED" | wc -l | tr -d ' ')

git commit -m "${CTYPE}(${SCOPE}): auto-commit ${COUNT} file(s)" \
  -m "" \
  -m "Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
