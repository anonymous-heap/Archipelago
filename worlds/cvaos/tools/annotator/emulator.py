"""
EmuHawk for the annotator: the test ROM, launching it, and talking to the bridge script.

The test ROM is the US ROM with three changes:

* Single-Jump Divekick, the movement patch seeds use and logic assumes;
* a boot patch that replaces the Nintendo logo's game-state update (0x08002B54). It calls the
  game's new-game setup (``sub_08014548``), stores ``GetRoomPointer(0, 0)`` as the room to load
  (0x020003CC) and returns 4, which means "in game". EmuHawk then starts in room 000 with a level 1
  Soma, without menus or a save file. The game's debug menu does the same for its "entrance"
  option, and DSVEdit's room test patches the same spot;
* the same patch marks the opening event as seen (bit 0 of 0x02000340). Otherwise that event
  starts in room 000 and locks the camera (0x02000427 = 1) until it ends, so a warp out of room
  000 mid-event leaves the camera fixed in every later room;
* Start skips event scenes (0x0805B56C), as in DSVEdit's room test.

``bridge.lua`` runs inside EmuHawk and serves reads, writes, per-frame freezes, held buttons,
screenshots and memory savestates on 127.0.0.1:43090 (see its header). :class:`Bridge` is the
client side.

What the game's memory means is kept apart from the connection, in :func:`parse_state`,
:func:`loadout_writes`, :func:`weapon_writes` and :func:`teleport_writes`. The automated routing
tools in ``tools/autoroute`` use those functions with a connection of their own.

Addresses are those in ``worlds/cvaos/ram/addresses.py`` and the decompilation. Teleporting uses
the game's own warp: TeleportPlayer's destination is stored at 0x020003F8, and game state 8 at
0x02000064 applies it on the next frames. Its camera y is 48 pixels above the screen's real top
row, because the game adds 48.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import struct
import subprocess
from dataclasses import dataclass
from typing import Callable, Iterable, NamedTuple, Optional, Sequence

from ...rom import single_jump_divekick

ROM_BASE = 0x08000000
US_ROM_MD5 = "e7470df4d241f73060d14437011b90ce"
BRIDGE_PORT = 43090

# The boot patch, assembled with keystone and checked with capstone (see the M0 notes):
# push {r4, lr}; bl 0x08014548; movs r0, #0; movs r1, #0; bl 0x08001980; r1 = 0x020003CC;
# str r0, [r1]; subs r1, #0x8C; ldrb r0, [r1]; movs r2, #1; orrs r0, r2; strb r0, [r1];
# movs r0, #4; pop {r4, pc}
BOOT_ADDRESS = 0x08002B54
BOOT_EXPECTED = bytes.fromhex("30b501256d420649")
BOOT_PATCH = bytes.fromhex("10b511f0f7fc00200021fef70fff02210906f322920089180860"
                           "8c390878012210430870042010bd")
EVENT_SKIP_ADDRESS = 0x0805B56C
EVENT_SKIP_EXPECTED = bytes.fromhex("186e")   # ldr r0, [r3, #0x60]
EVENT_SKIP = bytes.fromhex("0320")            # movs r0, #3

GAME_STATE = 0x02000010       # u8, 4 = in game
MENU_STATE = 0x02000064       # u8, 1 = normal play, 8 = teleport
MENU_SUBSTATE = 0x02000065
ROOM_POINTER = 0x02000088     # u32
AREA_ROOM = 0x0200009E        # u8 area, u8 room
CAMERA = (0x0200A09A, 0x0200A09E)
SOMA = (0x02000526, 0x0200052A)
VITALS = 0x0201327A           # s16 HP, s16 MP, u16 max HP, u16 max MP
TELEPORT = 0x020003F8         # u32 room, u16 camera x, u16 camera y, s16 Soma x, s16 Soma y
EQUIPPED_BLUE, EQUIPPED_YELLOW = 0x0201326A, 0x0201326B   # soul index + 1, 0 = none
BLUE_OWNED, YELLOW_OWNED, ABILITY_OWNED = 0x02013354, 0x0201336E, 0x02013392
ABILITY_ON = 0x02013396       # bit per ability soul
ENEMY_DEFEATED = 0x020133A0   # 16 bytes, bit (id & 7) of byte (id >> 3)
BOSS_FLAGS = 0x0200037E       # u16, a bit for each boss that has been defeated
EVENT_FLAGS = 0x0200033C      # a bit for each event, flag n at bit (n & 7) of byte (n >> 3)
# The event flag that the game sets once an event scene has played, by the scene's number in the
# room entry that starts it. The boot patch sets the opening scene's flag (scene 3). The others
# were read from the game after letting each scene play. A scene that doesn't start in a new game
# isn't listed, because no flag is needed to keep it from playing.
EVENT_SEEN = {3: 0x20, 4: 0x21, 5: 0x22, 6: 0x23, 8: 0x25, 0x16: 0x2B, 0x19: 0x2E}
MISC_FLAGS = 0x02000344       # a bit for each wall or switch, numbered like EVENT_FLAGS
CAMERA_LOCK = 0x02000427      # u8: the camera scrolls only while this is 0; events raise it
EQUIPPED_WEAPON = 0x02013268  # u8 weapon index, 0xFF = none
WEAPON_OWNED = 0x020132B4     # one count byte per weapon
# The game rebuilds Soma's current stats every frame from a block that it updates only when
# equipment changes. This is the STR in that block, and ATK is STR plus the weapon's attack.
EQUIPMENT_STR = 0x02013200    # s16

# STR 6000 defeats the toughest enemy that can hold a check (Shadow Knight, 3000 HP) in one hit,
# and keeps ATK far below the 16-bit limit. One hit killed a Flesh Golem (2000 HP) in testing.
TEST_STRENGTH = 6000
# Vjaya stabs like the Knife that Soma starts with, but reaches 61 pixels from him instead of 46,
# the longest of the weapons that share that swing. It deals Thunder rather than Slash damage,
# which fewer enemies resist. Weapon reach is measured from the hitboxes in the weapon sprites.
TEST_WEAPON = 16              # Vjaya

# Each movement soul by the sheet's token for it. Ability souls are switched on by a bit;
# guardian (blue) and enchant (yellow) souls are equipped, one of each at a time.
ABILITY_SOULS = {"BDash": 0, "Slide": 1, "DJump": 2, "Kick": 3, "HJump": 4, "Galamoth": 5}
BLUE_SOULS = {"Glide": 0, "Bat": 1, "Panth": 2, "Tform": 17}   # Tform: Devil
YELLOW_SOULS = {"WWalk": 0, "Dive": 1}
TOKENS = (*ABILITY_SOULS, *BLUE_SOULS, *YELLOW_SOULS)


def build_test_rom(rom: bytes) -> bytes:
    """Return the test ROM made from the US ROM's bytes. Raise ValueError for any other ROM."""
    if hashlib.md5(rom).hexdigest() != US_ROM_MD5:
        raise ValueError("That isn't the US ROM of Aria of Sorrow (its md5 differs).")
    out = bytearray(rom)
    for offset, data in single_jump_divekick.build_writes(rom).items():
        out[offset:offset + len(data)] = data
    for address, expected, data in ((BOOT_ADDRESS, BOOT_EXPECTED, BOOT_PATCH),
                                    (EVENT_SKIP_ADDRESS, EVENT_SKIP_EXPECTED, EVENT_SKIP)):
        offset = address - ROM_BASE
        if rom[offset:offset + len(expected)] != expected:
            raise ValueError(f"unexpected bytes at {address:#010x}")
        out[offset:offset + len(data)] = data
    return bytes(out)


