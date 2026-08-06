# spotifryer

Download Spotify tracks and playlists as high-quality audio, with metadata.

## How it works

1. **Spotify metadata** — fetches track/album/playlist metadata via the Spotify Web API
2. **streamrip** (primary) — downloads FLAC from Tidal/Qobuz by ISRC match
3. **yt-dlp** (fallback) — downloads from YouTube Music if not found on Tidal/Qobuz
4. **Metadata embedding** — applies Spotify metadata (cover art, tags) via mutagen
5. **Library organization** — optional beets import, Navidrome scan

## Stack

| Layer | Technology |
|---|---|
| Web framework | Flask 3 + HTMX |
| Spotify API | spotipy |
| Download engine | streamrip + yt-dlp |
| Queue | In-process thread pool |
| Progress | SSE (Server-Sent Events) |
| Database | SQLite (WAL mode) |
| Server | Gunicorn |
| Container | Docker |
