# Asymmetric Entrance Traversal

This document describes how the cvaos routing data handles **asymmetric** traversal, and what would
break that handling. See [PATHFINDING.md](PATHFINDING.md) for terminology.

"Asymmetric" here means: a route from entrance node A to entrance node B exists, but the reverse
route B → A does not, or has different requirements, or neither direction exists at all.

Recall that an entrance node `"A:B"` is **the door in room A that leads to room B**, so you are
standing in room A. Crossing that door moves you to `"B:A"`.

---

## What handles asymmetry correctly

### The entrance graph

`entrance_to_entrance_requirements.csv` gives every direction its own row, so each direction can
carry different requirements or be absent entirely. The current data relies on this. For
example, crossing room `003` from the `000` side to the `002` exit requires `DJump`, `HJump`, or
`Bat` (or a combo), while the reverse from `002` to `000` requires nothing.

### Pickup and enemy rows naming a specific neighbor

When the `dest_room_identifier` column names one or more neighboring rooms,
`_entrance_identifiers_from_cell` builds `f"{room_id}:{neighbor}"` directly. That is already the
directional node (the door on the pickup's own side), so no direction information is lost.

Each candidate is kept only if it appears in `by_from_entrance_for_transdoor`. Deleting a door from
`default_transdoor_entrance_connections.csv` therefore removes it here automatically, with no second
place to update.

### `dest_room_identifier = "Any"`

`"Any"` expands through `_arrivals_by_room`, which is built from the **`to_entrance`** side of every
transdoor row. For room `009` that yields only nodes of the form `"009:*"`: doors on room 009's own
side. A pickup in room 009 therefore attaches to nodes inside room 009, which sits behind whatever
gates the crossing into 009.

---

## What would break the asymmetry handling

### Expanding `"Any"` from the `from_entrance` side

Building `_arrivals_by_room` from `from_entrance` instead of `to_entrance` would attach a room's
pickups to the *neighbor's* side of each door, placing them in front of any gate on the crossing.

Generation and the route solvers both give a pickup edges that lead only towards it, so a pickup
attached to the wrong side cannot connect two doors. The harm is the misplaced gate: the pickup
would count as reachable from the neighbor's side, without crossing the door into its room.

### A direction-insensitive door identifier

A door identifier could sort its two room IDs into one canonical form, so that `("009", "006")`
and `("006", "009")` both resolve to `"006:009"`. That is sound only when traversal is symmetric.
A sorted identifier cannot express "this pickup is reachable only when approached from one side",
so any helper that sorts or otherwise normalizes the two halves of an entrance identifier loses
that information.
