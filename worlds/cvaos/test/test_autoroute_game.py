"""
The game session of the automated routing search, driven on a fake emulator.

``fake_game`` builds a FakeEmulator whose frame callback plays the part of the game, as far as a
session needs: it boots into normal play and it carries out the game's warp. The entry-state and
success tests import it, together with the helpers that write a game state and entity records.

Most tests need no ROM. The ROM tests read the USA ROM, and skip when it is not found.

    python -m pytest worlds/cvaos/test/test_autoroute_game.py -v
"""
from __future__ import annotations

import struct
import unittest
from typing import Callable, Optional
from unittest import mock

from ..tools.annotator import emulator as annotator
from ..tools.annotator import landing
from ..tools.autoroute import game
from ..tools.autoroute.emulator import FakeEmulator, read_many
from ..tools.autoroute.game import Entity, GameError, Session, parse_entities
from ..tools.autoroute.traces import Recording
from . import find_rom

# The room that the fake game boots into, and another room to warp to. Any addresses work.
ROOM_START = 0x0850EF9C
ROOM_A = 0x08510000
MAX_HP, MAX_MP = 320, 120
# The fake game's own counters, at addresses that no session reads or writes.
BOOT_COUNT = 0x03007F00   # u16: the frames since the reset, until the game is in play
WARP_COUNT = 0x03007F02   # u16: the frames since the current warp started
ARRIVED = 0x03007F04      # u16: 1 plus the frames since the last warp ended, or 0 before one


def read_rom() -> bytes:
    path = find_rom()
    if path is None:
        raise unittest.SkipTest("USA ROM not available")
    with open(path, "rb") as handle:
        return handle.read()


def u16(fake: FakeEmulator, address: int) -> int:
    return struct.unpack("<H", fake.read(address, 2))[0]


def put_u16(fake: FakeEmulator, address: int, value: int) -> None:
    fake.write(address, struct.pack("<H", value))


def camera(fake: FakeEmulator) -> tuple[int, int]:
    return u16(fake, annotator.CAMERA[0]), u16(fake, annotator.CAMERA[1])


def soma_at(fake: FakeEmulator) -> tuple[int, int]:
    """Return Soma's feet in room pixels: the camera plus his position on the screen."""
    screen = struct.unpack("<h", fake.read(annotator.SOMA[0], 2))[0], struct.unpack(
        "<h", fake.read(annotator.SOMA[1], 2))[0]
    left, top = camera(fake)
    return left + screen[0], top + screen[1]


def put_soma(fake: FakeEmulator, x: int, y: int) -> None:
    """Put Soma's feet at a room pixel, by his position on the screen."""
    left, top = camera(fake)
    fake.write(annotator.SOMA[0], struct.pack("<h", x - left))
    fake.write(annotator.SOMA[1], struct.pack("<h", y - top))


def set_hp(fake: FakeEmulator, hp: int) -> None:
    fake.write(annotator.VITALS, struct.pack("<h", hp))


def in_play(fake: FakeEmulator, room: int, soma: tuple[int, int],
            view: tuple[int, int] = (0, 0)) -> None:
    """Write a game state in normal play: Soma at full HP in ``room``, with his feet at ``soma``.

    ``view`` is the camera, which is the room pixel at the screen's top-left corner.
    """
    fake.write(annotator.GAME_STATE, b"\x04")
    fake.write(annotator.MENU_STATE, b"\x01")
    fake.write(annotator.ROOM_POINTER, struct.pack("<I", room))
    put_u16(fake, annotator.CAMERA[0], view[0])
    put_u16(fake, annotator.CAMERA[1], view[1])
    put_soma(fake, *soma)
    fake.write(annotator.VITALS, struct.pack("<hhHH", MAX_HP, MAX_MP, MAX_HP, MAX_MP))


def finish_warp(fake: FakeEmulator) -> None:
    """Put Soma where TELEPORT says, in normal play, as the game's warp does."""
    room, camera_x, camera_y, soma_x, soma_y = struct.unpack(
        "<IHHhh", fake.read(annotator.TELEPORT, 12))
    fake.write(annotator.ROOM_POINTER, struct.pack("<I", room))
    # TeleportPlayer's camera y is 48 px above the screen's top row, and the game adds the 48.
    put_u16(fake, annotator.CAMERA[0], camera_x)
    put_u16(fake, annotator.CAMERA[1], camera_y + 48)
    fake.write(annotator.SOMA[0], struct.pack("<h", soma_x))
    fake.write(annotator.SOMA[1], struct.pack("<h", soma_y))
    fake.write(annotator.MENU_STATE, b"\x01")
    put_u16(fake, WARP_COUNT, 0)
    put_u16(fake, ARRIVED, 1)


