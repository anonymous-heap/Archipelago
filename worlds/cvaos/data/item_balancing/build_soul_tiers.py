"""
Builds ``soul_tiers.csv``: each soul's rating from 0 to 1, and its tier from D to S.

The rating comes from the community soul tier lists in ``soul_tier_lists/lists/``. Each rater
scores a soul ``1 - tier_index / (n_tiers - 1)``, so their top tier scores 1.0 and their bottom
tier 0.0, and a soul's rating is the mean over the raters who ranked it. A soul that fewer than
three raters ranked takes its desirability from ``desirability.csv`` instead, when it has one,
because those four raters ranked the movement souls that most soul lists leave out.

Tiers are cut at ratings 0.2, 0.4, 0.6 and 0.8, so a soul rated 0.6 or more is in tier A or S.

Run from anywhere: ``python build_soul_tiers.py``.
"""

from __future__ import annotations

import csv
import json
import statistics
from pathlib import Path
from typing import Dict, List

HERE = Path(__file__).resolve().parent
LISTS_DIR = HERE / "soul_tier_lists" / "lists"
DESIRABILITY_CSV = HERE / "desirability.csv"
ITEM_INFO_CSV = HERE.parent / "item_info" / "item_info.csv"
OUT_CSV = HERE / "soul_tiers.csv"

COLOURS = {"red_soul": "red", "blue_soul": "blue", "yellow_soul": "yellow",
           "ability_soul": "ability"}
# item_info.csv names a red soul Cagnazzo (id 55) that no enemy or pickup holds. The tier lists'
# Cagnazzo is the blue soul.
UNUSED_SOULS = {("red_soul", 55)}
MIN_RATERS = 3
TIER_CUTS = (0.2, 0.4, 0.6, 0.8)
TIER_NAMES = ("D", "C", "B", "A", "S")
DECIMALS = 4


def tier_of(rating: float) -> str:
    """Return the tier letter for ``rating``: D below 0.2, then C, B and A, and S from 0.8."""
    return TIER_NAMES[sum(1 for cut in TIER_CUTS if rating >= cut)]


def _souls() -> Dict[str, str]:
    """Return each soul's colour by its name, in item number order."""
    with ITEM_INFO_CSV.open(encoding="utf-8", newline="") as fh:
        return {row["name"]: COLOURS[row["item_category"]] for row in csv.DictReader(fh)
                if row["item_category"] in COLOURS
                and (row["item_category"], int(row["id"])) not in UNUSED_SOULS}


def rater_scores(souls: Dict[str, str]) -> Dict[str, List[float]]:
    """Return every rater's score for each soul, by soul name."""
    scores: Dict[str, List[float]] = {}
    unknown = set()
    for path in sorted(LISTS_DIR.glob("*.json")):
        tiers = json.loads(path.read_text(encoding="utf-8"))
        for index, tier in enumerate(tiers):
            for raw in tier["items"]:
                name = raw.strip()
                if name not in souls:
                    unknown.add(name)
                    continue
                scores.setdefault(name, []).append(1.0 - index / (len(tiers) - 1))
    if unknown:
        raise ValueError(
            f"the soul tier lists name souls that item_info.csv lacks: {sorted(unknown)}")
    return scores


def build() -> List[dict]:
    """Return every soul_tiers.csv row, in item number order."""
    souls = _souls()
    scores = rater_scores(souls)
    with DESIRABILITY_CSV.open(encoding="utf-8", newline="") as fh:
        desirability = {row["name"]: float(row["desirability"]) for row in csv.DictReader(fh)}
    rows = []
    for name, colour in souls.items():
        values = scores.get(name, [])
        if len(values) >= MIN_RATERS or (values and name not in desirability):
            rating, source = round(statistics.mean(values), DECIMALS), "soul tier lists"
        elif name in desirability:
            rating, source = desirability[name], "desirability"
        else:
            raise ValueError(f"no tier list rates {name}")
        rows.append(dict(soul=name, colour=colour, rating=rating, tier=tier_of(rating),
                         raters=len(values), rating_source=source))
    return rows


def write_csv(rows: List[dict]) -> None:
    with OUT_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    rows = build()
    write_csv(rows)
    print(f"wrote {len(rows)} souls -> {OUT_CSV.relative_to(HERE.parent.parent)}")


if __name__ == "__main__":
    main()
