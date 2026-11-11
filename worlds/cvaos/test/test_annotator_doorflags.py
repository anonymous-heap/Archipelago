"""
The game flags that follow from coming in by a door, and the pieces they are built from.

These are the newer parts of the annotator's ``landing.py`` (``Collision.catch_below``, the
``line`` of ``teleport_args``, ``_doors_by_room``, ``_only_through``, ``_in_doorway`` and
``door_flags``) and of its ``emulator.py`` (``room_entries``, ``room_entry``, ``room_flags`` and
``Bridge.set_flags``). The tests of single functions build tiny ROMs or door graphs of their own.
The door tests read the USA ROM, and skip when it is not found. The boss doors, the walls and the
reach of Soma's feet that they check were measured in the game.

    python -m pytest worlds/cvaos/test/test_annotator_doorflags.py -v
"""
from __future__ import annotations

import asyncio
import collections
import struct
import unittest
from typing import NamedTuple
from unittest import mock

from ..data import entrance_info, room_info, routing_info
from ..data import enemy_info
from ..enemy_checks import SPAWNS_AFTER_BOSS_IN
from ..tools.annotator import emulator as annotator
from ..tools.annotator import landing
from ..tools.annotator.emulator import (AFTER_BOSS_ENEMY, AFTER_EVENT_ENEMY, BOSS_FLAGS,
                                        ENEMY_ENTRY, EVENT_FLAGS, EVENT_SCENE, EVENT_SEEN,
                                        MISC_FLAGS, OBJECT_ENTRY, WALLS, RoomEntry)
from ..tools.autoroute import success
from ..tools.autoroute.emulator import FakeEmulator
from ..tools.autoroute.game import Session
from . import find_rom

BASE = 0x08000000
SOLID, PLATFORM, SLOPE = 0x03, 0x01, 0x40   # collision bytes of each class, shape 0 or 4


def read_rom() -> bytes:
    path = find_rom()
    if path is None:
        raise unittest.SkipTest("USA ROM not available")
    with open(path, "rb") as handle:
        return handle.read()


def collision_rom(cells: dict[tuple[int, int], int]) -> bytes:
    """Return a ROM whose room at BASE is one screen in size, with these collision cells.

    ``cells`` maps (column, row) of an 8x8-pixel cell to its collision byte, and every other cell
    is empty. The collision tileset is stored as LZ77 data made of literal bytes only.
    """
    tiles, blocks = [], []
    for tile_y in range(8):
        for tile_x in range(8):
            block = bytes(cells.get((tile_x * 4 + sx, tile_y * 4 + sy), 0)
                          for sy in range(4) for sx in range(4))
            if not any(block):
                tiles.append(0)
                continue
            if block not in blocks:
                blocks.append(block)
            tiles.append(blocks.index(block) + 1)
    # The game drops the first 4 bytes of the decompressed tileset.
    data = bytes(4) + b"".join(blocks)
    packed = bytearray(struct.pack("<I", 0x10 | (len(data) << 8)))
    for start in range(0, len(data), 8):
        packed += b"\x00" + data[start:start + 8]
    rom = bytearray(0x1000)
    struct.pack_into("<I", rom, 0x08, BASE + 0x40)    # the room's layer list
    struct.pack_into("<I", rom, 0x48, BASE + 0x80)    # the first layer's metadata
    struct.pack_into("<BBHIII", rom, 0x80, 1, 1, 0, 0, BASE + 0x200, BASE + 0x100)
    struct.pack_into("<64H", rom, 0x100, *tiles)
    rom[0x200:0x200 + len(packed)] = packed
    return bytes(rom)


def run(cells: dict[tuple[int, int], int]) -> landing.Collision:
    return landing.Collision(collision_rom(cells), BASE)


