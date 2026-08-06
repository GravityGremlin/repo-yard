#!/usr/bin/env bash
# Repo Yard status — deployed SHA, container status, and HTTP health per service.
# Usage: ./deploy/status.sh   (self-forwards to cthulhu like deploy.sh)
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

main() {
  ensure_on_host "$@"
  load_services

  local sha status code
  for name in "${SERVICES[@]}"; do
    echo "── $name ─────────────────────────────"
    if [ "${SVC_MODE[$name]}" = "static" ]; then
      echo "  mode:    static (music-hub, no git — files synced from repo-yard copy)"
      echo "  sha:     -"
    else
      sha=$(git -C "$TOOLS_ROOT/$name" rev-parse --short HEAD 2>/dev/null || echo "n/a")
      echo "  mode:    git (${SVC_GIT_URL[$name]})"
      echo "  sha:     $sha ($(git -C "$TOOLS_ROOT/$name" branch --show-current 2>/dev/null || true))"
    fi
    status=$(docker ps --filter "name=${SVC_CONTAINER[$name]}" --format '{{.Names}}: {{.Status}}' 2>/dev/null | head -1 || true)
    [ -n "$status" ] || status="not running"
    echo "  docker:  $status"
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://${HOST_ADDR}:${SVC_PORT[$name]}${SVC_PATH[$name]}" 2>/dev/null || true)
    echo "  health:  HTTP ${code:-unreachable} @ ${HOST_ADDR}:${SVC_PORT[$name]}${SVC_PATH[$name]}"
    echo ""
  done
}

main "$@"
