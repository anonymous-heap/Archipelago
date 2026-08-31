"""
ROM authoring helpers.

``png_to_icon`` doubles as a command line tool and as the PNG decoder that
``rom/icon.py`` calls to resolve an ``ImageFile`` icon source, so it is imported during a
build rather than only by hand. ``dump_obj_palette.lua`` is a BizHawk script and is never
imported.
"""
