"""
The tracker's local web server: the page, the map, and a websocket that keeps the page current.

GET / and the page's own files come from web/ in this package, which pkgutil reads whether the
world is a folder or a zipped .apworld. GET /map.json is the map (mapdata.py). The page then opens
/ws, and the server sends it the tracker's snapshot whenever that may have changed:

    {"type": "snapshot", "app": "cvaos-tracker", "build": "1/2026.10.31/5d1e8c2a",
     "snapshot": {...}}

"app" and "build" say which tracker this is, so a page can reload when a newer one replaces it,
and a new launch can tell this tracker from another program. The page sends back what the player
asks for, each as a JSON object with a "type":

    {"type": "connect", "server": "localhost:38281", "slot": "Soma", "password": ""}
    {"type": "disconnect"}
    {"type": "route", "location": 123}      a route to the location with that id
    {"type": "clear_route"}
    {"type": "quit"}                        stop the tracker

A new launch sends the room and slot it was given, and whether this tracker may take them if it
follows nothing. It hears back "opened", "remembers", "idle", "busy" or "closing" (see
TrackerContext.hand_over):

    {"type": "handover", "server": "localhost:38281", "slot": "Soma", "password": null,
     "take": false}
    {"type": "handover", "result": "idle"}

The server answers only its own page. Any web site open in the browser could otherwise reach it,
read the slot and the room's chat, and point the tracker at another server or stop it. So every
request must name this server as its Host, which also defeats DNS rebinding, and the websocket
also needs the page's own Origin, which a browser always sends and a site can't forge.
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import pkgutil
import time
from http import HTTPStatus

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed, InvalidMessage, InvalidStatus
from websockets.http11 import Request, Response

from .. import CVAOSWorld
from .context import TrackerContext
from .mapdata import map_data

logger = logging.getLogger("Client")


class QuietHttp(logging.Filter):
    """Drop what websockets 13 logs for plain HTTP, which this server answers on purpose.

    It logs every page request it answers as a failed websocket handshake, with a traceback, and
    the same for each connection a browser opens ahead of time and then drops.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info and record.getMessage() == "opening handshake failed":
            return not isinstance(record.exc_info[1], (InvalidStatus, InvalidMessage, EOFError))
        return True


socket_logger = logging.getLogger("websockets.server.cvaos_tracker")
socket_logger.setLevel(logging.WARNING)  # each HTTP answer is also logged as a rejection, at INFO
socket_logger.addFilter(QuietHttp())

# Each of the page's files, with its content type. Guessing the type would ask the Windows
# registry, which another program can have changed, and a browser drops a stylesheet that isn't
# text/css.
PAGE_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
SOCKET_PATH = "/ws"
PUSH_DELAY = 0.05  # seconds to gather changes into one snapshot
# The tracker has no window of its own, so it stops once no page has been open this long, rather
# than run unseen after the player closes the tab. Opening it again starts a new one. The wait is
# long because a browser can hold back a background tab's reconnection, or put the tab to sleep,
# while the player is busy in the game.
IDLE_STOP = 1800.0
IDLE_CHECK = 15.0  # seconds between looks at whether a page is open

APP = "cvaos-tracker"
PROTOCOL = 1  # raise it when the page and the server would no longer understand each other


@functools.cache
def build_id() -> str:
    """Name this tracker's version: the messages it speaks, the world it ships with, and its page.

    The page's files count as well, so that open pages reload, and an older tracker is replaced,
    whenever they change, even between world versions.
    """
    page = hashlib.sha256()
    for name, _ in sorted(set(PAGE_FILES.values())):
        page.update(pkgutil.get_data(__package__, f"web/{name}"))
    world = ".".join(str(part) for part in CVAOSWorld.world_version)
    return f"{PROTOCOL}/{world}/{page.hexdigest()[:8]}"


def _response(status: HTTPStatus, body: bytes, content_type: str) -> Response:
    headers = Headers([("Content-Type", content_type), ("Content-Length", str(len(body))),
                       ("Cache-Control", "no-store")])
    return Response(status.value, status.phrase, headers, body)


