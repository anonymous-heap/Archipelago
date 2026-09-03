"""
Install a patched CVAoS ROM into the Steam Castlevania Advance Collection.

The collection keeps every ROM inside ``windata/alldata.bin`` (payload) plus
``windata/alldata.psb.m`` (encrypted index) — see ``cac_archive.py``.
"Installing" a randomized ROM therefore means rebuilding that pair with the
AoS member replaced. This module is the friendly wrapper around
``cac_archive.replace_member`` behind the "CVAoS Collection ROM Installer"
Launcher component (Messenger-style: the collection is located from the
hash-validated ``CVAOSSettings.collection_exe`` path; AP opens a browse dialog
once and persists the answer in host.yaml).

Safety model, in order:

* the patched ROM is validated before anything is touched — exact 8 MiB, AoS
  header, ``CVAOS_AP_V0.3`` marker at 0x670000, and M2's two additions (the
  replacement graphic at 0x660000 and the audio bridge at 0x700000; a ROM
  patched from a cart dump instead of the collection-extracted base lacks both
  and would silently break the collection);
* refuses while the collection is running (best effort via the client's own
  process attach; Windows file locks are the backstop);
* pristine ``*.apbackup`` copies of both windata files are made ONCE, ever —
  installing later seeds never overwrites them, so ``restore_backup`` always
  returns to stock, and once they exist the base ROM is extracted from them
  rather than from the live archive (which then holds the installed seed);
* the rebuilt pair is staged in a scratch subdirectory using the real
  filenames (the MDF key is derived from the lowercased basename), verified by
  re-extracting the member, and only then moved over the live files with
  ``os.replace``.

Steam's "Verify integrity of game files" restores M2's stock files; the fix is
simply to run the installer again (the backups stay valid).

The install input may be a patched ``.gba`` or the ``.apcvaos`` patch itself — a patch is
applied first (``Patch.create_rom_file``; an advance_collection-target patch auto-sources
its base ROM from the installed collection), so a player handed only the patch file never
touches an intermediate ROM.

CLI (from the Archipelago repo root)::

    python -m worlds.cvaos.advance_collection.install seed.apcvaos [--exe GAME_EXE]
    python -m worlds.cvaos.advance_collection.install patched.gba  [--exe GAME_EXE]
    python -m worlds.cvaos.advance_collection.install --restore    [--exe GAME_EXE]
"""
from __future__ import annotations

import argparse
import os
import shutil
from typing import Callable

from . import cac_archive
from .._bytemaker_compat import Entry
from ..constants import AC_DEFAULT_EXE_PATH
from ..rom.address_space import M2_AUDIO_BRIDGE, M2_GRAPHIC, gba_space
from ..rom.patch import ARCHIPELAGO_IDENTIFIER, ARCHIPELAGO_IDENTIFIER_START

AOS_MEMBER = "system/roms/03_Akatsuki_US.patch_210623m.bin"
ROM_SIZE = 0x800000
BACKUP_SUFFIX = ".apbackup"
WINDATA_FILES = ("alldata.bin", "alldata.psb.m")
INDEX_KEY_NAME = "alldata.psb.m"  # the index's key derives from this name, whatever the file is called
_AP_MARKER_PREFIX = b"CVAOS_AP_"   # shared by every patch version's identifier

# Mirrors collection_client.ROM_TITLE/ROM_TITLE_OFFSET (not imported: that module pulls
# in CommonClient, which the installer doesn't need).
_ROM_TITLE = b"CASTLEVANIA2"
_ROM_TITLE_OFFSET = 0xA0
# M2's two additions, present only in the collection-extracted base ROM. Declared once on the
# cart space (rom/address_space.py) and used here as file-offset (start, end) pairs.
def _file_range(region: Entry) -> tuple[int, int]:
    start, size = region.bind(gba_space()).request()
    return start, start + size


_M2_GRAPHIC = _file_range(M2_GRAPHIC)
_M2_AUDIO_BRIDGE = _file_range(M2_AUDIO_BRIDGE)

_STEAM_VERIFY_NOTE = ("Note: Steam's \"Verify integrity of game files\" restores the stock "
                      "archive; if you run it, just install the ROM again.")

Log = Callable[[str], None]


class InstallError(Exception):
    """Anything that should stop the install and be shown to the user."""


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def windata_dir_from_exe(exe_path: str) -> str:
    """The windata/ directory next to game.exe, with both archive files present."""
    windata = os.path.join(os.path.dirname(os.path.abspath(exe_path)), "windata")
    if not os.path.isdir(windata):
        raise InstallError(
            f"No windata directory next to {exe_path!r} — is that really the "
            "Castlevania Advance Collection's game.exe?")
    for name in WINDATA_FILES:
        if not os.path.isfile(os.path.join(windata, name)):
            raise InstallError(f"{name} is missing from {windata!r}; "
                               "verify the game files in Steam first.")
    return windata


