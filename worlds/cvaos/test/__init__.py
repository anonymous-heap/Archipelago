import os

from test.bases import WorldTestBase


class CVAOSTestBase(WorldTestBase):
    game = "Castlevania - Aria of Sorrow"


USA_ROM_NAME = "Castlevania - Aria of Sorrow (USA)/Castlevania - Aria of Sorrow (USA).gba"


def find_rom() -> str | None:
    """Return the path of a USA Aria of Sorrow ROM for the tests that read one, or None.

    The tests look at the CVAOS_USA_ROM and CVAOS_ROM environment variables first, then at the
    ROM that host.yaml names, then at the Downloads folder. A test that needs the ROM skips
    without one.
    """
    candidates = [os.environ.get("CVAOS_USA_ROM", ""), os.environ.get("CVAOS_ROM", "")]
    try:
        from settings import get_settings
        candidates.append(get_settings().cvaos_options.rom_file)
    except Exception:
        pass
    candidates.append(os.path.join(os.path.expanduser("~"), "Downloads", USA_ROM_NAME))
    return next((path for path in candidates if path and os.path.isfile(path)), None)


def find_collection_rom() -> bytes | None:
    """Return the Advance Collection's stock AoS ROM for the tests that read one, or None.

    The ROM is extracted from the collection that host.yaml names, or from the one at the default
    Steam path. Extracting only reads the collection's files. A test that needs the ROM skips
    without one, and also when the collection holds no pristine copy of it.
    """
    import hashlib

    from ..advance_collection.install import extract_base_rom_bytes, is_collection_exe
    from ..constants import AC_DEFAULT_EXE_PATH, AC_USA_ROM_MD5

    candidates = []
    try:
        from settings import get_settings
        candidates.append(str(get_settings().cvaos_options.collection_exe))
    except Exception:
        pass
    candidates.append(AC_DEFAULT_EXE_PATH)
    for exe in candidates:
        if exe and os.path.isfile(exe) and is_collection_exe(exe):
            try:
                rom = extract_base_rom_bytes(exe)
            except Exception:
                return None
            return rom if hashlib.md5(rom).hexdigest() == AC_USA_ROM_MD5 else None
    return None


def base_roms() -> dict[str, bytes]:
    """Return each base ROM that a test can patch: "cart" for a cart dump, and "collection" for
    the Advance Collection's copy. A ROM that can't be found is left out."""
    roms = {}
    cart = find_rom()
    if cart is not None:
        with open(cart, "rb") as fh:
            roms["cart"] = fh.read()
    collection = find_collection_rom()
    if collection is not None:
        roms["collection"] = collection
    return roms
