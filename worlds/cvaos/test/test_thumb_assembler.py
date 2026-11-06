"""Every hook's .s source and its shipped blob must agree (rom/thumb_assembler.py).

Each hook in rom/ ships as a hex blob with the assembly it came from beside it. Nothing used to
connect the two, so editing a .s left the blob alone and the source quietly became fiction. These
tests assemble each source and compare, which turns that drift into a failure here.

Assembling needs keystone (``pip install keystone-engine``), a dev-time dependency, so the
correspondence tests skip without it. The assembler's own error handling is tested either way.
"""
from __future__ import annotations

import os
import unittest

import worlds.cvaos.rom.custom_pickups as custom_pickups
import worlds.cvaos.rom.deathlink_hook as deathlink_hook
import worlds.cvaos.rom.received_item_box as received_item_box
import worlds.cvaos.rom.skull_key_warp as skull_key_warp
import worlds.cvaos.rom.soul_guarantee_hook as soul_guarantee_hook
from worlds.cvaos.rom import thumb_assembler as asm

ROM_DIR = os.path.dirname(received_item_box.__file__)

#: source file -> (the GBA address it is linked at, the blob its module ships).
HOOKS = {
    "received_item_box.s": (received_item_box.HOOK_BODY_GBA, received_item_box._HOOK_BODY),
    "custom_pickups.s": (custom_pickups.CUSTOMHOOK_BASE_GBA, custom_pickups.CUSTOMHOOK_BLOB),
    "skull_key_warp.s": (skull_key_warp.WARPHOOK_BASE_GBA, skull_key_warp.WARPHOOK_BLOB),
    "deathlink_hook.s": (deathlink_hook.DEATHLINK_TRAMPOLINE.addr, deathlink_hook._TRAMPOLINE),
    "soul_guarantee_hook.s": (soul_guarantee_hook.GUARANTEE_TRAMPOLINE.addr,
                              soul_guarantee_hook._TRAMPOLINE),
}


def has_keystone() -> bool:
    try:
        import keystone       # noqa: F401
    except ImportError:
        return False
    return True


@unittest.skipUnless(has_keystone(), "keystone is not installed (pip install keystone-engine)")
class SourceMatchesBlobTest(unittest.TestCase):
    def test_every_blob_is_what_its_own_source_assembles_to(self):
        for name, (base, blob) in HOOKS.items():
            with self.subTest(source=name):
                built = asm.assemble_file(os.path.join(ROM_DIR, name), base)
                self.assertEqual(
                    built.hex(), blob.hex(),
                    f"{name} and the blob in its module disagree: rebuild one or the other",
                )

    def test_every_hook_has_a_source(self):
        # The .s files in rom/ must be exactly the sources HOOKS lists. A missing file means a
        # blob has no source, and an extra file means a source whose blob is never compared.
        sources = {f for f in os.listdir(ROM_DIR) if f.endswith(".s")}
        self.assertEqual(sources, set(HOOKS))

    def test_a_trampoline_bakes_its_own_resume_address(self):
        # The trampolines return through a label of their own, so their bytes depend on where
        # they are linked. That is why each module pins the address it was built for.
        base, blob = HOOKS["deathlink_hook.s"]
        moved = asm.assemble_file(os.path.join(ROM_DIR, "deathlink_hook.s"), base + 0x40)
        self.assertEqual(len(moved), len(blob))
        self.assertNotEqual(moved, blob)

    def test_the_announcement_hook_does_not(self):
        # Its pool holds addresses of things elsewhere in the ROM, none of its own, so the same
        # bytes are valid at any word-aligned address. The .s says so; this checks it.
        base, blob = HOOKS["received_item_box.s"]
        source = os.path.join(ROM_DIR, "received_item_box.s")
        for delta in (0x200, 0x1000, -0x100):
            with self.subTest(delta=delta):
                self.assertEqual(asm.assemble_file(source, base + delta), blob)


class AssemblerRefusesBadInputTest(unittest.TestCase):
    """A silently mis-assembled hook is worse than one that fails to build, so the cases this
    assembler does not implement have to raise. None of these reach keystone."""

    BASE = 0x08670000

    # (the problem, the source, its base address, a fragment of the error message)
    CASES = (
        ("unimplemented directive", ".thumb\n.macro foo\n", BASE,
         "unimplemented directive .macro"),
        ("undefined symbol", "    ldr r0, =NOWHERE\n    .pool\n", BASE, "NOWHERE"),
        ("circular .equ", ".equ A, B\n.equ B, A\n    ldr r0, =A\n    .pool\n", BASE, "circular"),
        ("literal with no pool", "    ldr r0, =0x1234\n", BASE, "pool"),
        ("duplicate label", ".Lx:\n.Lx:\n", BASE, "defined twice"),
        ("out-of-range branch", ".Lstart:\n" + "    .hword 0\n" * 200 + "    beq .Lstart\n", BASE,
         "out of range"),
        ("misaligned base", "    .hword 0\n", BASE + 1, "not halfword aligned"),
        ("label that is also an .equ", ".equ Dup, 1\nDup:\n    .hword 0\n", BASE,
         "both a label and an .equ"),
    )

    def test_bad_input_raises_and_names_the_problem(self):
        for problem, source, base, fragment in self.CASES:
            with self.subTest(problem):
                with self.assertRaises(asm.AsmError) as caught:
                    asm.assemble(source, base)
                self.assertIn(fragment, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
