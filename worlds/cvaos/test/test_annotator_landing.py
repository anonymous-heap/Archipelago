"""
The annotator places Soma just inside each door, and gives TeleportPlayer arguments for that spot.

The known doors below were checked in an emulator. These tests read the USA ROM, and skip when it
is not found.

    python -m pytest worlds/cvaos/test/test_annotator_landing.py -v
"""
from __future__ import annotations

import unittest

from ..tools.annotator import landing
from . import find_rom

ROOM_008 = 0x0850F44C
ROOM_009 = 0x0850F4F4


def read_rom() -> bytes:
    path = find_rom()
    if path is None:
        raise unittest.SkipTest("USA ROM not available")
    with open(path, "rb") as handle:
        return handle.read()


def open_run(collision: landing.Collision, row: int, x: int) -> tuple[int, int]:
    """Return the run of passable cells in a row of cells around room x ``x``, in room pixels."""
    def passable(column: int) -> bool:
        return collision.kind(column, row) not in (landing.SOLID, landing.SLOPE)

    column = x // landing.CELL
    if not passable(column):
        raise AssertionError(f"x {x} is not open in cell row {row}")
    start = end = column
    while passable(start - 1):
        start -= 1
    while passable(end + 1):
        end += 1
    return start * landing.CELL, (end + 1) * landing.CELL


