#!/usr/bin/env bash
# Repo Yard deploy — the single deploy path for the quad + music-hub.
# Runs on the Docker host (cthulhu); self-forwards over SSH from anywhere.
#
# Usage:
#   ./deploy/deploy.sh [--dry-run] <spotifryer|qoochie|tidalwave|deeznutz|music-hub|all>
#
# Behavior per service:
#   1. Pull (git) or sync files (static music-hub)
#   2. Smart rebuild — only if requirements.txt/Dockerfile/entrypoint.sh/compose.yaml changed
#   3. docker compose up -d [--build]
#   4. Health gate — host-port route answers 2xx/3xx within 120s + no traceback in logs
#   5. Auto-rollback to the previous SHA/files if the gate fails
# Every run is appended to deploy/log/deploy.log (the rollback ledger).
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

usage() {
  cat <<'EOF'
Repo Yard deploy — single deploy path for the quad + music-hub.

Usage:
  ./deploy/deploy.sh [--dry-run] <service|all>

Services: spotifryer, qoochie, tidalwave, deeznutz, music-hub, repo-yard (or "all")
  --dry-run   print actions without executing anything

Runs on the Docker host (cthulhu). From anywhere else it self-forwards via
ssh alias "$CTHULHU_SSH" (default: cthulhu) and expects repo-yard checked
out at the same path there (override with REPO_YARD_REMOTE_DIR).
EOF
}

deploy_git() {
  local name=$1
  local dir="$TOOLS_ROOT/$name"
  local prev_sha="" cur_sha="" need_rebuild=0 changed=""

  if [ ! -d "$dir/.git" ]; then
    log "[deploy] $name: not present — cloning ${SVC_GIT_URL[$name]}"
    runc "git clone '${SVC_GIT_URL[$name]}' '$dir'"
  fi

  prev_sha=$(git -C "$dir" rev-parse HEAD 2>/dev/null || true)
  # Remote names differ per tool (origin, forgejo, fj) — use the branch's
  # configured upstream when present; otherwise pull from the repo's first
  # remote (only adding one from the registry if the repo has none).
  upstream_remote=$(git -C "$dir" config --get "branch.$BRANCH.remote" 2>/dev/null || true)
  if [ -n "$upstream_remote" ]; then
    pull_cmd="(cd '$dir' && git pull --ff-only)"
  else
    first_remote=$(git -C "$dir" remote 2>/dev/null | head -1 || true)
    if [ -z "$first_remote" ]; then
      first_remote=origin
      runc "git -C '$dir' remote add origin '${SVC_GIT_URL[$name]}' 2>/dev/null || true"
    fi
    pull_cmd="(cd '$dir' && git pull --ff-only '$first_remote' '$BRANCH')"
  fi
  runc "$pull_cmd" || {
    log "[deploy] $name: git pull FAILED — aborting, no changes made"
    return 1
  }
  cur_sha=$(git -C "$dir" rev-parse HEAD 2>/dev/null || true)

  if [ -z "$prev_sha" ]; then
    need_rebuild=1                                    # fresh clone — build from scratch
  elif [ "$prev_sha" != "$cur_sha" ]; then
    code_changed=1
    changed=$(git -C "$dir" diff --name-only "$prev_sha" "$cur_sha" 2>/dev/null || true)
    if printf '%s\n' "$changed" | grep -qE '(^|/)(requirements\.txt|Dockerfile|entrypoint\.sh)$|^compose\.yaml$'; then
      need_rebuild=1
    fi
  fi

  # First deploy of a service: the image doesn't exist yet (compose uses <name>:latest).
  if ! docker image inspect "${name}:latest" >/dev/null 2>&1; then
    need_rebuild=1
    log "[deploy] $name: image ${name}:latest not found — building"
  fi

  if [ -n "$prev_sha" ] && [ "$prev_sha" = "$cur_sha" ]; then
    log "[deploy] $name: already at $cur_sha — ensuring container is up"
  else
    log "[deploy] $name: ${prev_sha:-fresh} → $cur_sha (rebuild: $([ "$need_rebuild" = 1 ] && echo yes || echo no))"
  fi

  if [ "$need_rebuild" = 1 ]; then
    runc "(cd '$dir' && docker compose up -d --build)"
  elif [ "${code_changed:-0}" = 1 ]; then
    # Code changed but the image didn't need rebuilding (app/ is bind-mounted) —
    # recreate the container so the new code is actually loaded.
    runc "(cd '$dir' && docker compose up -d --force-recreate)"
  else
    runc "(cd '$dir' && docker compose up -d)"
  fi

  if ! wait_healthy "$name" "${SVC_PORT[$name]}" "${SVC_PATH[$name]}"; then
    log "[deploy] $name: HEALTH GATE FAILED"
    check_traceback "$name" || true
    if [ -n "$prev_sha" ] && [ -n "$cur_sha" ] && [ "$prev_sha" != "$cur_sha" ]; then
      log "[deploy] $name: rolling back to $prev_sha"
      if [ "$need_rebuild" = 1 ]; then
        runc "(cd '$dir' && git checkout '$prev_sha' && docker compose up -d --build)"
      else
        runc "(cd '$dir' && git checkout '$prev_sha' && docker compose up -d)"
      fi
      if wait_healthy "$name" "${SVC_PORT[$name]}" "${SVC_PATH[$name]}"; then
        log "[deploy] $name: rollback OK — back at $prev_sha"
      else
        log "[deploy] $name: ROLLBACK ALSO FAILED — manual intervention required"
      fi
    else
      log "[deploy] $name: no prior SHA to roll back to"
    fi
    return 1
  fi

  if ! check_traceback "$name"; then
    log "[deploy] $name: traceback in logs despite healthy HTTP — investigate"
    return 1
  fi
  log "[deploy] $name: OK @ $cur_sha"
}