def entries_rom(entries: list[tuple[int, ...]], listed: bool = True) -> tuple[bytes, int]:
    """Return a ROM with one room whose entity list holds ``entries``, and the room's address.

    Each entry is (x, y, entry id, kind, subtype, var_a, var_b). The list ends at an entry whose
    x is 0x7FFF. With ``listed`` false the room's list pointer is 0.
    """
    rom = bytearray(0x400)
    struct.pack_into("<I", rom, 0x10 + 0x14, BASE + 0x100 if listed else 0)
    for index, (x, y, entry_id, kind, subtype, var_a, var_b) in enumerate(entries):
        struct.pack_into("<hhBBBBHH", rom, 0x100 + 12 * index, x, y, entry_id, kind, subtype, 0,
                         var_a, var_b)
    struct.pack_into("<h", rom, 0x100 + 12 * len(entries), 0x7FFF)
    return bytes(rom), BASE + 0x10


# --- Collision.catch_below ------------------------------------------------------------------------

class TestCatchBelow(unittest.TestCase):
    """Soma falling in one column of a hand-built room one screen in size.

    The room's playable box runs from y 48 to y 208, so a floor top must lie in that range.
    """

    @classmethod
    def setUpClass(cls):
        cells = {}
        cells.update({(column, 16): SOLID for column in range(8, 12)})      # x 64-95, top y 128
        cells.update({(column, 20): PLATFORM for column in range(20, 22)})  # x 160-175, top 160
        cells[(26, 12)] = SLOPE                                             # x 208-215, y 96-103
        cells.update({(column, row): SOLID for column in (4, 5) for row in (14, 15)})  # x 32-47
        cells.update({(column, 27): SOLID for column in (14, 15)})          # in the bottom margin
        cells.update({(column, 4): SOLID for column in (14, 15)})           # in the top margin
        cls.room = run(cells)

    def test_the_hand_built_room_reads_back_as_built(self):
        room = self.room
        self.assertEqual((room.width, room.height, room.box), (1, 1, landing.Box(0, 48, 240, 208)))
        self.assertEqual((room.kind(8, 16), room.kind(20, 20), room.kind(26, 12), room.kind(0, 0)),
                         (landing.SOLID, landing.PLATFORM, landing.SLOPE, landing.EMPTY))

    def test_a_floor_in_his_own_column_stops_him(self):
        for x in (64, 80, 95):
            with self.subTest(x=x):
                self.assertEqual(self.room.catch_below(x, 64, 208), 128)

    def test_he_stands_5_px_past_either_end_of_a_solid_floor_and_falls_at_6(self):
        self.assertEqual(landing.FOOT_REACH, 5)
        self.assertEqual(self.room.catch_below(95 + 5, 64, 208), 128)
        self.assertIsNone(self.room.catch_below(95 + 6, 64, 208))
        self.assertEqual(self.room.catch_below(64 - 5, 64, 208), 128)
        self.assertIsNone(self.room.catch_below(64 - 6, 64, 208))

    def test_a_platform_catches_him_the_same_way(self):
        self.assertEqual(self.room.catch_below(175 + 5, 64, 208), 160)
        self.assertIsNone(self.room.catch_below(175 + 6, 64, 208))
        self.assertEqual(self.room.catch_below(160 - 5, 64, 208), 160)
        self.assertIsNone(self.room.catch_below(160 - 6, 64, 208))

    def test_floor_below_counts_only_his_own_column(self):
        self.assertIsNone(self.room.floor_below(100, 64, 208))
        self.assertEqual(self.room.floor_below(80, 64, 208), 128)

    def test_a_slope_stops_him_only_in_his_own_column(self):
        self.assertEqual(self.room.catch_below(208, 64, 208), 96)
        self.assertEqual(self.room.catch_below(212, 64, 208), self.room.floor_below(212, 64, 208))
        for x in (216, 217, 220):
            with self.subTest(x=x):
                self.assertIsNone(self.room.catch_below(x, 64, 208))

    def test_only_the_top_of_a_floor_beside_him_counts(self):
        # The block at x 32 to 47 runs from y 112 to y 127, so only y 112 is a top.
        self.assertEqual(self.room.catch_below(52, 100, 208), 112)
        self.assertIsNone(self.room.catch_below(52, 113, 208))

    def test_floors_in_the_top_and_bottom_margins_never_stop_him(self):
        for x in (120, 125, 130):
            with self.subTest(x=x):
                self.assertIsNone(self.room.catch_below(x, 0, 256))

    def test_the_scan_includes_start_and_leaves_out_stop(self):
        self.assertIsNone(self.room.catch_below(80, 64, 128))
        self.assertEqual(self.room.catch_below(80, 128, 129), 128)
        self.assertIsNone(self.room.catch_below(100, 129, 208))


