"""
Each item's worth, from ``worth.csv``.

Worth runs from 0 to 1 on the scale of a vanilla run. An item is worth the point in the run where
the castle's own pickups first offer as much, so a worth of 0.25 means "no better than what the
pickups offer a quarter of the way in". ``build_worth.py`` writes the table, and the README in
this folder explains how.

Every item of ``item_info.csv`` except the souls has a row, and so does every gold amount. A soul's
rating is in ``soul_tiers.csv`` instead.
"""

from __future__ import annotations

from typing import NamedTuple, Optional

from .._csv_resources import open_csv

__all__ = [
    "CHEAP_WORTH",
    "MID_GAME_WORTH",
    "STRONG_WORTH",
    "ItemWorth",
    "by_name",
    "item_worth",
    "rows",
]

# The lines that the Experimental options compare worth with. An item worth CHEAP_WORTH or less
# may arrive anywhere in a run. Weapons and armor worth MID_GAME_WORTH or more are mid-game gear,
# and items worth STRONG_WORTH or more are strong.
CHEAP_WORTH = 0.25
MID_GAME_WORTH = 0.45
STRONG_WORTH = 0.6


class ItemWorth(NamedTuple):
    name: str
    category: str               # item_info's category, or "money" for gold
    item_class: str             # the ladder class, such as "sword", "armor" or "hp"
    class_fit_depth: Optional[float]  # None for an item with no ladder
    desirability: Optional[float]     # None for an item the tier lists leave out
    worth: float
    basis: str                  # "class ladder", "desirability", "default" or "fixed"


def _number(text: str) -> Optional[float]:
    return float(text) if text else None


def _load() -> tuple[ItemWorth, ...]:
    return tuple(
        ItemWorth(row["name"], row["category"], row["item_class"],
                  _number(row["class_fit_depth"]), _number(row["desirability"]),
                  float(row["worth"]), row["basis"])
        for row in open_csv(__package__, "worth.csv"))


rows: tuple[ItemWorth, ...] = _load()
by_name: dict[str, ItemWorth] = {row.name: row for row in rows}


def item_worth(name: str) -> float:
    """Return the worth of the item named ``name``, from 0 to 1.

    Raises KeyError for a name with no row, such as a soul or the Study Sealswitch.
    """
    return by_name[name].worth
