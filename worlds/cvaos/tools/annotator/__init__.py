"""
The enemy routing annotator: a local page for filling in the logic sheet's enemy tab.

Run it with ``python -m worlds.cvaos.tools.annotator``. It signs in to Google, reads the enemy
tab, and serves a page at ``http://localhost:38400/`` with the castle map, a schematic of each
room, the rows of the selected placement, and a worklist of door and placement pairs still to
annotate. The design is ``local_docs/ROUTING_ANNOTATOR_DESIGN.md``.

The sheet's id, the US ROM's path and EmuHawk's path are settings, which are kept outside the
repository. ``settings.py`` lists them and says where the file is.

This package is a development tool. Nothing in the world imports it.
"""
