"""
The Aria of Sorrow tracker: a map of the castle in a web page, kept current from the multiworld.

The Launcher's "Aria of Sorrow Tracker" starts it, and so does a room's archipelago:// link or the
game client's /map command. It serves the page on this computer only and opens it in the browser.
The page shows every location as in logic or not, Soma where he is, and only the parts of the
castle he has explored (a toggle shows the rest). Clicking a location and then Find Route draws
the way there from where Soma is.

Only one tracker runs for a room and slot. A launch that finds a tracker already serving on the
port asks it to take the request, and just opens its page. It starts another tracker, on the
next port, only when that one is busy with another room or slot.

Its logic is Universal Tracker's engine (ut/), which rebuilds this world from the slot data, so
the tracker needs no YAML and no copy of UT.

    python Launcher.py "Aria of Sorrow Tracker" -- --connect localhost:38281 --name Soma
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import webbrowser
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from CommonClient import CommonContext

    from .server import TrackerServer

DEFAULT_PORT = 38300
# The ports tried after DEFAULT_PORT, when trackers for other rooms or slots hold it. They stay
# fixed because the page keeps its settings per address, which includes the port.
SPARE_PORTS = range(DEFAULT_PORT + 1, DEFAULT_PORT + 10)
# How long to wait for a running tracker to answer. Rebuilding a seed holds up its event loop for
# a few seconds, so a short wait would mistake it for some other program on the port.
ASK_TIMEOUT = 15.0
# The game client passes the room's password here rather than on the command line, where other
# programs on the computer could read it.
PASSWORD_ENV = "CVAOS_TRACKER_PASSWORD"

logger = logging.getLogger("Client")


def parse_args(args: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="A map tracker for Castlevania: Aria of Sorrow.")
    parser.add_argument("--connect", default=None, help="server address, such as localhost:38281")
    parser.add_argument("--name", default=None, help="the slot name to track")
    parser.add_argument("--password", default=os.environ.get(PASSWORD_ENV) or None,
                        help="the room's password, if it has one")
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


def page_address(port: int) -> str:
    return f"http://127.0.0.1:{port}/"


async def ask_running_tracker(port: int, options: argparse.Namespace) -> str:
    """Ask whatever holds ``port`` to take this launch's request, and return what happened.

    The answer is "opened" when a tracker took the request, so only its page needs opening, and
    "busy" when it tracks another room or slot. It is "outdated" when the tracker comes from
    another version of this world, which is then asked to quit, and "other" when the port belongs
    to some other program.
    """
    from websockets.asyncio.client import connect
    from websockets.exceptions import WebSocketException

    from .server import APP, SOCKET_PATH, build_id

    try:
        async with asyncio.timeout(ASK_TIMEOUT):
            async with connect(f"ws://127.0.0.1:{port}{SOCKET_PATH}",
                               origin=page_address(port).rstrip("/")) as socket:
                hello = json.loads(await socket.recv())
                if hello.get("app") != APP:
                    return "other"
                if hello.get("build") != build_id():
                    await socket.send(json.dumps({"type": "quit"}))
                    return "outdated"
                await socket.send(json.dumps({"type": "handover", "server": options.connect,
                                              "slot": options.name, "password": options.password}))
                while True:
                    reply = json.loads(await socket.recv())
                    if reply.get("type") == "handover":
                        return reply.get("result", "other")
    except (OSError, TimeoutError, WebSocketException, ValueError, AttributeError):
        return "other"


async def serve_or_hand_over(server: TrackerServer, options: argparse.Namespace) -> int | None:
    """Serve on the first port that is free, unless a running tracker takes the request first.

    Returns the port this tracker serves on, or None when a running tracker took the request.
    """
    ports = [options.port, *SPARE_PORTS] if options.port == DEFAULT_PORT else [options.port]
    for port in ports:
        for _ in range(40):  # an outdated tracker takes a moment to free the port once asked
            try:
                return await server.start("127.0.0.1", port)
            except OSError:
                pass
            answer = await ask_running_tracker(port, options)
            if answer == "opened":
                if not options.no_browser:
                    webbrowser.open(page_address(port))
                return None
            if answer != "outdated":
                break  # busy or not a tracker, so try the next port
            await asyncio.sleep(0.25)
    raise OSError(f"Every port the tracker can use, {ports[0]} to {ports[-1]}, is taken")


async def main(args: list[str]) -> None:
    from .context import TrackerContext
    from .server import TrackerServer

    options = parse_args(args)
    ctx = TrackerContext(options.connect, options.password, options.name)
    server = TrackerServer(ctx)
    try:
        port = await serve_or_hand_over(server, options)
    except BaseException:
        ctx.close()
        raise
    if port is None:
        ctx.close()
        logger.info("A tracker was already running, so this launch opened its page")
        return
    address = page_address(port)
    logger.info("The Aria of Sorrow tracker is at %s", address)
    if not options.no_browser:
        webbrowser.open(address)
    if options.connect and options.name:
        await ctx.connect_to(options.connect, options.name, options.password)
    idle_watch = asyncio.create_task(server.stop_when_idle())
    try:
        await ctx.exit_event.wait()
    finally:
        idle_watch.cancel()
        await ctx.shutdown()
        await server.stop()


def launch(*args: str) -> None:
    """Run the tracker until the page's Quit button stops it, or no page has been open a while."""
    asyncio.run(main(list(args)))


def launcher_command() -> list[str]:
    """Return the command that runs Archipelago's Launcher, frozen or from source."""
    import Utils
    if Utils.is_frozen():
        suffix = ".exe" if Utils.is_windows else ""
        return [Utils.local_path(f"ArchipelagoLauncher{suffix}")]
    return [sys.executable, Utils.local_path("Launcher.py")]


def tracker_command(address: str, slot_name: str) -> list[str]:
    """Return the command that starts the tracker for ``slot_name`` in the room at ``address``."""
    return [*launcher_command(), "Aria of Sorrow Tracker", "--",
            "--connect", address, "--name", slot_name]


def open_for_client(ctx: CommonContext) -> None:
    """Open the tracker for the slot a game client is connected as.

    It runs in its own process, as the Launcher's button would start it, so the client is never
    held up by it and keeps running if the tracker stops. A tracker already running for the slot
    just opens its page.
    """
    if ctx.slot is None:
        logger.info("Connect to the room first. Then /map opens the tracker for your slot.")
        return
    command = tracker_command(ctx.server_address or "", ctx.slot_info[ctx.slot].name)
    env = dict(os.environ)
    env.pop(PASSWORD_ENV, None)
    if ctx.password:
        env[PASSWORD_ENV] = ctx.password
    detach: dict = ({"creationflags": subprocess.DETACHED_PROCESS
                     | subprocess.CREATE_NEW_PROCESS_GROUP}
                    if sys.platform == "win32" else {"start_new_session": True})
    subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True, **detach)
    logger.info("Opening the map tracker in your browser")
