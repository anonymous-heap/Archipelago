"""
Change how often enemies drop their souls.

:func:`build_writes` makes three progression souls (Manticore, Devil and Curly) about four times
as likely to drop. :func:`build_multiplier_writes` scales every enemy's soul-drop chance by a
percentage.

The enemy table is a flat array at GBA 0x080E9644, with 113 entries of 0x24 (36) bytes each,
indexed by enemy id (Bat..Chaos). The enemy-death routine derives the soul-drop chance from the
byte at entry +0x12:

    chance ~= numerator / (rate*8 + 32 - LCK/16)      (denominator clamped to a floor of 16)

where ``rate`` is that +0x12 byte. The numerator is 3 if the soul is already owned and 6 if not,
and Hard Mode and the Soul Eater Ring raise it.
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

# The rate byte enters the chance's denominator as ``rate*8 + 32``, i.e. ``8 * (rate + 4)``,
# and LCK/16 is subtracted from that. The numerator does not depend on the rate, so it cancels in
# the ratio between two rates. While LCK is below 16, LCK/16 is 0, so the ratio is exactly
# ``(old + 4) / (new + 4)``. Higher LCK makes the real ratio somewhat larger (32->5 is about 4.2x
# at LCK 64). This constant comes from the exact ratio, which reproduces every multiplier
# SOUL_RATE_OVERRIDES documents (32->5 is 4.00x, 50->9 is 4.15x, 50->10 is 3.86x).
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

    Inverts ``(old + 4) / (new + 4)``. The new rate must be a whole number, and the multiplier
    varies with its reciprocal, so the nearest *rate* to the ideal is not always the nearest
    *multiplier*. For example, scaling rate 7 by 1.7x needs a new rate of 2.47. The nearer rate
    is 2, which gives 1.83x, but 3 gives 1.57x and so is closer to 1.7x. Both candidates are
    therefore compared by the multiplier they give, and a tie goes to the lower rate, which is
    the more common drop.

    A rate of 0 is left untouched, because the death routine skips the soul roll entirely for
    it. That is how the one-time bosses, whose own scripts award their souls, stay out of the
    generic drop. Scaling a 0 would switch a drop on rather than make one more common. For the
    same reason a nonzero rate never scales below 1: 0 would turn the drop off. So a rate can go
    no lower than 1, which multiplies the chance by ``(rate + 4) / 5``. An already-common soul
    therefore cannot be made much more common, however large ``percent`` is.
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
    Return the writes that scale *every* enemy's soul-drop chance by ``percent``%.

    Reads the rates out of ``base_rom`` rather than assuming vanilla, so this composes with
    both :func:`build_writes` and a soul shuffle that moved rates around: run it last and it
    scales whatever each enemy ended up with.

    Args:
        base_rom: the ROM as it stands *after* any earlier rate edits.
        percent: 100 leaves everything alone; 120 makes souls drop 1.2x as often.

    Returns ``{file offset: bytes}`` for the rates that change.
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
    Return the writes that apply the rate overrides in :data:`SOUL_RATE_OVERRIDES`.

    Args:
        base_rom: the ROM to patch. Each overridden +0x12 byte must still hold its expected
            vanilla value, or a ValueError is raised.

    Returns ``{file offset: bytes}``.
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
