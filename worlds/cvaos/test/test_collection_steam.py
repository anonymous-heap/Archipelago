"""
Finding the Advance Collection's game.exe in the player's Steam libraries (advance_collection/steam.py).

The tests build Steam folders in a temporary directory, and replace the registry and the
platform where a test needs a particular one, so they never read this machine's Steam.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

from ..advance_collection import steam

CURRENT_FORMAT = r'''"libraryfolders"
{
	"0"
	{
		"path"		"C:\\Program Files (x86)\\Steam"
		"label"		""
		"apps"
		{
			"228980"		"651361785"
			"1552550"		"511071821"
		}
	}
	"1"
	{
		"path"		"D:\\Dawn\\Steam"
		"label"		"FreeAgent GoFlex Drive"
		"apps"
		{
			"9420"		"8457249990"
		}
	}
}
'''

OLD_FORMAT = r'''"LibraryFolders"
{
	"TimeNextStatsReport"		"1234567890"
	"ContentStatsID"		"-123456789"
	"1"		"D:\\SteamLibrary"
}
'''


def platform(name: str):
    """Make steam.py see ``name`` as sys.platform, without changing it for anything else."""
    return mock.patch.object(steam, "sys", types.SimpleNamespace(platform=name))


class TempDirTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="cvaos_steam_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def write(self, *parts: str, text: str = "") -> str:
        path = os.path.join(self.root, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as file:
            file.write(text)
        return path

    def library(self, name: str, folder: str = steam.DEFAULT_INSTALL_FOLDER) -> tuple[str, str]:
        """Make a library holding the collection, and return the library and its game.exe."""
        exe = self.write(name, "steamapps", "common", folder, "game.exe", text="MZ")
        return os.path.join(self.root, name), exe

    def vdf_listing(self, steam_root: str, *libraries: str) -> None:
        entries = ""
        for n, path in enumerate(libraries):
            escaped = path.replace("\\", "\\\\")  # the vdf format escapes backslashes
            entries += f'\t"{n}"\n\t{{\n\t\t"path"\t\t"{escaped}"\n\t}}\n'
        self.write(steam_root, "steamapps", "libraryfolders.vdf",
                   text=f'"libraryfolders"\n{{\n{entries}}}\n')


class LibraryListTest(TempDirTest):
    """libraryfolders.vdf gives the library folders, in the current format and the older one."""

    def test_the_current_format_lists_each_path_and_no_app_ids(self) -> None:
        vdf = self.write("libraryfolders.vdf", text=CURRENT_FORMAT)
        self.assertEqual(steam.libraries_listed_in(vdf),
                         [r"C:\Program Files (x86)\Steam", r"D:\Dawn\Steam"])

    def test_the_older_format_lists_numbered_paths(self) -> None:
        vdf = self.write("libraryfolders.vdf", text=OLD_FORMAT)
        self.assertEqual(steam.libraries_listed_in(vdf), [r"D:\SteamLibrary"])


class SearchTest(TempDirTest):
    """The search reads every Steam folder's library list and finds game.exe in any library.

    The collection's folder is the one its app manifest names, or Steam's default name when there
    is no manifest.
    """

    def test_game_exe_is_found_in_a_library_on_another_drive(self) -> None:
        # Neither library has an app manifest, so game.exe is in the default folder.
        steam_root = os.path.join(self.root, "Steam")
        os.makedirs(steam_root)
        other, exe = self.library("OtherDrive")
        self.vdf_listing("Steam", steam_root, other)
        with mock.patch.object(steam, "steam_roots", return_value=[steam_root]):
            self.assertEqual(list(steam.game_exe_candidates()), [exe])

    def test_the_manifest_folder_is_searched(self) -> None:
        # This Steam folder has no libraryfolders.vdf, so it lists no other libraries.
        library, exe = self.library("Lib", folder="Renamed")
        self.write("Lib", "steamapps", f"appmanifest_{steam.APP_ID}.acf",
                   text='"AppState"\n{\n\t"installdir"\t\t"Renamed"\n}\n')
        with mock.patch.object(steam, "steam_roots", return_value=[library]):
            self.assertEqual(list(steam.game_exe_candidates()), [exe])

    def test_each_library_is_searched_once_and_missing_ones_are_skipped(self) -> None:
        library, exe = self.library("Lib")
        self.vdf_listing("Lib", library, library + os.sep, os.path.join(self.root, "Gone"))
        with mock.patch.object(steam, "steam_roots", return_value=[library, library]):
            self.assertEqual(steam.steam_libraries(), [library])
            self.assertEqual(list(steam.game_exe_candidates()), [exe])


class WineTest(unittest.TestCase):
    """Under Wine or Proton, host paths are reached through drive Z:."""

    def test_a_host_path_goes_through_drive_z(self) -> None:
        with platform("win32"):
            self.assertEqual(steam.local_path("/run/media/mmcblk0p1"), r"Z:\run\media\mmcblk0p1")
            self.assertEqual(steam.local_path(r"D:\SteamLibrary"), r"D:\SteamLibrary")
        with platform("linux"):
            self.assertEqual(steam.local_path("/run/media/mmcblk0p1"), "/run/media/mmcblk0p1")

    def test_proton_and_wine_name_the_host_steam_folders(self) -> None:
        environ = {"STEAM_COMPAT_CLIENT_INSTALL_PATH": "/home/deck/.local/share/Steam",
                   "WINEHOMEDIR": r"\??\Z:\home\deck"}
        with platform("win32"), mock.patch.dict(os.environ, environ), \
                mock.patch.object(steam, "_registry_roots", return_value=[r"C:\Steam"]):
            roots = steam.steam_roots()
        self.assertEqual(roots[:2], [r"C:\Steam", r"Z:\home\deck\.local\share\Steam"])
        self.assertIn(os.path.join(r"Z:\home\deck", ".steam", "steam"), roots)
        self.assertEqual(roots[-2:], list(steam.WINDOWS_STEAM_FOLDERS))

    def test_windows_outside_wine_looks_in_no_host_folders(self) -> None:
        with platform("win32"), mock.patch.dict(os.environ, clear=True), \
                mock.patch.object(steam, "_registry_roots", return_value=[]):
            self.assertEqual(steam.steam_roots(), list(steam.WINDOWS_STEAM_FOLDERS))

    def test_a_native_host_looks_in_its_home_folder(self) -> None:
        with platform("linux"), mock.patch.dict(os.environ, clear=True), \
                mock.patch.object(steam.os.path, "expanduser", return_value="/home/deck"):
            roots = steam.steam_roots()
        self.assertEqual(roots[0], os.path.join("/home/deck", ".steam", "steam"))
        self.assertEqual(len(roots), len(steam.HOME_STEAM_FOLDERS))


if __name__ == "__main__":
    unittest.main()