def _stock_archive_pair(windata: str) -> tuple[str, str]:
    """The ``(alldata.psb.m, alldata.bin)`` pair that still holds the stock ROM.

    Before any install that is the live pair. Afterwards the live pair holds the installed
    seed, and the installer's pristine ``*.apbackup`` copies are the stock source.
    """
    live = (os.path.join(windata, "alldata.psb.m"), os.path.join(windata, "alldata.bin"))
    backups = (live[0] + BACKUP_SUFFIX, live[1] + BACKUP_SUFFIX)
    return backups if all(os.path.isfile(p) for p in backups) else live


def extract_base_rom_bytes(exe_path: str) -> bytes:
    """Pull the collection's stock AoS ROM out of ``windata/`` next to *exe_path*.

    This is the base ROM AP patches against for collection play — it carries M2's
    audio bridge, unlike a cart dump. Lets the world source its base ROM from the
    installed collection instead of asking the user to run ``cac_archive extract`` by hand.
    Once a seed has been installed the live archive holds that seed, so the pristine
    backups are read instead; a patched ROM with no backups to fall back on is refused.
    """
    windata = windata_dir_from_exe(exe_path)
    psb_m, bin_path = _stock_archive_pair(windata)
    try:
        rom = cac_archive.extract_member(psb_m, bin_path, AOS_MEMBER, index_key_name=INDEX_KEY_NAME)
    except (OSError, ValueError, KeyError) as exc:
        raise InstallError(f"Could not extract the AoS ROM from {windata!r}: {exc}") from exc
    if len(rom) != ROM_SIZE:
        raise InstallError(
            f"Collection AoS ROM is {len(rom)} bytes, expected {ROM_SIZE} (8 MiB). "
            "The collection's archive may have changed — please report this.")
    if rom[_ROM_TITLE_OFFSET:_ROM_TITLE_OFFSET + len(_ROM_TITLE)] != _ROM_TITLE:
        raise InstallError(f"The member extracted from {windata!r} is not an Aria of Sorrow ROM.")
    marker = rom[ARCHIPELAGO_IDENTIFIER_START:ARCHIPELAGO_IDENTIFIER_START + len(_AP_MARKER_PREFIX)]
    if marker == _AP_MARKER_PREFIX:
        raise InstallError(
            f"The AoS ROM in {windata!r} is already Archipelago-patched and no pristine backup "
            f"({WINDATA_FILES[1]}{BACKUP_SUFFIX}) sits next to it. Use Steam's \"Verify integrity "
            "of game files\" to restore the stock archive, then try again.")
    return bytes(rom)


def extract_base_rom_to(out_path: str, exe_path: str, log: Log = print) -> str:
    """Extract the collection's stock AoS ROM and write it to *out_path*."""
    rom = extract_base_rom_bytes(exe_path)
    tmp = out_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(rom)
    os.replace(tmp, out_path)
    log(f"wrote {len(rom)} bytes to {out_path}")
    return f"Extracted the collection's stock AoS ROM to {out_path} ({len(rom)} bytes)."


def validate_patched_rom(rom_bytes: bytes) -> None:
    """Reject anything that is not an AC-based, AP-patched AoS ROM (before touching files)."""
    if len(rom_bytes) != ROM_SIZE:
        raise InstallError(
            f"ROM is {len(rom_bytes)} bytes, expected exactly {ROM_SIZE} (8 MiB). "
            "Select the patched .gba produced from your .apcvaos file.")
    if rom_bytes[_ROM_TITLE_OFFSET:_ROM_TITLE_OFFSET + len(_ROM_TITLE)] != _ROM_TITLE:
        raise InstallError("Not an Aria of Sorrow ROM (bad header title).")
    ident = rom_bytes[ARCHIPELAGO_IDENTIFIER_START:
                      ARCHIPELAGO_IDENTIFIER_START + len(ARCHIPELAGO_IDENTIFIER)]
    if ident != ARCHIPELAGO_IDENTIFIER.encode("ascii"):
        if ident.startswith(_AP_MARKER_PREFIX):
            raise InstallError(
                f"ROM was patched by a different world version ({ident.decode('ascii', 'replace')}, "
                f"this world writes {ARCHIPELAGO_IDENTIFIER}). Re-patch with the matching version.")
        raise InstallError(
            "ROM is not Archipelago-patched (no CVAOS_AP marker). Patch your .apcvaos "
            "first and select the resulting .gba.")
    for name, (start, end) in (("audio bridge", _M2_AUDIO_BRIDGE),
                               ("replacement graphic", _M2_GRAPHIC)):
        if not any(rom_bytes[start:end]):
            raise InstallError(
                "ROM was patched from a cart dump, not from the ROM extracted from the "
                f"collection — M2's {name} is missing, so the collection would break. "
                "Extract the collection's ROM (cac_archive.py extract), set it as your "
                "patch base, and re-patch.")


