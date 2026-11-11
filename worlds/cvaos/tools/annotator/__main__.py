"""Start the annotator and open its page: ``python -m worlds.cvaos.tools.annotator``."""
from __future__ import annotations

import argparse
import asyncio
import logging
import threading
import webbrowser

from .server import PORT, AnnotatorServer


def main() -> None:
    parser = argparse.ArgumentParser(description="The enemy routing annotator.")
    parser.add_argument("--no-browser", action="store_true", help="don't open the page")
    options = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not options.no_browser:
        threading.Timer(1.0, webbrowser.open, (f"http://localhost:{PORT}/",)).start()
    asyncio.run(AnnotatorServer().run())


if __name__ == "__main__":
    main()
