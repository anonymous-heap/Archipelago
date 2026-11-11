"""
The annotator's local server: the page, the map data, Google's file picker, and a websocket.

The page sends an action as a JSON object with a ``type``. Every action that writes names the
snapshot version the page was showing. Before writing, the server reads the tab again. If any row
of a placement the action touches has changed since that snapshot, it writes nothing, sends the
fresh snapshot and says why. A placement that needs a new row gets one inserted first, then the
action runs again on the re-read tab.

    {"type": "sign_in"}                       sign in to Google in the browser
    {"type": "refresh"}                       read the tab again
    {"type": "settings", "annotator", "emuhawk", "rom_path"}   any of: the name written to
                                              "Annotated by", and the paths for testing
    {"type": "record", "version", "enemy", "doors_cell", "new_doors_cell", "requirement"}
    {"type": "same_for_enemies", "version", "enemy", "doors_cell", "targets", "overwrite"}
    {"type": "same_for_entrances", "version", "enemy", "doors_cell", "doors"}
    {"type": "new_row", "version", "enemy"}   a blank row for another set of doors

Testing in EmuHawk (see testing.py and emulator.py):

    {"type": "launch_emulator"}               start EmuHawk on the test ROM, with the bridge
    {"type": "connect_emulator"}              connect to an EmuHawk that already runs the bridge
    {"type": "test", "enemy", "door", "loadout"}   start a run from a door with a loadout
    {"type": "retry"}                         go back to the run's start and try again
    {"type": "end_run", "outcome"}            end the run by hand: "reached" or "failed"

These run beside the sheet's actions rather than after them, since launching EmuHawk takes a
while. While a run goes on, the server sends ``{"type": "run", "run": {...}}`` twice a second.

The server sends ``{"type": "snapshot", ...}`` after every change, and ``{"type": "result",
"ok", "message"}`` after every action.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import pkgutil
from http import HTTPStatus
from typing import Optional

from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from ...data import entrance_info, map_info, routing_info
from ...data.enemy_info import by_name as enemy_types
from ...data.routing_info import AbilityCombo
from ...data.room_info import by_identifier as rooms_by_identifier
from ...tracker_app.mapdata import map_data
from . import emulator, settings, sheets, testing
from .model import TAB_TITLE, Edit, EditError, EnemyTab, Requirement
from .worklist import room_pairs, worklist

logger = logging.getLogger("cvaos.annotator")

# Both loopback addresses: a browser asked for "localhost" on Windows tries IPv6 first, and
# waits about 0.2 s for each request when only the IPv4 one answers. The port is fixed, because
# the picker's API key allows only pages served from it.
HOSTS, PORT = ["127.0.0.1", "::1"], 38400
EMULATOR_ACTIONS = ("launch_emulator", "connect_emulator", "test", "retry", "end_run")
# Overlay lines stay under about 32 characters, which is what fits across EmuHawk at 2x.
HOTKEY_HINT = "L+R+Sel retry, L+R+Start give up"
PAGE_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


def _response(status: HTTPStatus, body: bytes, content_type: str) -> Response:
    headers = Headers([("Content-Type", content_type), ("Content-Length", str(len(body))),
                       ("Cache-Control", "no-store")])
    return Response(status.value, status.phrase, headers, body)


def _signed(value: int) -> int:
    return value - 256 if value > 127 else value


def room_geometry() -> dict[str, dict]:
    """Return each room's top-left map cell, its cells, and its doors.

    A door is drawn between ``inside``, the room's cell next to it, and ``outside``, the cell just
    beyond it, which ``entrance_info`` stores relative to the room. Its ``label`` is the neighbor
    as a doors cell names it, such as "917" or "104 (left)". Portal nodes, which have no door row,
    are left out.
    """
    cells_by_room: dict[str, list[tuple[int, int]]] = {}
    for y in range(map_info.ROWS):
        for x in range(map_info.COLUMNS):
            room = map_info.room_at(x, y)
            if room:
                cells_by_room.setdefault(room, []).append((x, y))
    door_rows = {row.door_identifier_unique: row for row in entrance_info.rows}
    out = {}
    for room, info in rooms_by_identifier.items():
        origin = (int(info.x), int(info.y))
        cells = cells_by_room.get(room, [])
        doors = []
        for node in sorted(routing_info._arrivals_by_room.get(room, ())):
            row = door_rows.get(node)
            if row is None or not cells:
                continue
            outside = (origin[0] + _signed(int(row.x_pos_door)),
                       origin[1] + _signed(int(row.y_pos_door)))
            inside = min(cells, key=lambda c: abs(c[0] - outside[0]) + abs(c[1] - outside[1]))
            doors.append({"node": node, "label": node.split(":", 1)[1], "inside": inside,
                          "outside": outside})
        out[room] = {"origin": origin, "cells": cells, "doors": doors}
    return out


class AnnotatorServer:
    def __init__(self) -> None:
        self.settings = {"sheet_id": "", "annotator": "", "rom_path": "", "emuhawk": "",
                         "emuhawk_args": ["--gdi"], **settings.load()}
        self.bridge = emulator.Bridge()
        self.bridge.on_close = lambda: self.spawn(self.emulator_closed())
        self.bridge.on_event = lambda event: self.spawn(self.on_bridge_event(event))
        self.emulator_lock = asyncio.Lock()
        self.emulator_message = ""
        self.run_state: Optional[testing.Run] = None
        self.watcher: Optional[asyncio.Task] = None
        self.tasks: set[asyncio.Task] = set()
        self.rom: Optional[bytes] = None   # the US ROM, read on first use
        self.sockets: set[ServerConnection] = set()
        self.client: Optional[sheets.SheetClient] = None
        self.tab: Optional[EnemyTab] = None
        self.version = 0
        # The last few snapshots' tabs, by version, so that a page's action is checked against
        # the rows that page was showing, even after an unrelated reload.
        self.tabs: dict[int, EnemyTab] = {}
        self.state = {"signed_in": False, "access": False, "message": ""}
        self.combo_options: list[str] = []
        self.lock = asyncio.Lock()
        world_map = {key: value for key, value in map_data().items() if key != "locations"}
        self.map_json = json.dumps({**world_map, "geometry": room_geometry()}).encode("utf-8")

    # --- the sheet -----------------------------------------------------------------------------

    async def connect(self) -> None:
        """Load the saved sign-in and read the tab, noting what is missing if either fails."""
        if not self.settings["sheet_id"]:
            self.state.update(signed_in=False, access=False,
                              message=f'No sheet is set. Add "sheet_id" to {settings.path()}, '
                                      f'then start the annotator again.')
            await self.broadcast(self.snapshot())
            return
        try:
            creds = await asyncio.to_thread(sheets.load_credentials)
        except sheets.NeedsSignIn:
            self.state.update(signed_in=False, access=False, message="Sign in to Google to start.")
            await self.broadcast(self.snapshot())
            return
        self.client = sheets.SheetClient(self.settings["sheet_id"], creds)
        self.state["signed_in"] = True
        await self.reload()

    async def read_tab(self) -> EnemyTab:
        grid = await asyncio.to_thread(self.client.values, f"'{TAB_TITLE}'")
        return EnemyTab(grid)

    async def reload(self) -> None:
        try:
            self.tab = await self.read_tab()
        except sheets.NoAccess:
            self.state.update(access=False, message="Pick the logic sheet in Google's file picker "
                                                    "to let the annotator use it.")
            await self.broadcast(self.snapshot())
            return
        self.state.update(access=True, message="")
        if not self.combo_options and self.tab.rows:
            column = sheets.column_letter(self.tab.column[self.tab.combos[0]])
            self.combo_options = await asyncio.to_thread(
                self.client.validation_options, f"'{TAB_TITLE}'!{column}{self.tab.rows[0].number}")
        self.version += 1
        self.tabs[self.version] = self.tab
        for old in [v for v in self.tabs if v < self.version - 20]:
            del self.tabs[old]
        await self.broadcast(self.snapshot())

    async def apply(self, edit: Edit) -> None:
        await asyncio.to_thread(self.client.write, edit.writes)
        await asyncio.to_thread(self.client.write, edit.dated, True)
        await asyncio.to_thread(self.client.clear, edit.clears)

    # --- snapshots -----------------------------------------------------------------------------

    def snapshot(self) -> dict:
        message = {"type": "snapshot", "version": self.version, "state": self.state,
                   "settings": self.settings,
                   "emulator": {"connected": self.bridge.connected,
                                "message": self.emulator_message},
                   "run": self.run_state.status() if self.run_state else None,
                   "attempts": testing.summarize(testing.load(sheets.DATA_DIR)),
                   "tokens": list(emulator.TOKENS)}
        tab = self.tab
        if tab is None or not self.state["access"]:
            return message
        items, progress = worklist(tab)
        recorded = tab.pair_requirements()
        rows = []
        for row in tab.rows:
            requirement = tab.requirement(row)
            values = row.values
            rows.append({
                "row": row.number, "enemy": row.enemy, "room": values["room_id"].strip(),
                "name": values["Enemy Name"].strip(), "specifier": values["Specifier"].strip(),
                "x": _number(values.get("x")), "y": _number(values.get("-y")),
                "doors_cell": row.doors_cell, "doors": sorted(tab.doors(row)),
                "ticks": sorted(requirement.ticks), "combos": list(requirement.combos),
                "status": requirement.status, "notes": requirement.notes,
                "annotated_by": values.get("Annotated by", "").strip(),
                "date": values.get("Date", "").strip(),
                "shared_enemies": values.get("Shared enemies", "").strip(),
                "shared_entrances": values.get("Shared entrances", "").strip()})
        pairs = {}
        for room in progress:
            for pair in room_pairs(tab, room, recorded):
                pairs.setdefault(str(pair.enemy), []).append(
                    {"door": pair.door, "state": pair.state, "masks": list(pair.masks)})
        message.update(
            rows=rows, pairs=pairs, progress=progress,
            worklist=[{"enemy": item.enemy, "name": item.name, "room": item.room,
                       "open_doors": list(item.open_doors), "group": item.group}
                      for item in items],
            abilities=[name for name in tab.abilities if name != "<alt 2>"],
            combo_options=self.combo_options, statuses=["", "Tested", "Judged"],
            flags={str(int(flag)): flag.name for flag in AbilityCombo if flag})
        return message

    async def broadcast(self, message: dict) -> None:
        text = json.dumps(message)
        for socket in list(self.sockets):
            try:
                await socket.send(text)
            except ConnectionClosed:
                self.sockets.discard(socket)

    # --- actions -------------------------------------------------------------------------------

    async def act(self, message: dict) -> str:
        """Carry out one writing action and return a description of what changed."""
        seen = self.tabs.get(message.get("version"))
        if seen is None:
            raise EditError("The page was showing a copy of the tab too old to check against. "
                            "It has been refreshed, so check the row and try again.")
        enemies = [int(message["enemy"])] + [int(t) for t in message.get("targets", [])]
        fresh = await self.read_tab()
        changed = [e for e in enemies if fresh.placement_state(e) != seen.placement_state(e)]
        if changed:
            raise Conflict(changed)
        today = datetime.date.today()
        annotator = self.settings.get("annotator", "")
        kind = message["type"]
        if kind == "new_row":
            rows = fresh.rows_of(int(message["enemy"]))
            if not rows:
                raise EditError(f"no rows for enemy {message['enemy']}")
            if fresh.blank_row(int(message["enemy"])) is not None:
                return "This enemy already has a blank row, so that one is selected."
            await self.insert_rows(fresh, [rows[-1].number])
            return f"added a row for enemy {message['enemy']}"
        for _ in range(2):   # a second pass after inserting rows for "same for enemies"
            if kind == "record":
                requirement = message["requirement"]
                edit = fresh.record(int(message["enemy"]), message["doors_cell"], Requirement(
                    frozenset(requirement.get("ticks", [])), tuple(requirement.get("combos", [])),
                    requirement.get("status", ""), requirement.get("notes", "")),
                    annotator=annotator, today=today, new_doors_cell=message.get("new_doors_cell"))
            elif kind == "same_for_enemies":
                edit = fresh.same_for_enemies(int(message["enemy"]), message["doors_cell"],
                                              [int(t) for t in message["targets"]],
                                              annotator=annotator, today=today,
                                              overwrite=bool(message.get("overwrite")))
            elif kind == "same_for_entrances":
                edit = fresh.same_for_entrances(int(message["enemy"]), message["doors_cell"],
                                                message["doors"])
            else:
                raise EditError(f"unknown action {kind!r}")
            if not edit.insert_after:
                break
            await self.insert_rows(fresh, edit.insert_after)
            fresh = await self.read_tab()
        else:
            raise EditError("Rows were inserted, but the action still couldn't find them.")
        await self.apply(edit)
        after = await self.read_tab()
        enemy_at = {row.number: row.values["enemy_number"].strip() for row in after.rows}
        moved = sorted(number for number, enemy in edit.touched.items()
                       if enemy and enemy_at.get(number) != enemy)
        if moved:
            raise EditError(f"Rows moved while this was being written, so rows "
                            f"{', '.join(map(str, moved))} may have received another enemy's "
                            f"values. Check them in the sheet.")
        return f"wrote {len(edit.writes) + len(edit.dated)} cells and cleared {len(edit.clears)}"

    # --- testing in EmuHawk ---------------------------------------------------------------------

    def spawn(self, coroutine) -> asyncio.Task:
        """Run ``coroutine`` as a task, keeping a reference so that it isn't collected early."""
        task = asyncio.get_running_loop().create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def on_emulator_message(self, socket: ServerConnection, kind: str,
                                  message: dict) -> None:
        async with self.emulator_lock:
            try:
                ok, result = True, await self.emulator_action(kind, message)
            except (EditError, emulator.BridgeError) as error:
                ok, result = False, str(error)
            except Exception as error:  # report anything else to the page instead of dying
                logger.exception("emulator action failed")
                ok, result = False, f"{type(error).__name__}: {error}"
        await self.broadcast(self.snapshot())
        try:
            await socket.send(json.dumps({"type": "result", "ok": ok, "message": result,
                                          "action": kind}))
        except ConnectionClosed:
            pass

    async def on_bridge_event(self, event: dict) -> None:
        """Carry out a hotkey pressed in EmuHawk, and tell every page what happened."""
        actions = {"retry": ("retry", {}), "give_up": ("end_run", {"outcome": "failed"})}
        if event.get("event") != "hotkey" or event.get("name") not in actions:
            return
        kind, message = actions[event["name"]]
        async with self.emulator_lock:
            try:
                ok, result = True, await self.emulator_action(kind, message)
            except (EditError, emulator.BridgeError) as error:
                ok, result = False, str(error)
        await self.broadcast(self.snapshot())
        await self.broadcast({"type": "result", "ok": ok, "message": result, "action": kind})

    async def emulator_action(self, kind: str, message: dict) -> str:
        if kind == "launch_emulator":
            if self.bridge.connected:
                return "EmuHawk is already connected."
            await asyncio.to_thread(emulator.launch, self.file_setting("emuhawk", "EmuHawk.exe"),
                                    self.file_setting("rom_path", "US ROM"), sheets.DATA_DIR,
                                    list(self.settings.get("emuhawk_args", [])))
            return await self.connect_bridge(60)
        if kind == "connect_emulator":
            return await self.connect_bridge(5)
        if not self.bridge.connected:
            raise EditError("Launch or connect EmuHawk first.")
        if kind == "test":
            return await self.start_run(int(message["enemy"]), message["door"],
                                        [t for t in message.get("loadout", [])
                                         if t in emulator.TOKENS])
        run = self.run_state
        if run is None:
            raise EditError("No test has been started.")
        if kind == "retry":
            if run.outcome is not None:
                return await self.start_run(run.enemy, run.door, run.loadout)
            self.stop_watching()
            await self.restart(run)
            self.watcher = self.spawn(self.watch(run))
            return "Back at the door."
        outcome = message.get("outcome")
        if outcome not in ("reached", "failed") or run.outcome is not None:
            raise EditError("There is no run going on to end.")
        self.stop_watching()
        await self.finish_run(run, outcome)
        return f"Recorded the run as {outcome}."

    async def connect_bridge(self, timeout: float) -> str:
        try:
            await self.bridge.connect(timeout)
            await self.bridge.wait_until_playing()
            await self.bridge.keep_vitals_full()
            await self.bridge.keep_strength_up()
        except emulator.BridgeError as error:
            raise EditError(str(error)) from error
        self.emulator_message = "Connected."
        return "EmuHawk is connected and in the game."

    async def emulator_closed(self) -> None:
        self.stop_watching()
        self.run_state = None
        self.emulator_message = "EmuHawk closed."
        await self.broadcast(self.snapshot())

    async def start_run(self, enemy: int, door: str, loadout: list[str]) -> str:
        """Put Soma at ``door`` with ``loadout`` and start watching for the placement."""
        from .landing import door_flags, teleport_args

        rows = self.tab.rows_of(enemy) if self.tab else []
        if not rows:
            raise EditError(f"no rows for enemy {enemy}")
        values = rows[0].values
        name, room = values["Enemy Name"].strip(), values["room_id"].strip()
        if not door.startswith(f"{room}:"):
            raise EditError(f"{door} isn't a door of room {room}")
        type_id = enemy_types[name].id
        twins = [row for row in self.tab.rows if row.enemy != enemy
                 and row.values["room_id"].strip() == room
                 and row.values["Enemy Name"].strip() == name]
        self.stop_watching()
        rom = await self.us_rom()
        try:
            args = await asyncio.to_thread(teleport_args, door, rom)
            entered = await asyncio.to_thread(door_flags, door, rom)
        except Exception as error:
            raise EditError(f"no landing for {door}: {error}") from error
        target =(_number(values.get("x")), _number(values.get("-y")))
        # Two placements of one type at one spot have an entry each. They take the entries in
        # the order of their enemy numbers, so that each gets a different one.
        earlier = {row.enemy for row in twins if row.enemy < enemy and target == (
            _number(row.values.get("x")), _number(row.values.get("-y")))}
        entry = emulator.room_entry(rom, args["room_address"], type_id, *target,
                                    index=len(earlier))
        # The room's flags are set so that its event scenes don't play and the enemies that
        # wait for an event or for another room's boss are there.
        seen, boss_bit = emulator.room_flags(rom, args["room_address"])
        flags = [(emulator.ENEMY_DEFEATED, type_id, False),
                 *((address, number, True) for address, number in seen)]
        if boss_bit is not None:
            # An arena holds its boss while its boss flag is clear, and its other enemies once
            # the flag is set. Its only enemy entries are the boss.
            is_boss = entry is not None and entry.kind == emulator.ENEMY_ENTRY
            flags.append((emulator.BOSS_FLAGS, boss_bit, not is_boss))
        # A boss door or a wall that Soma has passed to reach this door is open, and the room's
        # other walls are whole, whatever an earlier run broke.
        await self.bridge.set_flags(*flags, *entered)
        entity_id = entry.entry_id if entry else None
        await self.bridge.set_loadout(set(loadout))
        await self.bridge.equip_test_weapon()
        await self.bridge.keep_vitals_full()
        await self.bridge.keep_strength_up()
        try:
            state = await self.bridge.teleport(args["room_address"], args["camera_x"],
                                               args["camera_y"], args["soma_x"], args["soma_y"])
        except emulator.BridgeError as error:
            raise EditError(str(error)) from error
        await self.bridge.call("state_save", slot="run")
        label = door.split(":", 1)[1]
        await self.bridge.call("overlay", text=f"{name} #{enemy} from {label}\n"
                                               f"{'+'.join(loadout) or 'No souls'}\n{HOTKEY_HINT}")
        run = testing.Run(enemy, name, room, door, target, type_id, list(loadout),
                          room_address=args["room_address"], entity_id=entity_id,
                          twins=bool(twins), used=set(loadout) | state.used)
        self.run_state = run
        self.watcher = self.spawn(self.watch(run))
        return f"Testing {name} #{enemy} from {label}."

    def file_setting(self, key: str, label: str) -> str:
        """Return the path that a setting holds, or raise EditError when no file is there.

        ``label`` is what the page calls the setting, and the error names it.
        """
        path = self.settings.get(key, "")
        if not path:
            raise EditError(f"Set the {label} path first.")
        if not os.path.isfile(path):
            raise EditError(f"No file is at the {label} path, {path}.")
        return path

    async def us_rom(self) -> bytes:
        if self.rom is None:
            with open(self.file_setting("rom_path", "US ROM"), "rb") as handle:
                self.rom = await asyncio.to_thread(handle.read)
        return self.rom

    async def defeated(self, run: testing.Run, state: emulator.GameState) -> bool:
        """Whether the run's target has been defeated.

        Its own entity at HP 0 or below shows it directly. Otherwise the type's defeated bit
        counts, unless another placement of the same type in the room could have set it.
        """
        if run.entity_id is not None:
            try:
                enemies = await self.bridge.enemies(run.type_id)
            except emulator.BridgeError:  # a bridge started before it had the entities command
                run.entity_id = None
                enemies = []
            if any(entry == run.entity_id and hp <= 0 for _, entry, hp in enemies):
                return True
        bit = state.defeated[run.type_id >> 3] & (1 << (run.type_id & 7))
        return bool(bit) and not run.twins

    async def restart(self, run: testing.Run) -> None:
        """Put the game back as it was when ``run`` started, and forget what was used since."""
        await self.bridge.call("state_load", slot="run")
        state = await self.bridge.state()
        run.used = set(run.loadout) | state.used
        run.distance = None

    async def watch(self, run: testing.Run) -> None:
        """Follow the run until Soma defeats or reaches the placement, or the run is replaced.

        Leaving the room puts Soma back at the door, as Retry does.
        """
        loop = asyncio.get_running_loop()
        pushed = 0.0
        try:
            while self.run_state is run and run.outcome is None:
                state = await self.bridge.state()
                if state.playing and state.room_pointer != run.room_address:
                    await self.restart(run)
                    await self.bridge.call("overlay", text="Left the room, so back\nat the door."
                                                           f"\n{HOTKEY_HINT}")
                    continue
                if state.playing:
                    run.used |= state.used
                    run.distance = int(((state.soma[0] - run.target[0]) ** 2
                                        + (state.soma[1] - run.target[1]) ** 2) ** 0.5)
                    if await self.defeated(run, state):
                        await self.finish_run(run, "defeated")
                        return
                    if run.distance <= testing.REACH_DISTANCE:
                        await self.finish_run(run, "reached")
                        return
                if loop.time() - pushed > 0.5:
                    pushed = loop.time()
                    await self.broadcast({"type": "run", "run": run.status()})
                await asyncio.sleep(0.15)
        except emulator.BridgeError:
            return

    async def finish_run(self, run: testing.Run, outcome: str) -> None:
        if run.outcome is not None:
            return
        run.outcome = outcome
        testing.append(sheets.DATA_DIR, run, self.settings.get("annotator", ""))
        try:
            await self.bridge.call("overlay", text=f"{outcome.capitalize()}.\n"
                                                   f"L+R+Select to try again.")
        except emulator.BridgeError:
            pass
        await self.broadcast(self.snapshot())

    def stop_watching(self) -> None:
        if self.watcher is not None and not self.watcher.done():
            self.watcher.cancel()
        self.watcher = None

    async def insert_rows(self, tab: EnemyTab, after_rows: list[int]) -> None:
        """Insert a row below each listed row, then copy the placement's identity into it."""
        sheet_tab = await asyncio.to_thread(self.client.tab, TAB_TITLE)
        enemy_by_row = {row.number: row.enemy for row in tab.rows}
        for after in sorted(set(after_rows), reverse=True):
            await asyncio.to_thread(self.client.insert_rows, sheet_tab, after)
        fill = Edit()
        for after in sorted(set(after_rows)):
            shift = sum(1 for other in set(after_rows) if other < after)
            fill.extend(tab.fill_new_row(enemy_by_row[after], after + 1 + shift))
        await self.apply(fill)

    # --- HTTP and websocket --------------------------------------------------------------------

    async def room_image(self, room: str) -> Response:
        """Return the room's background as a PNG, drawn from the ROM once and then cached."""
        info = rooms_by_identifier.get(room)
        rom_path = self.settings.get("rom_path", "")
        if info is None or not os.path.exists(rom_path):
            return _response(HTTPStatus.NOT_FOUND, b"Not found", "text/plain")
        try:
            from .rooms import room_png
            body = await asyncio.to_thread(room_png, rom_path, int(info.room_address),
                                           sheets.data_path("rooms"))
        except Exception:  # a room that fails to draw just has no background
            logger.exception("couldn't draw room %s", room)
            return _response(HTTPStatus.NOT_FOUND, b"Not found", "text/plain")
        return _response(HTTPStatus.OK, body, "image/png")

    async def on_request(self, connection: ServerConnection,
                         request: Request) -> Optional[Response]:
        path = request.path.split("?", 1)[0]
        hosts = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
        if request.headers.get("Host") not in hosts:
            return _response(HTTPStatus.FORBIDDEN, b"Forbidden", "text/plain")
        if path == "/ws":
            if request.headers.get("Origin") not in {f"http://{host}" for host in hosts}:
                return _response(HTTPStatus.FORBIDDEN, b"Forbidden", "text/plain")
            return None
        if path == "/map.json":
            return _response(HTTPStatus.OK, self.map_json, "application/json")
        if path.startswith("/room/") and path.endswith(".png"):
            return await self.room_image(path[len("/room/"):-len(".png")])
        if path == "/picker.json":
            return _response(HTTPStatus.OK, json.dumps(self.picker_config()).encode(),
                             "application/json")
        if path in PAGE_FILES:
            name, content_type = PAGE_FILES[path]
            return _response(HTTPStatus.OK, pkgutil.get_data(__package__, f"web/{name}"),
                             content_type)
        return _response(HTTPStatus.NOT_FOUND, b"Not found", "text/plain")

    def picker_config(self) -> dict:
        """Return what the page needs to show Google's file picker for the configured sheet."""
        try:
            creds = sheets.load_credentials()
        except sheets.NeedsSignIn:
            return {}
        config = sheets.google_config()
        return {"apiKey": config["picker_api_key"], "appId": str(config["project_number"]),
                "token": creds.token, "fileId": self.settings["sheet_id"]}

    async def on_socket(self, socket: ServerConnection) -> None:
        self.sockets.add(socket)
        try:
            await socket.send(json.dumps(self.snapshot()))
            async for text in socket:
                await self.on_message(socket, json.loads(text))
        except ConnectionClosed:
            pass
        finally:
            self.sockets.discard(socket)

    async def on_message(self, socket: ServerConnection, message: dict) -> None:
        kind = message.get("type")
        if kind in EMULATOR_ACTIONS:
            self.spawn(self.on_emulator_message(socket, kind, message))
            return
        async with self.lock:
            try:
                if kind == "sign_in":
                    await asyncio.to_thread(sheets.sign_in)
                    await self.connect()
                    result = "Signed in."
                elif kind in ("refresh", "picked"):
                    if self.client is None:
                        await self.connect()
                    else:
                        await self.reload()
                    result = "Reloaded the tab."
                elif kind == "settings":
                    for key in ("annotator", "emuhawk", "rom_path"):
                        if key in message:
                            self.settings[key] = str(message[key]).strip().strip('"')
                    settings.save(self.settings)
                    result = "Saved."
                else:
                    result = await self.act(message)
                    await self.reload()
                ok = True
            except Conflict as error:
                await self.reload()
                await socket.send(json.dumps({"type": "result", "ok": False, "action": kind,
                                              "conflict": error.enemies, "message": str(error)}))
                return
            except EditError as error:
                ok, result = False, str(error)
                await self.reload()
            except Exception as error:  # report anything else to the page instead of dying
                logger.exception("annotator action failed")
                ok, result = False, f"{type(error).__name__}: {error}"
        await socket.send(json.dumps({"type": "result", "ok": ok, "message": result,
                                      "action": kind}))

    async def run(self) -> None:
        async with serve(self.on_socket, HOSTS, PORT, process_request=self.on_request):
            logger.info("The annotator is at http://localhost:%d/", PORT)
            self.spawn(self.connect_quietly())
            try:
                await self.connect()
            except Exception as error:  # the page offers Refresh, so keep serving
                logger.exception("couldn't read the sheet")
                self.state["message"] = (f"Couldn't read the sheet ({type(error).__name__}). "
                                         f"Press Refresh to try again.")
            await asyncio.Future()


    async def connect_quietly(self) -> None:
        """Connect to an EmuHawk that already runs the bridge, if there is one."""
        async with self.emulator_lock:
            try:
                await self.connect_bridge(0)
            except EditError:
                return
        await self.broadcast(self.snapshot())


class Conflict(EditError):
    """An action refused because someone changed the placements' rows since the page saw them."""

    def __init__(self, enemies: list[int]):
        self.enemies = enemies
        super().__init__(f"Someone changed the rows of enemy {', '.join(map(str, enemies))} in "
                         f"the sheet since this page loaded them. Nothing was written.")


def _number(value) -> int:
    try:
        return int(str(value).strip() or 0)
    except ValueError:
        return 0
