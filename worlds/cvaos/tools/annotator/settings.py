"""
The annotator's settings, which are kept in its data folder and never in the repository.

The data folder is ``%LOCALAPPDATA%\\cvaos-annotator``. The file ``annotator_settings.json`` in
it holds the settings as one JSON object. Any of them may be left out:

* ``sheet_id``: the id of the Google sheet whose enemy tab the annotator fills in. It is the part
  of the sheet's address between ``/d/`` and the next ``/``.
* ``annotator``: the name written to "Annotated by".
* ``rom_path``: the path of the US ROM.
* ``emuhawk``: the path of ``EmuHawk.exe``.
* ``emuhawk_args``: the extra arguments that EmuHawk is launched with.

The page sets the name and the two paths. Nothing on the page sets ``sheet_id``, so add it to the
file by hand, and make the file if it isn't there::

    {"sheet_id": "the id from the sheet's address"}

A path written by hand needs each backslash doubled, as JSON requires, or forward slashes.
"""
from __future__ import annotations

import json
import os

from . import sheets

FILE = "annotator_settings.json"


def path() -> str:
    """Return the path of the settings file."""
    return sheets.data_path(FILE)


def load() -> dict:
    """Return the saved settings, or an empty dict when none are saved.

    Raise ValueError when the file doesn't hold one JSON object, and name the file in the message.
    """
    try:
        # "utf-8-sig" also reads a file that an editor saved with a byte order mark.
        with open(path(), encoding="utf-8-sig") as handle:
            saved = json.load(handle)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as error:
        raise ValueError(f"{path()} isn't valid JSON: {error}") from error
    if not isinstance(saved, dict):
        raise ValueError(f"{path()} must hold one JSON object.")
    return saved


def save(settings: dict) -> None:
    """Write the settings file, making the data folder first if it isn't there."""
    os.makedirs(sheets.DATA_DIR, exist_ok=True)
    with open(path(), "w", encoding="utf-8") as handle:
        json.dump(settings, handle, indent=2)


def us_rom_path() -> str:
    """Return the path of the US ROM, which is the saved ``rom_path`` setting.

    Raise FileNotFoundError when the setting is missing, or when no file is at its path.
    """
    rom_path = load().get("rom_path", "")
    if not rom_path:
        raise FileNotFoundError(f'No US ROM is set. Add "rom_path" to {path()}.')
    if not os.path.isfile(rom_path):
        raise FileNotFoundError(f"The US ROM isn't at {rom_path}, which is the path in {path()}.")
    return rom_path