deploy_static() {
  local name=$1
  local src="$SCRIPT_DIR/../music-hub"                      # repo-yard copy = deployable artifact
  local target="${STATIC_TARGET:-$TOOLS_ROOT/music-hub}" # live dir on the host
  local backup="$LOG_DIR/${name}.prev"

  log "[deploy] $name: syncing $src → $target"
  if [ "$DRY_RUN" = "1" ]; then
    printf '[dry-run] rsync -a --delete %s/ %s/\n' "$src" "$target"
    printf '[dry-run] (cd %s && docker compose up -d)\n' "$target"
  else
    mkdir -p "$backup" "$target"
    cp -a "$target/." "$backup/" 2>/dev/null || true      # snapshot pre-deploy state for rollback
    rsync -a --delete "$src/" "$target/"
    (cd "$target" && docker compose up -d)
  fi

  if ! wait_healthy "$name" "${SVC_PORT[$name]}" "${SVC_PATH[$name]}"; then
    log "[deploy] $name: HEALTH GATE FAILED — restoring previous files"
    runc "rsync -a --delete '$backup/' '$target/' && (cd '$target' && docker compose up -d)"
    if wait_healthy "$name" "${SVC_PORT[$name]}" "${SVC_PATH[$name]}"; then
      log "[deploy] $name: rollback OK"
    else
      log "[deploy] $name: ROLLBACK ALSO FAILED — manual intervention required"
    fi
    return 1
  fi
  log "[deploy] $name: OK"
}

deploy_service() {
  local name=$1
  log "[deploy] === $name ==="
  if [ "${SVC_MODE[$name]}" = "static" ]; then
    deploy_static "$name"
  else
    deploy_git "$name"
  fi
}

main() {
  ensure_on_host "$@"

  while [ $# -gt 0 ]; do
    case "$1" in
      --dry-run) DRY_RUN=1; shift ;;
      *) break ;;
    esac
  done

  load_services
  mkdir -p "$LOG_DIR"

  local target="${1:-all}" failed=0
  case "$target" in
    spotifryer|qoochie|tidalwave|deeznutz|music-hub|repo-yard)
      deploy_service "$target"
      ;;
    all)
      for s in "${SERVICES[@]}"; do
        deploy_service "$s" || failed=1
      done
      [ "$failed" = 0 ] || { log "[deploy] one or more services FAILED"; exit 1; }
      ;;
    *)
      usage
      exit 2
      ;;
  esac
}

main "$@"
