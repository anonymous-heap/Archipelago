"""
The game as the search drives it: a :class:`Session` over an :class:`~.emulator.Emulator`.

The emulator knows nothing about the game, and a session knows what the game's memory means. The
addresses and the byte layouts come from the annotator (``tools/annotator/emulator.py``), so a
person testing by hand and the search read and write the same memory in the same way.

A session only ever calls the emulator's own operations. So everything it does can be recorded
and replayed on another emulator (see ``traces.py``).
"""
from __future__ import annotations

import struct
from typing import Iterable, NamedTuple, Optional

from ..annotator import emulator as annotator
from ..annotator.emulator import GameState
from .emulator import Emulator, read_many, write_many

# The entity table. Slot 0 is Soma. A slot is live when the u32 at its start, which is its update
# function, is not 0.
ENTITY_BASE, ENTITY_SIZE, ENTITY_COUNT = 0x020004E4, 0x84, 0xE0
ENTITY_TABLE = (ENTITY_BASE, ENTITY_SIZE * ENTITY_COUNT)
# The slots of the entities that a room's entries spawn, which are its enemies and its special
# objects. Soma's weapons and souls are in lower slots, and particles and projectiles in higher
# ones.
ROOM_SLOTS = range(0x1B, 0x49)
# The collected-pickup flags, in which a pickup's ``flag_offset`` is its bit number.
PICKUP_FLAGS = (0x02000360, 20)


class GameError(Exception):
    """The game didn't reach the state a session asked for within the frames allowed."""


class Entity(NamedTuple):
    """One live slot of the game's entity table.

    The fields are read the same way from every slot, but only an enemy is sure to hold them. A
    special object holds its subtype where an enemy holds its type, and an entity that sets no
    type reads as type 0, which is also the Bat's.
    """

    slot: int
    type_id: int   # the enemy type, when the entity is an enemy or a part of one
    # The id of the room entry that spawned it (see annotator's room_entity_id). It is 0 for an
    # entity that another entity made, such as a generated enemy, a part or a projectile.
    entry_id: int
    hp: int
    x: int         # in room pixels
    y: int
    # The slot of the other entity that this one records as its parent, or None if it records
    # none. The heads of a Bone Pillar and the Ghosts of a Ghost generator record one.
    parent: Optional[int] = None


