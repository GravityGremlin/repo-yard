#!/bin/sh
set -e
umask 022

DEEZER_TOKEN_DIR="${DEEZER_CONFIG_DIR:-/app/.config/deeznutz_dz}"
DEEZER_TOKEN="$DEEZER_TOKEN_DIR/deezer_token.json"
BACKUP_TOKEN="/opt/backups/deeznutz-auth/deezer_token.json.latest"

echo "[entrypoint] Deeznutz v0.1.0 starting..."

# Restore Deezer ARL token from backup if missing
if [ ! -f "$DEEZER_TOKEN" ]; then
    echo "[entrypoint] Token missing — attempting auto-restore..."
    if [ -f "$BACKUP_TOKEN" ]; then
        mkdir -p "$DEEZER_TOKEN_DIR"
        cp "$BACKUP_TOKEN" "$DEEZER_TOKEN"
        chmod 600 "$DEEZER_TOKEN"
        echo "[entrypoint] Token restored from $BACKUP_TOKEN"
    else
        echo "[entrypoint] WARNING: No backup found — manual ARL re-auth required at /deezer/auth"
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
