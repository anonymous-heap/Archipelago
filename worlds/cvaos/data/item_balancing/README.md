# Item balancing — desirability smoothing

Make **less-useful items show up earlier and more-useful items later**, so a player isn't handed
something in sphere 1 that makes every later pickup boring — while keeping enough randomness that
you can still *get lucky*. This doc is the plan; the apworld wiring in **Stage 2 is not implemented
yet** (Stage 1, the aggregation script, is).

**Orientation, stated once so nothing downstream has to invert it:**
*higher desirability ⇒ place later.* Desirability runs `0.0` (junk, earliest) → `1.0` (best, latest),
and the placement target `t` ("lateness", `0` = first sphere, `1` = last) is simply `t = desirability`.

## The two axes

We rank items by community consensus on:

1. **"How much would this break things early in a run?"** (absolute power available too soon), and
2. **"How much would this make me unexcited to find new pickups past this?"** (obsolescence / ceiling).

Both point the same way — high desirability should arrive **late** — so we collapse them into one
desirability scalar. This is deliberately **separate from progression** (see
[below](#progression-vs-desirability)).

## Why tier lists instead of raw ATK

Raw weapon ATK can't tell you that Black Panther's dash or Satan's Ring trivialize the early game,
or that Valmanway/Claimh Solais outclass everything via multi-hit/element. The four community tier
lists in [`tier_jsons/`](tier_jsons/) encode exactly the two axes above and cover the whole loot
table (weapons, armor, accessories, souls, consumables), not just weapons.

Bonus: **cross-rater disagreement is a free luck signal.** Items everyone agrees on get tight
placement; contested items breathe (see [jitter](#per-item-jitter-long-tails-in-position-space)).

## Pipeline at a glance

| Stage | Input | Output | Status |
|---|---|---|---|
| 1 — Aggregate | [`tier_jsons/*.json`](tier_jsons/) + [`../item_info/item_info.csv`](../item_info/item_info.csv) | [`desirability.csv`](desirability.csv) | **Done** — [`aggregate_tiers.py`](aggregate_tiers.py) |
| 2 — Placement | `desirability.csv` + per-category config | rearranged item placements | **Planned** — `stage_post_fill` + book `pre_fill` |

---

## Stage 1 — Aggregation (done)

[`aggregate_tiers.py`](aggregate_tiers.py) collapses the raters into one consensus.

- **Per-rater desirability** = `1 - tier_index / (n_tiers - 1)` ∈ `[0, 1]`; top tier → `1.0`. Lists
  differ in tier count (7–9), so normalizing per rater keeps them comparable.
- **Within-tier order is ignored** — inspection shows it's category-grouped (souls, then weapons,
  then armor), not finely ranked, so we use tier *level* only.
- **`desirability`** = mean across raters who listed the item; **`disagreement`** = population stdev
  (the per-item jitter width downstream).
- **Name reconciliation:** strip `(+…HP)` annotations, alias `Vijaya → Vjaya`, recognize
  `"<n> Gold"` as the `money` category. The script raises if any tier-list name fails to resolve,
  so a future rename can't silently drop an item.

**Output schema** ([`desirability.csv`](desirability.csv)): `name, category, desirability,
disagreement, n_raters`, most-desirable first.

```
Giant Bat,blue_soul,1.0,0.0,4            <- most desirable: place LATE
Claimh Solais,weapon,0.9688,0.0541,4
...
Bamboo Sword,weapon,0.0,0.0,4            <- least desirable: place EARLY
100 Gold,money,0.0,0.0,4
```

**Coverage:** 86 items ranked (31 weapons, 13 armor, 12 accessories, 16 consumables, ~10 notable
souls, 4 gold). **174 canonical items are unranked** — mostly the minor soul roster and unremarkable
mid gear. That's fine, and it's actually load-bearing: see [the uniform background](#why-the-bands-mean-what-they-say-the-uniform-background).

---

## Stage 2 — Placement (planned)

### The core: least-displacement assignment

Two ingredients:

- Every **location** gets a progress coordinate `q ∈ [0, 1]` = its rank in sphere order ÷ (N−1)
  (earliest sphere → 0, last → 1).
- Every **non-progression item** gets a target `t ∈ [0, 1]` from its category's generator (below).

Then sort items by `t`, sort locations by `q`, and zip them together. For two 1-D sequences,
sorted-to-sorted is the assignment that **minimizes total displacement** `Σ|t − q|` — so this is the
"closest to its wish" placement, not a heuristic. It's **logic-safe** because it only permutes
*non-progression* items among the locations already holding them; reachability never depends on them.
This mirrors Dark Souls III's proven *item smoothing*:
[`worlds/dark_souls_3/__init__.py:1385-1513`](../../../dark_souls_3/__init__.py#L1385-L1513).

The only thing that varies per category is **how `t` is generated**.

### Why the bands mean what they say: the uniform background

Subtle but important: zip preserves *order*, so an item's realized sphere is its **quantile among
all the target-`t`s**, not its raw `t`. A band like "gold in `[0, 0.55]`" is only literal if quantile
≈ value — i.e. if the *combined* target distribution is roughly uniform on `[0, 1]`.

It is, and the 174 unranked items are why. We give each unranked item a **uniform prior**
`t ~ U(0, 1)` (honest: nobody ranked it, so place it anywhere). They outnumber the ranked items 2:1,
so they form a near-uniform background; the ranked points, the gold band, and the books ride on top
as perturbations. Result: quantile ≈ value, and **the absolute bands are honored**. (This also
replaces an earlier mistake — clumping unranked items at a narrow "neutral" `[0.35, 0.65]` would have
swamped the mid-game and buried the ranked signal.)

⚠️ The one thing to verify on real seeds is that the combined `t` distribution stays roughly uniform;
if a future content change makes it lumpy, switch from sort-zip to explicit quantile binning (place
each item in the location whose `q` is nearest its `t`, nearest-free on collision).

### Base targets per category

Each item first gets a **base target** (in desirability space); these are then rank-transformed to
positions and [jittered](#per-item-jitter-long-tails-in-position-space).

| Category | Base target | `scale` (jitter width) | Effect |
|---|---|---|---|
| weapons / armor / accessories / souls | `desirability` | `sigma_base + k · disagreement` | monotone weak-early/strong-late; contested items breathe more |
| **gold (money)** | **stratified** over `[0.0, ~0.55]`, all 18 copies | flat global | sprinkled early-to-mid; the tail rarely sends one late |
| **unranked** | `U(0, 1)` (uniform prior) | small/none (already broad) | background that keeps the aggregate uniform |

**Stratified band** (gold): for `K` copies, copy `i` draws its base from
`[lo + i/K·(hi−lo), lo + (i+1)/K·(hi−lo)]`, guaranteeing an even sprinkle with intra-stratum luck.
Gold's desirability (`~0.0`, the floor) would otherwise pin it to the very first spheres; the explicit
early-to-mid band is what lifts the top of its range to mid-game.

Money joins by **category, not name**: strip the ` (X)` disambiguation suffix; all 18 money pickups
(`100/500/1000/2000 Gold`, see `display_name` in
[`../pickup_info/__init__.py:101`](../pickup_info/__init__.py#L101)) feed the one stratified band.

### Per-item jitter: long tails, in *position* space

We want each item's placement **peaked at its target but with long tails**, so it *usually* lands
where it belongs yet can occasionally turn up far away — "you can still get lucky." Two pieces:

**(a) Jitter in position space, not desirability space.** The naive approach — add the jitter to the
item's *desirability* and re-rank — has no middle ground. Desirability is densely packed at the low
end and sparse at the top, so a top item has to cross the whole dense pack: a small jitter barely
moves it, a big one overshoots all the way to the very first pickup, with almost nothing in between
(landings pile up bimodally at "late" or "≈0.00"). The fix is to **rank-transform the bases into
evenly spaced positions first**, then jitter the *position* — now displacement is linear, so a
moderate draw moves an item a moderate fraction of the run.

**(b) A moderate-weight Student-t** (not Cauchy). `nu` sets the tail weight; `nu = 1` (Cauchy) is so
heavy that early jumps still overshoot and pile at the front, so we use `nu ≈ 2` (loose) / `4`
(strict) — heavy enough for the occasional lucky-early find, light enough that those finds land
across the early game instead of always at pickup #1.

```python
def student_t(nu, rng):
    # nu -> inf is Gaussian (thin tails); smaller nu = heavier tails / more extreme jumps.
    chi2 = rng.gammavariate(nu / 2.0, 2.0)
    return 0.0 if chi2 <= 0.0 else rng.gauss(0.0, 1.0) / math.sqrt(chi2 / nu)

def assign_targets(items, params, rng):
    base  = {it: base_target(it, params, rng) for it in items}   # gold band / desirability / U(0,1)
    order = sorted(items, key=base.get)                          # rank-transform ->
    pos   = {it: i / (len(order) - 1) for i, it in enumerate(order)}  #   uniform positions
    out = {}
    for it in items:
        if rng.random() < params.eps:                            # wildcard: place anywhere
            out[it] = rng.random()
        else:
            out[it] = pos[it] + scale_of(it, params) * student_t(params.nu, rng)  # jitter the POSITION
    return out                                                   # placement sorts by this; no clamp
```

The rank-transform also helps the *many-upper-tier-items* case: several items at similar desirability
get spread to distinct positions, so they don't all crater together. Tails still apply to every
category (a gold piece can rarely surface late), and out-of-range draws need no clamping — the
placement pass sorts by target, so they simply yield an extreme rank.

**The three knobs:**

| Knob | Controls | Default intent |
|---|---|---|
| `nu` (tail weight) | how often a draw is extreme | moderate: **2** (loose) / **4** (strict) — low enough to reach early, high enough to keep a middle ground |
| `scale` (width) | central spread (fraction of run), per item = `sigma_base + k · disagreement` | tight; a small `sigma_base` floor so even unanimous items keep a tail |
| `eps` (wildcard) | flat "anywhere" floor, independent of the item's target | small (≈0.02–0.05) |

`scale` is also the overall strength dial: the `item_smoothing: off | loose | strict` option maps onto
`sigma_base`/`k` (`off` skips the pass). Tying part of `scale` to `disagreement` keeps the
center-of-mass behavior honest — contested items breathe more than unanimous ones — while `nu` and
`eps` guarantee *nobody* is fully locked.

**Worked example (records the intent).** Claimh Solais, center `0.9688`, width ≈ its disagreement
`0.054`: with `nu = 1` (Cauchy), `P(first third) ≈ 2.7%`; the same width as a Gaussian gives ~10⁻³².
Add `eps = 0.03` → about `2.7% + 1% ≈ 3.7%`. Dial `nu` up to make the lucky-early seed rarer, down to
make it juicier.

> Named **`item_smoothing`**, not `weapon_smoothing`: it covers armor, accessories, souls,
> consumables and gold too.

### Ancient Books — logic-aware `pre_fill` (option 2, chosen)

The three Ancient Books are flagged **progression** in
[`../item_info/item_importance.csv`](../item_info/item_importance.csv) but **gate nothing** (no access
rule references them). We keep the progression flag and **spread them throughout the game** with a
dedicated `pre_fill` — they can't ride the post-fill pass (it only moves non-progression items).

- Spheres don't exist yet at `pre_fill` time, so use a fill-independent progress proxy: **region BFS
  depth from the origin**. (Same "how far into the run" axis as `q`, just the estimator available
  before fill.) Bin locations into early / mid / late by depth.
- Target one book per band with the same jittered edges (so they drift and lightly overlap rather
  than landing at clockwork thirds), restrict to currently-reachable unfilled locations in that band,
  and place with `fill_restrictive`. Run **before** the main fill so the books claim their slots.

---

## Progression vs desirability

Use the tier lists **only** as a desirability signal. Whether an item is *progression* stays owned by
the existing logic (`item_importance.csv` + [`../../regions.py`](../../regions.py)). The lists
conflate the two (sprasshu has a "Required for progression" tier; M names tiers "…progression +
…equips") and raters even disagree on what's progression (e.g. Galamoth) — all irrelevant noise once
their output is treated as pure desirability. Concretely: the smoothing pass skips any
`progression` item; the books are the one progression category we *also* want spread, via their own
`pre_fill`.

## Correctness notes (multiworld / accessibility / determinism)

- **Scoped to on-world locations.** Unlike DS3, the pass only reorders this world's items that sit in
  *this world's own* locations (`loc.player == loc.item.player == player`). A cvaos weapon sent
  off-world isn't gear its recipient can use, so ordering it in a foreign world is meaningless — and
  staying on-world makes the order a deterministic total order (own location names are unique) and
  respects `local_items`/`non_local_items` for free. The `can_fill` guard still protects excluded /
  item-rule constraints; excluded locations are skipped outright (they may hold only filler).
- **Limited accessibility:** `get_spheres()` yields any unreachable *filled* locations as a trailing
  set, so under `accessibility: minimal` those items **are** smoothed too (placed "latest"). Benign —
  the player may never reach them, and they're still valid placements.
- **Determinism:** everything stochastic goes through `self.random`; `stage_post_fill` computes
  `get_spheres()` **once** for all cvaos worlds.

---

## Where this hooks into the apworld (not yet wired)

Today [`__init__.py:89-94`](../../__init__.py#L89-L94) just dumps the pool into `multiworld.itempool` —
no fill hooks, so any item can land in sphere 1. Planned changes:

1. **Load** `desirability.csv` (package data, like the other CSVs) into `name → (desirability,
   disagreement, n)`, plus the per-category config.
2. **`pre_fill`** — spread the three Ancient Books across early/mid/late depth bands (logic-aware).
3. **`stage_post_fill`** — smooth this world's non-progression items, modeled on DS3 (one
   `get_spheres()`; `_pop_item`/`_shuffle` helpers at
   [`worlds/dark_souls_3/__init__.py:1515-1532`](../../../dark_souls_3/__init__.py#L1515-L1532)).
4. **`options.py`** — add `item_smoothing: off | loose | strict` (→ `sigma`); `off` keeps today's
   behavior.

### Reference sketch (post-fill smoothing)

```python
@classmethod
def stage_post_fill(cls, multiworld):
    worlds = [w for w in multiworld.get_game_worlds(cls.game) if w.options.item_smoothing]
    if not worlds:
        return
    spheres = list(multiworld.get_spheres())  # earliest -> latest, computed once
    for world in worlds:
        depth = region_bfs_depth(world)        # map-distance tiebreaker (spheres are coarse: ~3-7)
        # THIS player's own non-progression items in THIS player's own, unlocked, non-excluded
        # locations, ordered by (sphere, map depth, name) -- a deterministic total order.
        locs = []
        for sphere in spheres:
            band = [loc for loc in sphere
                    if loc.player == world.player and loc.item.player == world.player
                    and not loc.locked and loc.progress_type != EXCLUDED
                    and not loc.item.advancement]
            band.sort(key=lambda loc: (depth.get(loc.parent_region.name, BIG), loc.name))
            locs += band
        items = [loc.item for loc in locs]
        t = {item: world._target_t(item) for item in items}   # per-category band + long-tailed jitter
        items.sort(key=lambda it: t[it])                       # ascending t == low desirability first
        for loc in locs:                                       # earliest loc gets lowest-t fillable item
            loc.item = pop_fillable(loc, items)                # can_fill guard (excluded / item rules)
```

---

## Open knobs / verify

- **Jitter knobs:** `nu` (tail weight, default 1–2), `sigma_base` + `k` (width / strength, mapped by
  `item_smoothing`), `eps` (wildcard floor, ≈0.02–0.05), and gold's `[0.0, 0.55]` band are all
  starting guesses — tune against real seeds (and against how many spheres a seed produces; few
  spheres → coarse banding, softened by the jitter). Watch the *combined* extremes: with `nu = 1` and
  18 gold + others all carrying Cauchy tails, expect a couple of "wrong-place" items per seed by
  design — that's the long tail working, not a bug.
- **Smooth globally or per-category?** One global pass can drop a potion where a weak weapon "wanted"
  the same `t` (fine — pickups aren't category-locked). Per-category passes keep each silo's curve
  independent. Start global; split only if a category feels off.
- ⚠️ **Eyeball a real seed sphere-by-sphere:** gold not clustered; books early/mid/late; early game
  not *all* junk (if it is, lower `scale` or raise `nu`); combined `t` distribution roughly uniform.
- ⚠️ **Re-confirm books gate nothing** before relying on the `pre_fill` — if regions ever reference
  them, they become ordinary progression and drop out of the spread.
- **Adding a rater** is drop-in: a new `tier_jsons/*.json`, re-run [`aggregate_tiers.py`](aggregate_tiers.py).
