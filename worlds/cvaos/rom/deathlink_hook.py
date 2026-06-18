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
"""
from __future__ import annotations

from typing import Dict

from .entity import GBA_ROM_BASE

# --- placement (GBA addresses) ---
HOOK_SITE_GBA = 0x0801B9D0      # _0801B9D0; steals 4 insns: ldr r5,=gEwram; ldr r0,[r5]; ldr r4,=0x131B8; adds (8 bytes)
TRAMPOLINE_GBA = 0x08660040     # free ROM, just past AUTH_NUMBER (0x660010+16); clear of metadata
KILL_REQUEST_EWRAM = 0x0201324C  # client->hook flag; see ram.addresses.KILL_REQUEST

# Trampoline assembled from deathlink_hook.s at -Ttext=0x08660040 (THUMB, ARMv4T). 68 bytes:
# 18 instructions + a 6-word literal pool (KILL_REQUEST, sub_0801AF20, .Lresume+1, gEwramData ptr,
# 0x131B8, _0801B9D0+8). Verified byte-for-byte with capstone.
_TRAMPOLINE = bytes.fromhex(
    "0a490b78002b0bd0331c6d331b78332b06d000230b70301c054b06498e461847"
    "054d2868054c0019054908474c32010221af010861006608140b4f08b8310100d9b90108"
)


def _veneer() -> bytes:
    """The 8 bytes that overwrite the start of _0801B9D0:
    ``ldr r5,[pc,#0]; bx r5; .word TRAMPOLINE|thumb``. 0x0801B9D0 is word-aligned, so ``[pc,#0]``
    (PC&~3 = 0x0801B9D4) reads the inline target word right after ``bx r5``."""
    return bytes([0x00, 0x4D, 0x28, 0x47]) + (TRAMPOLINE_GBA | 1).to_bytes(4, "little")


# Sanity: catch an accidental edit of the blob/veneer before it ever reaches a ROM.
assert len(_TRAMPOLINE) == 68, f"trampoline must be 68 bytes, got {len(_TRAMPOLINE)}"
assert KILL_REQUEST_EWRAM.to_bytes(4, "little") in _TRAMPOLINE, "kill-request addr missing from blob"
assert len(_veneer()) == 8, "veneer must overwrite exactly the 8 stolen bytes"


def build_writes() -> Dict[int, bytes]:
    """``{rom_file_offset: bytes}`` installing the DeathLink kill hook: the veneer at the per-frame
    _0801B9D0 tail, and the trampoline in free ROM. Call only when the seed enables DeathLink."""
    return {
        HOOK_SITE_GBA - GBA_ROM_BASE: _veneer(),
        TRAMPOLINE_GBA - GBA_ROM_BASE: _TRAMPOLINE,
    }
