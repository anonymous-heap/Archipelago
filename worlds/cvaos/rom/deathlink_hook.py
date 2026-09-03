"""
DeathLink "real kill" ROM hook for Castlevania: Aria of Sorrow.

Instead of zeroing CURRENT_HP and hoping the engine notices (the old ``kill_player`` hack, which
loses to HP regen and only resolves when the player next takes damage), this installs a tiny THUMB
trampoline so the BizHawk client can ask the game to run its *real* death routine.

How it works:

* AoS has no per-frame "if HP<=0 die" check -- death is driven by the damage routine, which only
  runs the player update's ``bl sub_0801AF20`` block while the damage-active guard
  gEwramData[0x131D6] != 0. So we must NOT hook that block (it would only fire on taking damage).
* We hook ``_0801B9D0`` (GBA 0x0801B9D0): the per-frame position/animation tail of the player
  update, reached every normal frame (the guard's else-branch ``b _0801B9D0``) and exactly where
  the engine's own death lands after ``bl sub_0801AF20``. There r6 = player entity.
* The trampoline checks the client's one-byte kill-request flag (EWRAM 0x0201324C, ``pad_1324C``,
  verified dead). If set (and not already dying), it clears the flag and calls the real death
  handler; then -- set or not -- it reproduces _0801B9D0's stolen prologue and resumes the tail,
  which plays the death animation the handler selected. So a received DeathLink kills Soma through
  the canonical routine, immediately and regen-proof, and normal play is untouched.

The trampoline bytes below are assembled from ``deathlink_hook.s`` at DEV time (arm-none-eabi +
``-Ttext=0x08660040``) and verified with capstone, so AP generation needs no toolchain. Regenerate
with the commands in that .s file if you edit it.

Placement is declared as two ``Entry`` objects. The hook site is bounded to the 8 stolen bytes and
the trampoline to its free-ROM reservation, so a blob that outgrows its slot refuses to build
instead of silently overwriting its neighbour, and the veneer's jump target is read from the
trampoline's own declaration rather than kept as a second copy of the address.
"""
from __future__ import annotations

from typing import Dict

from .._bytemaker_compat import Entry, Patch, UInt8, count, unknown
from .address_space import gba_space

# --- placement ---
HOOK_SITE = Entry(0x0801B9D0, UInt8, count(8), name="deathlink hook site",
                  note="_0801B9D0 per-frame tail; steals 4 insns: ldr r5,=gEwram; ldr r0,[r5]; "
                       "ldr r4,=0x131B8; adds (8 bytes)")
DEATHLINK_TRAMPOLINE = Entry(0x08660040, UInt8, unknown("deathlink_hook.s blob"), reserve=0xC0,
                             name="DEATHLINK_TRAMPOLINE",
                             note="free ROM just past AUTH_NUMBER (0x660010+16), clear of the AP "
                                  "metadata; the Skull Key WarpHook starts at 0x660100")
KILL_REQUEST_EWRAM = 0x0201324C  # client->hook flag; see ram.addresses.KILL_REQUEST

HOOK_SITE_GBA = HOOK_SITE.addr
TRAMPOLINE_GBA = DEATHLINK_TRAMPOLINE.addr

# Trampoline assembled from deathlink_hook.s at -Ttext=0x08660040 (THUMB, ARMv4T). 68 bytes:
# 18 instructions + a 6-word literal pool (KILL_REQUEST, sub_0801AF20, .Lresume+1, gEwramData ptr,
# 0x131B8, _0801B9D0+8). Verified byte-for-byte with capstone. Position-DEPENDENT: the pool bakes
# absolute addresses, so the blob only runs at DEATHLINK_TRAMPOLINE.addr.
_TRAMPOLINE = bytes.fromhex(
    "0a490b78002b0bd0331c6d331b78332b06d000230b70301c054b06498e461847"
    "054d2868054c0019054908474c32010221af010861006608140b4f08b8310100d9b90108"
)
_POOL = tuple(int.from_bytes(_TRAMPOLINE[i:i + 4], "little")
              for i in range(len(_TRAMPOLINE) - 24, len(_TRAMPOLINE), 4))

# Literal-pool invariants, stated against the entries: the blob's baked words must agree with the
# declarations it lives in, so an edit to either side fails here before it reaches a ROM.
assert len(_TRAMPOLINE) == 68, f"trampoline must be 68 bytes, got {len(_TRAMPOLINE)}"
assert KILL_REQUEST_EWRAM in _POOL, "kill-request addr missing from literal pool"
assert ((HOOK_SITE.addr + 8) | 1) in _POOL, "blob does not resume at the hook site's 9th byte -- hook site moved?"
assert any(DEATHLINK_TRAMPOLINE.addr <= (word & ~1) < DEATHLINK_TRAMPOLINE.addr + len(_TRAMPOLINE)
           for word in _POOL), \
    "blob's internal .Lresume literal is outside its own placement -- relink at the new -Ttext"


def _far_jump_veneer(target: Entry) -> bytes:
    """The 8 bytes that overwrite the start of _0801B9D0:
    ``ldr r5,[pc,#0]; bx r5; .word target|thumb``. 0x0801B9D0 is word-aligned, so ``[pc,#0]``
    (PC&~3 = 0x0801B9D4) reads the inline target word right after ``bx r5``. The word is taken
    from ``target``'s declaration, so the jump cannot drift from where the blob is placed."""
    return bytes([0x00, 0x4D, 0x28, 0x47]) + (target.addr | 1).to_bytes(4, "little")


def build_writes() -> Dict[int, bytes]:
    """``{rom_file_offset: bytes}`` installing the DeathLink kill hook: the veneer at the per-frame
    _0801B9D0 tail, and the trampoline in its free-ROM reservation. Call only when the seed enables
    DeathLink.

    Built against a geometry-only space -- the ROM is the player's, not ours -- so both writes are
    recorded blind; the entry bounds (``count(8)`` for the veneer, ``reserve=0xC0`` for the blob)
    still check the fit at build time.
    """
    space = gba_space()
    patch = Patch(name="deathlink kill hook")
    HOOK_SITE.bind(space).write(_far_jump_veneer(DEATHLINK_TRAMPOLINE), patch=patch)
    DEATHLINK_TRAMPOLINE.bind(space).write(_TRAMPOLINE, patch=patch)
    return {edit.offset: edit.new for edit in patch.edits}
