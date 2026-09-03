"""
Offline tests for the Advance Collection ROM installer (no Steam files touched).

A miniature windata/ pair (alldata.bin + encrypted alldata.psb.m index) is built with
cac_archive's own primitives in a temp dir, then ``install_rom``/``restore_backup`` run
against it for real. ``collection_running`` is stubbed so no process attach happens.

    python -m pytest worlds/cvaos/test/test_collection_install.py -v
"""
from __future__ import annotations

import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

from ..advance_collection import cac_archive, install
from ..advance_collection.install import (
    AOS_MEMBER, BACKUP_SUFFIX, ROM_SIZE, InstallError, extract_base_rom_bytes,
    extract_base_rom_to, install_rom, restore_backup, validate_patched_rom, windata_dir_from_exe,
)
from ..rom.patch import ARCHIPELAGO_IDENTIFIER, ARCHIPELAGO_IDENTIFIER_START

_OTHER_MEMBER = "system/roms/99_other_game.bin"
_OTHER_BYTES = b"\x11untouched-neighbor-member\x22" * 4
_STOCK_AOS_BYTES = b"stock collection AoS rom stand-in"  # size need not be 8 MiB in the archive


def _quiet(_msg: str) -> None:
    pass


def _make_patched_rom(marker: str = ARCHIPELAGO_IDENTIFIER, with_bridge: bool = True,
                      with_graphic: bool = True) -> bytes:
    rom = bytearray(ROM_SIZE)
    rom[0xA0:0xAC] = b"CASTLEVANIA2"
    rom[ARCHIPELAGO_IDENTIFIER_START:ARCHIPELAGO_IDENTIFIER_START + len(marker)] = \
        marker.encode("ascii")
    if with_bridge:
        rom[0x700000:0x700010] = b"\xEA" * 16  # M2 audio bridge stand-in
    if with_graphic:
        rom[0x660000:0x660010] = b"\x10" * 16  # M2 replacement graphic stand-in
    return bytes(rom)


def _make_stock_base_rom() -> bytes:
    """A valid 8 MiB unpatched collection base ROM (AoS header + M2's additions, no AP marker)."""
    rom = bytearray(ROM_SIZE)
    rom[0xA0:0xAC] = b"CASTLEVANIA2"
    rom[0x700000:0x700010] = b"\xEA" * 16
    rom[0x660000:0x660010] = b"\x10" * 16
    return bytes(rom)


def _build_fake_collection(root: str, aos_bytes: bytes = _STOCK_AOS_BYTES) -> str:
    """Create <root>/game.exe + <root>/windata/{alldata.bin, alldata.psb.m}; returns exe path."""
    windata = os.path.join(root, "windata")
    os.makedirs(windata)
    exe_path = os.path.join(root, "game.exe")
    with open(exe_path, "wb") as f:
        f.write(b"MZ fake collection exe")

    members = {AOS_MEMBER: aos_bytes, _OTHER_MEMBER: _OTHER_BYTES}
    file_info: dict[str, list[int]] = {}
    pos = 0
    with open(os.path.join(windata, "alldata.bin"), "wb") as f:
        for name, blob in members.items():
            start = cac_archive._align(pos)
            f.write(b"\0" * (start - pos))
            file_info[name] = [start, len(blob)]
            f.write(blob)
            pos = start + len(blob)
        f.write(b"\0" * (cac_archive._align(pos) - pos))

    index = cac_archive.psb_write({"id": "cvaos-test", "file_info": file_info})
    with open(os.path.join(windata, "alldata.psb.m"), "wb") as f:
        f.write(cac_archive.mdf_encrypt("alldata.psb.m", index))
    return exe_path


class _InstallerCase(unittest.TestCase):
    """Temp fake collection + collection_running stubbed to 'not running'."""

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="cvaos_install_test_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.exe_path = _build_fake_collection(self.root)
        self.windata = os.path.join(self.root, "windata")
        patcher = mock.patch.object(install, "collection_running", return_value=False)
        self.running_stub = patcher.start()
        self.addCleanup(patcher.stop)
        self.rom_path = os.path.join(self.root, "patched.gba")
        with open(self.rom_path, "wb") as f:
            f.write(_make_patched_rom())

    def _member(self, name: str) -> bytes:
        return cac_archive.extract_member(os.path.join(self.windata, "alldata.psb.m"),
                                          os.path.join(self.windata, "alldata.bin"), name)

    def _write_rom(self, rom_bytes: bytes) -> str:
        with open(self.rom_path, "wb") as f:
            f.write(rom_bytes)
        return self.rom_path


