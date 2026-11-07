"""
Each soul's rating and tier, from ``soul_tiers.csv``.

A rating runs from 0 to 1 and comes from the community soul tier lists, as ``build_soul_tiers.py``
explains. Tiers are cut at ratings 0.2, 0.4, 0.6 and 0.8 and named D, C, B, A and S. A soul rated
STRONG_RATING or more, which puts it in tier A or S, is strong.
"""

from __future__ import annotations

from typing import NamedTuple

from .._csv_resources import open_csv

__all__ = [
    "STRONG_RATING",
    "SoulTier",
    "by_name",
    "matched_group",
    "rows",
    "soul_rating",
]

STRONG_RATING = 0.6


class SoulTier(NamedTuple):
    soul: str
    colour: str          # "red", "blue", "yellow" or "ability"
    rating: float
    tier: str            # "D", "C", "B", "A" or "S"
    raters: int          # how many of the soul tier lists rank the soul
    rating_source: str   # "soul tier lists" or "desirability"


def _load() -> tuple[SoulTier, ...]:
    return tuple(SoulTier(row["soul"], row["colour"], float(row["rating"]), row["tier"],
                          int(row["raters"]), row["rating_source"])
                 for row in open_csv(__package__, "soul_tiers.csv"))


rows: tuple[SoulTier, ...] = _load()
by_name: dict[str, SoulTier] = {row.soul: row for row in rows}


def soul_rating(name: str) -> float:
    """Return the rating of the soul named ``name``, from 0 to 1. Raises KeyError for a non-soul."""
    return by_name[name].rating


def matched_group(name: str) -> tuple[str, str]:
    """Return the group that the soul named ``name`` trades places within under Soul Shuffle's
    ``matched``: its colour and its tier, such as ``("red", "B")``."""
    row = by_name[name]
    return row.colour, row.tier
