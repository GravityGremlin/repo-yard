#!/bin/sh
set -e
umask 022

STREAMRIP_CONFIG_DIR="${STREAMRIP_CONFIG_DIR:-/app/.config/streamrip}"
SPOTIFY_CONFIG_DIR="${SPOTIFY_CONFIG_DIR:-/app/.config/spotify}"

BACKUP_DIR="/opt/backups/spotifryer-config"

# ── Restore streamrip credentials from backup ──
STREAMRIP_CREDS="$STREAMRIP_CONFIG_DIR/credentials.json"
if [ ! -s "$STREAMRIP_CREDS" ] && [ -f "$BACKUP_DIR/streamrip_credentials.json" ]; then
    echo "[entrypoint] Restoring streamrip credentials from backup..."
    mkdir -p "$STREAMRIP_CONFIG_DIR"
    cp "$BACKUP_DIR/streamrip_credentials.json" "$STREAMRIP_CREDS"
    chmod 600 "$STREAMRIP_CREDS"
    echo "[entrypoint] Streamrip credentials restored."
fi

# ── Restore Spotify token from backup ──
SPOTIFY_TOKEN="$SPOTIFY_CONFIG_DIR/spotify_token.json"
if [ ! -s "$SPOTIFY_TOKEN" ] && [ -f "$BACKUP_DIR/spotify_token.json" ]; then
    echo "[entrypoint] Restoring Spotify token from backup..."
    mkdir -p "$SPOTIFY_CONFIG_DIR"
    cp "$BACKUP_DIR/spotify_token.json" "$SPOTIFY_TOKEN"
    chmod 600 "$SPOTIFY_TOKEN"
    echo "[entrypoint] Spotify token restored."
fi

# ── Ensure runtime directories exist ──
mkdir -p /app/data /app/downloads

echo "[entrypoint] Starting spotifryer on port ${FLASK_PORT:-19291}..."
exec gunicorn \
    -w "${GUNICORN_WORKERS:-1}" \
    -t 120 \
    --threads "${GUNICORN_THREADS:-8}" \
    -b "0.0.0.0:${FLASK_PORT:-19291}" \
    app.main:app
