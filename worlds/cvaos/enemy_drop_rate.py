"""
Enemy Drop Rate: how often each enemy drops its soul and its common item, in percent of the usual
chance.

The option's value applies per kind of enemy. A kind that the seed first reaches before seed depth
EARLY_DEPTH gets 100 plus a sixth of the boost, for the whole run, because the player meets it
early anyway. At 350 that is 142. Every other kind gets the full value. An enemy with no seed depth
counts as depth 1.0.

Within a kind, three drops keep their usual chance:

* a strong soul (rated STRONG_RATING or more) whose enemy the seed reaches before
  STRONG_SOUL_DEPTH;
* a common item worth MID_GAME_WORTH or more;
* every rare item, because the rare drop slot never scales.

The rule reads the soul that each row holds after the soul shuffle, so it follows every shuffle
mode. :func:`drop_rate_percents` returns the percents for ``rom_config.json``, and
``rom/enemy_drop_rates.py`` applies them after every other feature.
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING, Dict, List, Mapping, Optional

from .data import enemy_type_collection
from .data.item_balancing.soul_tiers import STRONG_RATING, soul_rating
from .data.item_balancing.worth import MID_GAME_WORTH, item_worth
from .seed_depth import UNREACHED, enemy_seed_depths
from .soul_sources import vanilla_soul_name

if TYPE_CHECKING:
    from . import CVAOSWorld

__all__ = ["EARLY_DEPTH", "EARLY_SHARE", "STRONG_SOUL_DEPTH", "drop_rate_percents", "kind_percent"]

EARLY_DEPTH = 0.25        # kinds reached before this get only EARLY_SHARE of the boost
EARLY_SHARE = 1 / 6
STRONG_SOUL_DEPTH = 0.35  # strong souls on enemies reached before this keep their chance


def kind_percent(value: int, depth: float) -> int:
    """Return the percent that a kind of enemy at seed depth ``depth`` gets, for the option's
    ``value``, rounded to a whole percent."""
    if depth < EARLY_DEPTH:
        return 100 + math.floor((value - 100) * EARLY_SHARE + 0.5)
    return value


def drop_rate_percents(world: CVAOSWorld, soul_plan: Mapping[int, int],
                       common_drops: Optional[Mapping[int, Optional[str]]] = None
                       ) -> Dict[str, List[int]]:
    """Return ``rom_config.json``'s ``enemy_drop_rates`` for ``world``.

    That is ``{enemy id: [soul percent, common drop percent]}``, keyed by the id as a string, and
    leaving out each row whose two percents are both 100. ``soul_plan`` is the seed's soul shuffle
    plan, ``{target enemy: source enemy}``. Returns an empty dict when the option is 100.

    ``common_drops`` names the item in each enemy's common slot, by enemy id, with None for an
    empty slot. Without it, the slots hold their vanilla items. A seed that deals new drops must
    pass the dealt items, because the exemption for items worth MID_GAME_WORTH or more reads the
    item that the slot will hold.

    Reads the seed depths, so call it only after progression balancing.
    """
    value = world.options.enemy_drop_rate.value
    if value == 100:
        return {}
    if common_drops is None:
        common_drops = {enemy.id: enemy.drops[0] or None for enemy in enemy_type_collection}
    depths = enemy_seed_depths(world)
    percents: Dict[str, List[int]] = {}
    for enemy in enemy_type_collection:
        depth = depths.get(enemy.id, UNREACHED)
        kind = kind_percent(value, depth)
        soul = vanilla_soul_name(soul_plan.get(enemy.id, enemy.id))
        keeps_soul_chance = soul is None or (soul_rating(soul) >= STRONG_RATING
                                             and depth < STRONG_SOUL_DEPTH)
        common = common_drops.get(enemy.id)
        keeps_item_chance = common is None or item_worth(common) >= MID_GAME_WORTH
        row = [100 if keeps_soul_chance else kind, 100 if keeps_item_chance else kind]
        if row != [100, 100]:
            percents[str(enemy.id)] = row
    return percents
