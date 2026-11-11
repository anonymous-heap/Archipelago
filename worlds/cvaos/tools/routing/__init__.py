"""
Route solvers over the cvaos routing data.

These build a graph from the requirement CSVs in ``data/routing_info`` and answer
reachability questions about it. Generation does not use them. ``regions.py`` turns the
same CSV rows into Archipelago regions and access rules, and the graph here has the same
nodes and edges as those regions. ``test_routing_tool`` checks that both reach the same
pickups.

Three layers, each building on the one before it:

* ``entrances`` builds the entrance-node graph, with BFS and a subset-minimal requirement
  search over it.
* ``entrances_to_items`` extends that graph with pickup and enemy nodes.
* ``spheres`` builds reachability spheres, repeatedly collecting the reachable pickups and
  adding the abilities they grant until no new pickup becomes reachable.

See ``data/PATHFINDING.md`` for the terminology.
"""
