# cvaos Pathfinding Terminology

Glossary of the terms used by the routing data in `cvaos/data/` and the route solvers in `cvaos/tools/routing/`, ordered from lowest to highest abstraction.

---

## Physical game world

**Room**
A zone of the castle. Identified interchangeably by:
- `room_identifier`: three hex digits, the area and then the room within it, e.g. `"003"`
- `room_address`: GBA ROM address, e.g. `0x0850F15C`
- `room_number`: 1-based integer

Defined in `room_info/`.

**Door** (physical)
A transition between two rooms. Most doorways have **two rows** in `entrance_info.csv`, one for each side, and each row has its own `door_address` in ROM. The `door_identifier` field is `f"{room_identifier}:{dest_room_identifier}"`, verbatim and directional (not sorted).

**Pickup**
A collectible item sitting at a position in a room. Identified by a sequential `pickup_number` integer. Carries its ROM pointer, its save-flag bit, its x/y coordinates, and its `room_identifier`. Defined in `pickup_info/` by merging `pickup_identifiers.csv` (ROM offsets, item type) and `pickup_rooms.csv` (which room, position within room), plus `friendly_location_names.csv` for location names.

---

## Ability / requirement system

**Ability / AbilityCombo**
A movement capability the player can possess, modelled as bits in the `AbilityCombo` `IntFlag` enum in `routing_info/__init__.py`. Two categories:
- *Soul-based*: `Glide` (Flying Armor), `Slide` (Skeleton Blaze), `DJump` (Malphas), `HJump` (Hippogryph), `WWalk` (Undine), `Dive` (Skula), `Panth` (Black Panther), `Bat` (Giant Bat), `BDash` (Grave Keeper), `Kick` (Kicker Skeleton), `Tform` (Devil/Curly/Manticore), `Galamoth`, `Chronomage`
- *Technique flags*: `Enemy` (kickable enemy present), `PixPer` (pixel-perfect platforming), `Clip` (platform clip), `InZip` (inbounds zip), `Vert` (vertical entrance), `QSave` (quick-save entrance reset), `Floor` and `Ceil` (a floor or ceiling the player must reach and break, which generation meets with an item that breaks it), `Impossible` (never met, so a mask that includes it is never usable)

**ReqMask** (`int`)
A bitmask where each set bit is one required ability. Represents a **conjunction**: all set bits must be satisfied simultaneously. Multiple masks on the same edge are **disjunctive** (OR between masks, AND within each mask). So `(DJump|Glide, Panth)` means "DJump+Glide together, or Panth alone."

**have_mask** (`int`)
The ReqMask representing the player's current ability set. An edge with `req_mask` is traversable when `(have_mask & req_mask) == req_mask`.

**Variant** (`int | None`)
An optional integer on routing rows and edges, from the unnamed `""` column in routing CSVs. Distinguishes alternative routes through the same connection that have different requirement profiles. Used for path reconstruction bookkeeping; does not affect traversability logic.

---

## Routing data records

**RoutingInfo**
A parsed row from `routing_info/entrance_to_entrance_requirements.csv`. Answers: *"if you are in room `room_id` at the door to `from_room`, what abilities do you need to reach the door to `to_room`?"* In entrance-node terms, `regions.py` reads that as a route from `room_id:from_room` to `room_id:to_room`. Its `get_requirement_bitmasks()` converts TRUE/FALSE ability columns and `Misc. combo N` text cells into a minimized tuple of ReqMasks.

**EntranceToPickupRegionInfo**
A parsed row from `routing_info/symmetric_entrance_to_pickup_region_requirements.csv`. Answers: *"from entrance `entrance_identifier`, what abilities do you need to reach `pickup_number`?"* Marked **symmetric**: the same requirement applies going to the pickup and returning from it, so the graph builder adds edges in both directions.

The `entrance_identifier` is resolved by `_entrance_identifiers_from_cell()`. When `dest_room_identifier` names specific rooms it builds `f"{room_id}:{neighbor}"` for each, keeping only those that exist as a transdoor `from_entrance`. When the cell is `"Any"` it expands through `_arrivals_by_room`, which holds the doors on the pickup's own side of every crossing into that room.

