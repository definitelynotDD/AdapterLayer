#!/usr/bin/env python3
"""
app.py  —  API Explorer launcher

Starts the FastAPI backend (server.py) on localhost and
automatically opens the UI in your default browser.

Usage:
    python app.py                # http://127.0.0.1:8000
    python app.py --port 9000
    python app.py --no-browser
"""
import argparse
import threading
import time
import webbrowser

import uvicorn
from server import app  # noqa: F401


def _open(port: int) -> None:
    time.sleep(1.2)
    webbrowser.open(f"http://127.0.0.1:{port}")


def main() -> None:
    parser = argparse.ArgumentParser(description="API Explorer launcher")
    parser.add_argument("--port",       type=int, default=8000,
                        help="Port to listen on (default: 8000)")
    parser.add_argument("--no-browser", action="store_true",
                        help="Skip auto-opening the browser")
    args = parser.parse_args()

    if not args.no_browser:
        threading.Thread(target=_open, args=(args.port,), daemon=True).start()

    print(f"\n  API Explorer  ->  http://127.0.0.1:{args.port}\n"
          f"  Press Ctrl+C to stop.\n")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()