def test_rom(rom_path: str, data_dir: str) -> str:
    """Return the path of the test ROM built from ``rom_path``, writing it if it isn't there.

    The file is named by the built ROM's md5, so a change to the patches gives a new file rather
    than reusing an old build.
    """
    with open(rom_path, "rb") as handle:
        built = build_test_rom(handle.read())
    path = os.path.join(data_dir, "roms", f"aos_annotator_{hashlib.md5(built).hexdigest()[:8]}.gba")
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(built)
    return path


class RoomEntry(NamedTuple):
    """One entry of a room's entity list in the ROM.

    ``kind`` is :data:`ENEMY_ENTRY` or :data:`OBJECT_ENTRY`. ``subtype`` is the enemy type for an
    enemy, and says which special object it is for an object. What ``var_a`` and ``var_b`` hold
    depends on the subtype.
    """

    x: int          # in room pixels
    y: int
    entry_id: int   # unique within the room, and copied into each entity the entry spawns
    kind: int
    subtype: int
    var_a: int
    var_b: int


ENEMY_ENTRY, OBJECT_ENTRY = 1, 2
# Special objects by subtype, from DSVEdit's notes on them.
# A boss door has var_a 1 when it is inside the arena, and var_b is the boss's bit in BOSS_FLAGS.
# Soma is walked past a door of the second subtype when he enters the arena.
BOSS_DOORS = (2, 4)
# A wall is gone while its flag in MISC_FLAGS is set, and var_b is the flag's number. Breaking a
# wall of the first subtype sets its flag. Something else sets the flag of a wall of the second
# subtype, such as a ball on a chain that Soma knocks into it. A wall with flag 0 has no flag.
WALLS = (8, 9)
AFTER_BOSS_ENEMY = 10   # var_a is the enemy type, var_b the boss's bit in BOSS_FLAGS
AFTER_EVENT_ENEMY = 11   # var_a is the enemy type, var_b the event's flag in EVENT_FLAGS
EVENT_SCENE = 0x20       # var_a is the scene's number