def collection_running() -> bool:
    """Best effort: True only if the real collection's game.exe is attachable right now."""
    try:
        from ..collection_client import CollectionError, GameProcess
    except Exception:
        return False  # no pymem/non-Windows: fall back to file locks catching it
    try:
        proc = GameProcess.attach()
    except CollectionError:
        return False
    proc.close()
    return True


# ---------------------------------------------------------------------------
# Backup / restore
# ---------------------------------------------------------------------------

def ensure_backup(windata: str, log: Log = print) -> bool:
    """Create pristine *.apbackup copies of both archive files, exactly once.

    Returns True if the backups were created by this call. A mixed state (only
    one backup present) aborts: the pair must stay consistent with each other.
    """
    have = {name: os.path.isfile(os.path.join(windata, name + BACKUP_SUFFIX))
            for name in WINDATA_FILES}
    if all(have.values()):
        log("pristine backups already present — leaving them untouched")
        return False
    if any(have.values()):
        present = ", ".join(n + BACKUP_SUFFIX for n, v in have.items() if v)
        raise InstallError(
            f"Inconsistent backup state: only {present} exists. Verify the game files in "
            "Steam (restores stock), delete the stray backup, and run the installer again.")
    for name in WINDATA_FILES:
        src = os.path.join(windata, name)
        dst = src + BACKUP_SUFFIX
        log(f"backing up {name} -> {os.path.basename(dst)}")
        tmp = dst + ".tmp"
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    return True


def restore_backup(exe_path: str, log: Log = print) -> str:
    """Copy the pristine backups over the live files (backups are kept)."""
    windata = windata_dir_from_exe(exe_path)
    if collection_running():
        raise InstallError("The Castlevania Advance Collection is running — close it first.")
    missing = [name + BACKUP_SUFFIX for name in WINDATA_FILES
               if not os.path.isfile(os.path.join(windata, name + BACKUP_SUFFIX))]
    if missing:
        raise InstallError(
            f"No backup to restore ({', '.join(missing)} not found). If the archive needs "
            "resetting, use Steam's \"Verify integrity of game files\".")
    try:
        for name in WINDATA_FILES:
            live = os.path.join(windata, name)
            log(f"restoring {name} from backup")
            tmp = live + ".tmp"
            shutil.copy2(live + BACKUP_SUFFIX, tmp)
            os.replace(tmp, live)
    except OSError as exc:
        raise InstallError(f"Restore failed ({exc}). If this is a permissions problem, "
                           "run as administrator.") from exc
    return f"Restored the stock archive in {windata} (backups kept)."


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

def _apply_patch_file(patch_path: str, log: Log = print) -> str:
    """Apply a ``.apcvaos`` with AP's own machinery; returns the produced ``.gba`` path.

    The patch itself decides where the base ROM comes from (``target_platform`` in its
    rom_config.json): an advance_collection-target patch pulls the base out of the installed
    collection automatically, so the player only ever handles the patch file.
    """
    log(f"applying {os.path.basename(patch_path)}...")
    import Patch  # deferred: full AP import chain, only needed for this path
    _meta, out_path = Patch.create_rom_file(patch_path)
    log(f"patched ROM written to {out_path}")
    return out_path