class InstallTest(_InstallerCase):
    def test_install_swaps_member_and_creates_backups(self) -> None:
        summary = install_rom(self.rom_path, self.exe_path, log=_quiet)
        self.assertEqual(self._member(AOS_MEMBER), _make_patched_rom())
        self.assertEqual(self._member(_OTHER_MEMBER), _OTHER_BYTES, "other members must copy through")
        for name in install.WINDATA_FILES:
            self.assertTrue(os.path.isfile(os.path.join(self.windata, name + BACKUP_SUFFIX)))
        self.assertIn("apbackup", summary)
        self.assertFalse([p for p in os.listdir(self.windata) if p.startswith("_ap_staging")],
                         "staging directory must be cleaned up")

    def test_backups_hold_stock_bytes_and_survive_second_install(self) -> None:
        install_rom(self.rom_path, self.exe_path, log=_quiet)
        backup_bin = os.path.join(self.windata, "alldata.bin" + BACKUP_SUFFIX)
        first_backup = open(backup_bin, "rb").read()
        # Second seed: different ROM content; backups must not be overwritten.
        rom2 = bytearray(_make_patched_rom())
        rom2[0x100000] = 0x77
        install_rom(self._write_rom(bytes(rom2)), self.exe_path, log=_quiet)
        self.assertEqual(open(backup_bin, "rb").read(), first_backup,
                         "pristine backup must never be overwritten")
        self.assertEqual(self._member(AOS_MEMBER), bytes(rom2))

    def test_refuses_while_collection_running(self) -> None:
        self.running_stub.return_value = True
        with self.assertRaisesRegex(InstallError, "running"):
            install_rom(self.rom_path, self.exe_path, log=_quiet)
        self.assertEqual(self._member(AOS_MEMBER), _STOCK_AOS_BYTES, "must not modify anything")

    def test_inconsistent_backup_state_aborts(self) -> None:
        install_rom(self.rom_path, self.exe_path, log=_quiet)
        os.remove(os.path.join(self.windata, "alldata.psb.m" + BACKUP_SUFFIX))
        with self.assertRaisesRegex(InstallError, "Inconsistent backup"):
            install_rom(self.rom_path, self.exe_path, log=_quiet)

    def test_missing_windata_raises(self) -> None:
        lone_exe = os.path.join(self.root, "elsewhere", "game.exe")
        os.makedirs(os.path.dirname(lone_exe))
        with open(lone_exe, "wb") as f:
            f.write(b"MZ")
        with self.assertRaisesRegex(InstallError, "windata"):
            windata_dir_from_exe(lone_exe)


class PatchFileInstallTest(_InstallerCase):
    """Handing the installer the .apcvaos itself: applied first, then installed."""

    def test_apcvaos_is_applied_then_installed(self) -> None:
        apc = os.path.join(self.root, "AP_1_P1_SomaAC.apcvaos")
        with open(apc, "wb") as f:
            f.write(b"patch container stand-in")
        produced = os.path.join(self.root, "AP_1_P1_SomaAC.gba")

        def fake_apply(path: str, log=_quiet) -> str:
            self.assertEqual(path, apc)
            with open(produced, "wb") as f:
                f.write(_make_patched_rom())
            return produced

        with mock.patch.object(install, "_apply_patch_file", side_effect=fake_apply):
            install_rom(apc, self.exe_path, log=_quiet)
        self.assertEqual(self._member(AOS_MEMBER), _make_patched_rom())

    def test_gba_input_skips_patch_application(self) -> None:
        with mock.patch.object(install, "_apply_patch_file") as apply_stub:
            install_rom(self.rom_path, self.exe_path, log=_quiet)
        apply_stub.assert_not_called()