def room_entries(rom: bytes, room_address: int) -> list[RoomEntry]:
    """Return the entries of a room's entity list, in the ROM's order.

    The list's address is at offset 0x14 of the room's record. Its entries are 12 bytes each,
    and the list ends at an entry whose x is 0x7FFF.
    """
    pointer = struct.unpack_from("<I", rom, room_address + 0x14 - ROM_BASE)[0]
    entries = []
    while pointer:
        x, y, entry_id, kind, subtype, _, var_a, var_b = struct.unpack_from(
            "<hhBBBBHH", rom, pointer - ROM_BASE)
        if x == 0x7FFF:
            break
        entries.append(RoomEntry(x, y, entry_id, kind, subtype, var_a, var_b))
        pointer += 12
    return entries


def room_entry(rom: bytes, room_address: int, type_id: int, x: int, y: int,
               index: int = 0) -> Optional[RoomEntry]:
    """Return the room entry that spawns enemy ``type_id`` at ``(x, y)``, or None.

    That is an enemy entry of this type, or an object that spawns an enemy of this type once a
    boss is defeated or an event has played. A few rooms have two such entries at one spot, and
    ``index`` then chooses between them, counting from 0 in the ROM's order.
    """
    for entry in room_entries(rom, room_address):
        if (entry.x, entry.y) != (x, y):
            continue
        spawns = (entry.subtype == type_id if entry.kind == ENEMY_ENTRY else
                  entry.kind == OBJECT_ENTRY and entry.var_a == type_id
                  and entry.subtype in (AFTER_BOSS_ENEMY, AFTER_EVENT_ENEMY))
        if spawns:
            if index == 0:
                return entry
            index -= 1
    return None


def room_entity_id(rom: bytes, room_address: int, type_id: int, x: int, y: int) -> Optional[int]:
    """Return the id of the room entry that spawns enemy ``type_id`` at ``(x, y)``, or None.

    The game copies the id into each entity that the entry spawns, so the id tells one enemy's
    entity from another's of the same type. An enemy that an object spawns carries the object's
    id.
    """
    entry = room_entry(rom, room_address, type_id, x, y)
    return entry.entry_id if entry else None


def room_flags(rom: bytes, room_address: int) -> tuple[list[tuple[int, int]], Optional[int]]:
    """Return the flags that settle a room's contents, and the room's own boss bit.

    The first value lists flags as (address of the flag block, flag number) pairs. With all of
    them set, the room's event scenes have already played, and the enemies that appear only
    after an event or after another room's boss are there.

    The second value is the bit of :data:`BOSS_FLAGS` for the boss that is fought in this room,
    or None when the room isn't a boss arena. It is kept apart because neither setting suits
    every enemy of the room. While the bit is clear, the boss is there and the boss doors hold
    Soma in. Once it is set, the boss and the doors are gone, and the enemies that the arena
    holds after the fight are there.
    """
    entries = [entry for entry in room_entries(rom, room_address) if entry.kind == OBJECT_ENTRY]
    boss_bit = next((entry.var_b for entry in entries
                     if entry.subtype in BOSS_DOORS and entry.var_a == 1), None)
    flags = set()
    for entry in entries:
        if entry.subtype == EVENT_SCENE and entry.var_a in EVENT_SEEN:
            flags.add((EVENT_FLAGS, EVENT_SEEN[entry.var_a]))
        elif entry.subtype == AFTER_EVENT_ENEMY:
            flags.add((EVENT_FLAGS, entry.var_b))
        elif entry.subtype == AFTER_BOSS_ENEMY and entry.var_b != boss_bit:
            flags.add((BOSS_FLAGS, entry.var_b))
    return sorted(flags), boss_bit


