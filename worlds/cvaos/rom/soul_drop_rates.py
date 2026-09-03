"""
The enemy table is a flat array at GBA 0x080E9644 (0x24=36) bytes per entry).

113 entries indexed by enemy id (Bat-Chaos).

The enemy-death routine derives the soul-drop chance from the byte at entry +0x12:

    chance ~= numerator / (rate*8 + 32 - LCK/16)      (denominator clamped to a floor of 16)

where `rate` is that +0x12 byte and the numerator is 3 if the soul is already owned, 6 if not,
more with the Soul Eater Ring
"""
from __future__ import annotations

import math
from typing import Dict

from .._bytemaker_compat import Patch, PatchVerifyError, offset_of, sizeof
from .address_space import gba_space
from .enemy_table import ENEMY_COUNT, ENEMY_TABLE, EnemyDNA, field_offset
from .entity import GBA_ROM_BASE as GBA_ROM_BASE  # re-exported for callers that convert offsets

# Derived from the shared declaration (rom/enemy_table.py), kept as names for callers that
# compute offsets themselves.
ENEMY_TABLE_GBA = ENEMY_TABLE.addr
ENEMY_STRIDE = sizeof(EnemyDNA)
SOUL_RATE_OFF = offset_of(EnemyDNA, "soul_rate")

# The rate byte only enters the chance as ``rate*8 + 32``, i.e. ``8 * (rate + 4)``, so the
# ratio between two rates is exactly ``(old + 4) / (new + 4)`` -- LCK and the numerator
# cancel. That reproduces every multiplier SOUL_RATE_OVERRIDES documents (32->5 is 4.00x,
# 50->9 is 4.15x, 50->10 is 3.86x), which is where this constant comes from.
RATE_CHANCE_BIAS = 4

# enemy_id -> (name, expected current +0x12 byte, new +0x12 byte)
SOUL_RATE_OVERRIDES: Dict[int, tuple[str, int, int]] = {
    54: ("Manticore", 32, 5),   # 32 -> 5  : ~4.00x
    48: ("Devil",     32, 5),   # 32 -> 5  : ~4.00x
    43: ("Curly",     50, 9),   # 50 -> 9  : ~4.15x (10 would be ~3.86x)
}

def _rate_offset(enemy_id: int) -> int:
    """File offset of ``enemy_id``'s soul-rate byte, addressed through the table's layout."""
    return field_offset(enemy_id, "soul_rate")


def scaled_rate(rate: int, percent: int) -> int:
    """
    The rate byte that makes a soul closest to ``percent``% as likely to drop as ``rate`` does.

    Inverts ``(old + 4) / (new + 4)``. Only 256 multipliers exist for a given rate, and the
    mapping is reciprocal, so the nearest *rate* to the ideal is not always the nearest
    *multiplier* -- rate 25 at 2x wants 14.5, and while 14 is the nearer rate, 15 gives 1.93x
    against 14's 2.07x and so is the better answer. Both candidates are therefore compared in
    multiplier space, ties going to the more common drop.

    A rate of 0 is left untouched. The death routine skips the soul roll entirely for it (that
    is how the one-time bosses, whose own scripts award their souls, stay out of the generic
    drop), so scaling it would switch a drop on rather than make one more common. For the same
    reason a nonzero rate never scales below 1: 0 would turn the drop off. So the best a rate
    can become is 1, worth ``(rate + 4) / 5``, and an already-common soul cannot be sped up much
    however large ``percent`` is.
    """
    if rate == 0 or percent == 100:
        return rate

    wanted = percent / 100
    ideal = (rate + RATE_CHANCE_BIAS) / wanted - RATE_CHANCE_BIAS
    candidates = {max(1, min(0xFF, math.floor(ideal))), max(1, min(0xFF, math.ceil(ideal)))}

    def error(candidate: int) -> tuple[float, int]:
        realised = (rate + RATE_CHANCE_BIAS) / (candidate + RATE_CHANCE_BIAS)
        return abs(realised - wanted), candidate

    return min(candidates, key=error)


def build_multiplier_writes(base_rom: bytes, percent: int) -> Dict[int, bytes]:
    """
    Writes scaling *every* enemy's soul-drop rate by ``percent``%.

    Reads the rates out of ``base_rom`` rather than assuming vanilla, so this composes with
    both :func:`build_writes` and a soul shuffle that moved rates around: run it last and it
    scales whatever each enemy ended up with.

    Args:
        base_rom: the ROM as it stands *after* any earlier rate edits.
        percent: 100 leaves everything alone; 120 makes souls drop 1.2x as often.

    Returns the written dict.
    """
    if percent == 100:
        return {}

    enemies = ENEMY_TABLE.bind(gba_space(base_rom))
    patch = Patch(name=f"soul drop rates x{percent / 100:g}")
    for enemy_id in range(ENEMY_COUNT):
        rate = enemies.item(enemy_id).field("soul_rate")
        old: int = rate.read()
        new = scaled_rate(old, percent)
        if new != old:
            rate.write(new, expect=old, patch=patch)
    return {edit.offset: edit.new for edit in patch.edits}


def build_writes(base_rom: bytes) -> Dict[int, bytes]:
    """
    Writes the adjusted soul-drop rates into the ROM.

    Args:
        base_rom: the original ROM bytes, used to verify the current +0x12 byte matches the expected value.

    Returns the written dict.
    """
    enemies = ENEMY_TABLE.bind(gba_space(base_rom))
    patch = Patch(name="soul drop rates")
    for enemy_id, (name, expected_old, new_rate) in SOUL_RATE_OVERRIDES.items():
        try:
            enemies.item(enemy_id).field("soul_rate").write(new_rate, expect=expected_old, patch=patch)
        except PatchVerifyError as exc:
            off = _rate_offset(enemy_id)
            raise ValueError(
                f"{name} soul-rate byte at {off:#x} is {base_rom[off]}, expected {expected_old} "
                f"(ROM mismatch)"
            ) from exc
    return {edit.offset: edit.new for edit in patch.edits}