class TrackerServer:
    def __init__(self, ctx: TrackerContext) -> None:
        self.ctx = ctx
        self.sockets: set[ServerConnection] = set()
        self.launches: set[ServerConnection] = set()  # the sockets of launches asking, not pages
        self.server: Server | None = None
        self.port: int | None = None
        self.idle_since: float | None = time.monotonic()  # since the last page closed, or None
        # The map carries the build too, so a page knows which tracker served it before its socket
        # opens, possibly to a newer tracker that has taken over the address.
        self.map_json = json.dumps({**map_data(), "build": build_id()}).encode("utf-8")
        self._push_task: asyncio.Task | None = None
        ctx.listeners.append(self.push)

    async def start(self, host: str, port: int) -> int:
        """Start serving on ``host`` and ``port``, and return the port, which 0 lets the OS pick."""
        self.server = await serve(self.on_socket, host, port, process_request=self.on_request,
                                  logger=socket_logger)
        self.port = self.server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        self.close()
        await self.wait_closed()

    def close(self) -> None:
        """Stop listening and close the pages' connections, without waiting for them to end."""
        if self.server is not None:
            self.server.close()

    async def wait_closed(self) -> None:
        if self.server is not None:
            await self.server.wait_closed()

    async def stop_when_idle(self, limit: float = IDLE_STOP, check: float = IDLE_CHECK) -> None:
        """Stop the tracker once no page has been open for ``limit`` seconds."""
        while True:
            await asyncio.sleep(check)
            if self.idle_since is not None and time.monotonic() - self.idle_since >= limit:
                logger.info("No tracker page has been open for a while, so the tracker stopped")
                self.ctx.exit_event.set()
                return

    # --- HTTP ----------------------------------------------------------------------------------

    def own_hosts(self) -> set[str]:
        """The Host values a request to this server carries, however the page was opened."""
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    def on_request(self, connection: ServerConnection, request: Request) -> Response | None:
        path = request.path.split("?", 1)[0]
        hosts = self.own_hosts()
        if request.headers.get("Host") not in hosts:
            return _response(HTTPStatus.FORBIDDEN, b"Forbidden", "text/plain")
        if path == SOCKET_PATH:
            if request.headers.get("Origin") not in {f"http://{host}" for host in hosts}:
                return _response(HTTPStatus.FORBIDDEN, b"Forbidden", "text/plain")
            return None  # carry on with the websocket handshake
        if path == "/map.json":
            return _response(HTTPStatus.OK, self.map_json, "application/json")
        if path in PAGE_FILES:
            name, content_type = PAGE_FILES[path]
            return _response(HTTPStatus.OK, pkgutil.get_data(__package__, f"web/{name}"),
                             content_type)
        return _response(HTTPStatus.NOT_FOUND, b"Not found", "text/plain")

    # --- websocket -----------------------------------------------------------------------------

    async def on_socket(self, socket: ServerConnection) -> None:
        idle_since = self.idle_since  # kept in case this is a launch asking, not a page
        self.sockets.add(socket)
        self.idle_since = None
        try:
            await socket.send(self.snapshot_message())
            async for message in socket:
                await self.on_message(socket, message)
        except ConnectionClosed:
            pass  # a page closed without saying goodbye, as a closed tab or browser can
        finally:
            self.sockets.discard(socket)
            if not self.sockets:
                # A launch's question doesn't restart the wait, or a tracker whose tab is closed
                # would never stop while launches for other slots keep asking it.
                launch = socket in self.launches and idle_since is not None
                self.idle_since = idle_since if launch else time.monotonic()
            self.launches.discard(socket)

    async def on_message(self, socket: ServerConnection, message: str | bytes) -> None:
        try:
            request = json.loads(message)
            kind = request["type"]
        except (ValueError, KeyError, TypeError):
            logger.warning("The tracker page sent a message it doesn't understand: %r", message)
            return
        ctx = self.ctx
        if kind == "connect":
            await ctx.connect_to(str(request.get("server", "")).strip(),
                                 str(request.get("slot", "")).strip(), request.get("password"))
        elif kind == "disconnect":
            await ctx.disconnect()
        elif kind == "route":
            try:
                location_id = int(request["location"])
            except (KeyError, TypeError, ValueError):
                logger.warning("The tracker page asked for a route to %r", request.get("location"))
                return
            ctx.set_route(location_id)
        elif kind == "clear_route":
            ctx.set_route(None)
        elif kind == "quit":
            ctx.exit_event.set()
        elif kind == "handover":
            self.launches.add(socket)
            result = await ctx.hand_over(str(request.get("server") or "").strip(),
                                         str(request.get("slot") or "").strip(),
                                         request.get("password"), bool(request.get("take", True)))
            if result == "opened":
                # The launch now opens this tracker's page, so the half hour starts again, as for
                # a page, rather than the tracker stopping before the page has loaded.
                self.launches.discard(socket)
            await socket.send(json.dumps({"type": "handover", "result": result}))

    def snapshot_message(self) -> str:
        return json.dumps({"type": "snapshot", "app": APP, "build": build_id(),
                           "snapshot": self.ctx.snapshot()})

    def push(self) -> None:
        """Send the page a new snapshot soon, once for any number of changes before then."""
        if self._push_task is None or self._push_task.done():
            try:
                self._push_task = asyncio.get_running_loop().create_task(self._push_later())
            except RuntimeError:
                pass  # no event loop yet, so no page to tell

    async def _push_later(self) -> None:
        await asyncio.sleep(PUSH_DELAY)
        if self.sockets:
            message = self.snapshot_message()
            await asyncio.gather(*(socket.send(message) for socket in list(self.sockets)),
                                 return_exceptions=True)