def lua_dir() -> str:
    """Return Archipelago's ``data/lua`` folder, which holds json.lua and the LuaSocket DLL."""
    import Utils
    return Utils.local_path("data", "lua")


def launch(emuhawk: str, rom_path: str, data_dir: str, extra_args: list[str]) -> None:
    """Start EmuHawk on the test ROM with the bridge running, detached from this process."""
    boot = os.path.join(data_dir, "boot.lua")
    bridge = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge.lua")
    with open(boot, "w", encoding="utf-8") as handle:
        handle.write(f"ANNOTATOR_AP_LUA = [[{lua_dir()}]]\ndofile([[{bridge}]])\n")
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([emuhawk, *extra_args, f"--lua={boot}", test_rom(rom_path, data_dir)],
                     cwd=os.path.dirname(emuhawk), creationflags=flags, close_fds=True)


@dataclass(frozen=True)
class GameState:
    """What the annotator watches during a test, read in one batch."""
    in_game: bool
    menu_state: int
    room_pointer: int
    room: str                # such as "90D"
    camera: tuple[int, int]
    soma: tuple[int, int]    # Soma's feet in room pixels: the camera plus his screen position
    hp: tuple[int, int]
    mp: tuple[int, int]
    used: frozenset[str]     # the movement tokens switched on or equipped right now
    defeated: bytes          # the Enemy List's defeated bits

    @property
    def playing(self) -> bool:
        return self.in_game and self.menu_state == 1


# The memory that :func:`parse_state` reads, as ``(address, length)`` ranges.
STATE_READS = ((GAME_STATE, 1), (MENU_STATE, 1), (ROOM_POINTER, 4), (AREA_ROOM, 2), (CAMERA[0], 2),
               (CAMERA[1], 2), (SOMA[0], 2), (SOMA[1], 2), (VITALS, 8), (ABILITY_ON, 1),
               (EQUIPPED_BLUE, 1), (EQUIPPED_YELLOW, 1), (ENEMY_DEFEATED, 16))
# The souls owned, which :func:`loadout_writes` changes only in part.
LOADOUT_READS = ((BLUE_OWNED, 13), (YELLOW_OWNED, 18), (ABILITY_OWNED, 3))
# The number of test weapons owned, which :func:`weapon_writes` raises to 1 if it is 0.
WEAPON_READ = (WEAPON_OWNED + TEST_WEAPON, 1)


def parse_state(blocks: Sequence[bytes]) -> GameState:
    """Return the game's state from the bytes of :data:`STATE_READS`, one block per range."""
    (game, menu, pointer, area_room, cam_x, cam_y, soma_x, soma_y, vitals, on, blue, yellow,
     defeated) = blocks
    hp, mp, max_hp, max_mp = struct.unpack("<hhHH", vitals)
    camera = (struct.unpack("<H", cam_x)[0], struct.unpack("<H", cam_y)[0])
    screen = (struct.unpack("<h", soma_x)[0], struct.unpack("<h", soma_y)[0])
    used = {token for token, bit in ABILITY_SOULS.items() if on[0] & (1 << bit)}
    used |= {token for token, index in BLUE_SOULS.items() if blue[0] == index + 1}
    used |= {token for token, index in YELLOW_SOULS.items() if yellow[0] == index + 1}
    return GameState(game[0] == 4, menu[0], struct.unpack("<I", pointer)[0],
                     f"{area_room[0]:X}{area_room[1]:02X}", camera,
                     (camera[0] + screen[0], camera[1] + screen[1]), (hp, max_hp),
                     (mp, max_mp), frozenset(used), bytes(defeated))