**EntranceToEnemyRegionInfo**
The same for one enemy placement, from `routing_info/symmetric_entrance_to_enemy_region_requirements.csv`, keyed by `enemy_number`. Generation uses it; the route solvers do not.

---

## Graph nodes

**Entrance node** (`"ROOM_A:ROOM_B"`)
A directed graph node for **the door in room A that leads to room B**, so you are standing in room A. Built by `EntranceId.make(room_a, room_b)`. Crossing that door moves you to `"B:A"`, the matching node on the far side. The node records which side of the doorway you are on as well as which doorway it is, because a `RoutingInfo` row says which *other* doors of a room you can reach from a given starting door, and that depends on where in the room you entered.

**Pseudo start node** (`"__START__@ROOM"`)
A synthetic node for "standing somewhere in this room" without being at any specific doorway, such as a spawn position. `EntranceId.pseudo_start_node()` builds one, but no graph builder adds it.

**Pickup node** (`"PICKUP:N"`)
A graph node representing collectible item number N. Built by `PickupNodeId.make(pickup_number)`. Only present in the extended graph produced by `../tools/routing/entrances_to_items.py`, not in the entrance-only graph.

---

## Graph structure

**Edge**
A directed connection between two graph nodes. Carries:
- `to_node`: destination node ID
- `req_masks`: minimized tuple of ReqMasks (alternatives)
- `connection_number`: unique sequential int for lookup
- `variant`: optional int distinguishing alternative routes

**Entrance graph** (`../tools/routing/entrances.py`)
Adjacency-list graph where every node is an entrance node. Built by `RoutingGraphBuilder.from_requirements()` from `entrance_to_entrance_info_collection` and the transdoor connections. Each transdoor row becomes an edge with no requirement, from `from_entrance` to `to_entrance`. Each `RoutingInfo` row with at least one ReqMask becomes one directed edge, `"from_room:room_id" → "room_id:to_room"`, which starts on the far side of the entry door.

Generation reads the same row differently. `regions.py` starts it at `"room_id:from_room"`, the door inside `room_id`, as described under **RoutingInfo** above. So the solver and generation do not agree on a row's source node; see [FILE_DESCRIPTIONS.md](FILE_DESCRIPTIONS.md).

**Extended graph** (`../tools/routing/entrances_to_items.py`)
The entrance graph plus pickup nodes. Constructed by copying the entrance graph then layering in bidirectional edges between each entrance node and its reachable pickup nodes (from `EntranceToPickupRegionInfo`). Used for answering "can I reach item X, and what do I need?"

---

## Query results

**BFSResult / BFSStep**
Output of `reachable_with_options_bfs()`. `BFSResult` holds a boolean `reachable`, `path_nodes`, and a `steps` tuple of `BFSStep`. Each `BFSStep` records `from_node`, `to_node`, `connection_number`, `variant`, and which `req_masks` were usable under `have_mask` at traversal time.

**MinReqResult**
Output of `compute_min_requirements()`, a Dijkstra-like search (priority by bit count) that finds the **subset-minimal** requirement masks to reach every node from a start. Contains:
- `best: dict[NodeId, set[ReqMask]]`: the Pareto-minimal set of masks sufficient to reach each node
- `parent: dict[tuple[NodeId, ReqMask], ParentInfo]`: back-pointers for path reconstruction

**ParentInfo**
One back-pointer entry in `MinReqResult.parent`. Records the previous node, accumulated mask, which edge was crossed, and which specific `req_mask` option was chosen on that edge. Used for path reconstruction via `reconstruct_path_for_mask()`.

**RouteOption**
The user-facing result from `route_options()` or `pickup_route_options()`. Pairs a `required_mask` (the minimum ability set needed) with `steps: tuple[ParentInfo, ...]`, a concrete example path that achieves it. Multiple `RouteOption`s for the same goal represent different viable ability combinations (e.g., "use DJump" vs. "use Bat instead").
