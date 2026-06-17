"""
Tests for the Skull Key -> warp ROM hook (rom/skull_key_warp.py).

The pure-byte tests always run. The emulation test executes the injected routine
together with the *real* ROM helper functions under Unicorn; it is skipped unless
both ``unicorn`` is installed and a USA base ROM is reachable (env ``CVAOS_ROM`` or
the path configured in host.yaml).
"""
from __future__ import annotations

import os
import struct
import unittest

from .rom import skull_key_warp as skw


def _find_rom() -> str | None:
    env = os.environ.get("CVAOS_ROM")
    if env and os.path.isfile(env):
        return env
    try:
        from settings import get_settings
        path = get_settings().cvaos_options.rom_file
        if path and os.path.isfile(path):
            return path
    except Exception:
        pass
    return None


class TestWrites(unittest.TestCase):
    def test_blob_is_intact(self):
        self.assertEqual(len(skw.WARPHOOK_BLOB), 100)

    def test_trampoline_targets_warphook(self):
        writes = skw.build_writes()
        tramp = writes[skw.HOOK_SITE_FILE_OFFSET]
        self.assertEqual(len(tramp), 8)
        # ldr r3,[pc,#0]; bx r3
        self.assertEqual(tramp[:4], bytes.fromhex("004b1847"))
        # literal == WarpHook base with the Thumb bit set
        self.assertEqual(struct.unpack("<I", tramp[4:])[0], skw.WARPHOOK_BASE_GBA | 1)

    def test_default_destination_matches_baked_record(self):
        # build_writes() with the default must reproduce the assembled blob exactly.
        self.assertEqual(skw.build_writes()[skw.WARPHOOK_BASE_FILE_OFFSET], skw.WARPHOOK_BLOB)

    def test_custom_destination_is_patched_in_place(self):
        dest = (0x08522222, 0x1234, 0x5678, -16, 48)
        blob = skw.build_writes(dest)[skw.WARPHOOK_BASE_FILE_OFFSET]
        rec = blob[skw.DEST_RECORD_OFFSET:skw.DEST_RECORD_OFFSET + 12]
        self.assertEqual(struct.unpack("<IHHhh", rec), dest)
        # only the 12 record bytes change; code and literal pool are untouched
        self.assertEqual(blob[:skw.DEST_RECORD_OFFSET], skw.WARPHOOK_BLOB[:skw.DEST_RECORD_OFFSET])
        self.assertEqual(blob[skw.DEST_RECORD_OFFSET + 12:], skw.WARPHOOK_BLOB[skw.DEST_RECORD_OFFSET + 12:])


class TestEmulation(unittest.TestCase):
    """Run WarpHook + real ROM helpers under Unicorn."""

    @classmethod
    def setUpClass(cls):
        try:
            import unicorn  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("unicorn not installed")
        cls.rom_path = _find_rom()
        if not cls.rom_path:
            raise unittest.SkipTest("base ROM not found (set CVAOS_ROM)")
        base = bytearray(open(cls.rom_path, "rb").read())
        for off, data in skw.build_writes().items():
            base[off:off + len(data)] = data
        # WarpHook calls sub_08047B44 (the game's "resume gameplay after menu" routine) to fix
        # enemy spawning. That function does DMA / BIOS-SWI / deep hardware-state work a bare
        # Unicorn harness can't emulate, so stub it to `bx lr` for the unit test -- we only verify
        # the warp *staging* here; sub_08047B44's effect is validated by an in-game playtest.
        base[0x47B44:0x47B46] = b"\x70\x47"  # bx lr
        cls.rom = bytes(base)

    def _make_uc(self):
        from unicorn import (Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_HOOK_MEM_READ_UNMAPPED,
                             UC_HOOK_MEM_WRITE_UNMAPPED, UC_HOOK_MEM_FETCH_UNMAPPED)
        uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
        uc.mem_map(0x08000000, 0x00800000)
        uc.mem_write(0x08000000, self.rom)
        uc.mem_map(0x02000000, 0x00040000)
        uc.mem_map(0x03000000, 0x00008000)
        uc.mem_map(0x04000000, 0x00010000)
        uc.mem_map(0x00000000, 0x00010000)

        def on_unmapped(uc, access, addr, size, value, ud):
            try:
                uc.mem_map(addr & ~0xFFFF, 0x10000)
            except Exception:
                pass
            return True

        uc.hook_add(UC_HOOK_MEM_READ_UNMAPPED | UC_HOOK_MEM_WRITE_UNMAPPED
                    | UC_HOOK_MEM_FETCH_UNMAPPED, on_unmapped)
        return uc

    def test_skull_key_triggers_warp(self):
        from unicorn.arm_const import (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_SP,
                                       UC_ARM_REG_LR)
        uc = self._make_uc()
        menu = 0x02030000
        uc.mem_write(menu + 0x17, bytes([25]))  # selected item = Skull Key
        uc.reg_write(UC_ARM_REG_R0, 0x02031000)
        uc.reg_write(UC_ARM_REG_R1, menu)
        uc.reg_write(UC_ARM_REG_SP, 0x00009F00)
        uc.reg_write(UC_ARM_REG_LR, 0x00008001)
        uc.emu_start(skw.WARPHOOK_BASE_GBA | 1, 0x00008000, count=2000)

        def rd16(a): return int.from_bytes(uc.mem_read(a, 2), "little")
        def rd32(a): return int.from_bytes(uc.mem_read(a, 4), "little")
        self.assertEqual(rd32(0x020003F8), skw.DEFAULT_DESTINATION[0])  # room_ptr
        self.assertEqual(rd16(0x020003FC), skw.DEFAULT_DESTINATION[1])  # x
        self.assertEqual(rd16(0x020003FE), skw.DEFAULT_DESTINATION[2])  # y
        self.assertEqual(uc.mem_read(0x02000064, 1)[0], 8)             # unk_64 -> transfer
        self.assertEqual(uc.mem_read(0x02000065, 1)[0], 0)             # unk_65 -> state 0
        self.assertEqual(uc.reg_read(UC_ARM_REG_R0), 1)               # return 1 == consume

    def test_other_item_passes_through(self):
        from unicorn.arm_const import (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_SP,
                                       UC_ARM_REG_LR, UC_ARM_REG_R4, UC_ARM_REG_PC)
        uc = self._make_uc()
        menu = 0x02030000
        uc.mem_write(menu + 0x17, bytes([22]))   # Potion
        uc.reg_write(UC_ARM_REG_R0, 0x02031000)
        uc.reg_write(UC_ARM_REG_R1, menu)
        uc.reg_write(UC_ARM_REG_SP, 0x00009F00)
        uc.reg_write(UC_ARM_REG_LR, 0x00008001)
        uc.emu_start(skw.WARPHOOK_BASE_GBA | 1, skw.HOOK_RESUME_GBA, count=2000)
        self.assertEqual(uc.reg_read(UC_ARM_REG_PC), skw.HOOK_RESUME_GBA)
        self.assertEqual(uc.reg_read(UC_ARM_REG_R4), 0x02031000)   # adds r4,r0,#0


if __name__ == "__main__":
    unittest.main()
