#!/usr/bin/env python
"""spotifryer — Spotify track/playlist download web UI."""

import os

from app.factory import create_app

app = create_app()

if __name__ == "__main__":
    from app.config import FLASK_PORT
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes"))
