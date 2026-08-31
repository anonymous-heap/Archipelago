"""
Item-desirability data.

Loads ``desirability.csv`` into ``DesirabilityInfo`` rows keyed by item name.

``disagreement`` is the cross-rater stddev.
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
