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