def fake_game(boot_frames: int = 3, warp_frames: int = 2,
              after: Optional[Callable[[FakeEmulator, int], None]] = None) -> FakeEmulator:
    """Return a FakeEmulator that plays the part of the game, as far as a session needs.

    After a reset the game reaches normal play in room ROOM_START after ``boot_frames`` frames,
    with Soma at full HP. A write of 8 to MENU_STATE starts the warp, which ends ``warp_frames``
    frames later with Soma in normal play where TELEPORT says. ``after(fake, n)`` is then called
    on the nth frame after the warp ended, so that a test can play out what Soma does next.

    The counters are kept in memory, so a saved state holds them too.
    """
    def start(fake: FakeEmulator) -> None:
        fake.write(annotator.GAME_STATE, b"\x04")
        fake.write(annotator.MENU_STATE, b"\x01")
        fake.write(annotator.ROOM_POINTER, struct.pack("<I", ROOM_START))
        fake.write(annotator.VITALS, struct.pack("<hhHH", MAX_HP, MAX_MP, MAX_HP, MAX_MP))

    def on_reset(fake: FakeEmulator) -> None:
        if boot_frames <= 0:
            start(fake)

    def on_frame(fake: FakeEmulator, buttons: tuple[str, ...]) -> None:
        if fake.read(annotator.GAME_STATE, 1)[0] != 4:
            count = u16(fake, BOOT_COUNT) + 1
            put_u16(fake, BOOT_COUNT, count)
            if count >= boot_frames:
                start(fake)
            return
        if fake.read(annotator.MENU_STATE, 1)[0] == 8:
            count = u16(fake, WARP_COUNT) + 1
            put_u16(fake, WARP_COUNT, count)
            if count >= warp_frames:
                finish_warp(fake)
            return
        arrived = u16(fake, ARRIVED)
        if arrived:
            put_u16(fake, ARRIVED, arrived + 1)
            if after is not None:
                after(fake, arrived)

    return FakeEmulator(on_frame, on_reset)


def slot_address(slot: int) -> int:
    return game.ENTITY_BASE + slot * game.ENTITY_SIZE


def entity_record(type_id: int, entry_id: int, hp: int, screen: tuple[int, int],
                  link: int = 0, update: int = 0x0803A001) -> bytes:
    """Return the bytes of one live slot of the entity table.

    ``link`` is the raw u32 at offset 0x14, where an entity records its parent's slot address.
    ``update`` is the update function, and a slot whose update function is 0 is empty.
    """
    record = bytearray(game.ENTITY_SIZE)
    struct.pack_into("<I", record, 0, update)
    struct.pack_into("<I", record, 0x14, link)
    struct.pack_into("<hBB", record, 0x34, hp, type_id, entry_id)
    struct.pack_into("<h2xh", record, 0x42, *screen)
    return bytes(record)


def write_entities(fake: FakeEmulator, records: dict[int, bytes]) -> None:
    for slot, record in records.items():
        fake.write(slot_address(slot), record)


def table_of(records: dict[int, bytes], slots: int = game.ENTITY_COUNT) -> bytes:
    table = bytearray(game.ENTITY_SIZE * slots)
    for slot, record in records.items():
        table[slot * game.ENTITY_SIZE:(slot + 1) * game.ENTITY_SIZE] = record
    return bytes(table)


def booted(fake: Optional[FakeEmulator] = None) -> Session:
    session = Session(fake or fake_game())
    session.boot()
    return session


# --- parse_entities -------------------------------------------------------------------------------

