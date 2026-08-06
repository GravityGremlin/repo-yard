#!/usr/bin/env python
"""qoochie — Qobuz music downloader web UI."""

from app.factory import create_app

app = create_app()

if __name__ == "__main__":
    from app.config import FLASK_PORT
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=True)
