"""
The annotator keeps its settings in a file in its data folder, and reads the US ROM's path there.

Each test points the data folder at a temporary one, so the settings saved on this machine are
neither read nor changed.

    python -m pytest worlds/cvaos/test/test_annotator_settings.py -v
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from ..tools.annotator import landing, settings, sheets


class TestSettingsFile(unittest.TestCase):
    """Saving the settings and loading them again."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        # The data folder is one level down, so that it doesn't exist until something makes it.
        self.data_dir = os.path.join(folder.name, "cvaos-annotator")
        patch = mock.patch.object(sheets, "DATA_DIR", self.data_dir)
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_file_is_in_the_data_folder(self):
        self.assertEqual(settings.path(), os.path.join(self.data_dir, settings.FILE))

    def test_nothing_saved_loads_as_no_settings(self):
        self.assertEqual(settings.load(), {})

    def test_saved_settings_load_again(self):
        saved = {"sheet_id": "a-sheet", "annotator": "Somebody", "emuhawk_args": ["--gdi"]}
        settings.save(saved)
        self.assertEqual(settings.load(), saved)

    def test_saving_makes_the_data_folder(self):
        self.assertFalse(os.path.exists(self.data_dir))
        settings.save({"annotator": "Somebody"})
        with open(settings.path(), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), {"annotator": "Somebody"})

    def write(self, text: str, encoding: str = "utf-8") -> None:
        """Write the settings file as a person editing it by hand would."""
        os.makedirs(self.data_dir)
        with open(settings.path(), "w", encoding=encoding) as handle:
            handle.write(text)

    def test_a_file_with_a_byte_order_mark_loads(self):
        self.write('{"sheet_id": "a-sheet"}', "utf-8-sig")
        self.assertEqual(settings.load(), {"sheet_id": "a-sheet"})

    def test_a_file_that_is_not_json_raises_and_names_the_file(self):
        # A Windows path with single backslashes is the likely slip, and it isn't valid JSON.
        self.write('{"rom_path": "C:\\Games\\aria.gba"}')
        with self.assertRaises(ValueError) as raised:
            settings.load()
        self.assertIn(settings.path(), str(raised.exception))

    def test_a_file_that_is_not_an_object_raises_and_names_the_file(self):
        self.write('["a-sheet"]')
        with self.assertRaises(ValueError) as raised:
            settings.load()
        self.assertIn(settings.path(), str(raised.exception))


class TestUsRomPath(unittest.TestCase):
    """The US ROM's path comes from the ``rom_path`` setting and from nowhere else."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name
        patch = mock.patch.object(sheets, "DATA_DIR", os.path.join(self.folder, "data"))
        patch.start()
        self.addCleanup(patch.stop)
        landing._default_rom.cache_clear()
        self.addCleanup(landing._default_rom.cache_clear)

    def rom_file(self, content: bytes) -> str:
        """Write a file to stand for the ROM, and return its path."""
        path = os.path.join(self.folder, "aria.gba")
        with open(path, "wb") as handle:
            handle.write(content)
        return path

    def test_the_saved_path_is_returned(self):
        path = self.rom_file(b"rom")
        settings.save({"rom_path": path})
        self.assertEqual(settings.us_rom_path(), path)

    def test_no_setting_raises_and_names_the_settings_file(self):
        for saved in (None, {}, {"rom_path": ""}):
            with self.subTest(saved=saved):
                if saved is not None:
                    settings.save(saved)
                with self.assertRaises(FileNotFoundError) as raised:
                    settings.us_rom_path()
                self.assertIn(settings.path(), str(raised.exception))

    def test_a_path_with_no_file_raises_and_names_the_path(self):
        missing = os.path.join(self.folder, "missing.gba")
        settings.save({"rom_path": missing})
        with self.assertRaises(FileNotFoundError) as raised:
            settings.us_rom_path()
        self.assertIn(missing, str(raised.exception))

    def test_a_folder_is_not_a_rom(self):
        settings.save({"rom_path": self.folder})
        with self.assertRaises(FileNotFoundError):
            settings.us_rom_path()

    def test_landing_reads_the_rom_that_the_setting_names(self):
        settings.save({"rom_path": self.rom_file(b"the whole rom")})
        self.assertEqual(landing._default_rom(), b"the whole rom")

    def test_landing_raises_when_no_rom_is_set(self):
        with self.assertRaises(FileNotFoundError):
            landing._default_rom()
