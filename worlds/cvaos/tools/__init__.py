"""
Developer tooling for the Castlevania: Aria of Sorrow world.

Nothing here runs during generation or while the client is connected. These modules
exist to answer questions about the game data and to regenerate parts of it, so they
may import from ``worlds.cvaos.data``, but ``data`` must never import from here.
"""
