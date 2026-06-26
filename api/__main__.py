"""Serve the TradAlert control panel.

    python -m api [--open] [--port 8000] [--host 127.0.0.1] [--reload]

``--open`` launches the browser at the dashboard once the server is starting.
Run from the repo root in the project venv:  .venv\\Scripts\\python.exe -m api --open
"""

from __future__ import annotations

import argparse
import threading
import time
import webbrowser

import api  # noqa: F401  (sys.path + dotenv bootstrap on import)


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m api", description="Serve the TradAlert control panel.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--open", action="store_true", help="open the dashboard in a browser once it's up")
    ap.add_argument("--reload", action="store_true", help="auto-reload on code changes (dev)")
    args = ap.parse_args()

    shown = "localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host
    url = f"http://{shown}:{args.port}"
    if args.open:
        def _open():
            time.sleep(2.0)
            try:
                webbrowser.open(url)
            except Exception:
                pass
        threading.Thread(target=_open, daemon=True).start()

    print(f"TradAlert control panel → {url}   (Ctrl+C to stop)")
    import uvicorn
    uvicorn.run("api.main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
