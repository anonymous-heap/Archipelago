# cvaos/data — File Descriptions

See [PATHFINDING.md](PATHFINDING.md) for definitions of the terms used here.

---

## Top-level

**`__init__.py`**
Re-exports all public types and collections from the submodules. The single import point for consumers of this package.

**`parse_int.py`**
Utility functions `parse_int`, `parse_dec`, `parse_hex` for converting CSV string values (including `0x`-prefixed hex) to `int | None`. Used by Pydantic `BeforeValidator` annotations throughout the submodules.

---

## `entrance_info/`

**`entrance_info.csv`**
One row per directed physical door. Each physical connection between two rooms appears as **two rows** — one for each direction. Key columns:

| Column | Description |
|---|---|
| `door_number` | Sequential integer ID |
| `door_identifier` | `"{room_identifier}:{dest_room_identifier}"` — directional, not sorted |
| `room_identifier` | The room this door belongs to |
| `dest_room_identifier` | The room on the other side |
| `door_address` | GBA ROM address of this door's data struct |
| `room_address` / `dest_room_address` | ROM addresses of the two connected rooms |
| `x_pos_door` / `y_pos_door` | Position of the door within its room |
| `dest_x_door` / `dest_y_door` | Landing position in the destination room |
| `dest_x_offset_door` / `dest_y_offset_door` | Spawn offset applied on arrival |

**`__init__.py`**
Loads `entrance_info.csv` into `EntranceInfo` Pydantic models at module init. Builds lookup dicts by `door_number`, `door_identifier_unique`, and `door_address`. Exports the `rows` tuple.

---

## `item_info/`

**`item_info.csv`**
Master item table. Columns: `item_number` (sequential int), `item_category`, `id` (within-category int), `name`.

**`item_importance.csv`**
Archipelago classification for each item. Columns: `item_number`, `name`, `progression`, `useful`, `filler` (boolean flags). Split from `item_info.csv` so classification can be edited independently of the base item data.

**`__init__.py`**
Merges `item_info.csv` and `item_importance.csv` by `name` into `ItemInfo` Pydantic models at module init. Builds lookup dicts by `name`, `(item_category, id)`, and `item_number`. Exports `item_info_collection`.

---

## `pickup_info/`

**`pickup_identifiers.csv`**
ROM-level pickup data. Columns: `pickup_number` (sequential int), `ptr_address` (ROM pointer to this pickup's entry), `simple_name`, `specifier` (disambiguates duplicate names), `flag_offset (varA)` (save-flag bit index into the `0x02000360` collected-pickup bitfield, not a byte offset), `item_offset (varB)` (ROM item byte offset), `type_num`, `type_name`, `subtype_num`.

**`pickup_rooms.csv`**
Spatial and room data for each pickup. Columns: `pickup_number`, `ptr_address`, `room_identifier`, `room_address`, `pickup_number_within_room` (1-based index among pickups in the same room), `x`, `y`.

**`__init__.py`**
Merges `pickup_identifiers.csv` and `pickup_rooms.csv` by `pickup_number` into `PickupInfo` Pydantic models at module init. The `ptr_address` is cross-validated between both files where present. Builds lookup dicts by `ptr_address`, `pickup_number`, and `identifier_key` (`simple_name` + `specifier`). Exports `pickup_info_collection`.

---

## `room_info/`

**`room_identifiers.csv`**
Mapping table between the three room identifier forms. Columns: `room_number` (1-based int), `room_identifier` (zero-padded hex string), `room_address` (ROM address). Used as the join key when loading `room_info.csv`.

**`room_info.csv`**
Detailed room data extracted from ROM. Core columns: `room_address`, `entities_ptr`, `doors_ptr`, `num_doors`, `x`, `y` (room grid position). The remaining columns encode up to 27 entities per room, each as a group of `x_N`, `y_N`, `type_N`, `subtype_N`, `entityID_N`, `varA_N`, `varB_N`. Also includes `Region`, `Region Number`, and `Index in Region`.

**`__init__.py`**
Loads `room_identifiers.csv` as an index, then merges it into each `room_info.csv` row by `room_address` (falling back to `room_index`). Produces `RoomInfo` Pydantic models. Builds lookup dicts by `room_identifier`, `room_address`, and `room_number`. Exports `room_info_collection`.

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
| _(unnamed)_ | Variant integer — distinguishes alternative routes between the same pair of doors |
| `None`, `Glide`, … `Kick` | Boolean ability columns — `TRUE` means that ability alone is sufficient |
| `Misc. combo 1` … `Misc. combo 5` | Parenthetical-token conjunctions, e.g. `"Malphas (DJump), Flying Armor (Glide)"` — each cell is an AND combination; cells are OR alternatives |

Parsed by `routing_info/__init__.py` into `RoutingInfo` objects. `regions.py` turns each row into one directed edge within room `RoomID`, from the door to `From` to the door to `To`: `"{RoomID}:{From}" → "{RoomID}:{To}"`.

Note that `tools/routing/entrances.py` builds the same destination node but uses `"{From}:{RoomID}"` as the source. See `REFACTOR.md` §2.11.

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
| `Misc. combo 1` … `Misc. combo 5` | Conjunction combo cells (same as above) |

Parsed by `routing_info/__init__.py` into `EntranceToPickupRegionInfo` objects. `dest_room_identifier` values are resolved to entrance identifiers by `_entrance_identifiers_from_cell()`, which builds `f"{room_id}:{neighbor}"` for a named room and expands `Any` through `_arrivals_by_room`. Both forms yield doors on the pickup's own side of the crossing; see [WHAT_BREAKS.md](WHAT_BREAKS.md).

**`symmetric_entrance_to_enemy_region_requirements.csv`**
Enemy accessibility requirements, in the same shape as the pickup file above but keyed by `enemy_number` with `room_id`, `Enemy Name`, and `Specifier` columns. Used to put enemy-drop souls such as Flame Demon and Succubus into logic, since those never enter the item pool. A row whose `dest_room_identifier` resolves to nothing is skipped with a log line rather than raising, because this file is bulk data.

**`default_transdoor_entrance_connections.csv`**
One row per directed door crossing, as `from_entrance,to_entrance`: the two entrance nodes on either side of the same physical door.

**`override_transdoor_entrance_connections.csv`**
Adjustments layered over the file above. A row with `does_exist=FALSE` removes that crossing, and a row with `is_override=TRUE` adds one. This is where the chaotic-realm portals live, since they are not real doors in `entrance_info`.

**`__init__.py`**
Defines `AbilityCombo` (IntFlag enum of all ability bits) along with `RoutingInfo`, `EntranceToPickupRegionInfo`, `EntranceToEnemyRegionInfo`, and `TransdoorConnection`. Loads all five CSVs at module init and parses ability columns and combo-text cells into minimized ReqMask tuples. Transdoors load first, because resolving the `dest_room_identifier` column of the pickup and enemy files needs them. Exports the `rows`, `pickup_region_rows`, `enemy_region_rows`, and `transdoor_connection_rows` tuples, the lookup indexes over them, and `lookup_pickup_region_requirement()`.

---

## Route solvers

The graph search that used to live here now sits in [`../tools/routing/`](../tools/routing/),
because it is developer tooling rather than game data. Generation does not use it; `regions.py`
turns these same CSV rows directly into Archipelago regions and access rules.

See [`../tools/routing/__init__.py`](../tools/routing/__init__.py) for what each module covers, and
[PATHFINDING.md](PATHFINDING.md) for the types they return.