class RomValidationTest(_InstallerCase):
    def test_rejects_wrong_size(self) -> None:
        with self.assertRaisesRegex(InstallError, "8 MiB"):
            install_rom(self._write_rom(b"\x00" * 0x400000), self.exe_path, log=_quiet)

    def test_rejects_non_aos_rom(self) -> None:
        rom = bytearray(_make_patched_rom())
        rom[0xA0:0xAC] = b"CASTLEVANIA1"
        with self.assertRaisesRegex(InstallError, "header title"):
            validate_patched_rom(bytes(rom))

    def test_rejects_unpatched_rom(self) -> None:
        rom = bytearray(_make_patched_rom())
        rom[ARCHIPELAGO_IDENTIFIER_START:ARCHIPELAGO_IDENTIFIER_START + 16] = bytes(16)
        with self.assertRaisesRegex(InstallError, "not Archipelago-patched"):
            validate_patched_rom(bytes(rom))

    def test_rejects_version_mismatch(self) -> None:
        with self.assertRaisesRegex(InstallError, "different world version"):
            validate_patched_rom(_make_patched_rom(marker="CVAOS_AP_V9.9"))

    def test_rejects_cart_based_patch(self) -> None:
        with self.assertRaisesRegex(InstallError, "audio bridge"):
            validate_patched_rom(_make_patched_rom(with_bridge=False))

    def test_rejects_patch_missing_the_graphic(self) -> None:
        with self.assertRaisesRegex(InstallError, "replacement graphic"):
            validate_patched_rom(_make_patched_rom(with_graphic=False))


class RestoreTest(_InstallerCase):
    def test_restore_returns_to_stock_and_keeps_backups(self) -> None:
        stock_bin = open(os.path.join(self.windata, "alldata.bin"), "rb").read()
        stock_psb = open(os.path.join(self.windata, "alldata.psb.m"), "rb").read()
        install_rom(self.rom_path, self.exe_path, log=_quiet)
        self.assertNotEqual(open(os.path.join(self.windata, "alldata.bin"), "rb").read(),
                            stock_bin)
        restore_backup(self.exe_path, log=_quiet)
        self.assertEqual(open(os.path.join(self.windata, "alldata.bin"), "rb").read(), stock_bin)
        self.assertEqual(open(os.path.join(self.windata, "alldata.psb.m"), "rb").read(), stock_psb)
        for name in install.WINDATA_FILES:
            self.assertTrue(os.path.isfile(os.path.join(self.windata, name + BACKUP_SUFFIX)),
                            "restore must keep the backups")

    def test_restore_without_backup_raises(self) -> None:
        with self.assertRaisesRegex(InstallError, "No backup"):
            restore_backup(self.exe_path, log=_quiet)

    def test_restore_refuses_while_running(self) -> None:
        install_rom(self.rom_path, self.exe_path, log=_quiet)
        self.running_stub.return_value = True
        with self.assertRaisesRegex(InstallError, "running"):
            restore_backup(self.exe_path, log=_quiet)


class ExtractBaseRomTest(unittest.TestCase):
    """Source the base ROM from an installed collection (hand it the exe location)."""

    def _collection(self, aos_bytes: bytes) -> str:
        root = tempfile.mkdtemp(prefix="cvaos_extract_test_")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        return _build_fake_collection(root, aos_bytes)

    def test_extracts_stock_base_rom(self) -> None:
        stock = _make_stock_base_rom()
        exe = self._collection(stock)
        self.assertEqual(extract_base_rom_bytes(exe), stock)

    def test_extract_to_writes_file(self) -> None:
        stock = _make_stock_base_rom()
        exe = self._collection(stock)
        out = os.path.join(os.path.dirname(exe), "base.gba")
        extract_base_rom_to(out, exe, log=_quiet)
        self.assertEqual(open(out, "rb").read(), stock)

    def test_rejects_wrong_size_member(self) -> None:
        exe = self._collection(_STOCK_AOS_BYTES)  # tiny, != 8 MiB
        with self.assertRaisesRegex(InstallError, "8 MiB"):
            extract_base_rom_bytes(exe)

    def test_rejects_non_aos_member(self) -> None:
        rom = bytearray(_make_stock_base_rom())
        rom[0xA0:0xAC] = b"NOTAOSGAME12"
        with self.assertRaisesRegex(InstallError, "not an Aria of Sorrow"):
            extract_base_rom_bytes(self._collection(bytes(rom)))

    def test_prefers_the_pristine_backups_once_a_seed_is_installed(self) -> None:
        stock = _make_stock_base_rom()
        exe = self._collection(stock)
        rom_path = os.path.join(os.path.dirname(exe), "patched.gba")
        with open(rom_path, "wb") as f:
            f.write(_make_patched_rom())
        with mock.patch.object(install, "collection_running", return_value=False):
            install_rom(rom_path, exe, log=_quiet)
        # The live archive now holds the seed; the base ROM must still be the stock one.
        self.assertEqual(extract_base_rom_bytes(exe), stock)

    def test_refuses_an_installed_seed_with_no_backup_to_fall_back_on(self) -> None:
        exe = self._collection(_make_patched_rom())  # patched member, no .apbackup pair
        with self.assertRaisesRegex(InstallError, "already Archipelago-patched"):
            extract_base_rom_bytes(exe)


