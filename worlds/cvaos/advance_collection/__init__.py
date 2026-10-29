"""Tooling for the Steam Castlevania Advance Collection.

Making this directory a package lets the world import the archive tool and the
ROM installer (``worlds.cvaos.advance_collection.cac_archive`` / ``.install``).
``cac_archive.py`` stays runnable as a plain script, because it uses no
relative imports.
"""