def parse_entities(table: bytes, camera: tuple[int, int]) -> list[Entity]:
    """Return the live entities in the bytes of :data:`ENTITY_TABLE`.

    The table holds each entity's position on the screen, and ``camera`` is the room pixel at the
    screen's top-left corner, which turns that into a position in the room.
    """
    found = []
    for slot in range(len(table) // ENTITY_SIZE):
        record = table[slot * ENTITY_SIZE:(slot + 1) * ENTITY_SIZE]
        if record[:4] == bytes(4):
            continue
        hp, type_id, entry_id = struct.unpack_from("<hBB", record, 0x34)
        screen_x, screen_y = struct.unpack_from("<h2xh", record, 0x42)
        # An entity records its parent as the address of the parent's slot. Other entities keep
        # other values in the same field, which aren't the address of a slot, or keep the
        # address of their own slot there.
        index, rest = divmod(struct.unpack_from("<I", record, 0x14)[0] - ENTITY_BASE, ENTITY_SIZE)
        parent = index if rest == 0 and 0 <= index < ENTITY_COUNT and index != slot else None
        found.append(Entity(slot, type_id, entry_id, hp, camera[0] + screen_x,
                            camera[1] + screen_y, parent))
    return found


class Session:
    """One game running on an emulator, with the operations the search needs.

    ``emulator`` is used for everything, and nothing else touches the game. ``rom`` is the whole
    US ROM, which :meth:`enter` needs for the rooms' collision. The annotator's copy of the ROM is
    read when it is None.

    ``kept`` maps addresses to bytes that :meth:`step` writes before it runs frames. It is how a
    session holds HP, MP and STR up, in place of the per-frame freezes that the annotator's bridge
    has. :meth:`boot` empties it.
    """

    def __init__(self, emulator: Emulator, rom: Optional[bytes] = None) -> None:
        self.emulator = emulator
        self.rom = rom
        self.kept: dict[int, bytes] = {}

    # --- Reading ----------------------------------------------------------------------------------

    def state(self) -> GameState:
        """Return the game's state now."""
        return annotator.parse_state(read_many(self.emulator, annotator.STATE_READS))

    def entities(self, type_id: Optional[int] = None) -> list[Entity]:
        """Return the live entities, or only those of enemy type ``type_id``.

        A defeated enemy has HP below 0, and it stays in the table for a short time before the
        game removes it (see ``success.dying``).
        """
        table, camera_x, camera_y = read_many(
            self.emulator, (ENTITY_TABLE, (annotator.CAMERA[0], 2), (annotator.CAMERA[1], 2)))
        camera = (struct.unpack("<H", camera_x)[0], struct.unpack("<H", camera_y)[0])
        return [entity for entity in parse_entities(table, camera)
                if type_id is None or entity.type_id == type_id]

    def pickup_flags(self) -> bytes:
        """Return the collected-pickup flags (see :data:`PICKUP_FLAGS`)."""
        return self.emulator.read(*PICKUP_FLAGS)

    # --- Running ----------------------------------------------------------------------------------

    def step(self, buttons: Iterable[str] = (), frames: int = 1) -> None:
        """Write the kept values, then run ``frames`` frames with ``buttons`` held down.

        The kept values are written once for the call and not once for each frame. The game can
        change them during the call, and they are written again at the start of the next one.
        """
        if frames <= 0:
            return
        if self.kept:
            write_many(self.emulator, self.kept.items())
        self.emulator.step(buttons, frames)

    def boot(self, limit: int = 600) -> int:
        """Reset the console and run until the game is in normal play. Return the frames it took.

        The annotator's test ROM starts in room 000 with a level 1 Soma and no menus, so no
        buttons are pressed. Raise :class:`GameError` if play hasn't started after ``limit``
        frames.
        """
        self.kept.clear()
        self.emulator.reset()
        for frames in range(limit + 1):
            state = self.state()
            if state.playing and state.hp[1] > 0:
                return frames
            if frames < limit:
                self.emulator.step((), 1)
        raise GameError(f"The game didn't reach normal play in {limit} frames.")

    # --- Setting up -------------------------------------------------------------------------------

    def set_flag(self, base: int, number: int, on: bool) -> None:
        """Set or clear bit ``number`` of the flags that start at address ``base``.

        Bits are numbered from the lowest bit of the first byte, as the game numbers its
        collected-pickup, defeated-enemy and boss flags.
        """
        address, mask = base + number // 8, 1 << (number % 8)
        value = self.emulator.read(address, 1)[0]
        self.emulator.write(address, bytes([value | mask if on else value & ~mask]))

    def set_loadout(self, tokens: Iterable[str]) -> None:
        """Own every movement soul, but switch on or equip only ``tokens``.

        One guardian and one enchant can be equipped at a time, so when ``tokens`` has several
        of either, the first in the annotator's order is equipped.
        """
        owned = read_many(self.emulator, annotator.LOADOUT_READS)
        write_many(self.emulator, annotator.loadout_writes(owned, tokens))

    def equip_test_weapon(self) -> None:
        """Own the annotator's test weapon and equip it."""
        write_many(self.emulator,
                   annotator.weapon_writes(self.emulator.read(*annotator.WEAPON_READ)))

    def keep_vitals_full(self) -> None:
        """Keep HP and MP at the maximums they have now."""
        _, _, max_hp, max_mp = struct.unpack("<hhHH", self.emulator.read(annotator.VITALS, 8))
        self.kept[annotator.VITALS] = struct.pack("<hh", max_hp, max_mp)

    def keep_strength_up(self) -> None:
        """Keep STR at the annotator's test strength, so that one hit defeats an enemy."""
        self.kept[annotator.EQUIPMENT_STR] = struct.pack("<h", annotator.TEST_STRENGTH)

    # --- Moving between rooms ---------------------------------------------------------------------

    def teleport(self, room_address: int, camera_x: int, camera_y: int, soma_x: int, soma_y: int,
                 limit: int = 600) -> int:
        """Move Soma to a room through the game's warp, and return the frames the warp took.

        The arguments are TeleportPlayer's, as ``landing.teleport_args`` returns them. The call
        returns on the first frame on which the game is in normal play in the new room. Raise
        :class:`GameError` if the game isn't in normal play to begin with, or if Soma hasn't
        arrived after ``limit`` frames.
        """
        if not self.state().playing:
            raise GameError("The game isn't in normal play, so it can't teleport now.")
        write_many(self.emulator, annotator.teleport_writes(room_address, camera_x, camera_y,
                                                            soma_x, soma_y))
        for frames in range(1, limit + 1):
            self.step()
            state = self.state()
            if state.playing and state.room_pointer == room_address:
                return frames
        raise GameError(f"Soma didn't arrive in room {room_address:#010x} in {limit} frames.")

    def enter(self, door: str, walls_gone: bool = False) -> dict:
        """Put Soma just inside ``door``, and return the landing that ``teleport_args`` gave.

        ``door`` is a door's unique identifier, such as ``"009:008"``. Soma arrives standing
        still at the landing, or in the air above it where the landing has no floor.

        The flags that follow from coming in by that door are set first (see
        ``landing.door_flags``). So a boss door or a wall that Soma has passed to get there
        is open, and the room's other walls are whole. With ``walls_gone``, every wall of the
        room that a flag removes is gone instead.
        """
        # Imported here because reading collision needs Pillow, which nothing else here does.
        from ..annotator import landing

        args = landing.teleport_args(door, self.rom)
        for address, number, on in landing.door_flags(door, self.rom, walls_gone):
            self.set_flag(address, number, on)
        self.teleport(args["room_address"], args["camera_x"], args["camera_y"], args["soma_x"],
                      args["soma_y"])
        return args