def loadout_writes(blocks: Sequence[bytes], selected: Iterable[str]) -> list[tuple[int, bytes]]:
    """Return the writes that own every movement soul and use only the ``selected`` tokens.

    ``blocks`` are the bytes of :data:`LOADOUT_READS`, which hold the souls owned now. An ability
    soul is used by being switched on, and a guardian or enchant soul by being equipped. One
    guardian and one enchant can be equipped at a time, so when several are selected the first
    of each is equipped.
    """
    selected = set(selected)
    blue, yellow, ability = (bytearray(block) for block in blocks)
    for data, indexes in ((blue, BLUE_SOULS.values()), (yellow, YELLOW_SOULS.values()),
                          (ability, ABILITY_SOULS.values())):
        for index in indexes:
            shift = 4 * (index % 2)
            data[index // 2] = (data[index // 2] & ~(0xF << shift)) | (1 << shift)
    enabled = sum(1 << bit for token, bit in ABILITY_SOULS.items() if token in selected)
    guardian = next((BLUE_SOULS[t] + 1 for t in BLUE_SOULS if t in selected), 0)
    enchant = next((YELLOW_SOULS[t] + 1 for t in YELLOW_SOULS if t in selected), 0)
    return [(BLUE_OWNED, bytes(blue)), (YELLOW_OWNED, bytes(yellow)),
            (ABILITY_OWNED, bytes(ability)), (ABILITY_ON, bytes([enabled])),
            (EQUIPPED_BLUE, bytes([guardian])), (EQUIPPED_YELLOW, bytes([enchant]))]


def weapon_writes(owned: bytes) -> list[tuple[int, bytes]]:
    """Return the writes that own :data:`TEST_WEAPON` and equip it.

    ``owned`` is the byte of :data:`WEAPON_READ`. The game notices the change on the next swing
    and loads the weapon's graphics then.
    """
    return [(WEAPON_OWNED + TEST_WEAPON, bytes([max(owned[0], 1)])),
            (EQUIPPED_WEAPON, bytes([TEST_WEAPON]))]


def teleport_writes(room_address: int, camera_x: int, camera_y: int, soma_x: int,
                    soma_y: int) -> list[tuple[int, bytes]]:
    """Return the writes that start the game's warp to a room.

    ``camera_y`` is TeleportPlayer's value: the screen's real top row minus 48. The writes also
    clear the camera lock, in case an event had raised it, and the new room raises it again if
    it needs to. The warp is safe to start only in normal play.
    """
    block = struct.pack("<IHHhh", room_address, camera_x, camera_y, soma_x, soma_y)
    return [(TELEPORT, block), (CAMERA_LOCK, b"\x00"), (MENU_SUBSTATE, b"\x00"),
            (MENU_STATE, b"\x08")]


class BridgeError(Exception):
    """The bridge is unreachable, or a command failed inside EmuHawk."""


class Bridge:
    """The annotator's connection to ``bridge.lua`` inside EmuHawk."""

    def __init__(self) -> None:
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.next_id = 0
        self.pending: dict[int, asyncio.Future] = {}
        self.listener: Optional[asyncio.Task] = None
        self.on_close: Optional[Callable[[], None]] = None
        # Called with each message the bridge sends unasked, such as a hotkey.
        self.on_event: Optional[Callable[[dict], None]] = None

    @property
    def connected(self) -> bool:
        return self.writer is not None and not self.writer.is_closing()

    async def connect(self, timeout: float = 30.0) -> None:
        """Connect to the bridge, retrying until it answers or ``timeout`` seconds pass."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            try:
                self.reader, self.writer = await asyncio.open_connection("127.0.0.1", BRIDGE_PORT)
                break
            except OSError:
                if loop.time() > deadline:
                    raise BridgeError("EmuHawk's bridge didn't answer on port 43090.") from None
                await asyncio.sleep(0.5)
        self.listener = asyncio.create_task(self._listen())

    async def _listen(self) -> None:
        try:
            while True:
                line = await self.reader.readline()
                if not line:
                    break
                message = json.loads(line)
                if "event" in message:
                    if self.on_event:
                        self.on_event(message)
                    continue
                future = self.pending.pop(message.get("id"), None)
                if future is not None and not future.done():
                    future.set_result(message)
        except (ConnectionError, OSError, json.JSONDecodeError):
            pass
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(BridgeError("EmuHawk closed the connection."))
            self.pending.clear()
            if self.writer is not None:
                self.writer.close()
            self.writer = None
            if self.on_close:
                self.on_close()

    async def call(self, cmd: str, **args) -> dict:
        if not self.connected:
            raise BridgeError("EmuHawk isn't connected.")
        self.next_id += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[self.next_id] = future
        self.writer.write((json.dumps({"id": self.next_id, "cmd": cmd, **args}) + "\n").encode())
        await self.writer.drain()
        reply = await asyncio.wait_for(future, 10)
        if "error" in reply:
            raise BridgeError(reply["error"])
        return reply

    async def read(self, *reads: tuple[int, int]) -> list[bytes]:
        reply = await self.call("read", reads=[list(r) for r in reads])
        return [bytes(data) for data in reply["data"]]

    async def write(self, *writes: tuple[int, bytes]) -> None:
        """Write every ``(address, bytes)`` pair within one frame."""
        await self.call("write", writes=[[address, list(data)] for address, data in writes])

    async def set_flags(self, *flags: tuple[int, int, bool]) -> None:
        """Set or clear flags, each given as (address of its flag block, flag number, on).

        Flag ``n`` of a block is bit ``n & 7`` of the byte ``n >> 3`` bytes into it.
        """
        addresses = sorted({address + (number >> 3) for address, number, _ in flags})
        if not addresses:
            return
        blocks = await self.read(*((address, 1) for address in addresses))
        values = {address: block[0] for address, block in zip(addresses, blocks)}
        for address, number, on in flags:
            mask = 1 << (number & 7)
            byte = address + (number >> 3)
            values[byte] = values[byte] | mask if on else values[byte] & ~mask
        await self.write(*((address, bytes([value])) for address, value in values.items()))

    async def state(self) -> GameState:
        return parse_state(await self.read(*STATE_READS))

    async def enemies(self, type_id: int) -> list[tuple[int, int, int]]:
        """Return ``(slot, room entry id, HP)`` for each live entity of enemy type ``type_id``.

        A defeated enemy stays in the table for a moment with HP at or below 0.
        """
        reply = await self.call("entities", type=type_id)
        return [(slot, entry_id, hp) for slot, _, entry_id, hp, *_ in reply["entities"]]

    async def set_loadout(self, selected: set[str]) -> None:
        """Own every movement soul, but switch on or equip only the ``selected`` tokens.

        One guardian and one enchant can be equipped at a time, so when several are selected
        the first of each is equipped, and the tester swaps in the menu.
        """
        await self.write(*loadout_writes(await self.read(*LOADOUT_READS), selected))

    async def keep_vitals_full(self) -> None:
        """Freeze HP and MP at their maximums, so the tester can't die or run out of MP."""
        _, _, max_hp, max_mp = struct.unpack("<hhHH", (await self.read((VITALS, 8)))[0])
        await self.call("freeze", key="hp", addr=VITALS, bytes=list(struct.pack("<h", max_hp)))
        await self.call("freeze", key="mp", addr=VITALS + 2, bytes=list(struct.pack("<h", max_mp)))

    async def keep_strength_up(self) -> None:
        """Freeze STR at :data:`TEST_STRENGTH`, so that one hit defeats an enemy."""
        await self.call("freeze", key="str", addr=EQUIPMENT_STR,
                        bytes=list(struct.pack("<h", TEST_STRENGTH)))

    async def equip_test_weapon(self) -> None:
        """Own :data:`TEST_WEAPON` and equip it. The tester can still change weapons in the menu.

        The game notices the change on the next swing and loads the weapon's graphics then.
        """
        await self.write(*weapon_writes((await self.read(WEAPON_READ))[0]))

    async def teleport(self, room_address: int, camera_x: int, camera_y: int, soma_x: int,
                       soma_y: int, timeout: float = 10.0) -> GameState:
        """Move Soma to another room through the game's warp, and wait until he is there.

        ``camera_y`` is TeleportPlayer's value: the screen's real top row minus 48. The warp also
        clears the camera lock, in case a test was started in the middle of an event that had
        raised it. The new room raises it again if it needs to.
        """
        state = await self.state()
        if not state.playing:
            raise BridgeError("The game isn't in normal play, so it can't teleport now.")
        await self.write(*teleport_writes(room_address, camera_x, camera_y, soma_x, soma_y))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        await asyncio.sleep(0.2)
        while loop.time() < deadline:
            state = await self.state()
            if state.playing and state.room_pointer == room_address:
                return state
            await asyncio.sleep(0.1)
        raise BridgeError("Soma didn't arrive in the room.")

    async def wait_until_playing(self, timeout: float = 60.0) -> GameState:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            state = await self.state()
            if state.playing and state.hp[1] > 0:
                return state
            await asyncio.sleep(0.5)
        raise BridgeError("The game didn't reach normal play.")