class BaseRomSourceTest(unittest.TestCase):
    """get_base_rom_bytes(target): AC sources the collection; GBA uses rom_file (ROM or exe)."""

    def setUp(self) -> None:
        from ..rom import patch
        from ..options import TARGET_ADVANCE_COLLECTION, TARGET_GBA
        self.patch = patch
        self.TARGET_GBA = TARGET_GBA
        self.TARGET_AC = TARGET_ADVANCE_COLLECTION
        self.root = tempfile.mkdtemp(prefix="cvaos_baserom_test_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _patch_settings(self, collection_exe: str, rom_file: str) -> None:
        fake = types.SimpleNamespace(cvaos_options=types.SimpleNamespace(
            collection_exe=collection_exe, rom_file=rom_file))
        p = mock.patch.object(self.patch, "get_settings", return_value=fake)
        p.start()
        self.addCleanup(p.stop)

    def test_ac_target_sources_the_collection(self) -> None:
        stock = _make_stock_base_rom()
        exe = _build_fake_collection(self.root, stock)
        # rom_file points somewhere that must NOT be read for an AC-target seed.
        self._patch_settings(exe, os.path.join(self.root, "unused-cart.gba"))
        self.assertEqual(self.patch.get_base_rom_bytes(self.TARGET_AC), stock)

    def test_ac_target_falls_back_to_rom_file_without_a_collection(self) -> None:
        cart = os.path.join(self.root, "cart.gba")
        with open(cart, "wb") as f:
            f.write(b"an AC-extracted rom placed by hand")
        self._patch_settings(os.path.join(self.root, "no-such-game.exe"), cart)
        self.assertEqual(self.patch.get_base_rom_bytes(self.TARGET_AC),
                         b"an AC-extracted rom placed by hand")

    def test_gba_target_uses_rom_file_and_ignores_collection(self) -> None:
        stock = _make_stock_base_rom()
        exe = _build_fake_collection(self.root, stock)  # collection present...
        cart = os.path.join(self.root, "cart.gba")
        with open(cart, "wb") as f:
            f.write(b"a cart dump for BizHawk")
        self._patch_settings(exe, cart)  # ...but a gba seed must use rom_file
        self.assertEqual(self.patch.get_base_rom_bytes(self.TARGET_GBA), b"a cart dump for BizHawk")

    def test_gba_target_accepts_the_exe_and_extracts(self) -> None:
        # The user picks game.exe at the gba prompt: the ROM is extracted from its windata.
        stock = _make_stock_base_rom()
        exe = _build_fake_collection(self.root, stock)
        self._patch_settings("", exe)  # rom_file IS the exe
        self.assertEqual(self.patch.get_base_rom_bytes(self.TARGET_GBA), stock)

    def test_gba_is_the_default_target(self) -> None:
        cart = os.path.join(self.root, "cart.gba")
        with open(cart, "wb") as f:
            f.write(b"default-target cart")
        self._patch_settings(os.path.join(self.root, "no-such-game.exe"), cart)
        self.assertEqual(self.patch.get_base_rom_bytes(), b"default-target cart")


if __name__ == "__main__":
    unittest.main()
