# Item balancing: desirability, worth and soul tiers

This directory holds three tables that rate items, with the scripts that build them:

- **Desirability**, which item smoothing reads, comes from the community tier lists. Most of this
  README describes it and the placement that uses it.
- **Worth** places every item except the souls on the scale of a vanilla run (see
  [Worth](#worth)). Enemy checks, filler, Item Drop Shuffle, Enemy Drop Rate and Wide Item
  Smoothing compare items by it.
- **Soul tiers** give each soul a rating and a tier (see [Soul tiers](#soul-tiers)). Soul
  Shuffle's `matched` and Enemy Drop Rate read them.

Item smoothing places less useful items earlier in a run and more useful items later, so that a
player isn't handed something in sphere 1 that makes every later pickup boring. Placement keeps
enough randomness that a lucky early find is still possible.

The desirability data consists of the community tier lists, the script that combines them, and
the resulting desirability table. The placement code is in
[`../../item_smoothing.py`](../../item_smoothing.py), and it runs only when the `item_smoothing`
option is `loose` or `strict`.

**Orientation:** higher desirability means a later placement. Desirability runs from `0.0` (junk,
placed earliest) to `1.0` (the best items, placed latest). Positions in the run go from `0` (the
start) to `1` (the end). A placement target is a position plus random jitter, so it can fall
outside that range.

## What the tier lists measure

The raters judged items on two questions:

1. How much would this break the game if found early in a run? (absolute power that comes too soon)
2. How much would finding this make later pickups less exciting? (obsolescence)

A high answer to either question means the item should arrive late, so the two are combined into
one desirability value. Desirability is kept separate from progression; see
[Progression vs desirability](#progression-vs-desirability).

Tier lists are used instead of raw weapon ATK because ATK can't show that Black Panther's dash or
Satan's Ring trivialize the early game, and it covers only weapons. The four lists in
[`tier_jsons/`](tier_jsons/) cover weapons, armor, accessories, souls and consumables.

Disagreement between raters is useful too. It widens the placement jitter of contested items (see
[Jitter in position space](#jitter-in-position-space)), while items that every rater agrees on stay
closer to their targets.

## Pipeline

| Stage | Input | Output | Code |
|---|---|---|---|
| 1. Aggregate | [`tier_jsons/*.json`](tier_jsons/) and [`../item_info/item_info.csv`](../item_info/item_info.csv) | [`desirability.csv`](desirability.csv) | [`aggregate_tiers.py`](aggregate_tiers.py), run by hand |
| 2. Place | `desirability.csv`, loaded by [`__init__.py`](__init__.py) | rearranged item placements | [`../../item_smoothing.py`](../../item_smoothing.py), during generation |

---

## Stage 1: aggregation

[`aggregate_tiers.py`](aggregate_tiers.py) combines the raters into one consensus.

- **Per-rater desirability** is `1 - tier_index / (n_tiers - 1)`, in `[0, 1]`, so a rater's top
  tier scores `1.0`. The lists have 7 to 9 tiers, and normalizing per rater keeps them comparable.
- **Only the tier is used.** The order within a tier is not a ranking, because the lists group a
  tier by category (souls, then weapons, then armor) or sort it alphabetically.
- **`desirability`** is the mean across the raters who listed the item, and **`disagreement`** is
  the population standard deviation. Placement uses `disagreement` to widen an item's jitter.
- **Name reconciliation:** the script strips `(+…HP)` annotations, maps `Vijaya` to `Vjaya`, and
  recognizes `"<n> Gold"` as the `money` category. It raises an error if any tier-list name fails
  to resolve, so a renamed item can't drop out silently.

The output, [`desirability.csv`](desirability.csv), has the columns `name, category,
desirability, disagreement, n_raters`, most desirable first:

```
Giant Bat,blue_soul,1.0,0.0,4            <- most desirable
Hippogryph,ability_soul,1.0,0.0,4
Claimh Solais,weapon,0.9688,0.0541,4
...
100 Gold,money,0.0,0.0,4                 <- least desirable
Bamboo Sword,weapon,0.0,0.0,4
CASTLE MAP 1,consumable,0.0,0.0,4
CASTLE MAP 3,consumable,0.0,0.0,4
```

**Coverage:** 86 items are rated: 31 weapons, 13 armor, 12 accessories, 16 consumables, 10 souls
and 4 gold amounts. The other 174 items in `item_info.csv` are unrated, mostly minor souls and
unremarkable mid-game gear. Placement gives each unrated item a random base value (see
[Base values and the rank-transform](#base-values-and-the-rank-transform)). In a default seed,
though, every item that the post-fill pass moves is rated or gold.

---

## Stage 2: placement

### Where it runs

| Step | World method | Function |
|---|---|---|
| Hold the books, the mobility souls and Black Panther out of the pool | `create_items` | `take_smoothing_items` |
| Place the mobility souls and Black Panther near their targets | `pre_fill` | `bias_progression_souls` |
| Place the Ancient Books in early, middle and late bands of map depth | `pre_fill` | `spread_ancient_books` |
| Put anything left unplaced into the pool for the main fill | `pre_fill` | `add_unplaced_to_pool` |
| Move the souls and books closer to their targets in sphere order | `stage_post_fill` | `refine_soul_positions`, `refine_ancient_books` |
| Reorder the placed non-progression items and the floor and ceiling breakers | `stage_post_fill` | `smooth_placed_items` |

Items that `pre_fill` places must not be in the multiworld pool, and `create_items` is the last
step that may change the pool, so these items are held back there. Until they are placed, the world
returns them from `get_pre_fill_items`, so `get_all_state` still collects them. An item named by an
item link, a plando or `non_local_items` stays in the pool. Nothing is held back at all when doing
so would leave too few progression items for the priority locations.

### The post-fill pass: least-displacement assignment

`smooth_placed_items` works with two orderings:

- Every movable **location** has a place `q` in run order, from `0` to `1` (see
  [Location order](#location-order)).
- Every movable **item** gets a target `t` (see [Item targets](#item-targets)).

The items are sorted by `t`, and the earliest location gets the lowest-`t` item, the next location
the next item, and so on. For two 1-D sequences, matching sorted to sorted minimizes the total
displacement `Σ|t − q|`, so each item lands as close to its target as the other items allow. A
location whose item rule rejects the next item takes the first later item it accepts, or the next
item anyway if it accepts none.

The pass is logic-safe because it only permutes non-progression items among the locations that
already hold them, and reachability never depends on those items. The one exception is the floor
and ceiling breakers, described under [Progression vs desirability](#progression-vs-desirability).

The design follows Dark Souls III's item smoothing (`stage_post_fill` and `_pop_item` in
[`worlds/dark_souls_3/__init__.py`](../../../dark_souls_3/__init__.py)).

### Location order

`smoothable_locations_in_order` collects the locations the pass may change. They are this player's
reachable pickup locations that hold one of this player's own items, either a non-progression item
or a floor or ceiling breaker. Locked, excluded and priority locations are skipped. Defeat
locations (enemy checks) are left out too, because most of them come late in logic and counting
them would push every pickup's position earlier.

The `item_smoothing_order` option picks the order:

- `map_distance` (the default): by breadth-first distance from the start in the region graph,
  ignoring access rules, then by name. A room comes as early as its place on the map, whatever the
  seed.
- `spheres`: by logical sphere in this seed, then by map distance, then by name.

### Item targets

`_assign_targets` gives each item a position in the run, then adds random jitter to get its
target.

#### Base values and the rank-transform

Most items get their position from a base value:

| Item | Base value | Jitter width (`scale`) |
|---|---|---|
| Rated item | its `desirability` | `sigma_base + k · disagreement` |
| Unrated item | uniform random in `[0, 1]` | `sigma_base` |
| One copy of each gold amount except 100 Gold | above every rated item | `sigma_base` |

The bases are then **rank-transformed** into evenly spaced positions in `[0, 1]`, so the item with
the lowest base gets `0` and the highest gets `1`. The random bases scatter the unrated items
evenly through the run, between the rated ones. The promoted gold copies have the highest bases,
so they take the last positions.

#### Items with fixed bands

Two groups skip the base value and get a position directly. Both are left out of the
rank-transform, and both get the jitter width `sigma_base`.

- **Gold.** Every gold copy that isn't promoted is spread over the whole run. For `K` such copies
  in shuffled order, copy `i` gets a position drawn uniformly from `[i/K, (i+1)/K]`, so each `1/K`
  slice of the run holds one copy. Gold's desirability is near zero, so ranking it would put all of
  it at the very start and push every other item later. Gold is recognized by its category, so
  every amount (`100`, `500`, `1000` and `2000 Gold`) is included.
- **Second-half consumables.** A fixed number of copies of some mid-tier consumables (2 High
  Potion, 3 Super Potion, 4 Mana Prism and 1 High Mind Up) are spread the same way over `[0.5, 1]`,
  so that not all the healing and mana items come early. Their other copies are ranked as usual.

#### Why a position means a fraction of the run

The assignment preserves order, so an item's real place in the run is the quantile of its target
among all the targets, not the target itself. A band such as "the second half" therefore means what
it says only when the combined targets are close to uniform on `[0, 1]`. The rank-transformed
positions are evenly spaced by construction and the gold band is uniform too, so the only skew is
the second-half consumables (at most 10 items), which add a little weight to the back half.

#### Jitter in position space

Each target is the item's position plus `scale · T`, where `T` is a Student-t draw with `nu`
degrees of freedom. With probability `eps`, the item gets a uniformly random target instead (a
wildcard). Targets are not clamped, because the pass sorts by target and an out-of-range draw just
gives an extreme rank.

The jitter goes on the position rather than on the desirability. Desirability values are packed
densely at the low end and sparsely at the top, so a top item jittered in desirability would have
to cross the whole dense group. A small draw would barely move it, and a large one would send it to
the very first pickup, with almost nothing in between. Positions are evenly spaced, so a moderate
draw moves an item a moderate fraction of the run.

`nu` sets the tail weight. With `nu = 1` (the Cauchy distribution), the tails are so heavy that
most items that jump early overshoot to the very first pickups. The presets therefore use `nu = 2`
(`loose`) and `nu = 4` (`strict`). That is heavy enough for an occasional lucky early find, and
light enough that those finds land across the early game rather than always at the first pickup.

The parameters are in `PARAMS_BY_KEY`, keyed by the `item_smoothing` option:

| Parameter | Controls | `loose` | `strict` |
|---|---|---|---|
| `nu` | tail weight: how often a draw is extreme | 2 | 4 |
| `sigma_base` | jitter width every item gets, as a fraction of the run | 0.08 | 0.03 |
| `k` | extra jitter width per unit of `disagreement` | 1.5 | 0.8 |
| `eps` | chance of a wildcard target | 0.05 | 0.02 |

`sigma_base` gives some jitter even to an item that every rater agrees on, and `nu` and `eps` mean
that no item's location is ever certain.

**Worked example.** Claimh Solais has desirability `0.9688` and disagreement `0.0541`. In a default
seed, only the promoted gold copies and at most one other item rank above it after the
rank-transform, so its position is about `0.95`. Under `loose`, its jitter width is
`0.08 + 1.5 · 0.0541 ≈ 0.16`. The chance that its target is below `1/3`, which puts it in about the
first third of the run, is then about 4.6%: about 2.9% from the Student-t tail and 1.7% from the
wildcard. Under `strict`, the width is about `0.073` and the chance is about 0.7%, nearly all of it
from the wildcard. A lower `nu`, a wider `scale` or a higher `eps` makes a lucky early find more
common.

> The option is named `item_smoothing`, not `weapon_smoothing`, because it covers armor,
> accessories, souls, consumables and gold too.

### Ancient Books

The three Ancient Books are progression, because the gate on the way to Chaos needs all three (see
`_chaotic_realm_gate` in [`../../regions.py`](../../regions.py)). The post-fill pass moves only
non-progression items, so the books get two passes of their own.

- **Before the main fill**, `spread_ancient_books` sorts this player's empty, non-excluded pickup
  locations by map depth and splits them into early, middle and late bands. The books are shuffled
  and aimed at one band each, with the long-tailed jitter described above, so a book sometimes
  lands in another band. A book goes only where it is reachable without any of the books. Map
  depth stands in for run order here because the spheres don't exist yet.
- **After the main fill**, `refine_ancient_books` draws one target from each of `[0, 1/3]`,
  `[1/3, 2/3]` and `[1/2, 3/4]` of the run in sphere order. It moves each book towards its target
  by swapping it with a non-progression item at a location reachable without that book. A swap is
  kept only if the whole multiworld still fulfills accessibility.

If no band has a free location reachable without the books, the main fill places that book instead.
A book that can't be moved safely after the fill stays where it is, and a book that plando locked in
place is never moved.

### Mobility souls and Black Panther

Malphas (double jump), Hippogryph (high jump) and Giant Bat (flight) gate movement, so they are
placed by lateness targets of their own rather than by desirability, which rates all three highly.

- `early_mobility_souls` decides whether one of them is aimed into the first third of the run: in
  about 93% of runs under `bias_early`, and in every run under `guarantee_early`. The early soul is
  Malphas 80% of the time and Hippogryph otherwise. Giant Bat is never chosen, because its flight
  reaches nearly everything the other two souls do. The souls that aren't chosen get late targets
  from `SOUL_LATE_TARGETS`, centered at `0.60` for Malphas, `0.86` for Hippogryph and `0.92` for
  Giant Bat.
- `black_panther_bias` aims Black Panther into the first half of the run, with a target drawn
  uniformly from `[0, 0.5]`.
- Before the main fill, `bias_progression_souls` puts each soul at the reachable location whose
  lateness is nearest its target. Lateness comes from ability tiers: the step at which a location
  first becomes reachable when the movement souls are collected one at a time, most useful first.
  After the main fill, `refine_soul_positions` moves each soul towards its target in sphere order,
  with the same safety checks as the books.

With Progressive Movement on, two of the Progressive Movement copies are aimed like Malphas and the
third like Giant Bat. The post-fill refine pass doesn't move them.

---

## Progression vs desirability

The tier lists are used only as a desirability signal. Whether an item is progression is decided by
the logic ([`../item_info/item_importance.csv`](../item_info/item_importance.csv) and
[`../../regions.py`](../../regions.py)). The lists mix the two: sprasshu has a "Required for
progression" tier, M names tiers like "Strong progression + Best equips", and raters even disagree
on what counts as progression (Galamoth, for example). None of that matters once the lists are read
purely as desirability.

The post-fill pass therefore moves only non-progression items, with one exception: the weapons
whose only progression use is breaking floors or ceilings. Logic counts them as progression, but
they are reordered by desirability like other equipment. The reordering doesn't check
reachability, so `smooth_placed_items` first records where the main fill put each breaker. If the
new arrangement fails the accessibility check, the breakers go back to those locations. The
Ancient Books, the mobility souls and Black Panther are the other progression items that smoothing
places, each through the passes described above.

## Correctness notes

- **Only this world's own locations.** Unlike DS3, the post-fill pass reorders only this world's
  items that sit in this world's locations (`loc.player == loc.item.player == player`). A cvaos
  weapon sent to another world isn't gear its recipient can use, so ordering it there has no
  meaning. Staying in this world also makes the order a total order, since this world's location
  names are unique, and it respects `local_items` and `non_local_items` without extra work.
- **Location settings.** Excluded and priority locations are skipped, and the `can_fill` check
  still applies each location's item rule. An excluded location may hold only filler, so a useful
  item can't move into one. A priority location could lose its progression item if a breaker moved
  out of it.
- **Minimal accessibility.** `get_spheres()` reports the unreachable filled locations as one set
  after an empty set. `smoothable_locations_in_order` stops at the empty set, so under
  `accessibility: minimal` those locations are never smoothed.
- **Determinism.** Every random draw comes from the world's own `random`, and `smooth_placed_items`
  computes `get_spheres()` once for all cvaos worlds.

## Open questions

- **Tuning.** The parameters, the second-half quotas and the gold rules are starting values. Tune
  them against real seeds, including how many spheres a seed has: with few spheres, the `spheres`
  order is coarse, which the jitter softens.
- **Long tails are intended.** Every item's jitter has a long tail, so expect a few items per seed
  in surprising places.
- **One global pass or one per category?** One global pass can give a potion the location that a
  weak weapon with a similar target would otherwise get. That is fine, because pickups aren't tied
  to a category. Per-category passes would give each category its own curve. Split only if a
  category feels wrong.
- **Check a real seed sphere by sphere.** Look for gold that isn't clustered, books early, middle
  and late, an early game that isn't all junk, and combined targets that are roughly uniform. If
  the early game is all junk, widen the jitter (raise `sigma_base` or `k`) or lower `nu`. If the
  targets ever become lumpy, replace the sort-and-match assignment with quantile binning: place
  each item at the free location whose `q` is nearest its `t`.
- **Adding a rater** takes a new `tier_jsons/*.json` file and a rerun of
  [`aggregate_tiers.py`](aggregate_tiers.py).

---

## Worth

[`worth.csv`](worth.csv) gives every item except the souls a worth from 0 to 1, on the scale of a
vanilla run. An item is worth the point in the run where the castle's own pickups first offer as
much, so an item worth 0.25 is no better than what the pickups offer a quarter of the way in.
Enemy checks, filler, Item Drop Shuffle, Enemy Drop Rate and Wide Item Smoothing read it through
[`worth.py`](worth.py). Item smoothing does not read it.

[`build_worth.py`](build_worth.py) writes the table, and is run by hand. It gives each item one of
these worths:

| Item | Worth |
|---|---|
| Weapon | its depth on its class's ladder, ranked by ATK, or its desirability when that is higher |
| Armor | its depth on the armor ladder, ranked by DEF, or its desirability when that is higher |
| HP or MP restorative | its depth on the HP or MP ladder, ranked by the logarithm of the amount restored |
| Accessory, cure, or other consumable | its desirability, or 0.2 for an unrated accessory and 0.1 for any other unrated item |
| Gold | 0.02 |
| Valmanway, Chaos Ring, Soul Eater Ring | 1.0 |

A ladder holds the pickups of one class, ordered by the depth where the vanilla route first reaches
them. A pickup is a rung only when it beats every earlier pickup of its class, and an item between
two rungs gets a depth interpolated between them. A weapon outside its class's ladder, or in a
class with no pickup (daggers, for example), uses the ladder of all weapons instead. Desirability
lets a weapon or armor count effects that ATK and DEF miss.

The table's columns are `name`, `category`, `item_class`, `class_fit_depth` (the ladder depth),
`desirability`, `worth`, and `basis`, which says where the worth came from: `class ladder`,
`desirability`, `default` or `fixed`.

The `worth` values keep six decimals, and the build fails if rounding to them moves an item across
0.25, 0.45 or 0.6, or changes whether two items are within 0.05 of each other. Cutall and Estoc
sit at 0.4478, so they are just under the 0.45 line.

**Where the inputs come from.** [`worth_inputs.csv`](worth_inputs.csv) holds the facts that no
other table in the repository records. It was taken from `items.csv` in the design workspace for
the options that use worth (`local_docs/experimental_options_design/foundation/`, which is not in
the repository).

- `item_class` and `utility` were read from the US ROM's item records. A weapon's class comes from
  its record's family byte (+0x08) and animation byte (+0x12), at GBA 0x08505D3C with 28 bytes per
  weapon, and its utility is its ATK (+0x0A). Armor's utility is its DEF (+0x0B of its record at
  GBA 0x085063B0). A consumable's class comes from its kind byte (+0x08 at GBA 0x08505B3C), and a
  restorative's utility is the amount it restores (+0x0A).
- `earliest_pickup_depth` is the vanilla depth of the earliest normal pickup that holds the item:
  the share of the 115 normal pickups that the vanilla route reaches before it, counting half of
  those it reaches at the same point. The route gains abilities one at a time, in the order a
  vanilla run first gains them, and orders the pickups of one step by how many rooms away they
  are.

---

## Soul tiers

[`soul_tiers.csv`](soul_tiers.csv) gives every soul a rating from 0 to 1 and a tier from D to S.
[`build_soul_tiers.py`](build_soul_tiers.py) writes it, and is run by hand.
[`soul_tiers.py`](soul_tiers.py) loads it for Soul Shuffle's `matched` and for Enemy Drop Rate.

- **Rating.** Each list in [`soul_tier_lists/lists/`](soul_tier_lists/lists/) scores a soul
  `1 - tier_index / (n_tiers - 1)`, as stage 1 does for items. The rating is the mean over the
  lists that rank the soul. A soul that fewer than three lists rank takes its `desirability.csv`
  value instead when it has one, because the four item raters ranked the movement souls that most
  soul lists leave out.
- **Tier.** Ratings are cut at 0.2, 0.4, 0.6 and 0.8 into the tiers D, C, B, A and S. A soul in
  tier A or S, rated 0.6 or more, is strong: 23 of the 110 souls that enemies drop are.
- **Matched groups.** Under Soul Shuffle's `matched`, a soul trades places only with souls of the
  same color and tier, which `matched_group` returns.

The table's columns are `soul`, `color` (`red`, `blue`, `yellow` or `ability`), `rating`,
`tier`, `raters` (how many soul lists rank it) and `rating_source`. The red soul Cagnazzo in
`item_info.csv` has no source in the game, so it has no row. The lists' Cagnazzo is the blue
soul.
