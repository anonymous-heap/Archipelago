"""
The Aria of Sorrow tracker: a map of the castle in a web page, kept current from the multiworld.

The Launcher's "Aria of Sorrow Tracker" entry starts it, and so do a room's archipelago:// link
and the clients' /map command. It serves the page on this computer only and opens it in the
browser. The page marks each location as in logic, with tricks, or out of logic, and shows where
Soma is. It shows only the parts of the castle he has explored, unless "Reveal full map" is on.
Selecting a location and pressing "Find route" draws a route to it, starting at the door Soma
came into his current room by.

Only one tracker runs for a room and slot. A launch first asks any trackers already running on
the ports the tracker uses. When one of them follows the requested room and slot, or follows
nothing and can take them, the launch hands it the request and just opens its page. Otherwise
the launch serves the page itself, on the first free port.

The tracker's logic comes from Universal Tracker's engine (in ut/). The engine rebuilds this
world from the slot data, so the tracker needs no YAML and no installed copy of UT.

To start the tracker from source for the slot "Soma" on a local server:

    python Launcher.py "Aria of Sorrow Tracker" -- --connect localhost:38281 --name Soma
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
import webbrowser
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from CommonClient import CommonContext

    from .server import TrackerServer

DEFAULT_PORT = 38300
# The ports tried after DEFAULT_PORT when it is taken, such as by a tracker for another room or
# slot. They stay fixed because the page keeps its settings per address, and the address includes
# the port.
SPARE_PORTS = range(DEFAULT_PORT + 1, DEFAULT_PORT + 10)
# How long to wait for a running tracker to answer. Rebuilding a seed holds up its event loop for
# a few seconds, so a short wait would mistake it for some other program on the port.
ASK_TIMEOUT = 15.0
# The clients pass the room's password in this environment variable rather than on the command
# line, where other programs on the computer could read it.
PASSWORD_ENV = "CVAOS_TRACKER_PASSWORD"

logger = logging.getLogger("Client")


def without_login(address: str) -> str:
    """Return a server address without the slot name and password it can carry."""
    scheme, has_scheme, rest = address.partition("://")
    host = (rest if has_scheme else address).rsplit("@", 1)[-1]
    return f"{scheme}://{host}" if has_scheme else host


def parse_args(args: list[str]) -> argparse.Namespace:
    # The password is read once and then removed from the environment. That way a browser the
    # tracker opens doesn't inherit it, and neither does anything the browser starts later.
    password = os.environ.pop(PASSWORD_ENV, None) or None
    parser = argparse.ArgumentParser(description="A map tracker for Castlevania: Aria of Sorrow.")
    parser.add_argument("--connect", default=None, help="server address, such as localhost:38281")
    parser.add_argument("--name", default=None, help="the slot name to track")
    parser.add_argument("--password", default=password,
                        help="the room's password, if it has one")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="the port the page is served on, or 0 for any free port")
    parser.add_argument("--no-browser", action="store_true", help="don't open the page")
    parser.add_argument("url", nargs="?", help="an archipelago://name:password@host:port URL")
    parsed = parser.parse_args(args)
    if parsed.url:
        from CommonClient import handle_url_arg
        parsed = handle_url_arg(parsed, parser)
        # handle_url_arg leaves the name and password in the address, which the page shows, so
        # they are removed here. The rest is kept as written, so an IPv6 address keeps its brackets.
        parsed.connect = without_login(parsed.url.netloc)
    elif parsed.connect:
        parsed.connect = without_login(parsed.connect)  # the slot and password come separately
    return parsed


def page_address(port: int) -> str:
    return f"http://127.0.0.1:{port}/"


def port_is_free(port: int) -> bool:
    """Return whether nothing listens on ``port``, by trying to bind to it.

    A bind succeeds or fails at once, while on Windows a connection to a port that nothing listens
    on can take a second or two to fail.

    On Linux and macOS, a port that a tracker just stopped serving on stays in TIME_WAIT for about
    a minute, and a plain bind fails during that time. asyncio's listener sets SO_REUSEADDR there,
    so the probe sets it too. On Windows the same option would let the probe share a port that is
    in use, so it stays off.

    With SO_REUSEADDR, macOS also lets the probe bind beside another program that listens on
    every address. So on Linux and macOS the probe also tries to connect, and the port counts as
    free only if that connection fails. When nothing listens, the connection is refused at once.
    """
    posix = os.name == "posix"
    with socket.socket() as probe:
        if posix:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    if posix:
        with socket.socket() as knock:
            knock.settimeout(0.5)
            return knock.connect_ex(("127.0.0.1", port)) != 0
    return True


async def wait_until_free(port: int, seconds: float = ASK_TIMEOUT) -> bool:
    """Wait up to ``seconds`` for a stopping tracker to free ``port``, and return whether it did."""
    for _ in range(int(seconds / 0.25)):
        if port_is_free(port):
            return True
        await asyncio.sleep(0.25)
    return port_is_free(port)


async def ask_running_tracker(port: int, options: argparse.Namespace, take: bool) -> str:
    """Ask whatever holds ``port`` about this launch's request, and return its answer.

    The answer is "opened" when the tracker there follows the requested room and slot, or has
    just taken them, so the launch only needs to open its page. It is "busy" when the tracker
    follows another room or slot. Without ``take``, a tracker that could take the request answers
    "remembers" when the slot is the one it last followed, and "idle" otherwise.

    The answer is "closing" when the tracker is shutting down. It is "outdated" when the tracker
    is from another version, and this function then tells it to quit. Either way the port comes
    free shortly. The answer is "other" when the port belongs to some other program, or when nothing
    answers in time.
    """
    from websockets.asyncio.client import connect
    from websockets.exceptions import WebSocketException

    from .server import APP, SOCKET_PATH, build_id

    try:
        async with asyncio.timeout(ASK_TIMEOUT):
            async with connect(f"ws://127.0.0.1:{port}{SOCKET_PATH}", open_timeout=ASK_TIMEOUT,
                               origin=page_address(port).rstrip("/")) as tracker:
                hello = json.loads(await tracker.recv())
                if hello.get("app") != APP:
                    return "other"
                if hello.get("build") != build_id():
                    await tracker.send(json.dumps({"type": "quit"}))
                    return "outdated"
                await tracker.send(json.dumps({"type": "handover", "server": options.connect,
                                               "slot": options.name,
                                               "password": options.password, "take": take}))
                while True:
                    reply = json.loads(await tracker.recv())
                    if reply.get("type") == "handover":
                        return reply.get("result", "other")
    except (OSError, TimeoutError, WebSocketException, ValueError, AttributeError):
        return "other"


async def serve_or_hand_over(server: TrackerServer, options: argparse.Namespace) -> int | None:
    """Hand the request to a running tracker, or else serve on the first free port.

    Every running tracker is asked first, without letting it take the request. That way the
    tracker that already follows this room and slot is found even when it holds a later port than
    an idle tracker or a free port. Only then is an idle tracker given the request, starting with
    one that last followed this slot, and only after that is a port taken.

    Returns the port this tracker serves on, or None when a running tracker has the request.
    """
    def opened(port: int) -> None:
        if not options.no_browser:
            webbrowser.open(page_address(port))

    ports = [options.port, *SPARE_PORTS] if options.port == DEFAULT_PORT else [options.port]
    free, remembers, idle = [], [], []
    for port in ports:
        if port_is_free(port):
            free.append(port)
            continue
        answer = await ask_running_tracker(port, options, take=False)
        if answer == "opened":
            opened(port)
            return None
        if answer == "remembers":
            remembers.append(port)
        elif answer == "idle":
            idle.append(port)
        elif answer in ("closing", "outdated") and await wait_until_free(port):
            free.append(port)
    # A tracker that last followed this slot is given the request first, so that its page keeps
    # its settings.
    for port in remembers + idle:
        if await ask_running_tracker(port, options, take=True) == "opened":
            opened(port)
            return None
    for port in sorted(free, key=ports.index):
        try:
            return await server.start("127.0.0.1", port)
        except OSError:
            pass
        # Another launch took the port a moment ago, and that launch may be for this room and
        # slot too.
        if await ask_running_tracker(port, options, take=True) == "opened":
            opened(port)
            return None
    # Other programs hold every one of these ports, or Windows has reserved them, so the tracker
    # takes any free port. The page's saved settings belong to its usual port, so on this port the
    # page opens without them.
    taken = f"Port {ports[0]} is" if len(ports) == 1 else f"Ports {ports[0]} to {ports[-1]} are all"
    logger.warning("%s taken, so the tracker uses another free port", taken)
    return await server.start("127.0.0.1", 0)


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
        # The port is closed first, so that a launch arriving now serves there instead of handing
        # its request to a tracker that is about to stop. A page's request can still be waiting on
        # a connection that is opening. Only the context's shutdown ends that wait, so the server
        # finishes closing after it.
        server.close()
        await ctx.shutdown()
        await server.wait_closed()


def launch(*args: str) -> None:
    """Run the tracker until "Quit tracker" stops it, or no page has been open for a while."""
    import Utils
    Utils.init_logging("CVAoSTracker", exception_logger="Client")
    asyncio.run(main(list(args)))


def launcher_command() -> list[str]:
    """Return the command that runs Archipelago's Launcher, frozen or from source."""
    import Utils
    if Utils.is_frozen():
        suffix = ".exe" if Utils.is_windows else ""
        return [Utils.local_path(f"ArchipelagoLauncher{suffix}")]
    return [sys.executable, Utils.local_path("Launcher.py")]


