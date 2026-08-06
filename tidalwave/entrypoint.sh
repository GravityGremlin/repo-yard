#!/bin/sh
set -e
umask 022

TIDAL_TOKEN_DIR="${TIDAL_CONFIG_DIR:-/app/.config/tidal_dl_ng}"
TIDAL_TOKEN="$TIDAL_TOKEN_DIR/token.json"
BACKUP_TOKEN="/opt/backups/tidalwave-auth/token.json.latest"
NFS_BACKUP_TOKEN="/mnt/backups/cthulhu/tidalwave-auth/token.json.latest"

_token_valid() {
    [ -s "$1" ] || return 1
    python3 -c "import json,sys; d=json.load(open('$1')); assert d.get('refresh_token')" 2>/dev/null
}

# Restore missing or empty token from backup
if ! _token_valid "$TIDAL_TOKEN"; then
    echo "[entrypoint] Token missing or invalid — attempting auto-restore..."
    RESTORE_SRC=""
    if [ -f "$BACKUP_TOKEN" ]; then
        RESTORE_SRC="$BACKUP_TOKEN"
    elif [ -f "$NFS_BACKUP_TOKEN" ]; then
        RESTORE_SRC="$NFS_BACKUP_TOKEN"
    fi
    if [ -n "$RESTORE_SRC" ]; then
        mkdir -p "$TIDAL_TOKEN_DIR"
        cp "$RESTORE_SRC" "$TIDAL_TOKEN"
        chmod 600 "$TIDAL_TOKEN"
        echo "[entrypoint] Token restored from $RESTORE_SRC"
    else
        echo "[entrypoint] WARNING: No backup found — manual re-auth required"
    fi
fi

# Refresh the access token (refresh_token is long-lived, access token expires hourly)
if _token_valid "$TIDAL_TOKEN"; then
    echo "[entrypoint] Refreshing Tidal access token..."
    python3 -c "
import json, tidalapi, time
from pathlib import Path
from datetime import datetime, timezone
token_file = Path('$TIDAL_TOKEN')
raw = json.loads(token_file.read_text())
session = tidalapi.Session()
session.config.quality = 'HIGH'
expiry = datetime.fromtimestamp(float(raw['expiry_time']), tz=timezone.utc)
session.load_oauth_session(
    token_type=raw.get('token_type', 'Bearer'),
    access_token=raw.get('access_token', ''),
    refresh_token=raw.get('refresh_token'),
    expiry_time=expiry,
)
if session.token_refresh(raw.get('refresh_token')):
    data = {
        'access_token': session.access_token,
        'refresh_token': session.refresh_token or raw.get('refresh_token'),
        'token_type': session.token_type,
    }
    if session.expiry_time:
        data['expiry_time'] = session.expiry_time.replace(tzinfo=timezone.utc).timestamp()
    token_file.write_text(json.dumps(data, indent=2))
    print('[entrypoint] Token refreshed, expires ' + str(session.expiry_time))
else:
    print('[entrypoint] WARNING: Token refresh failed')
" 2>/dev/null || echo "[entrypoint] WARNING: Token refresh failed"
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
