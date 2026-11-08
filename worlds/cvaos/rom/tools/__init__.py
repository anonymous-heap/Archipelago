"""
ROM authoring helpers.

``png_to_icon`` is a command line tool and also the image code that ``rom/icon.py`` uses for an
``ImageFile`` icon source: the PNG decoder, the color quantization, the 16x16 fit and the 4bpp
tile packing. So it is imported during a build rather than only run by hand.
``dump_obj_palette.lua`` is a BizHawk script and is never imported.
"""
