# Qoochie — Qobuz music downloader web UI

Self-hosted Flask web UI for downloading music from Qobuz. Native Qobuz API integration. Authenticate via token, search for tracks/albums/artists/playlists, download with real-time progress, and organise with beets + Navidrome.

## Quick start

### Docker (recommended)

```bash
# Clone
git clone https://fj.n0g.xyz/gravitas/Qoochie.git
cd Qoochie

# Set your Qobuz token (from https://play.qobuz.com/account)
export QOBUZ_TOKEN="your_token_here"
export SECRET_KEY="your_random_secret"

# Build & run
docker compose up -d --build
```

Open `http://localhost:19295`.

### Local development
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app/main.py
```

### Auth (Qobuz token)

1. Log into `qobuz.com` in a browser.
2. Open DevTools → Application → look for the Qobuz API token.
3. Either set the `QOBUZ_TOKEN` environment variable (Docker) or paste it at `/qobuz/auth` in the web UI.

The token is long-lived and stored in `config/qobuz/qobuz_token.json`.

## Features

- **Search** — tracks, albums, artists, playlists
- **Download** — single tracks, full albums, entire discographies, and playlists
- **Quality** — FLAC, MP3 320kbps, MP3 128kbps
- **Background worker pool** — LIFO queue with pause/resume/cancel, SSE progress
- **Metadata tagging** — mutagen (ID3/Vorbis/MP4) with cover art
- **Beets integration** — automatic library organization via `beet import`
- **Navidrome sync** — auto-scan on completion
- **Import upload** — drag-drop local audio files or archives
- **Library browser** — browse, search, stream, ZIP download
- **Collection overview** — artist/album/track stats
- **PWA** — installable web app with offline caching
