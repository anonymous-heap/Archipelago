"""
Which enemy drops which item in one player's seed.

Item Drop Shuffle's rules live in ``drop_shuffle.py``. This module applies them to a world:
:func:`travel_plan` reads the options that decide which drops move into the item pool.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .drop_shuffle import TravelPlan, plan_travel

if TYPE_CHECKING:
    from . import CVAOSWorld

__all__ = ["travel_plan"]


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
