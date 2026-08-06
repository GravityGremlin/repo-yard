# Repo Yard deploy tooling — shared helpers.
# Source this file from deploy scripts: . "$(dirname "$0")/lib.sh"

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_YARD_DIR="$(dirname "$SCRIPT_DIR")"          # the repo-yard checkout
TOOLS_ROOT="$(dirname "$REPO_YARD_DIR")"          # parent dir holding the tool repos (siblings of repo-yard)
SERVICES_CONF="$SCRIPT_DIR/services.conf"
LOG_DIR="$SCRIPT_DIR/log"
LOG_FILE="$LOG_DIR/deploy.log"

CTHULHU_SSH="${CTHULHU_SSH:-cthulhu}"                       # ssh alias for the Docker host
REPO_YARD_REMOTE_DIR="${REPO_YARD_REMOTE_DIR:-$REPO_YARD_DIR}"  # repo-yard path on cthulhu
BRANCH="${BRANCH:-main}"
# Host address the containers publish on (compose binds 10.8.0.10, not 0.0.0.0 —
# health checks must hit this, not 127.0.0.1).
HOST_ADDR="${HOST_ADDR:-10.8.0.10}"
DRY_RUN="${DRY_RUN:-0}"

SERVICES=(spotifryer qoochie tidalwave deeznutz music-hub repo-yard)

log() {
  local line
  line="$(date -u +%Y-%m-%dT%H:%M:%SZ) $*"
  printf '%s\n' "$line"
  [ "$DRY_RUN" = "1" ] || printf '%s\n' "$line" >> "$LOG_FILE"
}

# Run a shell command string, honoring --dry-run.
runc() {
  if [ "$DRY_RUN" = "1" ]; then
    printf '[dry-run] %s\n' "$1"
    return 0
  fi
  eval "$1"
}

# If not running on the Docker host (cthulhu), re-execute the caller over SSH.
ensure_on_host() {
  if [ "${REPO_YARD_ON_CTHULHU:-}" = "1" ]; then
    return 0
  fi
  case "$(hostname)" in
    cthulhu*) export REPO_YARD_ON_CTHULHU=1; return 0 ;;
  esac
  if [ "$DRY_RUN" = "1" ]; then
    printf '[dry-run] would ssh %s: cd %s \&\& REPO_YARD_ON_CTHULHU=1 bash %s %s\n' \
      "$CTHULHU_SSH" "$REPO_YARD_REMOTE_DIR" "${BASH_SOURCE[1]##*/}" "$*"
    exit 0
  fi
  echo "[deploy] not on cthulhu — forwarding to $CTHULHU_SSH ($REPO_YARD_REMOTE_DIR)"
  local args=()
  for a in "$@"; do args+=( "$(printf '%q' "$a")" ); done
  exec ssh "$CTHULHU_SSH" \
    "cd '$REPO_YARD_REMOTE_DIR' && REPO_YARD_ON_CTHULHU=1 bash ${BASH_SOURCE[1]##*/} ${args[*]}"
}

# ── Service registry (from services.conf) ─────────────────────────────
declare -A SVC_GIT_URL SVC_COMPOSE SVC_CONTAINER SVC_PORT SVC_PATH SVC_MODE

load_services() {
  local name url compose container port path mode
  while IFS='|' read -r name url compose container port path mode; do
    [ -z "$name" ] && continue
    case "$name" in \#*) continue ;; esac
    SVC_GIT_URL[$name]=$url
    SVC_COMPOSE[$name]=$compose
    SVC_CONTAINER[$name]=$container
    SVC_PORT[$name]=$port
    SVC_PATH[$name]=$path
    SVC_MODE[$name]=$mode
  done < "$SERVICES_CONF"
}

# ── Health gates ──────────────────────────────────────────────────────
# Wait until the host-port route answers 2xx/3xx, or time out.
wait_healthy() {
  local name=$1 port=$2 path=$3 timeout=${4:-120}
  if [ "$DRY_RUN" = "1" ]; then
    printf '[dry-run] health gate: expect 2xx/3xx @ http://%s:%s%s\n' "$HOST_ADDR" "$port" "$path"
    return 0
  fi
  local url="http://${HOST_ADDR}:${port}${path}" code="" elapsed=0
  echo "[deploy] waiting for $name @ $url ..."
  while [ "$elapsed" -lt "$timeout" ]; do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url" 2>/dev/null || true)
    if [ -n "$code" ] && [ "$code" -ge 200 ] && [ "$code" -lt 400 ]; then
      echo "[deploy] $name healthy (HTTP $code)"
      return 0
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done
  echo "[deploy] $name NOT healthy after ${timeout}s (last HTTP code: ${code:-none})"
  return 1
}

# Fail if the container logs show a Python traceback.
check_traceback() {
  local name=$1
  if docker logs --tail 80 "$name" 2>&1 | grep -q 'Traceback'; then
    echo "[deploy] WARNING: Traceback found in $name logs"
    return 1
  fi
  return 0
}