# --- _in_doorway, _only_through and _doors_by_room ------------------------------------------------

class TestInDoorway(unittest.TestCase):
    SIDE = (0, 48, 0, 256)          # a left exit line
    BOTTOM = (256, 976, 512, 976)   # a bottom exit line

    def test_a_pixel_is_in_the_doorway_up_to_24_px_from_a_side_line(self):
        self.assertEqual(landing.DOORWAY_DEPTH, 24)
        self.assertTrue(landing._in_doorway(self.SIDE, 24, 100))
        self.assertFalse(landing._in_doorway(self.SIDE, 25, 100))
        self.assertTrue(landing._in_doorway(self.SIDE, -24, 100))

    def test_a_side_line_includes_its_first_end_and_leaves_out_its_last(self):
        self.assertTrue(landing._in_doorway(self.SIDE, 8, 48))
        self.assertTrue(landing._in_doorway(self.SIDE, 8, 255))
        self.assertFalse(landing._in_doorway(self.SIDE, 8, 256))
        self.assertFalse(landing._in_doorway(self.SIDE, 8, 47))

    def test_a_top_or_bottom_line_works_the_same_way_along_x(self):
        self.assertTrue(landing._in_doorway(self.BOTTOM, 300, 976 - 24))
        self.assertFalse(landing._in_doorway(self.BOTTOM, 300, 976 - 25))
        self.assertTrue(landing._in_doorway(self.BOTTOM, 256, 976))
        self.assertFalse(landing._in_doorway(self.BOTTOM, 512, 976))

    def test_no_line_has_no_doorway(self):
        self.assertFalse(landing._in_doorway(None, 0, 0))


class Door(NamedTuple):
    door_identifier_unique: str
    room_address: int
    dest_room_address: int


def door_graph(*pairs: tuple[int, int]) -> tuple[dict[int, list[Door]], dict[str, Door]]:
    """Return doors between numbered rooms, grouped by room, and the same doors by identifier.

    A pair named twice gets the tags " (upper)" and " (lower)", as entrance_info names them.
    """
    counts = collections.Counter(pairs)
    seen: collections.Counter = collections.Counter()
    grouped: dict[int, list[Door]] = {}
    named = {}
    for room, dest in pairs:
        name = f"{room}:{dest}"
        if counts[room, dest] > 1:
            name += (" (upper)", " (lower)")[seen[room, dest]]
            seen[room, dest] += 1
        door = Door(name, room, dest)
        grouped.setdefault(room, []).append(door)
        named[name] = door
    return grouped, named


class TestOnlyThrough(unittest.TestCase):
    def only_through(self, door: str, *pairs: tuple[int, int]) -> bool:
        grouped, named = door_graph(*pairs)
        with mock.patch.object(landing, "_doors_by_room", return_value=grouped):
            return landing._only_through(named[door])

    CHAIN = ((1, 2), (2, 1), (2, 3), (3, 2))

    def test_a_dead_end_is_reached_only_through_its_one_door(self):
        self.assertTrue(self.only_through("2:3", *self.CHAIN))
        self.assertTrue(self.only_through("2:1", *self.CHAIN))

    def test_a_room_on_a_loop_is_reached_another_way(self):
        loop = self.CHAIN + ((3, 4), (4, 3), (4, 1), (1, 4))
        self.assertFalse(self.only_through("2:3", *loop))

    def test_a_second_door_to_the_same_room_is_another_way(self):
        self.assertFalse(self.only_through("2:3 (upper)", (1, 2), (2, 1), (2, 3), (2, 3), (3, 2)))

    def test_a_one_way_door_from_elsewhere_is_another_way(self):
        self.assertFalse(self.only_through("2:3", *self.CHAIN, (1, 5), (5, 1), (5, 3)))

    def test_doors_out_of_the_room_that_the_door_leads_to_do_not_count(self):
        self.assertTrue(self.only_through("2:3", *self.CHAIN, (3, 6), (6, 3)))

    def test_the_castle_has_one_way_into_room_301_and_two_into_room_108(self):
        doors = entrance_info.by_door_identifier_unique
        self.assertTrue(landing._only_through(doors["305:301"]))
        self.assertFalse(landing._only_through(doors["109:108"]))