def install_rom(rom_path: str, exe_path: str, log: Log = print) -> str:
    """Install *rom_path* — a patched ``.gba`` **or** a ``.apcvaos`` patch (applied first) —
    as the collection's AoS ROM. Returns a user-facing summary."""
    windata = windata_dir_from_exe(exe_path)
    if rom_path.lower().endswith(".apcvaos"):
        rom_path = _apply_patch_file(rom_path, log)
    try:
        with open(rom_path, "rb") as f:
            rom_bytes = f.read()
    except OSError as exc:
        raise InstallError(f"Could not read {rom_path!r}: {exc}") from exc
    validate_patched_rom(rom_bytes)
    if collection_running():
        raise InstallError("The Castlevania Advance Collection is running — close it first.")

    live_bin = os.path.join(windata, "alldata.bin")
    live_psb = os.path.join(windata, "alldata.psb.m")
    free = shutil.disk_usage(windata).free
    needed = os.path.getsize(live_bin) + (64 << 20)  # rebuilt copy + slack
    if free < needed:
        raise InstallError(f"Not enough free disk space on the game drive: the rebuild "
                           f"needs ~{needed >> 20} MiB, {free >> 20} MiB free.")

    ensure_backup(windata, log)

    # Stage under the real filenames: the MDF key is derived from the basename, and
    # os.replace within the same directory is atomic on the same volume.
    staging = os.path.join(windata, f"_ap_staging_{os.getpid()}")
    try:
        os.makedirs(staging, exist_ok=True)
        out_bin = os.path.join(staging, "alldata.bin")
        out_psb = os.path.join(staging, "alldata.psb.m")
        log("rebuilding alldata.bin with the patched ROM (this can take a minute)...")
        cac_archive.replace_member(live_psb, live_bin, AOS_MEMBER, rom_bytes,
                                   out_psb, out_bin)
        if cac_archive.extract_member(out_psb, out_bin, AOS_MEMBER) != rom_bytes:
            raise InstallError("Verification failed: the rebuilt archive does not round-trip "
                               "the ROM. Nothing was changed — please report this bug.")
        # The AoS member is MDF-compressed, so its stored size (and every later member's
        # offset) changes with the ROM's content: the two files only agree as a pair. Payload
        # first, so the disagreement window is the single os.replace of the index.
        os.replace(out_bin, live_bin)
        os.replace(out_psb, live_psb)
    except InstallError:
        raise
    except (OSError, ValueError, KeyError) as exc:
        raise InstallError(f"Install failed ({exc}). Nothing should be half-written — the "
                           "live files are only touched by atomic replaces. If this is a "
                           "permissions problem, run as administrator.") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return (f"Installed {os.path.basename(rom_path)} into {windata}.\n"
            f"Stock files are kept as {WINDATA_FILES[0]}{BACKUP_SUFFIX} / "
            f"{WINDATA_FILES[1]}{BACKUP_SUFFIX}; use the installer's restore option to go "
            f"back.\n{_STEAM_VERIFY_NOTE}")


# ---------------------------------------------------------------------------
# Launcher component entry point
# ---------------------------------------------------------------------------

def _exe_path_from_settings() -> str:
    # Triggers AP's browse dialog on first use; md5-validated (CVAOSSettings.CollectionExePath).
    from .. import CVAOSWorld
    return str(CVAOSWorld.settings.collection_exe)


def run_from_launcher(*args: str) -> None:
    """Body of the "CVAoS Collection ROM Installer" component (runs in the Launcher).

    ``args`` may carry a patched .gba path or ``--restore``; with no args the user is
    prompted for the ROM with a file dialog.
    """
    import logging

    from Utils import messagebox, open_filename

    log = logging.getLogger("CVAoSCollectionInstaller").info
    try:
        try:
            exe_path = _exe_path_from_settings()
        except ValueError:
            messagebox("Wrong file", "Selected file did not match the Castlevania Advance "
                       "Collection's game.exe. Please try again.", True)
            return
        if "--restore" in args:
            summary = restore_backup(exe_path, log)
        else:
            rom_path = next((a for a in args if not a.startswith("--")), None)
            if not rom_path:
                rom_path = open_filename(
                    "Select your CVAoS patch (.apcvaos) or patched ROM (.gba)",
                    (("CVAoS patch or patched ROM", (".apcvaos", ".gba")),))
            if not rom_path:
                return  # user cancelled
            summary = install_rom(rom_path, exe_path, log)
    except InstallError as exc:
        messagebox("CVAoS Collection ROM Installer", str(exc), True)
        return
    log(summary)
    messagebox("CVAoS Collection ROM Installer", summary)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description="Install a patched CVAoS ROM into the Steam Castlevania Advance Collection.")
    parser.add_argument("rom", nargs="?",
                        help=".apcvaos patch or patched .gba (omit with --restore/--extract-base)")
    parser.add_argument("--exe", default=AC_DEFAULT_EXE_PATH,
                        help="path to the collection's game.exe (default: standard Steam path)")
    parser.add_argument("--restore", action="store_true",
                        help="restore the stock archive from the *.apbackup files")
    parser.add_argument("--extract-base", metavar="OUT_GBA",
                        help="write the collection's stock (unpatched) AoS ROM to OUT_GBA and exit")
    args = parser.parse_args(argv)
    if args.extract_base:
        print(extract_base_rom_to(args.extract_base, args.exe))
    elif args.restore:
        print(restore_backup(args.exe))
    elif args.rom:
        print(install_rom(args.rom, args.exe))
    else:
        parser.error("a patched .gba is required unless --restore or --extract-base is given")


if __name__ == "__main__":
    main()
