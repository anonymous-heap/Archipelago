"""
Find the Castlevania Advance Collection's game.exe in the player's Steam libraries.

Steam lists its library folders in ``steamapps/libraryfolders.vdf`` under its install folder,
and the collection can be in any of them. :func:`game_exe_candidates` finds Steam's install
folders, reads each one's library list, and yields every game.exe of the collection it finds.

Archipelago may run as a Windows program inside Wine or Proton while the game sits in a Linux or
macOS Steam library. Wine shows the host's root folder as drive Z:, so a host path is looked up
through Z:. Proton passes the host's Steam folder to the programs it starts in
``STEAM_COMPAT_CLIENT_INSTALL_PATH``, and Wine passes the host's home folder in ``WINEHOMEDIR``.
"""
from __future__ import annotations

import os
import re
import sys
from typing import Iterator

APP_ID = "1552550"
DEFAULT_INSTALL_FOLDER = "Castlevania Advance Collection"

# Where Steam keeps its own folder, relative to the home folder: Linux, Linux with Steam from
# Flatpak or Snap, and macOS.
HOME_STEAM_FOLDERS = (
    ".steam/steam",
    ".local/share/Steam",
    ".var/app/com.valvesoftware.Steam/.local/share/Steam",
    "snap/steam/common/.local/share/Steam",
    "Library/Application Support/Steam",
)
WINDOWS_STEAM_FOLDERS = (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam")

# The registry values that hold Steam's install folder on Windows, and in a Wine bottle that has
# Windows Steam installed. The machine-wide values come first because they keep the folder's
# capitalization, which the user value lowercases.
_REGISTRY_VALUES = (
    ("HKEY_LOCAL_MACHINE", r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
    ("HKEY_LOCAL_MACHINE", r"SOFTWARE\Valve\Steam", "InstallPath"),
    ("HKEY_CURRENT_USER", r"Software\Valve\Steam", "SteamPath"),
)

_QUOTED = r'"((?:[^"\\]|\\.)*)"'
_PATH_ENTRY = re.compile(r'"path"\s+' + _QUOTED, re.IGNORECASE)
# Before 2021, libraryfolders.vdf listed each extra library as "1" "D:\\SteamLibrary". The current
# format's "apps" lists also pair a number with a number, so only a value that isn't a number is
# taken as a path.
_OLD_ENTRY = re.compile(r'^\s*"\d+"\s+' + _QUOTED + r'\s*$', re.MULTILINE)
_INSTALL_DIR = re.compile(r'"installdir"\s+' + _QUOTED, re.IGNORECASE)


def game_exe_candidates() -> Iterator[str]:
    """Yield each existing game.exe of the collection in the player's Steam libraries.

    The libraries are searched in the order :func:`steam_libraries` returns them. The caller
    should still check the file's hash, because nothing here reads the file.
    """
    for library in steam_libraries():
        exe = os.path.join(library, "steamapps", "common", install_folder(library), "game.exe")
        if os.path.isfile(exe):
            yield exe


def steam_libraries() -> list[str]:
    """Return every Steam library folder found, without repeats.

    Each Steam install folder comes first, followed by the libraries its libraryfolders.vdf
    lists. A folder that doesn't exist is left out. Two paths to the same folder count once, as
    on Linux, where ``~/.steam/steam`` links to ``~/.local/share/Steam``.
    """
    libraries: list[str] = []
    seen: set[str] = set()
    for root in steam_roots():
        vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
        for library in (root, *libraries_listed_in(vdf)):
            library = local_path(library)
            if not os.path.isdir(library):
                continue
            key = os.path.normcase(os.path.realpath(library))
            if key not in seen:
                seen.add(key)
                libraries.append(library)
    return libraries


def steam_roots() -> list[str]:
    """Return the folders where Steam may be installed, most likely first."""
    roots: list[str] = []
    if sys.platform == "win32":
        roots.extend(_registry_roots())
    compat = os.environ.get("STEAM_COMPAT_CLIENT_INSTALL_PATH")
    if compat:
        roots.append(compat)
    home = _host_home()
    if home:
        roots.extend(os.path.join(home, *folder.split("/")) for folder in HOME_STEAM_FOLDERS)
    if sys.platform == "win32":
        roots.extend(WINDOWS_STEAM_FOLDERS)
    return [local_path(root) for root in roots]


def libraries_listed_in(vdf_path: str) -> list[str]:
    """Return the library folders a libraryfolders.vdf lists, or nothing if it can't be read."""
    try:
        with open(vdf_path, encoding="utf-8", errors="replace") as file:
            text = file.read()
    except OSError:
        return []
    paths = _PATH_ENTRY.findall(text)
    paths += [value for value in _OLD_ENTRY.findall(text) if not value.isdigit()]
    return [_unescape(path) for path in paths]


def install_folder(library: str) -> str:
    """Return the collection's folder name under the library's ``steamapps/common``.

    Steam records it in the collection's app manifest. Without a readable manifest, this
    returns the folder name Steam uses by default.
    """
    manifest = os.path.join(library, "steamapps", f"appmanifest_{APP_ID}.acf")
    try:
        with open(manifest, encoding="utf-8", errors="replace") as file:
            match = _INSTALL_DIR.search(file.read())
    except OSError:
        match = None
    return _unescape(match.group(1)) if match else DEFAULT_INSTALL_FOLDER


def local_path(path: str) -> str:
    """Return ``path`` as this process can open it.

    A host path such as ``/home/user/.steam/steam`` is reached through drive Z: when this process
    runs under Wine or Proton. On the host itself, and for a Windows path, it is returned as is.
    """
    if sys.platform == "win32" and path.startswith("/"):
        return "Z:" + path.replace("/", "\\")
    return path


def _host_home() -> str | None:
    """Return the home folder of the host's user, as this process can open it.

    Under Wine, this is the folder ``WINEHOMEDIR`` names. On Windows outside Wine, there is no
    host, so this returns None.
    """
    if sys.platform != "win32":
        return os.path.expanduser("~")
    wine_home = os.environ.get("WINEHOMEDIR", "")
    if wine_home.startswith("\\??\\"):
        wine_home = wine_home[4:]
    return wine_home or None


def _registry_roots() -> list[str]:
    """Return the Steam install folders the Windows registry records."""
    try:
        import winreg
    except ImportError:
        return []
    roots = []
    for hive, key_path, value_name in _REGISTRY_VALUES:
        try:
            with winreg.OpenKey(getattr(winreg, hive), key_path) as key:
                value, _kind = winreg.QueryValueEx(key, value_name)
        except OSError:
            continue
        if isinstance(value, str) and value:
            roots.append(os.path.normpath(value))
    return roots


def _unescape(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value)