class TestParseEntities(unittest.TestCase):
    def test_a_slot_whose_update_function_is_zero_is_left_out(self):
        table = table_of({0x1B: entity_record(3, 5, 10, (0, 0), update=0),
                          0x1C: entity_record(3, 6, 10, (0, 0))})
        self.assertEqual([entity.slot for entity in parse_entities(table, (0, 0))], [0x1C])

    def test_an_entity_reads_its_type_its_entry_and_its_signed_hp(self):
        table = table_of({0x20: entity_record(57, 8, -12, (0, 0))})
        (entity,) = parse_entities(table, (0, 0))
        self.assertEqual((entity.slot, entity.type_id, entity.entry_id, entity.hp),
                         (0x20, 57, 8, -12))

    def test_the_position_is_the_screen_position_plus_the_camera(self):
        table = table_of({0x1B: entity_record(3, 5, 10, (-20, 37))})
        (entity,) = parse_entities(table, (512, 300))
        self.assertEqual((entity.x, entity.y), (492, 337))

    def test_entities_come_back_in_slot_order(self):
        table = table_of({0x40: entity_record(1, 1, 1, (0, 0)), 0: entity_record(0, 0, 320, (0, 0)),
                          0x1B: entity_record(2, 2, 1, (0, 0))})
        self.assertEqual([entity.slot for entity in parse_entities(table, (0, 0))], [0, 0x1B, 0x40])

    def test_a_parent_is_the_slot_whose_address_the_entity_records(self):
        table = table_of({0x1B: entity_record(3, 5, 10, (0, 0)),
                          0x60: entity_record(3, 0, -1, (0, 0), link=slot_address(0x1B))})
        found = {entity.slot: entity.parent for entity in parse_entities(table, (0, 0))}
        self.assertEqual(found, {0x1B: None, 0x60: 0x1B})

    def test_the_first_and_the_last_slot_can_be_parents(self):
        last = game.ENTITY_COUNT - 1
        table = table_of({1: entity_record(3, 0, 1, (0, 0), link=slot_address(0)),
                          2: entity_record(3, 0, 1, (0, 0), link=slot_address(last))})
        self.assertEqual([entity.parent for entity in parse_entities(table, (0, 0))], [0, last])

    def test_an_entity_that_records_its_own_slot_has_no_parent(self):
        table = table_of({0x30: entity_record(3, 0, 1, (0, 0), link=slot_address(0x30))})
        self.assertIsNone(parse_entities(table, (0, 0))[0].parent)

    def test_a_value_between_two_slots_is_not_a_parent(self):
        table = table_of({0x30: entity_record(3, 0, 1, (0, 0), link=slot_address(0x1B) + 4)})
        self.assertIsNone(parse_entities(table, (0, 0))[0].parent)

    def test_an_address_outside_the_table_is_not_a_parent(self):
        for link in (0, game.ENTITY_BASE - game.ENTITY_SIZE, slot_address(game.ENTITY_COUNT),
                     0x08123456):
            with self.subTest(link=hex(link)):
                table = table_of({0x30: entity_record(3, 0, 1, (0, 0), link=link)})
                self.assertIsNone(parse_entities(table, (0, 0))[0].parent)


# --- Reading --------------------------------------------------------------------------------------

class TestSessionReading(unittest.TestCase):
    def test_state_reads_the_game_through_the_emulator(self):
        fake = FakeEmulator()
        in_play(fake, ROOM_A, (300, 180), view=(256, 48))
        state = Session(fake).state()
        self.assertTrue(state.playing)
        self.assertEqual((state.room_pointer, state.soma, state.camera, state.hp),
                         (ROOM_A, (300, 180), (256, 48), (MAX_HP, MAX_HP)))

    def test_entities_use_the_camera_and_can_be_limited_to_one_type(self):
        fake = FakeEmulator()
        in_play(fake, ROOM_A, (300, 180), view=(256, 48))
        write_entities(fake, {0x1B: entity_record(3, 5, 10, (10, 20)),
                              0x1C: entity_record(52, 6, 10, (30, 40))})
        session = Session(fake)
        self.assertEqual(session.entities(),
                         [Entity(0x1B, 3, 5, 10, 266, 68), Entity(0x1C, 52, 6, 10, 286, 88)])
        self.assertEqual([entity.slot for entity in session.entities(52)], [0x1C])
        self.assertEqual(session.entities(99), [])

    def test_the_pickup_flags_are_the_twenty_bytes_at_their_address(self):
        fake = FakeEmulator()
        fake.write(game.PICKUP_FLAGS[0], bytes(range(1, 22)))
        self.assertEqual(Session(fake).pickup_flags(), bytes(range(1, 21)))


# --- Running --------------------------------------------------------------------------------------