class TestDoorsByRoom(unittest.TestCase):
    def test_every_door_is_listed_once_under_its_own_room_in_entrance_info_order(self):
        grouped = landing._doors_by_room()
        self.assertEqual(sum(len(doors) for doors in grouped.values()), len(entrance_info.rows))
        for address, doors in grouped.items():
            with self.subTest(room=hex(address)):
                self.assertEqual(doors, [row for row in entrance_info.rows
                                         if row.room_address == address])


# --- room_entries, room_entry and room_flags ------------------------------------------------------

class TestRoomEntries(unittest.TestCase):
    def test_the_entries_are_read_in_order_up_to_the_end_marker(self):
        rom, room = entries_rom([(16, 152, 0, OBJECT_ENTRY, 8, 15, 0),
                                 (-8, 300, 4, ENEMY_ENTRY, 3, 0, 0)])
        self.assertEqual(annotator.room_entries(rom, room),
                         [RoomEntry(16, 152, 0, OBJECT_ENTRY, 8, 15, 0),
                          RoomEntry(-8, 300, 4, ENEMY_ENTRY, 3, 0, 0)])

    def test_a_room_without_a_list_has_no_entries(self):
        rom, room = entries_rom([(16, 152, 0, OBJECT_ENTRY, 8, 15, 0)], listed=False)
        self.assertEqual(annotator.room_entries(rom, room), [])

    def test_an_empty_list_has_no_entries(self):
        rom, room = entries_rom([])
        self.assertEqual(annotator.room_entries(rom, room), [])


class TestRoomEntry(unittest.TestCase):
    ENTRIES = [(0, 0, 0, OBJECT_ENTRY, 7, 1, 28),
               (192, 696, 11, ENEMY_ENTRY, 3, 0, 0),
               (192, 696, 2, OBJECT_ENTRY, 8, 3, 33),       # a wall whose var_a happens to be 3
               (192, 696, 5, ENEMY_ENTRY, 52, 0, 0),        # another type at the same spot
               (192, 696, 12, ENEMY_ENTRY, 3, 0, 0),
               (256, 160, 4, OBJECT_ENTRY, AFTER_BOSS_ENEMY, 77, 5),
               (300, 160, 6, OBJECT_ENTRY, AFTER_EVENT_ENEMY, 77, 0x2B)]

    @classmethod
    def setUpClass(cls):
        cls.rom, cls.room = entries_rom(cls.ENTRIES)

    def entry_id(self, *args, **kwargs):
        entry = annotator.room_entry(self.rom, self.room, *args, **kwargs)
        return entry.entry_id if entry else None

    def test_an_enemy_entry_of_the_type_at_the_spot_is_found(self):
        self.assertEqual(self.entry_id(3, 192, 696), 11)
        self.assertEqual(self.entry_id(52, 192, 696), 5)

    def test_index_chooses_among_entries_at_one_spot_in_rom_order(self):
        self.assertEqual([self.entry_id(3, 192, 696, index=index) for index in range(3)],
                         [11, 12, None])

    def test_an_object_that_spawns_the_type_after_a_boss_or_an_event_is_found(self):
        self.assertEqual(self.entry_id(77, 256, 160), 4)
        self.assertEqual(self.entry_id(77, 300, 160), 6)

    def test_nothing_is_found_at_another_spot_or_for_another_type(self):
        self.assertIsNone(self.entry_id(3, 192, 697))
        self.assertIsNone(self.entry_id(77, 192, 696))
        self.assertIsNone(self.entry_id(8, 192, 696))

    def test_room_entity_id_is_the_id_of_the_first_entry(self):
        self.assertEqual(annotator.room_entity_id(self.rom, self.room, 3, 192, 696), 11)
        self.assertIsNone(annotator.room_entity_id(self.rom, self.room, 3, 0, 0))