def tracker_command(address: str, slot_name: str) -> list[str]:
    """Return the command that starts the tracker for ``slot_name`` in the room at ``address``.

    Each value is joined to its option, so a slot name that starts with "-" stays a value.
    """
    return [*launcher_command(), "Aria of Sorrow Tracker", "--",
            f"--connect={without_login(address)}", f"--name={slot_name}"]


def open_for_client(ctx: CommonContext) -> None:
    """Open the tracker for the slot a game client is connected as.

    The tracker runs in its own process, started as the Launcher's entry would start it. So the
    client is never held up by the tracker, and keeps running if the tracker stops. When a tracker
    is already running for the slot, the new process just opens that tracker's page.
    """
    if ctx.slot is None:
        logger.info("Connect to the room first. Then /map opens the tracker for your slot.")
        return
    # The BizHawk Client keeps /map and the button after a ROM for another system is loaded,
    # because it asks this world only about GBA ROMs. So the slot's game is checked here.
    from .. import CVAOSWorld
    slot = ctx.slot_info[ctx.slot]
    if slot.game != CVAOSWorld.game:
        logger.info("This slot plays %s, so the Aria of Sorrow map tracker can't follow it.",
                    slot.game)
        return
    command = tracker_command(ctx.server_address or "", slot.name)
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


