"""
Skull Key -> warp consumable ROM hook for Castlevania: Aria of Sorrow.

Turns "use a Skull Key from the item menu" into a teleport to a fixed destination,
by intercepting the consumable-effect dispatcher and reusing the game's own warp
helpers. See ``worlds/cvaos/SKULL_KEY_WARP.md`` for the full design and the ROM
addresses this relies on.

Mechanism (all verified against the USA ROM):

* ``sub_0804B36C`` (GBA 0x0804B36C) is the consumable-effect dispatcher. The first
  8 bytes of its prologue are overwritten with a trampoline that long-jumps to the
  injected ``WarpHook`` routine in ROM free space.
* ``WarpHook`` reads the selected item index from ``menuState + 0x17``:
    - index 25 (Skull Key): stage a destination with ``sub_08011F44`` and start the
      transfer with ``sub_08011AD0`` (sets ``unk_64 = 8`` / ``unk_65 = 0``), then
      return 1 so the caller decrements the Skull Key (real consumable cost).
    - anything else: reconstruct the displaced prologue and resume vanilla code at
      0x0804B374.
* Because using an item leaves the menu open, ``sub_08047764`` returns 0 that frame,
  so ``GameModeInGameUpdate`` case 6 does *not* clobber ``unk_64``; the next frame the
  dispatcher runs case 8 (``sub_08011B2C``) and performs the transfer.

Verification status: the injected routine and the vanilla passthrough are unit-tested
under Unicorn against the real ROM helper functions (see ``test_skull_key_warp.py``).
The full in-game handoff (menu teardown visuals, landing) still wants an emulator
playtest. The blob is machine-assembled from ``skull_key_warp.s``; to regenerate::

    arm-none-eabi-as -mcpu=arm7tdmi -mthumb skull_key_warp.s -o /tmp/w.o
    arm-none-eabi-ld -Ttext=0x08660100 /tmp/w.o -o /tmp/w.elf
    arm-none-eabi-objcopy -O binary /tmp/w.elf /tmp/w.bin   # -> WARPHOOK_BLOB

Gating lives with the caller: ``rom/patch.py`` emits these writes when the "Skull Key Warp" option
is set, and the client floors the count when ``slot_data["skull_key_warp"]`` is set. This module
just builds the bytes. The Skull Key is also the AP placeholder for remote pickups
(``rom/patch.py`` ``_AP_PLACEHOLDER``); with the option on, those placeholders double as usable
warps, which is harmless (the client's count clamp keeps pickup detection working).
"""
from __future__ import annotations

import struct
from typing import Dict, Tuple

# --- Hook site: consumable-effect dispatcher sub_0804B36C ---
# File offset == GBA address - 0x08000000. We overwrite the first 8 bytes of the prologue
# (push {r4-r7,lr}; mov r7,r8; push {r7}; adds r4,r0,#0), which WarpHook reconstructs.
HOOK_SITE_FILE_OFFSET = 0x04B36C
HOOK_RESUME_GBA = 0x0804B374          # baked into the blob's literal pool

# --- Injected routine placement (ROM free space, past the AP metadata block) ---
WARPHOOK_BASE_GBA = 0x08660100
WARPHOOK_BASE_FILE_OFFSET = WARPHOOK_BASE_GBA - 0x08000000   # 0x660100

# Machine-assembled from skull_key_warp.s, linked at 0x08660100. Position-DEPENDENT:
# its literal pool bakes in absolute addresses (DestRecord, sub_08011F44+1, sub_08011AD0+1,
# 0x0804B375, &unk_60), so WARPHOOK_BASE_GBA must stay 0x08660100 unless reassembled.
WARPHOOK_BLOB = bytes.fromhex(
    "cb7d192b05d0f0b5474680b4041c104b184730b50f4c00f014f80f4c2068a188"
    "e2882389658920b40c4c00f00af801b00b480c4c00f005f80b4c192020700120"
    "30bd20479cef5008100100020000000075b30408457b040844016608451f0108"
    "60000002d11a0108430e0002"
)
assert len(WARPHOOK_BLOB) == 108

# NOTE: both "start with 1" and "never drop below 1" are handled by the CLIENT, not the ROM.
# ram/accessors.py ``cap_skull_keys(floor=1)`` runs every poll while in-game: it clamps the Skull
# Key count down (existing cap) AND up to 1 (the floor). The floor fires the instant a new game is
# in-game, so the player effectively starts with one and always has at least one. (An earlier ROM
# edit tried to add the Skull Key to a starting-items table, but that table (sUnk_080E0DE4) belongs
# to GameModeBossRushMenuUpdate, i.e. Boss Rush, not the normal new game, so it did nothing in real
# play. The normal Soma new game starts with an empty inventory.)

