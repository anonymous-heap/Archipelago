# cvaos/data: file descriptions

See [PATHFINDING.md](PATHFINDING.md) for definitions of the terms used here.

---

## Top-level

**`__init__.py`**
Re-exports the public types from the submodules, a few lookups, and each table's `rows` tuple under a name that says which table it came from, such as `item_info_collection`. Everything else is imported from its own subpackage: the other lookups (such as `item_info.by_item_number`), the worth and soul tier tables in `item_balancing`, and `map_info`.

**`_csv_resources.py`**
`open_csv` and `open_csv_if_exists`, which read a CSV through the package loader so that loading also works from a zipped `.apworld`.

**`parse_int.py`**
Utility functions `parse_int`, `parse_dec`, `parse_hex` for converting CSV string values (including `0x`-prefixed hex) to `int | None`. The models attach `parse_hex` to their address fields as a `pre=True` validator, and the pickup and room loaders also call it directly.

---

## `enemy_info/`

**`enemy_types.csv`**
The game's 113 enemy types. Columns: `enemy_id` (the type's row in the enemy stat table), `name`, and `drop_1` / `drop_2` (the item names in the two drop slots, blank for an empty slot).

**`enemy_stats.csv`**
Each type's HP and the rarity byte of each drop slot, for the item drop shuffle. Columns: `enemy_id`, `name`, `hp` (+0x0C of the type's row), `drop_1_rarity` (+0x15) and `drop_2_rarity` (+0x16). A higher rarity byte makes the slot drop less often.

**`build_enemy_stats.py`**
Writes `enemy_stats.csv` from the USA ROM, and says where the game's code reads each field. It checks each row's drops against `enemy_types.csv` first. Run by hand: `python build_enemy_stats.py <path to the USA ROM>`.

**`__init__.py`**
Loads `enemy_types.csv` and `enemy_stats.csv` into `EnemyType` tuples and checks that the ids run from 0 in order. Builds lookup dicts by `name` and `id`. Exports the `rows` tuple.

---

## `entrance_info/`

**`entrance_info.csv`**
One row per door. Each door belongs to one room and leads to a neighboring room, so most doorways appear as two rows, one for each side. The doorways `505:509`, `509:50A` and `50A:505` are the exception, with one row each. Key columns:

| Column | Description |
|---|---|
| `door_number` | Sequential integer ID |
| `door_identifier` | `"{room_identifier}:{dest_room_identifier}"`, which is directional, not sorted |
| `room_identifier` | The room this door belongs to |
| `dest_room_identifier` | The room on the other side |
| `door_address` | GBA ROM address of this door's data struct |
| `room_address` / `dest_room_address` | ROM addresses of the two connected rooms |
| `x_pos_door` / `y_pos_door` | Map cell just outside the door, relative to the room's map position, as signed bytes |
| `dest_x_door` / `dest_y_door` | Landing position in the destination room |
| `dest_x_offset_door` / `dest_y_offset_door` | Spawn offset applied on arrival |

**`__init__.py`**
Loads `entrance_info.csv` into `EntranceInfo` Pydantic models at module init. It replaces `door_identifier` with `door_identifier_nonunique` and `door_identifier_unique`. The unique form adds a position tag such as ` (left)` where two doors share a room pair. Builds lookup dicts by `door_number`, `door_identifier_unique`, and `door_address`. Exports the `rows` tuple, and `ambiguous_door_identifiers`, which holds the untagged identifiers that the routing loaders reject.

---

## `item_info/`

**`item_info.csv`**
Master item table. Columns: `item_number` (sequential int), `item_category`, `id` (within-category int), `name`.

**`item_importance.csv`**
Archipelago classification for each item. Columns: `item_number`, `name`, `progression`, `useful`, `filler` (boolean flags). Split from `item_info.csv` so classification can be edited independently of the base item data.

**`item_info_extended.csv`**
The base item columns plus `breaks_ceilings` and `breaks_floors`, which mark the items that satisfy the `Ceil` and `Floor` requirements. The file is optional, and when it is absent no item breaks ceilings or floors.

**`__init__.py`**
Merges the three CSVs by `name` into `ItemInfo` Pydantic models at module init, taking `item_number` from `item_importance.csv`. Builds lookup dicts by `name`, `(item_category, id)`, and `item_number`. Exports the `rows` tuple.

---

## `item_balancing/`

