"""
Which enemy drops which soul.

The enemy table names each enemy's soul by two bytes: its type (0 red, 1 blue, 2 yellow,
3 ability) and its 1-based index within that type (``rom/soul_shuffle.py``). ``item_info.csv``
lists the same souls by category and 0-based id, so :func:`soul_name` converts the table's bytes
into a soul's name.
"""
from __future__ import annotations

from .data.item_info import by_category_id
from .rom.soul_shuffle import (SOUL_TYPE_ABILITY, SOUL_TYPE_BLUE, SOUL_TYPE_RED, SOUL_TYPE_YELLOW,
                               VANILLA)

__all__ = [
    "SOUL_CATEGORIES",
    "soul_name",
    "vanilla_soul_name",
]

# Each soul type byte, with the item_info.csv category of its souls.
SOUL_CATEGORIES = {
    SOUL_TYPE_RED: "red_soul",
    SOUL_TYPE_BLUE: "blue_soul",
    SOUL_TYPE_YELLOW: "yellow_soul",
    SOUL_TYPE_ABILITY: "ability_soul",
}


def soul_name(soul_type: int, soul_index: int) -> str:
    """Return the name of the soul that a table row's type byte and 1-based index byte name.

    Raises KeyError for an index of 0, which means the enemy drops no soul.
    """
    return by_category_id[(SOUL_CATEGORIES[soul_type], soul_index - 1)].name


def vanilla_soul_name(enemy_id: int) -> str | None:
    """Return the name of the soul that ``enemy_id`` drops in vanilla, or None if it drops none."""
    soul_type, soul_index, _rate = VANILLA[enemy_id]
    return soul_name(soul_type, soul_index) if soul_index else None
