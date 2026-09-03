"""
Classicvania Movement (the "Classicvania Movement" option).

Port of the "Classicvania Movement v1" patch from Xanthus 
(https://github.com/Xanthus1/aos_patches) with their generous permission.

NES-style
movement with no air control. Jump trajectory is committed on takeoff; walking off a ledge drops
nearly straight down. Direction can still be changed with a double jump or Hippogryph, and the
flight passives (Flying Armor / Giant Bat) retain normal air control.

Two pieces, kept at the addresses the original patch uses (position-dependent THUMB blobs whose
literal pools bake absolute 0x087Dxxxx addresses; also clear of this world's own free-space
allocations at 0x661400..0x666xxx and 0x670000..0x671xxx, and of the Advance Collection ROM's
M2 additions at 0x660000-0x6610BC / 0x700000-0x7000E3):

1. The Xanthus update-hook framework, which now lives in rom/xanthus_framework.py because
   every per-frame hook has to share the one 0x08043104 pointer. That module installs the
   dispatcher and registers a slot; this feature owns slot 1.

2. The no-air-control hook itself, registered in slot 1 (list entry -> 0x087D0101) with its body at
   0x087D0100. Per frame it snapshots the player's X velocity (player entity at EWRAM 0x020004E4,
   velocity at +0x48) into scratch EWRAM at 0x0203E000 and, while midair without a flight passive
   (masks 0x100/0x200 at 0x02013260), pins the velocity so steering input does nothing. Divekicks
   clamp to +/-0x20000, and the double-jump/Hippogryph takeoff state re-reads the jump button
   (0x02000014 / config 0x0201339A) so a direction flip is still possible there.
"""
from __future__ import annotations

from typing import Dict

from . import xanthus_framework

HOOK_SLOT = 1

# The no-air-control function body at GBA 0x087D0100: 332 bytes of THUMB + an 11-word literal pool
# (scratch 0x0203E000, player entity 0x020004E4, passives 0x02013260, masks 0x100/0x200, status
# 0x020131B8, 0x400, buttons 0x02000014, jump config 0x0201339A, velocity clamps +/-0x20000).
_HOOK_BODY = bytes.fromhex(
    "ffb400b55148524c417800290bd101214170a36c4360227c0f231a400270a17a"
    "052211408170a27c40231a409a4202d1a36c436058e0227c0f231a40a37a072b"
    "04d1a36c00f076f843604de0062b32d0404b1b68404d2b40ab4202d1a36c4360"
    "42e03c4b1b683d4d2b40ab4202d1a36c436039e00178002902d1002a21d133e0"
    "002a31d0062a02d10178022913d0a17a072916d005290bd0217c802529400029"
    "0fd12f4909782023194099421cd019e08178052916d000f020f8002901d10023"
    "a364a36c217c80252940002907d1214d2d6824490d40002d01d100f02bf800f0"
    "11f843604168a1640270a17a05221140817002bc8e46ffbc70471b4909883023"
    "1940704706b4a27a052a11d0a27c40210a408a420cd0217c80252940002907d1"
    "114a5288114909881140002900d1002306bc7047002b04db0d4dab4205dd2b1c"
    "03e00c4dab4200dc2b1c704700e00302e4040002603201020001000000020000"
    "b831010200040000140000029a330102000002000000feff"
)

assert len(_HOOK_BODY) == 376, f"hook body must be 376 bytes, got {len(_HOOK_BODY)}"
assert (0x0203E000).to_bytes(4, "little") in _HOOK_BODY, "scratch EWRAM addr missing from hook body"
assert (0x020004E4).to_bytes(4, "little") in _HOOK_BODY, "player-entity addr missing from hook body"


def build_writes(base_rom: bytes) -> Dict[int, bytes]:
    """``{rom_file_offset: bytes}`` installing the no-air-control hook in slot 1 of the shared
    update-hook framework (rom/xanthus_framework.py, which validates the ROM state)."""
    return xanthus_framework.writes(base_rom, HOOK_SLOT, _HOOK_BODY)
