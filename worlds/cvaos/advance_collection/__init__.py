"""Tooling for the Steam Castlevania Advance Collection.

Making this directory a package lets the world import the archive tool and the
ROM installer (``worlds.cvaos.advance_collection.cac_archive`` / ``.install``).
``cac_archive.py`` and ``probe_attach.py`` stay runnable as plain scripts —
neither uses relative imports.
"""