class TestRoomFlags(unittest.TestCase):
    def flags(self, *entries):
        rom, room = entries_rom(list(entries))
        return annotator.room_flags(rom, room)

    def test_the_boss_bit_comes_from_a_boss_door_inside_the_arena(self):
        for subtype in annotator.BOSS_DOORS:
            with self.subTest(subtype=subtype):
                self.assertEqual(self.flags((16, 152, 1, OBJECT_ENTRY, subtype, 0, 9),
                                            (224, 152, 2, OBJECT_ENTRY, subtype, 1, 11)),
                                 ([], 11))

    def test_a_room_with_only_an_outside_boss_door_is_not_an_arena(self):
        self.assertEqual(self.flags((16, 152, 1, OBJECT_ENTRY, 2, 0, 11)), ([], None))

    def test_a_scene_known_to_play_in_a_new_game_gives_its_seen_flag(self):
        self.assertEqual(self.flags((0, 0, 1, OBJECT_ENTRY, EVENT_SCENE, 0x16, 0)),
                         ([(EVENT_FLAGS, EVENT_SEEN[0x16])], None))
        self.assertEqual(self.flags((0, 0, 1, OBJECT_ENTRY, EVENT_SCENE, 0x30, 0)), ([], None))

    def test_an_enemy_that_waits_for_an_event_gives_the_events_flag(self):
        self.assertEqual(self.flags((64, 64, 1, OBJECT_ENTRY, AFTER_EVENT_ENEMY, 77, 0x2E)),
                         ([(EVENT_FLAGS, 0x2E)], None))

    def test_an_enemy_that_waits_for_another_rooms_boss_gives_that_bosss_bit(self):
        self.assertEqual(self.flags((64, 64, 1, OBJECT_ENTRY, AFTER_BOSS_ENEMY, 77, 5)),
                         ([(BOSS_FLAGS, 5)], None))

    def test_an_enemy_that_waits_for_the_arenas_own_boss_gives_no_flag(self):
        self.assertEqual(self.flags((224, 152, 1, OBJECT_ENTRY, 4, 1, 11),
                                    (64, 64, 2, OBJECT_ENTRY, AFTER_BOSS_ENEMY, 2, 11)),
                         ([], 11))

    def test_enemy_entries_give_no_flags(self):
        self.assertEqual(self.flags((64, 64, 1, ENEMY_ENTRY, EVENT_SCENE, 0x16, 0),
                                    (64, 64, 2, ENEMY_ENTRY, AFTER_BOSS_ENEMY, 77, 5),
                                    (64, 64, 3, ENEMY_ENTRY, 4, 1, 11)),
                         ([], None))

    def test_the_flags_are_sorted_and_each_is_listed_once(self):
        flags, _ = self.flags((0, 0, 1, OBJECT_ENTRY, AFTER_BOSS_ENEMY, 77, 5),
                              (0, 0, 2, OBJECT_ENTRY, EVENT_SCENE, 0x19, 0),
                              (0, 0, 3, OBJECT_ENTRY, AFTER_BOSS_ENEMY, 74, 5),
                              (0, 0, 4, OBJECT_ENTRY, AFTER_EVENT_ENEMY, 74, 0x20))
        self.assertEqual(flags, [(EVENT_FLAGS, 0x20), (EVENT_FLAGS, EVENT_SEEN[0x19]),
                                 (BOSS_FLAGS, 5)])


