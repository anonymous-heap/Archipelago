"""
Scale enemies' soul and common-item drop chances by Enemy Drop Rate's percents.

``rom_config.json`` holds two percents for each enemy row the option changes, under
``enemy_drop_rates``: one for the soul and one for the common drop (slot 1). The world decides them
at generation (``enemy_drop_rate.py``). The patch applies them in its last step, after every other
feature has set the row's bytes, and scales each byte as it then stands.

Both chances have the same form (``rom/enemy_table.py``). A soul's denominator is
``rate*8 + 32``, which is ``8 * (rate + 4)``, and an item's is ``rarity*4 + 16``, which is
``4 * (rarity + 4)``. So for both, the chance ratio between two byte values is
``(old + 4) / (new + 4)``, and :func:`.soul_drop_rates.scaled_rate` inverts it for either byte. A
byte of 0 stays 0, and no other byte falls below 1.

Scaling the bytes in place keeps what the earlier features decided. A scripted boss keeps its soul
rate of 0, so its soul never gains a random roll. An ordinary enemy that holds a boss's soul keeps
a nonzero rate, so that soul can still drop.
"""
from __future__ import annotations

from typing import Dict, Mapping, Sequence

from .._bytemaker_compat import Patch
from .address_space import gba_space
from .enemy_table import ENEMY_COUNT, ENEMY_TABLE
from .soul_drop_rates import scaled_rate

# The row fields that the two percents scale, in the order rom_config.json lists the percents.
SCALED_FIELDS = ("soul_rate", "drop_1_rarity")


def build_writes(rom: bytes, percents: Mapping[str, Sequence[int]]) -> Dict[int, bytes]:
    """
    Return the writes that scale the soul rate and the common drop's rarity of each listed row.

    Args:
        rom: the ROM as every earlier feature left it.
        percents: ``{enemy id: [soul percent, common drop percent]}``, with each id as a string,
            as rom_config.json stores it. A row that is not listed keeps both bytes.

    Returns ``{file offset: bytes}`` for the bytes that change. Raises ValueError for an enemy id
    outside the table.
    """
    enemies = ENEMY_TABLE.bind(gba_space(rom))
    patch = Patch(name="enemy drop rates")
    for key, row_percents in percents.items():
        enemy_id = int(key)
        if not 0 <= enemy_id < ENEMY_COUNT:
            raise ValueError(f"enemy_drop_rates names enemy {enemy_id}, which the table lacks")
        row = enemies.item(enemy_id)
        for name, percent in zip(SCALED_FIELDS, row_percents, strict=True):
            byte = row.field(name)
            old: int = byte.read()
            new = scaled_rate(old, percent)
            if new != old:
                byte.write(new, expect=old, patch=patch)
    return {edit.offset: edit.new for edit in patch.edits}