def map_button(ctx: CommonContext, height: float):
    """Make an "Open map" button for a game client's window, shaped like its "Connect" button."""
    from kivy.metrics import dp
    from kivymd.uix.button import MDButton, MDButtonText

    button = MDButton(MDButtonText(text="Open map"), style="outlined", size=(dp(110), height),
                      size_hint_x=None, size_hint_y=None, radius=5, pos_hint={"center_y": 0.55})
    button.bind(on_press=lambda _button: open_for_client(ctx))
    return button


def add_map_button(ctx: CommonContext) -> None:
    """Put an "Open map" button after "Connect" in a game client's window, unless it has one.

    A client running without a window gets no button, and so does one whose window is built
    differently from what this function expects. /map works either way, so a failure here is
    logged rather than raised. Raising it would break the client's connection.
    """
    ui = getattr(ctx, "ui", None)
    layout = getattr(ui, "connect_layout", None)
    if layout is None or getattr(ui, "cvaos_map_button", None) is not None:
        return
    try:
        button = map_button(ctx, ui.server_connect_bar.height)
        layout.add_widget(button)
    except Exception:
        logger.exception("Couldn't add the \"Open map\" button, so use /map instead")
        return
    ui.cvaos_map_button = button


def remove_map_button(ctx: CommonContext) -> None:
    """Take the "Open map" button out of a game client's window, if it has one."""
    ui = getattr(ctx, "ui", None)
    button = getattr(ui, "cvaos_map_button", None)
    if button is None:
        return
    ui.cvaos_map_button = None
    try:
        ui.connect_layout.remove_widget(button)
    except Exception:
        logger.exception("Couldn't remove the \"Open map\" button")
