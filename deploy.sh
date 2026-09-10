#!/usr/bin/env bash
# Deploy a committed revision to the existing user service. Never installs a server
# or prints environment/credentials. Usage: bash deploy.sh <commit-or-tag>
set -Eeuo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$ROOT"
SERVICE=${BRIDGE_SERVICE:-codex-provider-bridge.service}
HEALTH_URL=${BRIDGE_HEALTH_URL:-http://127.0.0.1:8000/health}
PYTHON=${BRIDGE_PYTHON:-$ROOT/.venv/bin/python}
[[ $# == 1 ]] || { echo 'Usage: bash deploy.sh <commit-or-tag>' >&2; exit 2; }
exec 9>"$ROOT/.git/bridge-deploy.lock"
flock -n 9 || { echo 'Another deployment is running' >&2; exit 1; }
[[ -z $(git status --porcelain) ]] || { echo 'Refusing deployment from a dirty worktree' >&2; exit 1; }
TARGET=$(git rev-parse --verify --end-of-options "$1^{commit}")
PREVIOUS=$(git rev-parse HEAD)
PREVIOUS_BRANCH=$(git symbolic-ref --quiet --short HEAD || true)
[[ $(systemctl --user show "$SERVICE" -p WorkingDirectory --value) == "$ROOT" ]] || {
  echo 'Service working directory does not match this checkout' >&2; exit 1;
}
systemctl --user is-active --quiet "$SERVICE" || { echo 'Existing service must be active' >&2; exit 1; }
health() {
  "$PYTHON" - "$HEALTH_URL" "$1" "${2:-}" <<'PY'
import json, sys, urllib.request
from urllib.parse import urlsplit
url, commit, old_start = sys.argv[1:]
if urlsplit(url).hostname not in {'localhost', '127.0.0.1', '::1'}:
    raise SystemExit('Health URL must be loopback')
try:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=3) as response:
        data = json.load(response)
    assert data['status'] == 'ok'
    if commit and commit != 'identify':
        assert data.get('deployment_commit') == commit
        assert data.get('started_at') and data['started_at'] != old_start
    if commit == 'identify':
        print(data.get('deployment_commit', 'unknown'))
    else:
        print(data.get('started_at', ''))
except Exception:
    raise SystemExit(1)
PY
}
OLD_START=$(health '')
DEPLOYED=$(health identify)
if [[ $DEPLOYED =~ ^[0-9a-fA-F]{40,64}$ ]]; then
  ROLLBACK=$(git rev-parse --verify --end-of-options "$DEPLOYED^{commit}")
elif [[ -n ${BRIDGE_ROLLBACK_COMMIT:-} ]]; then
  ROLLBACK=$(git rev-parse --verify --end-of-options "$BRIDGE_ROLLBACK_COMMIT^{commit}")
else
  echo 'Old health does not identify deployed revision; set BRIDGE_ROLLBACK_COMMIT to the known running commit' >&2
  exit 1
fi
RESTARTED=0
rollback() {
  status=$?
  trap - ERR INT TERM
  echo 'Deployment failed; restoring previous revision' >&2
  RESTORE=$PREVIOUS
  if [[ $RESTARTED == 1 ]]; then RESTORE=$ROLLBACK; fi
  git switch --detach "$RESTORE" --quiet || exit 1
  if [[ $RESTORE == "$PREVIOUS" && -n $PREVIOUS_BRANCH ]]; then git switch "$PREVIOUS_BRANCH" --quiet || exit 1; fi
  if [[ $RESTARTED == 1 ]]; then
    systemctl --user restart "$SERVICE" || exit 1
    for _ in {1..30}; do
      if health '' >/dev/null; then echo 'Previous revision restarted (liveness verified)' >&2; exit "$status"; fi
      sleep 1
    done
    echo 'CRITICAL: rollback health check failed; inspect service locally' >&2
  fi
  exit "$status"
}
trap rollback ERR
trap 'false' INT TERM
git switch --detach "$TARGET" --quiet
"$PYTHON" -m pytest -q
RESTARTED=1
systemctl --user restart "$SERVICE"
for _ in {1..30}; do
  if systemctl --user is-active --quiet "$SERVICE" && health "$TARGET" "$OLD_START" >/dev/null; then
    if [[ $TARGET == "$PREVIOUS" && -n $PREVIOUS_BRANCH ]]; then git switch "$PREVIOUS_BRANCH" --quiet; fi
    trap - ERR INT TERM
    echo "Deployment healthy: $TARGET"
    if [[ $TARGET != "$PREVIOUS" ]]; then echo 'Checkout is detached at deployed revision; no branch was moved'; fi
    exit 0
  fi
  sleep 1
done
false