class TestKnownDoors(unittest.TestCase):
    """The doors whose landings were tried in an emulator."""

    @classmethod
    def setUpClass(cls):
        cls.rom = read_rom()

    def args(self, door: str) -> dict:
        return landing.teleport_args(door, self.rom)

    def test_a_left_door_stands_soma_16_px_inside_on_the_floor(self):
        spot = self.args("60E:60D")
        self.assertEqual((spot["edge"], spot["rule"]), ("left", "side"))
        self.assertEqual((spot["feet_x"], spot["feet_y"]), (16, 160))

    def test_a_right_door_in_a_room_one_screen_wide_stays_inside_x_240(self):
        spot = self.args("00C:00D")
        self.assertEqual(spot["edge"], "right")
        self.assertEqual((spot["feet_x"], spot["feet_y"]), (224, 160))

    def test_a_top_door_starts_soma_below_the_top_margin(self):
        spot = self.args("010:011")
        self.assertEqual(spot["edge"], "top")
        self.assertTrue(832 <= spot["feet_x"] < 960, spot)
        self.assertTrue(56 <= spot["feet_y"] <= 64, spot)
        self.assertIsNotNone(spot["floor_y"])

    def test_a_bottom_door_stands_soma_on_the_platform_over_the_shaft(self):
        spot = self.args("002:003")
        self.assertEqual((spot["edge"], spot["rule"]), ("bottom", "bottom"))
        self.assertTrue(96 <= spot["feet_x"] < 160, spot)
        self.assertEqual(spot["feet_y"], 960)

    def test_room_009_from_008_falls_onto_the_ledge_under_the_shared_doorway(self):
        # Soma fell through 008's floor opening at x 80 to 144 in the emulator and arrived in 009
        # at x 384, standing on the ledge at y 110.
        spot = self.args("009:008")
        self.assertEqual(spot["edge"], "top")
        self.assertTrue(336 <= spot["feet_x"] <= 400, spot)
        self.assertTrue(56 <= spot["feet_y"] <= 64, spot)
        self.assertTrue(100 <= spot["floor_y"] <= 120, spot)

    def test_the_landing_for_009_from_008_is_open_in_both_rooms(self):
        spot = self.args("009:008")
        room_008 = landing.Collision(self.rom, ROOM_008)
        room_009 = landing.Collision(self.rom, ROOM_009)
        # The last row of 008 above its bottom exit line, and the first row of 009 below its top
        # exit line. The door moves Soma's x by 256 + 16 on the way down.
        opening_008 = open_run(room_008, (208 - 8) // landing.CELL, spot["feet_x"] - 272)
        opening_009 = open_run(room_009, 48 // landing.CELL, spot["feet_x"])
        self.assertEqual(opening_008, (80, 144))
        self.assertEqual(opening_009, (352, 416))
        start, end = spot["doorway"]
        self.assertTrue(opening_009[0] <= start < end <= opening_009[1], spot)
        self.assertTrue(opening_008[0] + 272 <= start < end <= opening_008[1] + 272, spot)

    def test_soma_is_drawn_where_his_feet_are(self):
        for door in ("60E:60D", "00C:00D", "010:011", "002:003", "009:008"):
            with self.subTest(door=door):
                spot = self.args(door)
                self.assertEqual(spot["soma_x"], spot["feet_x"] - spot["camera_x"])
                # TeleportPlayer adds 48 to camera_y to get the screen's top row.
                self.assertEqual(spot["soma_y"], spot["feet_y"] - spot["camera_y"] - 48)

    def test_a_door_identifier_must_name_exactly_one_door(self):
        with self.assertRaises(KeyError):
            self.args("FFF:000")
        with self.assertRaisesRegex(KeyError, r"\(left\).*\(right\)"):
            self.args("103:104")
        self.assertEqual(self.args("103:104 (left)")["edge"], "bottom")


class TestAllDoors(unittest.TestCase):
    """Properties that every door's landing has."""

    @classmethod
    def setUpClass(cls):
        cls.rom = read_rom()
        cls.landings = landing.all_landings(cls.rom)
        cls.collisions: dict[int, landing.Collision] = {}

    def collision(self, spot: dict) -> landing.Collision:
        address = spot["room_address"]
        if address not in self.collisions:
            self.collisions[address] = landing.Collision(self.rom, address)
        return self.collisions[address]

    def test_every_door_is_placed(self):
        self.assertEqual(len(self.landings), len(landing.entrance_info.rows))
        unplaced = [door for door, spot in self.landings.items() if spot["edge"] is None]
        self.assertEqual(unplaced, [])

    def test_every_landing_is_inside_the_playable_box_with_a_margin(self):
        for door, spot in self.landings.items():
            collision = self.collision(spot)
            # The game moves Soma through a door past these lines (sub_08011A44).
            right = 240 if collision.width == 1 else collision.width * 256
            bottom = collision.height * 256 - 48
            with self.subTest(door=door):
                self.assertTrue(16 <= spot["feet_x"] <= right - 16, spot)
                self.assertTrue(48 + 16 <= spot["feet_y"] <= bottom, spot)

    def test_no_landing_is_inside_solid_collision(self):
        for door, spot in self.landings.items():
            collision = self.collision(spot)
            body = [collision.solid(spot["feet_x"], spot["feet_y"] - k) for k in range(1, 33)]
            with self.subTest(door=door):
                self.assertFalse(any(body), spot)

    def test_soma_is_on_the_screen_and_the_camera_inside_the_room(self):
        for door, spot in self.landings.items():
            collision = self.collision(spot)
            width = collision.width * 256 - (16 if collision.width == 1 else 0)
            height = collision.height * 256 - 96
            with self.subTest(door=door):
                self.assertTrue(0 <= spot["camera_x"] <= width - 240, spot)
                self.assertTrue(0 <= spot["camera_y"] <= height - 160, spot)
                self.assertTrue(0 <= spot["soma_x"] < 240, spot)
                self.assertTrue(0 <= spot["soma_y"] <= 160, spot)

    def test_one_door_matches_all_landings(self):
        self.assertEqual(landing.teleport_args("009:008", self.rom), self.landings["009:008"])


class TestCamera(unittest.TestCase):
    def test_a_room_one_screen_in_size_has_one_camera_position(self):
        self.assertEqual(landing.camera_for(1, 1, 16, 160), (0, 0))
        self.assertEqual(landing.camera_for(1, 1, 224, 100), (0, 0))

    def test_the_camera_centers_on_soma_away_from_the_room_edges(self):
        # Soma's feet sit at screen x 120 and y 111, so the top row is 111 above them.
        camera_x, camera_y = landing.camera_for(4, 4, 600, 500)
        self.assertEqual(camera_x, 600 - 120)
        self.assertEqual(camera_y + 48, 500 - 111)


if __name__ == "__main__":
    unittest.main()
