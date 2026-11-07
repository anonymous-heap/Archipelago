"""
Which enemy drops which item in one player's seed.

Item Drop Shuffle's rules live in ``drop_shuffle.py``. This module applies them to a world.
:func:`travel_plan` reads the options that decide which drops move into the item pool, and
:func:`final_drop_table` decides the table that the patch writes (``rom/drop_table.py``).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Mapping, Optional, Tuple

from .drop_shuffle import DropSlot, TravelPlan, plan_drop_shuffle, plan_travel
from .seed_depth import enemy_seed_depths

if TYPE_CHECKING:
    from . import CVAOSWorld

__all__ = ["DropTable", "final_drop_table", "travel_plan"]

# Each enemy id's two drop slots, common first.
DropTable = Mapping[int, Tuple[DropSlot, DropSlot]]


def _wide_item_smoothing(world: CVAOSWorld) -> bool:
    """Whether Wide Item Smoothing is on, which moves more drops into the pool."""
    # Wide Item Smoothing has no option yet, so it is always off.
    return False


def travel_plan(world: CVAOSWorld) -> Optional[TravelPlan]:
    """Return which drops move into ``world``'s item pool, or None with Item Drop Shuffle off.

    The plan depends only on options that the slot data carries, so Universal Tracker's rebuild of
    the world gets the same pool.
    """
    options = world.options
    if not options.item_drop_shuffle:
        return None
    return plan_travel(options.enemy_checks.value, options.goal.value, _wide_item_smoothing(world))


def final_drop_table(world: CVAOSWorld) -> Optional[DropTable]:
    """Return the drop table that ``world``'s patch writes, or None to keep the game's own.

    With Item Drop Shuffle on, it is the table that the shuffle deals by seed depth. This is the
    one place that decides the final table, so any later step that changes drops belongs here.

    Reads the seed depths, so call it only after progression balancing.
    """
    options = world.options
    if not options.item_drop_shuffle:
        return None
    plan = plan_drop_shuffle(options.enemy_checks.value, options.goal.value,
                             _wide_item_smoothing(world), enemy_seed_depths(world))
    return plan.table
