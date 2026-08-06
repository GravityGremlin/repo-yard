"""repo-yard entrypoint — dev server (port 19297)."""
import os

from app import create_app

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("FLASK_PORT", "19297"))
    app.run(host="0.0.0.0", port=port, debug=False)