Three tables that rate items, and the scripts that build them. `aggregate_tiers.py` writes `desirability.csv`, the community desirability ratings, and `__init__.py` loads it into `DesirabilityInfo` rows. `build_worth.py` writes `worth.csv`, each item's worth on the scale of a vanilla run, and `worth.py` loads it. `build_soul_tiers.py` writes `soul_tiers.csv`, each soul's rating and tier, from the soul tier lists in `soul_tier_lists/`, and `soul_tiers.py` loads it. See its [README.md](item_balancing/README.md).

---

## `map_info/`

**`map_grid.txt`**, **`map_edges.txt`**, **`hidden_doors.txt`**
The in-game map's cell grid, edge table, and hidden-door list, as read from a US ROM.

**`__init__.py`**
Loads the three files, and its docstring describes their formats. Provides `cell`, `room_at`, `top_edge`, and `left_edge` over the loaded tables, plus the `extract` functions that read the same tables from a ROM.

**`__main__.py`**
Rewrites the three files from a ROM: `python -m worlds.cvaos.data.map_info <rom path>`.

---

## `pickup_info/`

**`pickup_identifiers.csv`**
ROM-level pickup data. Columns: `pickup_number` (sequential int), `ptr_address` (ROM pointer to this pickup's entry), `simple_name`, `specifier` (disambiguates duplicate names), `flag_offset (varA)` (save-flag bit index into the `0x02000360` collected-pickup bitfield, not a byte offset), `item_offset (varB)` (ROM item byte offset), `type_num`, `type_name`, `subtype_num`.

**`pickup_rooms.csv`**
Spatial and room data for each pickup. Columns: `pickup_number`, `ptr_address`, `room_identifier`, `room_address`, `pickup_number_within_room` (1-based index among pickups in the same room), `x`, `y`.

**`friendly_location_names.csv`**
A friendly name for each pickup's location. Columns: `pickup_number`, `room_identifier`, `simple_name`, `specifier`, `region`, and `SchwartzGandhi's names`, which holds the friendly name. The loader checks that each named row's `room_identifier` matches `pickup_rooms.csv`.

**`__init__.py`**
Merges the three CSVs by `pickup_number` into `PickupInfo` Pydantic models at module init. The `ptr_address` is cross-validated between `pickup_identifiers.csv` and `pickup_rooms.csv` where both have it. Builds lookup dicts by `ptr_address`, `pickup_number`, and `identifier_key` (`simple_name` + `specifier`). Exports the `rows` tuple, and `area_names`, which maps the first character of a room identifier to the name of its area, as the `region` column gives it.

Each pickup has three names. `item_name` is the Archipelago item name, which every pickup of the same item shares. `display_name` adds the specifier, as in `Potion (A)`, and `location_name` adds the friendly name and room, as in `White Dragon Hallway [00D Potion (A)]`.

---

## `room_info/`

**`room_identifiers.csv`**
Mapping table between the three room identifier forms. Columns: `room_number` (1-based int), `room_identifier` (three hex digits: the area, then the room within it), `room_address` (ROM address). Used as the join key when loading `room_info.csv`.

**`room_info.csv`**
Detailed room data extracted from ROM. Core columns: `room_address`, `entities_ptr`, `doors_ptr`, `num_doors`, `x`, `y` (room grid position). The remaining columns encode up to 27 entities per room, each as a group of `x_N`, `y_N`, `type_N`, `subtype_N`, `entityID_N`, `varA_N`, `varB_N`. Also includes `Region`, `Region Number`, and `Index in Region`.

**`__init__.py`**
Loads `room_identifiers.csv` as an index, then merges it into each `room_info.csv` row by `room_address` (falling back to `room_index`). Produces `RoomInfo` Pydantic models. Builds lookup dicts by `room_identifier`, `room_address`, and `room_number`. Exports the `rows` tuple.

---

## `routing_info/`

**`entrance_to_entrance_requirements.csv`**
Within-room traversal requirements. Each row describes the abilities needed to move from one entrance node to another within the same room. Key columns:

| Column | Description |
|---|---|
| `entrance_connection_number` | Sequential ID for this traversal rule |
| `RoomID` | The room where the traversal occurs |
| `From` | The neighboring room on the starting-door side |
| `To` | The neighboring room on the destination-door side |
| _(unnamed)_ | Variant integer, which distinguishes alternative routes between the same pair of doors |
| `None`, `Glide`, … `Kick` | Boolean ability columns: `TRUE` means that ability alone is sufficient, and `TRUE` under `None` means nothing is needed |
| `Misc. combo 1` … `Misc. combo 5` | Ability combinations, written as flag names in parentheses, e.g. `"Malphas (DJump), Flying Armor (Glide)"`. A cell needs every flag it names, and any one cell is enough |

Where two doors of `RoomID` lead to the same room, `From` or `To` carries the door's position tag, as in `500 (left)`.

Parsed by `routing_info/__init__.py` into `RoutingInfo` objects. `regions.py` turns each row into one directed edge within room `RoomID`, from the door to `From` to the door to `To`: `"{RoomID}:{From}" → "{RoomID}:{To}"`.

`tools/routing/entrances.py` builds the same edge, and `test_routing_tool.py` checks that the
solver and generation reach the same pickups.

**`symmetric_entrance_to_pickup_region_requirements.csv`**
Pickup accessibility requirements. Each row describes the abilities needed to reach a pickup from a specific entrance, and applies symmetrically in both directions. Key columns:

| Column | Description |
|---|---|
| `pickup_number` | Which pickup this row applies to |
| `Room` | The room containing the pickup |
| `Item Name` | Human-readable item name (informational) |
| _(unnamed)_ | Variant integer |
| `dest_room_identifier` | Which entrance(s) this rule applies to: a specific neighboring room ID, a comma-separated list of room IDs, or `Any` (all doors touching `Room`) |
| `None`, `Glide`, … `Kick` | Boolean ability columns (same as above) |
| `<alt 2>` | Ends the ability columns, and is not read as a requirement |
| `Misc. combo 1` … `Misc. combo 6` | Combination cells (same as above) |

Parsed by `routing_info/__init__.py` into `EntranceToPickupRegionInfo` objects. `dest_room_identifier` values are resolved to entrance identifiers by `_entrance_identifiers_from_cell()`, which builds `f"{room_id}:{neighbor}"` for a named room and expands `Any` through `_arrivals_by_room`. Both forms yield doors on the pickup's own side of the crossing; see [WHAT_BREAKS.md](WHAT_BREAKS.md).

**`symmetric_entrance_to_enemy_region_requirements.csv`**
Enemy accessibility requirements, in the same shape as the pickup file above but keyed by `enemy_number` with `room_id`, `Enemy Name`, and `Specifier` columns. Used to put enemy-drop souls such as Flame Demon and Succubus into logic, since those never enter the item pool. Enemy checks also use it to decide when each placement can be reached. A row whose `dest_room_identifier` resolves to nothing is skipped with a log line rather than raising, because this file is bulk data. `enemy_placements` reads every row that has an enemy name, routed or not, and takes its position from the `x` and `-y` columns.

**`default_transdoor_entrance_connections.csv`**
One row per directed door crossing, as `from_entrance,to_entrance`: the two entrance nodes on either side of the same physical door.

**`override_transdoor_entrance_connections.csv`**
Adjustments layered over the file above. A row with `does_exist=FALSE` removes that crossing, and a row with `is_override=TRUE` adds one. This is where the chaotic-realm portals live, since they are not real doors in `entrance_info`.

**`__init__.py`**
Defines `AbilityCombo` (IntFlag enum of all ability bits) along with `RoutingInfo`, `EntranceToPickupRegionInfo`, `EntranceToEnemyRegionInfo`, `EnemyPlacement`, and `TransdoorConnection`. Loads all five CSVs at module init. Each requirement row keeps its ability columns and combo cells, and its `get_requirement_bitmasks()` turns them into a minimized tuple of ReqMasks. Transdoors load first, because resolving the `dest_room_identifier` column of the pickup and enemy files needs them. Exports the `rows`, `pickup_region_rows`, `enemy_region_rows`, `transdoor_connection_rows`, and `enemy_placements` tuples, the lookup indexes over them, and `lookup_pickup_region_requirement()`.

---

## Route solvers

The graph search over this data is in [`../tools/routing/`](../tools/routing/), because it is
developer tooling rather than game data. Generation does not use it; `regions.py` turns these
same CSV rows directly into Archipelago regions and access rules.

See [`../tools/routing/__init__.py`](../tools/routing/__init__.py) for what each module covers, and
[PATHFINDING.md](PATHFINDING.md) for the types they return.
