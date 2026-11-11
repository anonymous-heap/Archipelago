"""
The annotator draws a room's background layers from the ROM, and caches each room's PNG on disk.

These tests read the USA ROM, and skip when it is not found.

    python -m pytest worlds/cvaos/test/test_annotator_rooms.py -v
"""
from __future__ import annotations

import hashlib
import io
import os
import tempfile
import unittest
from unittest import mock

from PIL import Image

from ..tools.annotator import rooms
from . import find_rom

# Room 000, where the game starts. Its first layer is 2 screens wide and 3 tall.
ROOM_000 = 0x0850EF9C


def rom_path() -> str:
    path = find_rom()
    if path is None:
        raise unittest.SkipTest("USA ROM not available")
    return path


class TestRenderRoom(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(rom_path(), "rb") as handle:
            cls.rom = handle.read()
        cls.image = rooms.render_room(cls.rom, ROOM_000)

    def test_the_image_is_the_room_size_in_screens(self):
        self.assertEqual(self.image.mode, "RGBA")
        self.assertEqual(self.image.size, (2 * 256, 3 * 256))

    def test_most_of_the_room_is_drawn(self):
        alpha = self.image.getchannel("A")
        drawn = sum(alpha.histogram()[1:])
        self.assertGreater(drawn, 0.5 * self.image.width * self.image.height)

    def test_a_room_address_outside_the_rom_is_an_error(self):
        with self.assertRaises(ValueError):
            rooms.render_room(self.rom, 0x09000000)


class TestRoomPng(unittest.TestCase):
    def test_the_second_call_reads_the_cached_file(self):
        path = rom_path()
        with open(path, "rb") as handle:
            md5 = hashlib.md5(handle.read()).hexdigest()
        with tempfile.TemporaryDirectory() as folder:
            first = rooms.room_png(path, ROOM_000, os.path.join(folder, "rooms"))
            self.assertEqual(Image.open(io.BytesIO(first)).size, (512, 768))
            names = os.listdir(os.path.join(folder, "rooms"))
            self.assertEqual(len(names), 1)
            self.assertIn(md5, names[0])
            self.assertIn("0850EF9C", names[0])

            with mock.patch.object(rooms, "render_room", side_effect=AssertionError("drew again")):
                second = rooms.room_png(path, ROOM_000, os.path.join(folder, "rooms"))
            self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
