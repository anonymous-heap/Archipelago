# Asymmetric Entrance Traversal

This document describes how the cvaos routing data handles **asymmetric** traversal, and what would
break it again. See [PATHFINDING.md](PATHFINDING.md) for terminology.

"Asymmetric" here means: a route from entrance node A to entrance node B exists, but the reverse
route B → A does not, or has different requirements, or neither direction exists at all.

Recall that an entrance node `"A:B"` is **the door in room A that leads to room B**, so you are
standing in room A. Crossing that door moves you to `"B:A"`.

---

## What handles asymmetry correctly

### The entrance graph

`entrance_to_entrance_requirements.csv` gives every direction its own row, so each direction can
carry different requirements or be absent entirely. The current data already relies on this. For
example, crossing room `003` from the `000` side to the `002` exit requires `DJump`, `HJump`, or
`Bat` (or a combo), while the reverse from `002` to `000` requires nothing.

### Pickup and enemy rows naming a specific neighbour

When the `dest_room_identifier` column names one or more neighbouring rooms,
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

## What would break it again

### Expanding `"Any"` from the `from_entrance` side

Building `_arrivals_by_room` from `from_entrance` instead of `to_entrance` would attach a room's
pickups to the *neighbour's* side of each door, placing them in front of any gate on the crossing.

It would also reintroduce a worse problem, because pickup and enemy edges are bidirectional. Suppose
a pickup in room 009 gained edges to both `"009:006"` and `"006:009"`, and the door between 006 and
009 were one-way in the 006 → 009 direction. A search reaching the pickup legitimately from inside
room 009 would then expand to `"006:009"`, and from there to every within-room route in room 006 that
departs from the 009-side door. The pickup node would act as a shortcut around the one-way door:

```
start → ... → "009:006" → PICKUP:N → "006:009" → (room 006 entrance nodes)
```

Keeping the expansion on the arrival side is what prevents this, since every node it produces is
already inside the pickup's own room.

### Reintroducing a direction-insensitive door identifier

An earlier version sorted the two room IDs into a single canonical identifier, so that `("009",
"006")` and `("006", "009")` both resolved to `"006:009"`. That is only sound when traversal is
symmetric, because it cannot express "this pickup is reachable only when approached from one side".
Any helper that sorts or otherwise normalises the two halves of an entrance identifier will lose the
same information.
