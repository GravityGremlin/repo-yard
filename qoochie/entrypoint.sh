#!/bin/sh
set -e
umask 022

QOBUZ_TOKEN_DIR="${QOBUZ_CONFIG_DIR:-/app/.config/qobuz}"
QOBUZ_TOKEN_FILE="$QOBUZ_TOKEN_DIR/qobuz_token.json"
BACKUP_TOKEN="/opt/backups/qoochie-auth/qobuz_token.json.latest"

echo "[entrypoint] Qoochie v0.1.0 starting..."

# Restore Qobuz token from backup if missing
if [ ! -f "$QOBUZ_TOKEN_FILE" ]; then
    echo "[entrypoint] Token missing — attempting auto-restore..."
    if [ -f "$BACKUP_TOKEN" ]; then
        mkdir -p "$QOBUZ_TOKEN_DIR"
        cp "$BACKUP_TOKEN" "$QOBUZ_TOKEN_FILE"
        chmod 600 "$QOBUZ_TOKEN_FILE"
        echo "[entrypoint] Token restored from $BACKUP_TOKEN"
    else
        echo "[entrypoint] WARNING: No backup found — manual Qobuz re-auth required at /qobuz/auth"
    fi
fi

WORKERS="${GUNICORN_WORKERS:-1}"
THREADS="${GUNICORN_THREADS:-8}"
PORT="${FLASK_PORT:-19290}"

exec gunicorn \
    -w "$WORKERS" \
    --threads "$THREADS" \
    --timeout 120 \
    --max-requests 2000 \
    --max-requests-jitter 500 \
    -b "0.0.0.0:${PORT}" \
    app.main:app
