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

The install input may be a patched ``.gba`` or the ``.apcvaos`` patch itself. A patch is
applied first, against the stock ROM of the collection being installed into, whatever target
the seed was generated for, so a player handed only the patch file never touches an
intermediate ROM.

CLI (from the Archipelago repo root)::

    python -m worlds.cvaos.advance_collection.install seed.apcvaos [--exe GAME_EXE]
    python -m worlds.cvaos.advance_collection.install patched.gba  [--exe GAME_EXE]
    python -m worlds.cvaos.advance_collection.install --restore    [--exe GAME_EXE]
"""
from __future__ import annotations

import argparse
import contextlib
import filecmp
import hashlib
import os
import shutil
import zlib
from typing import Callable

from . import cac_archive
from .._bytemaker_compat import Entry
from ..constants import AC_DEFAULT_EXE_PATH, AC_USA_ROM_MD5
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
    except (OSError, ValueError, KeyError, zlib.error) as exc:
        raise InstallError(f"Could not extract the AoS ROM from {windata!r}: {exc}") from exc
    if len(rom) != ROM_SIZE:
        raise InstallError(
            f"Collection AoS ROM is {len(rom)} bytes, expected {ROM_SIZE} (8 MiB). "
            "The collection's archive may have changed — please report this.")
    if rom[_ROM_TITLE_OFFSET:_ROM_TITLE_OFFSET + len(_ROM_TITLE)] != _ROM_TITLE:
        raise InstallError(f"The member extracted from {windata!r} is not an Aria of Sorrow ROM.")
    marker = rom[ARCHIPELAGO_IDENTIFIER_START:ARCHIPELAGO_IDENTIFIER_START + len(_AP_MARKER_PREFIX)]
    if marker == _AP_MARKER_PREFIX:
        missing = ", ".join(name + BACKUP_SUFFIX for name in WINDATA_FILES
                            if not os.path.isfile(os.path.join(windata, name + BACKUP_SUFFIX)))
        problem = (f"the pristine backup is incomplete ({missing} missing)" if missing
                   else "so are its backups")
        raise InstallError(
            f"The AoS ROM in {windata!r} is already Archipelago-patched, and {problem}. Use "
            "Steam's \"Verify integrity of game files\" to restore the stock archive, then try "
            "again.")
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
                "ROM was patched from a cart dump, not from the collection's own ROM: M2's "
                f"{name} is missing, so the collection would break. Install the .apcvaos patch "
                "file itself instead, and the installer patches the collection's ROM.")


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
    # The backups are what restore puts back and where every later install and base ROM comes
    # from, so only the stock files may become them.
    live_rom = cac_archive.extract_member(os.path.join(windata, "alldata.psb.m"),
                                          os.path.join(windata, "alldata.bin"), AOS_MEMBER)
    if hashlib.md5(live_rom).hexdigest() != AC_USA_ROM_MD5:
        raise InstallError(
            "The collection's archive is not the stock one (its Aria of Sorrow ROM has been "
            "changed), so it cannot be kept as the backup. Use Steam's \"Verify integrity of "
            "game files\" to restore the stock files, then run the installer again.")
    # Both copies are made under temporary names first, and both backups are removed again if
    # the second cannot be moved into place, so a failure never leaves half a backup behind.
    copies: list[tuple[str, str]] = []
    created: list[str] = []
    try:
        for name in WINDATA_FILES:
            src = os.path.join(windata, name)
            dst = src + BACKUP_SUFFIX
            log(f"backing up {name} -> {os.path.basename(dst)}")
            # Listed before copying, so a copy that fails partway is removed too.
            copies.append((dst + ".tmp", dst))
            shutil.copy2(src, dst + ".tmp")
        for tmp, dst in copies:
            os.replace(tmp, dst)
            created.append(dst)
    except OSError:
        for dst in created:
            with contextlib.suppress(OSError):
                os.remove(dst)
        raise
    finally:
        for tmp, _dst in copies:
            with contextlib.suppress(OSError):
                os.remove(tmp)
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
    error = _restore_from_backups(windata, log)
    if error is not None and not all(_matches_backup(windata, name) for name in WINDATA_FILES):
        raise InstallError(
            f"Restore failed ({error}). The collection's files do not match each other, so do not "
            "play until they do: close anything that may be using them (the collection, an "
            "antivirus scan) and restore again, or use Steam's \"Verify integrity of game "
            "files\". If this is a permissions problem, run as administrator.") from error
    return f"Restored the stock archive in {windata} (backups kept)."


def _restore_from_backups(windata: str, log: Log) -> OSError | None:
    """Copy each backup over its live file, and return the first error, if any.

    Each file is restored even if the other one fails, and no temporary file is left behind.
    """
    error: OSError | None = None
    for name in WINDATA_FILES:
        live = os.path.join(windata, name)
        log(f"restoring {name} from backup")
        tmp = live + ".tmp"
        try:
            shutil.copy2(live + BACKUP_SUFFIX, tmp)
            os.replace(tmp, live)
        except OSError as exc:
            error = error or exc
        finally:
            with contextlib.suppress(OSError):
                os.remove(tmp)
    return error


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

def _apply_patch_file(patch_path: str, exe_path: str, log: Log = print) -> str:
    """Apply a ``.apcvaos`` with AP's own machinery; returns the produced ``.gba`` path.

    The base ROM is always the stock ROM of the collection at *exe_path*, whatever target the
    seed was generated for, because that is the only base the collection can run. A gba-target
    patch applies to it as well as to a cart dump.
    """
    log(f"applying {os.path.basename(patch_path)}...")
    import Patch  # deferred: full AP import chain, only needed for this path
    from ..rom.patch import base_rom_override
    with base_rom_override(extract_base_rom_bytes(exe_path)):
        try:
            _meta, out_path = Patch.create_rom_file(patch_path)
        except ValueError as exc:  # the base ROM check, shown as the installer's own message
            raise InstallError(str(exc)) from exc
    log(f"patched ROM written to {out_path}")
    return out_path


def _swap_failed_message(windata: str, exc: OSError, log: Log) -> str:
    """Restore both live files from the backups after the index swap failed, and return the error
    to show.

    The new payload is already live by then, and it does not match the old index, so every member
    after the AoS ROM would read from the wrong offset. Restoring both files from the backups
    returns the collection to its stock, consistent state. Each file is restored even if the other
    one fails. When a restore fails, the message depends on whether the files on disk still match
    the backups, which they do when the file that failed already held the stock bytes.
    """
    log("restoring the stock files after the failed install")
    restore_exc = _restore_from_backups(windata, log)
    if restore_exc is None or all(_matches_backup(windata, name) for name in WINDATA_FILES):
        return (f"Install failed ({exc}). The collection's files were restored to the stock game. "
                "Close anything that may be using them (the collection, an antivirus scan) and run "
                "the installer again.")
    return (f"Install failed ({exc}), and restoring the stock files also failed "
            f"({restore_exc}). The collection's files do not match each other, so run the "
            "Launcher's \"CVAoS Collection Restore\" or Steam's \"Verify integrity of game "
            "files\" before "
            "playing.")


def _matches_backup(windata: str, name: str) -> bool:
    """Whether the live file has the same bytes as its backup (False if either can't be read)."""
    live = os.path.join(windata, name)
    try:
        return filecmp.cmp(live, live + BACKUP_SUFFIX, shallow=False)
    except OSError:
        return False


def install_rom(rom_path: str, exe_path: str, log: Log = print) -> str:
    """Install *rom_path* — a patched ``.gba`` **or** a ``.apcvaos`` patch (applied first) —
    as the collection's AoS ROM. Returns a user-facing summary."""
    windata = windata_dir_from_exe(exe_path)
    if rom_path.lower().endswith(".apcvaos"):
        rom_path = _apply_patch_file(rom_path, exe_path, log)
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
    if not all(os.path.isfile(os.path.join(windata, name + BACKUP_SUFFIX)) for name in WINDATA_FILES):
        needed += os.path.getsize(live_bin) + os.path.getsize(live_psb)  # the first install backs up
    if free < needed:
        raise InstallError(f"Not enough free disk space on the game drive: the install "
                           f"needs ~{needed >> 20} MiB, {free >> 20} MiB free.")

    # Stage under the real filenames: the MDF key is derived from the basename, and
    # os.replace within the same directory is atomic on the same volume.
    staging = os.path.join(windata, f"_ap_staging_{os.getpid()}")
    # A killed install leaves its staging folder behind, holding up to a full archive copy.
    for entry in os.listdir(windata):
        if entry.startswith("_ap_staging_") and entry != os.path.basename(staging):
            shutil.rmtree(os.path.join(windata, entry), ignore_errors=True)
    try:
        ensure_backup(windata, log)
        os.makedirs(staging, exist_ok=True)
        out_bin = os.path.join(staging, "alldata.bin")
        out_psb = os.path.join(staging, "alldata.psb.m")
        log("rebuilding alldata.bin with the patched ROM (this can take a minute)...")
        # Rebuild from the stock backups rather than the live pair, so whatever state an earlier
        # install left the live files in never carries into this one.
        stock_psb, stock_bin = _stock_archive_pair(windata)
        cac_archive.replace_member(stock_psb, stock_bin, AOS_MEMBER, rom_bytes,
                                   out_psb, out_bin, index_key_name=INDEX_KEY_NAME)
        if cac_archive.extract_member(out_psb, out_bin, AOS_MEMBER) != rom_bytes:
            raise InstallError("Verification failed: the rebuilt archive does not round-trip "
                               "the ROM. Nothing was changed. Please report this bug.")
        # The AoS member is MDF-compressed, so its stored size (and every later member's
        # offset) changes with the ROM's content: the two files only agree as a pair. Payload
        # first, so the disagreement window is the single os.replace of the index.
        os.replace(out_bin, live_bin)
        try:
            os.replace(out_psb, live_psb)
        except OSError as exc:
            raise InstallError(_swap_failed_message(windata, exc, log)) from exc
    except InstallError:
        raise
    except (OSError, ValueError, KeyError, zlib.error) as exc:
        raise InstallError(f"Install failed ({exc}). The live files were not changed. If this "
                           "is a permissions problem, run as administrator.") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return (f"Installed {os.path.basename(rom_path)} into {windata}.\n"
            f"Stock files are kept as {WINDATA_FILES[0]}{BACKUP_SUFFIX} / "
            f"{WINDATA_FILES[1]}{BACKUP_SUFFIX}; use the Launcher's \"CVAoS Collection "
            f"Restore\" to go back.\n{_STEAM_VERIFY_NOTE}")


# ---------------------------------------------------------------------------
# Launcher component entry point
# ---------------------------------------------------------------------------

def _exe_path_from_settings() -> str | None:
    """Return the collection's game.exe path from host.yaml, and ask for it when no file is there.

    ``collection_exe`` is optional, so AP never asks for it by itself, and a Steam library outside
    the default location would otherwise end in a "no windata" error. The chosen file must match
    the collection's game.exe hash (a mismatch raises ``ValueError``), and the answer is saved to
    host.yaml. Returns None when the player cancels the dialog.
    """
    from settings import get_settings

    from .. import CVAOSWorld
    options = CVAOSWorld.settings
    current = options.collection_exe
    if os.path.isfile(current):
        return str(current)
    chosen = current.browse()
    if chosen is None:
        return None
    options.collection_exe = chosen
    get_settings().save()
    return chosen.resolve()


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
        if exe_path is None:
            return  # the player cancelled the game.exe dialog
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
    except Exception as exc:  # noqa: BLE001 - the Launcher would otherwise only log it
        logging.getLogger("CVAoSCollectionInstaller").exception("installer failed")
        messagebox("CVAoS Collection ROM Installer", f"The installer failed: {exc}", True)
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