class TestSessionStep(unittest.TestCase):
    def test_step_writes_the_kept_values_once_and_then_runs_the_frames(self):
        recording = Recording(FakeEmulator())
        session = Session(recording)
        session.kept[annotator.VITALS] = struct.pack("<hh", 320, 120)
        session.step(["A"], 5)
        self.assertEqual(recording.ops, [["write", "0x0201327a", "40017800"], ["step", ["A"], 5]])

    def test_a_step_of_no_frames_writes_and_runs_nothing(self):
        recording = Recording(FakeEmulator())
        session = Session(recording)
        session.kept[annotator.VITALS] = b"\x01\x00"
        session.step(["A"], 0)
        session.step([], -3)
        self.assertEqual(recording.ops, [])

    def test_a_kept_value_that_the_game_lowers_is_written_again_at_the_next_step(self):
        def hurt(fake, held):
            set_hp(fake, struct.unpack("<h", fake.read(annotator.VITALS, 2))[0] - 10)

        fake = FakeEmulator(on_frame=hurt)
        session = Session(fake)
        session.kept[annotator.VITALS] = struct.pack("<h", 100)
        session.step(frames=3)
        self.assertEqual(struct.unpack("<h", fake.read(annotator.VITALS, 2))[0], 70)
        session.step()
        self.assertEqual(struct.unpack("<h", fake.read(annotator.VITALS, 2))[0], 90)

    def test_keep_vitals_full_keeps_the_maximums_that_it_reads(self):
        fake = FakeEmulator()
        fake.write(annotator.VITALS, struct.pack("<hhHH", 12, 3, 400, 150))
        session = Session(fake)
        session.keep_vitals_full()
        self.assertEqual(session.kept, {annotator.VITALS: struct.pack("<hh", 400, 150)})
        session.step()
        self.assertEqual(Session(fake).state().hp, (400, 400))
        self.assertEqual(Session(fake).state().mp, (150, 150))

    def test_keep_strength_up_keeps_the_test_strength(self):
        fake = FakeEmulator()
        session = Session(fake)
        session.keep_strength_up()
        session.step()
        self.assertEqual(struct.unpack("<h", fake.read(annotator.EQUIPMENT_STR, 2))[0],
                         annotator.TEST_STRENGTH)


class TestSessionBoot(unittest.TestCase):
    def test_boot_returns_the_frames_until_normal_play(self):
        fake = fake_game(boot_frames=5)
        session = Session(fake)
        self.assertEqual(session.boot(), 5)
        self.assertTrue(session.state().playing)
        self.assertEqual(session.state().room_pointer, ROOM_START)

    def test_a_game_that_starts_in_play_boots_in_no_frames(self):
        self.assertEqual(Session(fake_game(boot_frames=0)).boot(), 0)

    def test_boot_resets_the_console_and_forgets_the_kept_values(self):
        fake = fake_game(boot_frames=2)
        session = Session(fake)
        fake.write(annotator.MISC_FLAGS, b"\xff")
        session.kept[annotator.VITALS] = b"\x01\x00"
        session.boot()
        self.assertEqual(fake.read(annotator.MISC_FLAGS, 1), b"\x00")
        self.assertEqual(fake.frame, 2)
        self.assertEqual(session.kept, {})

    def test_boot_waits_for_soma_to_have_hp(self):
        def on_frame(fake, held):
            fake.write(annotator.GAME_STATE, b"\x04")
            fake.write(annotator.MENU_STATE, b"\x01")
            if fake.frame >= 4:
                fake.write(annotator.VITALS, struct.pack("<hhHH", 50, 0, 50, 0))

        self.assertEqual(Session(FakeEmulator(on_frame)).boot(), 4)

    def test_boot_raises_game_error_when_play_does_not_start_in_time(self):
        fake = fake_game(boot_frames=50)
        session = Session(fake)
        with self.assertRaises(GameError):
            session.boot(limit=10)
        self.assertEqual(fake.frame, 10)
        self.assertEqual(session.boot(limit=50), 50)


# --- Setting up -----------------------------------------------------------------------------------

class TestSetFlag(unittest.TestCase):
    BASE = annotator.MISC_FLAGS

    def test_flags_count_from_the_lowest_bit_of_the_first_byte(self):
        fake = FakeEmulator()
        session = Session(fake)
        for number in (0, 7, 13, 35):
            session.set_flag(self.BASE, number, True)
        self.assertEqual(fake.read(self.BASE, 5), b"\x81\x20\x00\x00\x08")

    def test_clearing_a_flag_keeps_the_other_bits_of_its_byte(self):
        fake = FakeEmulator()
        fake.write(self.BASE + 4, b"\xff")
        Session(fake).set_flag(self.BASE, 32, False)
        self.assertEqual(fake.read(self.BASE + 4, 1), b"\xfe")

    def test_setting_a_flag_that_is_set_changes_nothing(self):
        fake = FakeEmulator()
        fake.write(self.BASE + 1, b"\x20")
        Session(fake).set_flag(self.BASE, 13, True)
        self.assertEqual(fake.read(self.BASE + 1, 1), b"\x20")


