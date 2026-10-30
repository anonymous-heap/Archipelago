"""
The tracker's local web server: the page, the map, and a websocket that keeps the page current.

GET / and the page's own files come from web/ in this package, which pkgutil reads whether the
world is a folder or a zipped .apworld. GET /map.json is the map (mapdata.py). The page then opens
/ws, and the server sends it the tracker's snapshot whenever that may have changed. The page sends
back what the player asks for, each as a JSON object with a "type":

    {"type": "connect", "server": "localhost:38281", "slot": "Soma", "password": ""}
    {"type": "disconnect"}
    {"type": "route", "location": 123}      a route to the location with that id
    {"type": "clear_route"}
    {"type": "quit"}                        stop the tracker
"""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import pkgutil
from http import HTTPStatus

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed, InvalidMessage, InvalidStatus
from websockets.http11 import Request, Response

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

PAGE_FILES = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js",
              "/style.css": "style.css"}
SOCKET_PATH = "/ws"
PUSH_DELAY = 0.05  # seconds to gather changes into one snapshot


def _response(status: HTTPStatus, body: bytes, content_type: str) -> Response:
    headers = Headers([("Content-Type", content_type), ("Content-Length", str(len(body))),
                       ("Cache-Control", "no-store")])
    return Response(status.value, status.phrase, headers, body)


class TrackerServer:
    def __init__(self, ctx: TrackerContext) -> None:
        self.ctx = ctx
        self.sockets: set[ServerConnection] = set()
        self.server: Server | None = None
        self.map_json = json.dumps(map_data()).encode("utf-8")
        self._push_task: asyncio.Task | None = None
        ctx.listeners.append(self.push)

    async def start(self, host: str, port: int) -> int:
        """Start serving on ``host`` and ``port``, and return the port, which 0 lets the OS pick."""
        self.server = await serve(self.on_socket, host, port, process_request=self.on_request,
                                  logger=socket_logger)
        return self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    # --- HTTP ----------------------------------------------------------------------------------

    def on_request(self, connection: ServerConnection, request: Request) -> Response | None:
        path = request.path.split("?", 1)[0]
        if path == SOCKET_PATH:
            return None  # carry on with the websocket handshake
        if path == "/map.json":
            return _response(HTTPStatus.OK, self.map_json, "application/json")
        if path in PAGE_FILES:
            name = PAGE_FILES[path]
            body = pkgutil.get_data(__package__, f"web/{name}")
            content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type.endswith("javascript"):
                content_type += "; charset=utf-8"
            return _response(HTTPStatus.OK, body, content_type)
        return _response(HTTPStatus.NOT_FOUND, b"Not found", "text/plain")

    # --- websocket -----------------------------------------------------------------------------

    async def on_socket(self, socket: ServerConnection) -> None:
        self.sockets.add(socket)
        try:
            await socket.send(self.snapshot_message())
            async for message in socket:
                await self.on_message(message)
        except ConnectionClosed:
            pass  # a page closed without saying goodbye, as a closed tab or browser can
        finally:
            self.sockets.discard(socket)

    async def on_message(self, message: str | bytes) -> None:
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
            ctx.set_route(int(request["location"]))
        elif kind == "clear_route":
            ctx.set_route(None)
        elif kind == "quit":
            ctx.exit_event.set()

    def snapshot_message(self) -> str:
        return json.dumps({"type": "snapshot", "snapshot": self.ctx.snapshot()})

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
