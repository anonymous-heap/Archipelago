"""
Pins for rom/skull_key_pickup.py, which lets a Skull Key pickup be collected while the player
holds 9 Skull Keys.

The tests check the written bytes, their disassembly, and that apply_rom_features applies them to
every seed. With a USA ROM and capstone, they also step through the collect routine's consumable
branch to show which pickups it collects at the cap, with and without the patch.

    python -m pytest worlds/cvaos/test/test_skull_key_pickup.py -v
"""
from __future__ import annotations

import unittest

from ..ram import addresses as addr
from ..rom import patch as rom_patch
from ..rom import skull_key_pickup as skp
from ..rom.entity import GBA_ROM_BASE
from . import find_rom, run_apply_rom_features

try:
    import capstone
except ImportError:  # dev-only verifier; the byte pins below still run
    capstone = None

SITE = skp.CAP_CHECK.addr - GBA_ROM_BASE
POTION = 0

# The consumable branch of sub_080441F8, from its count test to the two places that test leads.
COUNT_TEST = 0x08044312   # cmp r1, #8, with r1 = owned count, r2 = item offset, r4 = entity
COLLECT = 0x08044322      # adds one below 9, shows the "Got <name>" box, writes the save flag
REFUSE = 0x0804443C       # plays sound 0x13B, clears the despawn bit, returns without the flag


def listing(blob: bytes, address: int) -> list[str]:
    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB)
    return [f"{i.mnemonic} {i.op_str}".strip() for i in md.disasm(blob, address)]


def destination(rom: bytes, item: int, count: int, entity_flags: int = 0) -> int:
    """Step from COUNT_TEST until control reaches COLLECT or REFUSE, and return which one.

    Only the instructions between those addresses are modelled. The entity sits at address 0, and
    its byte +0x0B holds ``entity_flags``.
    """
    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB)
    regs = {"r0": 0, "r1": count, "r2": item, "r4": 0}
    entity = bytearray(0x10)
    entity[0x0B] = entity_flags
    n = z = v = False
    pc = COUNT_TEST
    for _ in range(16):
        if pc in (COLLECT, REFUSE):
            return pc
        insn = next(md.disasm(rom[pc - GBA_ROM_BASE:pc - GBA_ROM_BASE + 4], pc, 1))
        ops = [op.strip(" []#") for op in insn.op_str.split(",")]
        value = lambda op: regs[op] if op in regs else int(op, 0)  # noqa: E731
        pc += insn.size
        taken = {"b": True, "beq": z, "bne": not z, "ble": z or n != v, "bmi": n}
        if insn.mnemonic in taken:
            pc = int(ops[0], 0) if taken[insn.mnemonic] else pc
        elif insn.mnemonic == "ldrb":
            regs[ops[0]] = entity[regs[ops[1]] + value(ops[2])]
        elif insn.mnemonic == "cmp":
            a, b = regs[ops[0]], value(ops[1])
            result = (a - b) & 0xFFFFFFFF
            n, z, v = bool(result >> 31), result == 0, bool(((a ^ b) & (a ^ result)) >> 31)
        elif insn.mnemonic in ("movs", "ands", "lsls"):
            if insn.mnemonic == "movs":
                result = value(ops[1])
            elif insn.mnemonic == "ands":
                result = regs[ops[0]] & regs[ops[1]]
            else:
                result = (regs[ops[1]] << value(ops[2])) & 0xFFFFFFFF
            regs[ops[0]] = result
            n, z = bool(result >> 31), result == 0
        else:
            raise AssertionError(f"unmodelled instruction at {insn.address:#x}: "
                                 f"{insn.mnemonic} {insn.op_str}")
    raise AssertionError("the branch reached neither COLLECT nor REFUSE")


class WritesTest(unittest.TestCase):
    def test_writes_replace_only_the_ten_byte_test(self):
        self.assertEqual(skp.build_writes(), {0x44316: bytes.fromhex("192a03d0e17a890700d4")})

    def test_the_exempt_item_is_the_placeholder_skull_key(self):
        self.assertEqual(skp.SKULL_KEY, addr.SKULL_KEY_CONSUMABLE_INDEX)
        self.assertEqual(rom_patch._AP_PLACEHOLDER, (4, 2, skp.SKULL_KEY))


@unittest.skipUnless(capstone, "capstone not installed")
class DisassemblyTest(unittest.TestCase):
    def test_vanilla_bytes_test_only_the_entity_flag(self):
        self.assertEqual(listing(skp.VANILLA, skp.CAP_CHECK.addr), [
            "ldrb r1, [r4, #0xb]", "movs r0, #2", "ands r0, r1", "cmp r0, #0", "bne #0x8044322",
        ])

    def test_patched_bytes_accept_the_skull_key_then_test_the_same_flag(self):
        self.assertEqual(listing(skp.build_writes()[SITE], skp.CAP_CHECK.addr), [
            "cmp r2, #0x19", "beq #0x8044322",
            "ldrb r1, [r4, #0xb]", "lsls r1, r1, #0x1e", "bmi #0x8044322",
        ])


class PipelineTest(unittest.TestCase):
    def test_every_seed_gets_the_patch(self):
        stubs = run_apply_rom_features(self, bytes(0x800000))
        rom = stubs["inventory_menu"].call_args.args[0]
        self.assertEqual(rom[SITE:SITE + 10], skp.PATCHED)


ROM_PATH = find_rom()


@unittest.skipUnless(ROM_PATH and capstone, "needs a USA ROM and capstone")
class CollectRoutineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(ROM_PATH, "rb") as fh:
            cls.vanilla = fh.read()
        patched = bytearray(cls.vanilla)
        for offset, data in skp.build_writes().items():
            patched[offset:offset + len(data)] = data
        cls.patched = bytes(patched)

    def test_the_base_rom_holds_the_replaced_instructions(self):
        self.assertEqual(self.vanilla[SITE:SITE + 10], skp.VANILLA)

    def test_the_refusal_returns_past_the_save_flag_write(self):
        # The finish that writes the save flag starts at 0x08044508 and falls through into the
        # epilogue at 0x08044584. The refusal jumps straight to that epilogue.
        start = REFUSE - GBA_ROM_BASE
        self.assertEqual(listing(self.vanilla[start:start + 0x20], REFUSE)[-1], "b #0x8044584")
        self.assertEqual(listing(self.vanilla[0x44584:0x44590], 0x08044584),
                         ["add sp, #4", "pop {r3}", "mov r8, r3", "pop {r4, r5, r6, r7}",
                          "pop {r0}", "bx r0"])

    def test_vanilla_refuses_a_skull_key_at_the_cap(self):
        self.assertEqual(destination(self.vanilla, skp.SKULL_KEY, 9), REFUSE)

    def test_a_skull_key_is_collected_at_any_count(self):
        for count in (0, 8, 9):
            with self.subTest(count=count):
                self.assertEqual(destination(self.patched, skp.SKULL_KEY, count), COLLECT)

    def test_other_consumables_keep_the_vanilla_rule(self):
        cases = {(8, 0): COLLECT, (9, 0): REFUSE, (9, 0x02): COLLECT, (9, 0xFD): REFUSE}
        for (count, flags), expected in cases.items():
            for rom in (self.vanilla, self.patched):
                with self.subTest(count=count, flags=flags, patched=rom is self.patched):
                    self.assertEqual(destination(rom, POTION, count, flags), expected)


if __name__ == "__main__":
    unittest.main()