class TestRoomEntriesOnTheRom(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rom = read_rom()

    def address(self, room: str) -> int:
        return room_info.by_identifier[room].room_address

    def test_the_two_medusa_head_generators_of_room_615_are_entries_7_and_8(self):
        found = [annotator.room_entry(self.rom, self.address("615"), 57, 0, 0, index=index)
                 for index in range(3)]
        self.assertEqual([entry.entry_id if entry else None for entry in found], [7, 8, None])

    def test_the_two_mermen_at_one_spot_of_room_72d_are_entries_11_and_12(self):
        found = [annotator.room_entry(self.rom, self.address("72D"), 3, 192, 696, index=index)
                 for index in range(2)]
        self.assertEqual([entry.entry_id for entry in found], [11, 12])

    def test_room_flags_of_an_arena_and_of_a_room_that_waits_for_legion(self):
        self.assertEqual(annotator.room_flags(self.rom, self.address("00A")), ([], 11))
        self.assertEqual(annotator.room_flags(self.rom, self.address("725"))[1], 5)
        self.assertEqual(annotator.room_flags(self.rom, self.address("710")),
                         ([(BOSS_FLAGS, 5)], None))


class TestSpawnsAfterBossOnTheRom(unittest.TestCase):
    """SPAWNS_AFTER_BOSS_IN, checked against the room entries that wait for a boss."""

    @classmethod
    def setUpClass(cls):
        cls.rom = read_rom()
        cls.own_bits = {row.room_identifier: annotator.room_flags(cls.rom, row.room_address)[1]
                        for row in room_info.rows}
        cls.boss_rooms = {bit: room for room, bit in cls.own_bits.items() if bit is not None}

    def test_each_placement_that_waits_for_another_rooms_boss_is_listed_with_that_room(self):
        found = {}
        for row in room_info.rows:
            own = self.own_bits[row.room_identifier]
            for entry in annotator.room_entries(self.rom, row.room_address):
                if (entry.kind != OBJECT_ENTRY or entry.subtype != AFTER_BOSS_ENEMY
                        or entry.var_b == own):
                    continue
                for placement in routing_info.enemy_placements:
                    if (placement.room == row.room_identifier
                            and (placement.x, placement.y) == (entry.x, entry.y)
                            and enemy_info.by_name[placement.name].id == entry.var_a):
                        found[placement.enemy_number] = self.boss_rooms[entry.var_b]
        self.assertEqual(found, SPAWNS_AFTER_BOSS_IN)

    def test_preparing_the_room_of_such_a_placement_sets_its_bosss_flag(self):
        for number, boss_room in SPAWNS_AFTER_BOSS_IN.items():
            placement = next(placement for placement in routing_info.enemy_placements
                             if placement.enemy_number == number)
            with self.subTest(placement=number):
                flags = success.room(placement.room, self.rom).flags
                self.assertIn((BOSS_FLAGS, self.own_bits[boss_room]), flags)


# --- Bridge.set_flags -----------------------------------------------------------------------------

class MemoryBridge(annotator.Bridge):
    """A Bridge whose reads and writes go to a fake emulator's memory, recording each call."""

    def __init__(self) -> None:
        super().__init__()
        self.memory = FakeEmulator()
        self.calls: list[tuple] = []

    async def read(self, *reads):
        self.calls.append(("read", reads))
        return [self.memory.read(address, length) for address, length in reads]

    async def write(self, *writes):
        self.calls.append(("write", writes))
        for address, data in writes:
            self.memory.write(address, data)


class TestSetFlags(unittest.TestCase):
    def test_flags_in_one_byte_are_read_once_and_written_once_together(self):
        bridge = MemoryBridge()
        bridge.memory.write(MISC_FLAGS + 4, b"\x01")
        asyncio.run(bridge.set_flags((MISC_FLAGS, 35, True), (MISC_FLAGS, 36, True),
                                     (MISC_FLAGS, 32, False)))
        self.assertEqual(bridge.memory.read(MISC_FLAGS + 4, 1), b"\x18")
        self.assertEqual(bridge.calls, [("read", ((MISC_FLAGS + 4, 1),)),
                                        ("write", ((MISC_FLAGS + 4, b"\x18"),))])

    def test_flags_of_several_blocks_keep_the_other_bits_of_their_bytes(self):
        bridge = MemoryBridge()
        bridge.memory.write(BOSS_FLAGS, b"\xff\x00")
        asyncio.run(bridge.set_flags((BOSS_FLAGS, 11, True), (MISC_FLAGS, 35, True),
                                     (BOSS_FLAGS, 0, False)))
        self.assertEqual(bridge.memory.read(BOSS_FLAGS, 2), b"\xfe\x08")
        self.assertEqual(bridge.memory.read(MISC_FLAGS + 4, 1), b"\x08")
        self.assertEqual([kind for kind, _ in bridge.calls], ["read", "write"])

    def test_no_flags_read_and_write_nothing(self):
        bridge = MemoryBridge()
        asyncio.run(bridge.set_flags())
        self.assertEqual(bridge.calls, [])

    def test_the_bridge_numbers_flags_as_a_session_does(self):
        flags = [(MISC_FLAGS, 32, False), (MISC_FLAGS, 41, True), (BOSS_FLAGS, 7, True),
                 (EVENT_FLAGS, 0x2B, True)]
        bridge = MemoryBridge()
        fake = FakeEmulator()
        for memory in (bridge.memory, fake):
            memory.write(MISC_FLAGS, b"\xff" * 8)
        asyncio.run(bridge.set_flags(*flags))
        session = Session(fake)
        for address, number, on in flags:
            session.set_flag(address, number, on)
        self.assertEqual(bridge.memory.memory, fake.memory)


# --- door_flags and teleport_args on the ROM ------------------------------------------------------

class TestDoorFlagsOnTheRom(unittest.TestCase):
    # Each outside boss door, with its boss's bit in BOSS_FLAGS.
    BOSS_DOORS = {"009:00A": 11, "023:00A": 11, "102:106": 10, "105:106": 10, "10A:106": 10,
                  "210:20F": 15, "30F:313": 12, "406:40B": 4, "40E:40B": 4, "613:607": 1,
                  "724:725": 5, "805:801": 6, "901:904": 0, "B0D:B0B": 7}
    # Doors to a room that only they lead to, with the misc flag of the wall in their doorway.
    OPENED_WALLS = {"305:301": 35, "023:00B": 32, "604:61B": 39, "70A:709": 40, "71E:720": 41,
                    "30D:310": 36}

    @classmethod
    def setUpClass(cls):
        cls.rom = read_rom()
        cls.flags = {row.door_identifier_unique: landing.door_flags(row.door_identifier_unique,
                                                                    cls.rom)
                     for row in entrance_info.rows}

    def walls(self, room_address: int) -> list[RoomEntry]:
        return [entry for entry in annotator.room_entries(self.rom, room_address)
                if entry.kind == OBJECT_ENTRY and entry.subtype in WALLS]

    def test_the_flag_blocks_are_where_the_game_keeps_them(self):
        self.assertEqual((MISC_FLAGS, BOSS_FLAGS), (0x02000344, 0x0200037E))
        self.assertEqual(WALLS, (8, 9))

    def test_each_outside_boss_door_sets_its_bosss_bit(self):
        for door, bit in self.BOSS_DOORS.items():
            with self.subTest(door=door):
                self.assertIn((BOSS_FLAGS, bit, True), self.flags[door])

    def test_no_other_door_touches_a_boss_bit(self):
        touching = {door for door, flags in self.flags.items()
                    if any(address == BOSS_FLAGS for address, _, _ in flags)}
        self.assertEqual(touching, set(self.BOSS_DOORS))

    def test_a_wall_in_the_doorway_of_a_dead_end_is_opened_and_nothing_else_changes(self):
        for door, flag in self.OPENED_WALLS.items():
            with self.subTest(door=door):
                self.assertEqual(self.flags[door], [(MISC_FLAGS, flag, True)])

    def test_the_wall_at_109_108_stays_whole_because_room_108_has_another_way_in(self):
        self.assertEqual(self.flags["109:108"], [(MISC_FLAGS, 33, False)])
        row = entrance_info.by_door_identifier_unique["109:108"]
        line = landing.teleport_args("109:108", self.rom)["line"]
        (wall,) = self.walls(row.room_address)
        self.assertTrue(landing._in_doorway(tuple(line), wall.x, wall.y))
        self.assertFalse(landing._only_through(row))

    def test_entering_a_room_by_another_door_leaves_its_walls_whole(self):
        for door, flag in self.OPENED_WALLS.items():
            room = door.split(":")[0]
            others = [row.door_identifier_unique for row in entrance_info.rows
                      if row.room_identifier == room and row.door_identifier_unique != door]
            for other in others:
                with self.subTest(door=other):
                    self.assertIn((MISC_FLAGS, flag, False), self.flags[other])

    def test_every_numbered_wall_of_a_room_gets_a_flag_at_every_door(self):
        for row in entrance_info.rows:
            numbers = {wall.var_b for wall in self.walls(row.room_address) if wall.var_b}
            given = [number for address, number, _ in self.flags[row.door_identifier_unique]
                     if address == MISC_FLAGS]
            with self.subTest(door=row.door_identifier_unique):
                self.assertEqual(sorted(given), sorted(numbers))

    def test_a_wall_without_a_flag_number_gets_no_flag(self):
        room_50e = room_info.by_identifier["50E"].room_address
        self.assertTrue(any(wall.var_b == 0 for wall in self.walls(room_50e)))
        for door, flags in self.flags.items():
            with self.subTest(door=door):
                self.assertNotIn((MISC_FLAGS, 0), [(address, number)
                                                   for address, number, _ in flags])

    def test_walls_of_both_kinds_are_opened(self):
        kinds = set()
        for door, flags in self.flags.items():
            opened = {number for address, number, on in flags if address == MISC_FLAGS and on}
            address = entrance_info.by_door_identifier_unique[door].room_address
            kinds |= {wall.subtype for wall in self.walls(address) if wall.var_b in opened}
        self.assertEqual(kinds, set(WALLS))

    def test_27_doors_set_a_flag_and_33_more_only_clear_wall_flags(self):
        setting = [door for door, flags in self.flags.items() if any(on for _, _, on in flags)]
        clearing = [door for door, flags in self.flags.items()
                    if flags and not any(on for _, _, on in flags)]
        self.assertEqual((len(self.flags), len(setting), len(clearing)), (725, 27, 33))

    def test_the_flags_are_sorted_and_each_is_given_once(self):
        for door, flags in self.flags.items():
            with self.subTest(door=door):
                self.assertEqual(flags, sorted(flags))
                self.assertEqual(len({(address, number) for address, number, _ in flags}),
                                 len(flags))


class TestLandingLineOnTheRom(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rom = read_rom()
        cls.landings = landing.all_landings(cls.rom)

    def test_the_line_is_the_doors_exit_line_wherever_the_door_is_on_an_edge(self):
        for row in room_info.rows:
            for exit_ in landing.exits(row.room_address, self.rom):
                if exit_.line is None:
                    continue
                with self.subTest(door=exit_.door):
                    self.assertEqual(tuple(self.landings[exit_.door]["line"]), exit_.line)

    def test_the_two_doors_off_their_rooms_edges_take_the_line_of_their_reverse_door(self):
        self.assertEqual(self.landings["505:50B"]["line"], [256, 256, 256, 464])
        self.assertEqual(self.landings["901:904"]["line"], [512, 48, 512, 256])

    def test_a_line_runs_along_its_edge_for_at_most_one_screen(self):
        for door, spot in self.landings.items():
            x0, y0, x1, y1 = spot["line"]
            with self.subTest(door=door):
                if spot["edge"] in ("left", "right"):
                    self.assertEqual(x0, x1)
                    self.assertTrue(0 < y1 - y0 <= 256)
                else:
                    self.assertEqual(y0, y1)
                    self.assertTrue(0 < x1 - x0 <= 256)

    def test_the_platform_edge_doors_land_on_a_floor_beside_somas_column(self):
        for door in ("10B:108", "B05:B24", "20E:201", "804:803", "A05:A03", "90D:90C"):
            spot = self.landings[door]
            collision = landing.Collision(self.rom, spot["room_address"])
            bottom = collision.box.bottom
            own = collision.floor_below(spot["feet_x"], spot["feet_y"], bottom)
            with self.subTest(door=door):
                self.assertEqual(spot["rule"], "top")
                self.assertEqual(spot["floor_y"],
                                 collision.catch_below(spot["feet_x"], spot["feet_y"], bottom))
                self.assertTrue(own is None or own > spot["floor_y"], (own, spot))


if __name__ == "__main__":
    unittest.main()
