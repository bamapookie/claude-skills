#!/usr/bin/env bash
# Record this session's content-free timeline onto the metrics/session-timelogs branch.
# Run from the repository root. Never touches the working tree or current branch.
set -euo pipefail

BRANCH="${TIMELOG_BRANCH:-metrics/session-timelogs}"
REMOTE="${TIMELOG_REMOTE:-origin}"
REPO_ROOT="$(git rev-parse --show-toplevel)"

# 1. Locate this session's transcript directory (repo root path, non-alnum -> '-').
SAN="$(printf '%s' "$REPO_ROOT" | sed 's/[^a-zA-Z0-9]/-/g')"
PROJ_DIR="$HOME/.claude/projects/$SAN"
[ -d "$PROJ_DIR" ] || { echo "ERROR: no transcript dir at $PROJ_DIR" >&2; exit 1; }

MAIN="$(ls -t "$PROJ_DIR"/*.jsonl 2>/dev/null | head -1 || true)"
[ -n "$MAIN" ] || { echo "ERROR: no main transcript found in $PROJ_DIR" >&2; exit 1; }
SID="$(basename "$MAIN" .jsonl)"
SID8="${SID:0:8}"
STAMP="$(date -u +%Y-%m-%d)"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
OUT="$TMP/extract"
mkdir -p "$OUT"

# 2. Extract timestamps-only ({timestamp, type} is field-lookup shorthand, not builtins).
extract() { # $1=src $2=dst
  jq -c 'select(type=="object" and .timestamp!=null) | {timestamp, type, sessionId}' "$1" \
    | LC_ALL=C sort > "$2"
}
extract "$MAIN" "$OUT/$STAMP-$SID8.timeline.jsonl"
if [ -d "$PROJ_DIR/$SID/subagents" ]; then
  for f in "$PROJ_DIR/$SID/subagents"/agent-*.jsonl; do
    [ -e "$f" ] || continue
    extract "$f" "$OUT/$STAMP-$SID8.$(basename "$f" .jsonl).timeline.jsonl"
  done
fi

# 3. Scan clean: content-free extracts must never carry these.
HITS=$(grep -rcE '@|/home/|/root/|proxy|trustStore' "$OUT" | awk -F: '{s+=$2} END{print s+0}' || true)
if [ "$HITS" -ne 0 ]; then
  echo "ERROR: extract failed the cleanliness scan ($HITS hits) — not committing." >&2
  grep -rlE '@|/home/|/root/|proxy|trustStore' "$OUT" >&2
  exit 1
fi
N=$(cat "$OUT"/*.jsonl | wc -l)
echo "extracted $N timestamped events from session $SID8 ($(ls "$OUT" | wc -l) file(s)), scan clean"

# 4. Commit to the tracking branch via a temporary worktree.
git fetch "$REMOTE" "$BRANCH" 2>/dev/null || true
if ! git rev-parse --verify --quiet "refs/remotes/$REMOTE/$BRANCH" >/dev/null \
   && ! git rev-parse --verify --quiet "refs/heads/$BRANCH" >/dev/null; then
  # Orphan init: an empty commit with no parents, without touching the working tree.
  EMPTY_TREE=$(git hash-object -t tree /dev/null)
  INIT=$(git commit-tree "$EMPTY_TREE" -m "init: session timelog tracking branch (no PRs; consumed by time-report)")
  git branch "$BRANCH" "$INIT"
fi
WT="$TMP/wt"
if git rev-parse --verify --quiet "refs/heads/$BRANCH" >/dev/null; then
  git worktree add "$WT" "$BRANCH" --quiet
else
  git worktree add "$WT" -b "$BRANCH" "$REMOTE/$BRANCH" --quiet
fi
(
  cd "$WT"
  git pull --ff-only "$REMOTE" "$BRANCH" 2>/dev/null || true
  mkdir -p timelines
  cp "$OUT"/*.jsonl timelines/
  git add timelines
  if git diff --cached --quiet; then
    echo "no changes since last snapshot — nothing to commit"
  else
    git commit --quiet -m "timelog: session $SID8 snapshot $(date -u +%Y-%m-%dT%H:%MZ) ($N events)"
    pushed=0
    for delay in 0 2 4 8 16; do
      sleep "$delay"
      if git push -u "$REMOTE" "$BRANCH"; then pushed=1; break; fi
    done
    if [ "$pushed" -ne 1 ]; then
      echo "ERROR: push to $REMOTE/$BRANCH failed after all retries — timeline NOT recorded" >&2
      exit 1
    fi
  fi
) || PUSH_FAILED=1
git worktree remove --force "$WT"
[ -z "${PUSH_FAILED:-}" ] || exit 1
echo "done: timelines on branch $BRANCH (no PR — consumed by the time-report skill)"