class TestLoadout(unittest.TestCase):
    def test_set_loadout_writes_what_the_annotator_writes_for_the_same_tokens(self):
        tokens = ["DJump", "Bat", "WWalk", "Dive", "Slide"]
        fake = FakeEmulator()
        fake.write(annotator.BLUE_OWNED, b"\x10\x22")
        expected = FakeEmulator()
        expected.write(annotator.BLUE_OWNED, b"\x10\x22")
        for address, data in annotator.loadout_writes(
                read_many(expected, annotator.LOADOUT_READS), tokens):
            expected.write(address, data)
        Session(fake).set_loadout(tokens)
        self.assertEqual(fake.memory, expected.memory)

    def test_set_loadout_uses_only_the_tokens_and_one_soul_of_each_equipped_kind(self):
        fake = FakeEmulator()
        session = Session(fake)
        session.set_loadout(["Panth", "Glide", "Dive", "WWalk", "HJump", "Kick"])
        # Glide comes before Panth among the guardian souls, and WWalk before Dive.
        self.assertEqual(session.state().used, {"Glide", "WWalk", "HJump", "Kick"})

    def test_an_empty_loadout_uses_nothing(self):
        fake = FakeEmulator()
        session = Session(fake)
        session.set_loadout(["DJump", "Bat"])
        session.set_loadout([])
        self.assertEqual(session.state().used, frozenset())

    def test_equip_test_weapon_owns_and_equips_it(self):
        fake = FakeEmulator()
        Session(fake).equip_test_weapon()
        self.assertEqual(fake.read(annotator.EQUIPPED_WEAPON, 1)[0], annotator.TEST_WEAPON)
        self.assertEqual(fake.read(*annotator.WEAPON_READ), b"\x01")


# --- Moving between rooms -------------------------------------------------------------------------

class TestTeleport(unittest.TestCase):
    def test_teleport_returns_the_frames_the_warp_took_and_soma_is_at_the_landing(self):
        fake = fake_game(warp_frames=6)
        session = booted(fake)
        # Camera y 80 is TeleportPlayer's value, so the screen's top row is at y 128.
        self.assertEqual(session.teleport(ROOM_A, 256, 80, 100, 60), 6)
        state = session.state()
        self.assertTrue(state.playing)
        self.assertEqual((state.room_pointer, state.soma), (ROOM_A, (356, 188)))

    def test_teleport_waits_for_normal_play_in_the_new_room(self):
        # The room changes on the third frame of this warp, and play resumes on the fifth.
        def on_frame(fake: FakeEmulator, held: tuple[str, ...]) -> None:
            if fake.read(annotator.MENU_STATE, 1)[0] != 8:
                return
            count = u16(fake, WARP_COUNT) + 1
            put_u16(fake, WARP_COUNT, count)
            if count == 3:
                fake.write(annotator.ROOM_POINTER, struct.pack("<I", ROOM_A))
            if count == 5:
                fake.write(annotator.MENU_STATE, b"\x01")

        fake = FakeEmulator(on_frame)
        in_play(fake, ROOM_START, (100, 100))
        self.assertEqual(Session(fake).teleport(ROOM_A, 0, 0, 16, 112), 5)

    def test_teleport_starts_the_warp_with_the_annotators_writes_and_nothing_else(self):
        recording = Recording(fake_game())
        session = booted(recording)
        start = len(recording.ops)
        session.teleport(ROOM_A, 0, 0, 16, 112)
        writes = [op for op in recording.ops[start:] if op[0] == "write"]
        block = struct.pack("<IHHhh", ROOM_A, 0, 0, 16, 112).hex()
        self.assertEqual(writes, [["write", f"{annotator.TELEPORT:#010x}", block],
                                  ["write", f"{annotator.CAMERA_LOCK:#010x}", "00"],
                                  ["write", f"{annotator.MENU_SUBSTATE:#010x}", "00"],
                                  ["write", f"{annotator.MENU_STATE:#010x}", "08"]])

    def test_teleport_raises_game_error_outside_normal_play(self):
        fake = fake_game()
        session = booted(fake)
        fake.write(annotator.MENU_STATE, b"\x02")
        with self.assertRaises(GameError):
            session.teleport(ROOM_A, 0, 0, 16, 112)
        self.assertNotEqual(fake.read(annotator.MENU_STATE, 1), b"\x08")

    def test_teleport_raises_game_error_when_soma_does_not_arrive_in_time(self):
        fake = fake_game(warp_frames=1000)
        session = booted(fake)
        start = fake.frame
        with self.assertRaises(GameError):
            session.teleport(ROOM_A, 0, 0, 16, 112, limit=30)
        self.assertEqual(fake.frame - start, 30)