# The 12-byte destination record lives inside the blob (struct SkullKeyWarpDestination):
#   u32 room_ptr; u16 x; u16 y; s16 x_offset; s16 y_offset
DEST_RECORD_OFFSET = 0x44
_DEST_STRUCT = "<IHHhh"
assert struct.calcsize(_DEST_STRUCT) == 12

# Default prototype destination: room 000 (start corridor). The room pointer and the safe
# landing tuple are reused from an existing door transition (door 5 -> room 000) in
# data/entrance_info/entrance_info.csv, so the landing is guaranteed valid.
DEFAULT_DESTINATION: Tuple[int, int, int, int, int] = (0x0850EF9C, 272, 512, 0, 0)


def pack_destination(room_ptr: int, x: int, y: int, x_offset: int, y_offset: int) -> bytes:
    """Pack a destination into the 12-byte record the warp helper consumes."""
    return struct.pack(_DEST_STRUCT, room_ptr, x, y, x_offset, y_offset)


# --- Skull Key menu description (re-pointed so the text reflects the warp behavior) ---
# All game text resolves through one string-pointer array, sUnk_08506B38 @ GBA 0x08506B38 (one u32 per
# entry), indexed by a text-id. A
# consumable's *description* text-id is 0x08506936[consumable_index] == 348 + index; for the Skull Key
# (index 25) that is array index 373. Each string is `01 00` + ASCII (0x06 = line break) + `06 0a`
# (terminator). We write a new string into free space and repoint just that one array entry; nothing
# else moves. The description box fits up to 2 lines of <= 34 chars (measured from the vanilla strings).
# (File offset = 0x506B38 + 373*4 = 0x50710C. Earlier comments framed this as "table 0x08506C64,
#  entry 298 = idx25 + 273" -- that base/index pair is mislabeled but resolves to the same byte.)
DESC_TABLE_ENTRY_FILE_OFFSET = 0x50710C            # sUnk_08506B38[373] (Skull Key desc text-id), file offset
DESC_STRING_GBA = 0x08660200                       # new string in free space (clear of the blob)
DESC_STRING_FILE_OFFSET = DESC_STRING_GBA - 0x08000000
SKULL_KEY_DESCRIPTION_LINES: Tuple[str, ...] = (
    "Teleports to start. Opened",
    "skull-marked doors in another era.",
)


def _encode_menu_string(lines: Tuple[str, ...]) -> bytes:
    if len(lines) > 2 or any(len(line) > 34 for line in lines):
        raise ValueError("description box fits at most 2 lines of 34 chars")
    return b"\x01\x00" + b"\x06".join(line.encode("ascii") for line in lines) + b"\x06\x0a"


def _description_writes() -> Dict[int, bytes]:
    return {
        DESC_STRING_FILE_OFFSET: _encode_menu_string(SKULL_KEY_DESCRIPTION_LINES),
        DESC_TABLE_ENTRY_FILE_OFFSET: struct.pack("<I", DESC_STRING_GBA),
    }


def _trampoline_bytes(target_gba: int) -> bytes:
    # ldr r3,[pc,#0]; bx r3; .word target|1  (Thumb-1 far jump; r3 is dead at both hook entries)
    return bytes.fromhex("004b1847") + struct.pack("<I", target_gba | 1)


def build_writes(destination: Tuple[int, int, int, int, int] = DEFAULT_DESTINATION) -> Dict[int, bytes]:
    """
    Return ``{file_offset: bytes}`` for every ROM write that installs the Skull Key warp:
    the warp hook (trampoline + ``WarpHook`` blob with ``destination`` baked in) and the matching
    description. Caller writes these like any other patch token. ("Start with 1" and "floor at 1"
    are both handled by the client's ``cap_skull_keys(floor=1)``, not the ROM.)
    """
    blob = bytearray(WARPHOOK_BLOB)
    blob[DEST_RECORD_OFFSET:DEST_RECORD_OFFSET + 12] = pack_destination(*destination)
    writes = {
        # warp: use a Skull Key -> teleport
        HOOK_SITE_FILE_OFFSET: _trampoline_bytes(WARPHOOK_BASE_GBA),
        WARPHOOK_BASE_FILE_OFFSET: bytes(blob),
    }
    writes.update(_description_writes())   # repoint the Skull Key description to match the warp
    return writes
