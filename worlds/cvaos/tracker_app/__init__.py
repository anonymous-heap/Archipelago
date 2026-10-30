"""
The Aria of Sorrow tracker: a map of the castle in a web page, kept current from the multiworld.

The Launcher's "Aria of Sorrow Tracker" starts it. It serves the page on this computer only and
opens it in the browser. The page shows every location as in logic or not, Soma where he is, and
only the parts of the castle he has explored (a toggle shows the rest). Clicking a location and
then Find Route draws the way there from where Soma is.

Its logic is Universal Tracker's engine (ut/), which rebuilds this world from the slot data, so
the tracker needs no YAML and no copy of UT.

    python Launcher.py "Aria of Sorrow Tracker" -- --connect localhost:38281 --name Soma
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import webbrowser

DEFAULT_PORT = 38300

logger = logging.getLogger("Client")


def parse_args(args: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="A map tracker for Castlevania: Aria of Sorrow.")
    parser.add_argument("--connect", default=None, help="server address, such as localhost:38281")
    parser.add_argument("--name", default=None, help="the slot name to track")
    parser.add_argument("--password", default=None, help="the room's password, if it has one")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="the port the page is served on, or 0 for any free port")
    parser.add_argument("--no-browser", action="store_true", help="don't open the page")
    parser.add_argument("url", nargs="?", help="an archipelago://name:password@host:port URL")
    parsed = parser.parse_args(args)
    if parsed.url:
        from CommonClient import handle_url_arg
        parsed = handle_url_arg(parsed, parser)
        # handle_url_arg keeps the name and password in the address, which the page shows.
        url = parsed.url
        parsed.connect = f"{url.hostname}:{url.port}" if url.port else url.hostname
    return parsed


async def main(args: list[str]) -> None:
    from .context import TrackerContext
    from .server import TrackerServer

    options = parse_args(args)
    ctx = TrackerContext(options.connect, options.password, options.name)
    server = TrackerServer(ctx)
    port = await server.start("127.0.0.1", options.port)
    address = f"http://127.0.0.1:{port}/"
    logger.info("The Aria of Sorrow tracker is at %s", address)
    if not options.no_browser:
        webbrowser.open(address)
    if options.connect and options.name:
        await ctx.connect_to(options.connect, options.name, options.password)
    try:
        await ctx.exit_event.wait()
    finally:
        await ctx.shutdown()
        await server.stop()


def launch(*args: str) -> None:
    """Run the tracker until the page's Quit button, or the process, stops it."""
    asyncio.run(main(list(args)))