class TestEnter(unittest.TestCase):
    ARGS = {"room_address": ROOM_A, "camera_x": 256, "camera_y": 80, "soma_x": 100,
            "soma_y": 60, "feet_x": 356, "feet_y": 188}
    FLAGS = [(annotator.MISC_FLAGS, 32, False), (annotator.MISC_FLAGS, 35, True),
             (annotator.BOSS_FLAGS, 11, True)]

    def test_enter_sets_the_door_flags_before_it_starts_the_warp(self):
        recording = Recording(fake_game())
        session = booted(recording)
        session.rom = b"a ROM"
        recording.emulator.write(annotator.MISC_FLAGS + 4, b"\x01")
        with mock.patch.object(landing, "teleport_args", return_value=dict(self.ARGS)) as args, \
                mock.patch.object(landing, "door_flags", return_value=self.FLAGS) as flags:
            got = session.enter("305:301")
        args.assert_called_once_with("305:301", b"a ROM")
        flags.assert_called_once_with("305:301", b"a ROM", False)
        self.assertEqual(got, self.ARGS)
        fake = recording.emulator
        self.assertEqual(fake.read(annotator.MISC_FLAGS + 4, 1), b"\x08")
        self.assertEqual(fake.read(annotator.BOSS_FLAGS + 1, 1), b"\x08")
        writes = [index for index, op in enumerate(recording.ops) if op[0] == "write"]
        flag_writes = [index for index in writes
                       if recording.ops[index][1] in (f"{annotator.MISC_FLAGS + 4:#010x}",
                                                      f"{annotator.BOSS_FLAGS + 1:#010x}")]
        warp = recording.ops.index(["write", f"{annotator.MENU_STATE:#010x}", "08"])
        self.assertEqual(len(flag_writes), 3)
        self.assertLess(max(flag_writes), warp)
        state = session.state()
        self.assertEqual((state.room_pointer, state.soma), (ROOM_A, (356, 188)))

    def test_enter_on_the_rom_opens_the_wall_of_a_dead_end_and_puts_soma_at_the_landing(self):
        rom = read_rom()
        fake = fake_game()
        session = booted(fake)
        session.rom = rom
        args = session.enter("305:301")
        self.assertEqual(args, landing.teleport_args("305:301", rom))
        # Flag 35 of the misc flags is bit 3 of their fifth byte.
        self.assertEqual(fake.read(annotator.MISC_FLAGS + 4, 1)[0] & 0x08, 0x08)
        state = session.state()
        self.assertEqual(state.room_pointer, args["room_address"])
        self.assertEqual(state.soma, (args["feet_x"], args["feet_y"]))

    def test_enter_with_walls_gone_asks_for_the_flags_of_the_room_without_its_walls(self):
        session = booted(fake_game())
        session.rom = b"a ROM"
        with mock.patch.object(landing, "teleport_args", return_value=dict(self.ARGS)), \
                mock.patch.object(landing, "door_flags", return_value=self.FLAGS) as flags:
            session.enter("305:301", walls_gone=True)
        flags.assert_called_once_with("305:301", b"a ROM", True)

    def test_enter_on_the_rom_leaves_the_wall_of_109_108_whole_unless_the_walls_are_gone(self):
        rom = read_rom()
        # Flag 33 of the misc flags is bit 1 of their fifth byte. It starts as the opposite of
        # what each visit should leave.
        for walls_gone, before, after in ((False, b"\x02", 0x00), (True, b"\x00", 0x02)):
            with self.subTest(walls_gone=walls_gone):
                fake = fake_game()
                session = booted(fake)
                session.rom = rom
                fake.write(annotator.MISC_FLAGS + 4, before)
                session.enter("109:108", walls_gone)
                self.assertEqual(fake.read(annotator.MISC_FLAGS + 4, 1)[0] & 0x02, after)


if __name__ == "__main__":
    unittest.main()
