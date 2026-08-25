#!/usr/bin/env bash
# Five-minute poll loop. The hourly Actions cron is only a backstop.
set -uo pipefail
cd "$(dirname "$0")"

POLL_INTERVAL=${POLL_INTERVAL:-300}
MAX_RUNTIME=${MAX_RUNTIME:-20400}
COMMIT_EVERY=${COMMIT_EVERY:-1800}

in_ci() { [ "${GITHUB_ACTIONS:-}" = "true" ]; }

commit_state() {
  in_ci || return 0
  if [ -n "$(git status --porcelain state.json 2>/dev/null)" ]; then
    git add state.json \
      && git commit -q -m "Update layoff monitor state [skip ci]" \
      && git push -q || true
  fi
}

if in_ci; then
  git config user.name "github-actions[bot]"
  git config user.email "github-actions[bot]@users.noreply.github.com"
fi

start=$(date +%s)
last_commit=0
while [ $(( $(date +%s) - start )) -lt "$MAX_RUNTIME" ]; do
  python3 layoff_monitor.py "$@" || true
  now=$(date +%s)
  if [ $(( now - last_commit )) -ge "$COMMIT_EVERY" ]; then
    commit_state
    last_commit=$now
  fi
  if in_ci; then
    git fetch -q origin main 2>/dev/null || true
    if ! git diff --quiet HEAD origin/main -- config.json layoff_monitor.py loop.sh \
         .github/workflows/monitor.yml 2>/dev/null; then
      echo "loop: upstream code/config changed — restarting"
      break
    fi
  fi
  sleep "$POLL_INTERVAL"
done

commit_state
in_ci && (gh workflow run monitor.yml >/dev/null 2>&1 || true)
exit 0
