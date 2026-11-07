"""
Item desirability ratings from the community tier lists.

Loads ``desirability.csv``, which ``aggregate_tiers.py`` writes, into ``DesirabilityInfo`` rows.
``rows`` holds them in the file's order, most desirable first, and ``by_name`` maps each item name
to its row. ``desirability`` runs from 0 (least desirable) to 1, and ``disagreement`` is its
standard deviation across raters.

The package's other tables load from their own modules: ``worth`` holds each item's worth, and
``soul_tiers`` holds each soul's rating and tier.
"""

from __future__ import annotations

from ..._pydantic_compat import BaseModel, parse_obj_as
from .._csv_resources import open_csv


class DesirabilityInfo(BaseModel):
    name: str
    category: str
    desirability: float
    disagreement: float
    n_raters: int


def _load() -> list[DesirabilityInfo]:
    merged: list[dict] = []
    for row in open_csv(__name__, "desirability.csv"):
        if not any((v or "").strip() for v in row.values()):
            continue
        merged.append(
            {
                "name": row["name"],
                "category": row["category"],
                "desirability": float(row["desirability"]),
                "disagreement": float(row["disagreement"]),
                "n_raters": int(row["n_raters"]),
            }
        )
    return parse_obj_as(list[DesirabilityInfo], merged)


rows: tuple[DesirabilityInfo, ...] = tuple(_load())
by_name: dict[str, DesirabilityInfo] = {row.name: row for row in rows}

__all__ = ["DesirabilityInfo", "by_name", "rows"]
