"""
The enemy table is a flat array at GBA 0x080E9644 (0x24=36) bytes per entry).

113 entries indexed by enemy id (Bat-Chaos).

The enemy-death routine derives the soul-drop chance from the byte at entry +0x12:

    chance ~= numerator / (rate*8 + 32 - LCK/16)      (denominator clamped to a floor of 16)

where `rate` is that +0x12 byte and the numerator is 3 if the soul is already owned, 6 if not,
more with the Soul Eater Ring
"""
from __future__ import annotations

from typing import Dict

from .entity import GBA_ROM_BASE

ENEMY_TABLE_GBA = 0x080E9644      # enemy table
ENEMY_STRIDE = 0x24               # bytes per enemy entry
SOUL_RATE_OFF = 0x12              # soul-rarity byte within an entry

# enemy_id -> (name, expected current +0x12 byte, new +0x12 byte)
SOUL_RATE_OVERRIDES: Dict[int, tuple[str, int, int]] = {
    54: ("Manticore", 32, 5),   # 32 -> 5  : ~4.00x
    48: ("Devil",     32, 5),   # 32 -> 5  : ~4.00x
    43: ("Curly",     50, 9),   # 50 -> 9  : ~4.15x (10 would be ~3.86x)
}


def _rate_offset(enemy_id: int) -> int:
    return (ENEMY_TABLE_GBA - GBA_ROM_BASE) + enemy_id * ENEMY_STRIDE + SOUL_RATE_OFF


def build_writes(base_rom: bytes) -> Dict[int, bytes]:
    """
    Writes the adjusted soul-drop rates into the ROM.

    Args: 
        base_rom: the original ROM bytes, used to verify the current +0x12 byte matches the expected value.

    Returns the written dict.
    """
    writes: Dict[int, bytes] = {}
    for enemy_id, (name, expected_old, new_rate) in SOUL_RATE_OVERRIDES.items():
        off = _rate_offset(enemy_id)
        if base_rom[off] != expected_old:
            raise ValueError(
                f"{name} soul-rate byte at {off:#x} is {base_rom[off]}, expected {expected_old} "
                f"(ROM mismatch)"
            )
        writes[off] = bytes([new_rate])
    return writes
