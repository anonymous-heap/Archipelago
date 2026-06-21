"""
Aggregates the community tier lists in ``tier_jsons/`` into a single ``desirability.csv``.

Turns N raters' tier lists into per-item consensus *desirability* values along with
  cross-rater *disagreement*.

Orientation: **higher = more desirable**.
Per-rater desirability = ``1 - tier_index / (n_tiers - 1)`` in ``[0, 1]``,
so the rater's top tier is ``1.0`` and their worst is ``0.0``.

Currently, only the tier *level* is used. Ideally, the tier lists are
   also more finely ranked within each tier, but we don't currently
   use that fact.

Run from anywhere.
"""

from __future__ import annotations

import csv
import json
import re
import statistics
from pathlib import Path
from typing import Dict, List, NamedTuple

HERE = Path(__file__).resolve().parent
TIER_DIR = HERE / "tier_jsons"
ITEM_INFO_CSV = HERE.parent / "item_info" / "item_info.csv"
OUT_CSV = HERE / "desirability.csv"

# Tier-list spellings that don't match the canonical item_info names.
NAME_ALIASES: Dict[str, str] = {
    "Vijaya": "Vjaya",
}

# "Potion (+100HP)" / "Melon (+600HP)" / "Super Potion (+Full HP)" -> base consumable name.
_ANNOTATION = re.compile(r"\s*\(\+.*?\)\s*$")
# "100 Gold" / "2000 Gold" -> money; these have no item_info row (money is its own transfer).
_GOLD = re.compile(r"^\d+ Gold$")


def normalize(raw: str) -> str:
    name = _ANNOTATION.sub("", raw).strip()
    return NAME_ALIASES.get(name, name)


class Row(NamedTuple):
    name: str
    category: str
    desirability: float  # mean across raters; higher = more desirable = place later
    disagreement: float  # population stdev across raters; the per-item jitter width
    n_raters: int


def _load_canonical() -> Dict[str, str]:
    """name -> item_category, from the canonical item table."""
    with ITEM_INFO_CSV.open(encoding="utf-8") as fh:
        return {r["name"]: r["item_category"] for r in csv.DictReader(fh)}


def _category_for(name: str, canonical: Dict[str, str]) -> str:
    if name in canonical:
        return canonical[name]
    if _GOLD.match(name):
        return "money"
    return "UNKNOWN"  # should not happen once NAME_ALIASES covers the lists; flagged loudly below


def aggregate() -> List[Row]:
    canonical = _load_canonical()
    scores: Dict[str, List[float]] = {}

    for path in sorted(TIER_DIR.glob("*.json")):
        tiers = json.loads(path.read_text(encoding="utf-8"))
        n = len(tiers)
        if n < 2:
            raise ValueError(f"{path.name}: need >= 2 tiers to normalize, got {n}")
        for index, tier in enumerate(tiers):
            desirability = 1.0 - index / (n - 1)  # top tier -> 1.0, worst -> 0.0
            for raw in tier["items"]:
                scores.setdefault(normalize(raw), []).append(desirability)

    rows: List[Row] = []
    unknown: List[str] = []
    for name, vals in scores.items():
        category = _category_for(name, canonical)
        if category == "UNKNOWN":
            unknown.append(name)
        rows.append(Row(
            name=name,
            category=category,
            desirability=round(statistics.mean(vals), 4),
            disagreement=round(statistics.pstdev(vals) if len(vals) > 1 else 0.0, 4),
            n_raters=len(vals),
        ))

    if unknown:
        raise ValueError(
            "tier-list names with no canonical item_info row and not recognized as gold "
            f"(add to NAME_ALIASES): {sorted(unknown)}")

    rows.sort(key=lambda r: (-r.desirability, r.name))  # most desirable first
    return rows


def write_csv(rows: List[Row]) -> None:
    with OUT_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["name", "category", "desirability", "disagreement", "n_raters"])
        for r in rows:
            writer.writerow([r.name, r.category, r.desirability, r.disagreement, r.n_raters])


def main() -> None:
    rows = aggregate()
    write_csv(rows)
    canonical = _load_canonical()
    ranked_names = {r.name for r in rows}
    unranked = sorted(n for n in canonical if n not in ranked_names)
    print(f"wrote {len(rows)} ranked items -> {OUT_CSV.relative_to(HERE.parent.parent)}")
    print(f"{len(unranked)} canonical items are unranked (placement applies the neutral fallback)")
    print("\nmost desirable (place LATE):")
    for r in rows[:6]:
        print(f"  {r.desirability:.2f} +/-{r.disagreement:.2f} [{r.category}] {r.name}")
    print("least desirable (place EARLY):")
    for r in rows[-6:]:
        print(f"  {r.desirability:.2f} +/-{r.disagreement:.2f} [{r.category}] {r.name}")


if __name__ == "__main__":
    main()
