"""
EmuHawk as an :class:`~.emulator.Emulator`.

:class:`BizHawk` is a client of the annotator's ``bridge.lua``. It uses the bridge's stepping
mode, in which EmuHawk stays paused and runs frames only for a ``step`` request (see the header of
``bridge.lua``).

:func:`launch` starts an EmuHawk for this purpose. It shares nothing with an EmuHawk that a person
plays in, because it listens on its own port, runs on its own copy of EmuHawk's configuration, and
keeps its save file in its own folder.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import uuid
from typing import Iterable, Optional

from .emulator import check_buttons

PORT = 43091   # the annotator's own EmuHawk listens on 43090

# The settings that the launched EmuHawk's configuration changes. Sound and rewind cost time,
# and a save file backup would put a second save file beside the first. Frame skipping is fixed
# at none, which made no difference to the speed when it was measured.
SETTINGS = {"SoundEnabled": False, "SpeedPercent": 100, "RunInBackground": True,
            "AcceptBackgroundInput": False, "StartPaused": False, "PauseWhenMenuActivated": False,
            "SingleInstanceMode": False, "AutosaveSaveRAM": False, "BackupSaveram": False,
            "UpdateAutoCheckEnabled": False, "AutoMinimizeSkipping": False, "FrameSkip": 0}
# EmuHawk draws its window on every pass of its main loop, however many frames it skips. While
# the window is visible, that drawing holds an unthrottled game to about 260 frames a second,
# against about 920 with the window minimized. This setting stops the drawing, so the game runs
# at the higher rate either way and the window shows no picture.
NO_PICTURE = {"DispSpeedupFeatures": 0}
SW_SHOWMINNOACTIVE = 7


class BizHawkError(Exception):
    """EmuHawk's bridge is unreachable, or a command failed inside EmuHawk."""


class BizHawk:
    """A connection to ``bridge.lua`` that keeps EmuHawk in stepping mode.

    Making one connects to the bridge and pauses EmuHawk. Closing it ends stepping mode, and the
    game then runs freely again. It is also a context manager that closes itself.

    EmuHawk normally holds the game to 60 frames a second, and it does so for stepped frames
    too. A connection therefore removes that limit while it is open and puts it back when it
    closes, unless it is made with ``fast=False``.

    Saved states are held in EmuHawk's memory, at about 1.5 megabytes each, until they are
    dropped. Closing the connection drops the ones that are left.
    """

    def __init__(self, port: int = PORT, wait: float = 30.0, timeout: float = 30.0,
                 fast: bool = True) -> None:
        """Connect to the bridge on ``port``, retrying until it answers or ``wait`` seconds pass.

        ``timeout`` is how long a request may take, in seconds. A step is given longer, in
        proportion to its number of frames.
        """
        deadline = time.monotonic() + wait
        while True:
            try:
                self._sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise BizHawkError(f"EmuHawk's bridge didn't answer on port {port}.") from None
                time.sleep(0.5)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._file = self._sock.makefile("rb")
        self.timeout = timeout
        self._next_id = 0
        self._prefix = uuid.uuid4().hex[:8]
        self._states: set[str] = set()
        self._closed = False
        self._fast = fast
        self.call("hold", on=True)
        if fast:
            self.speed(throttle=False)

    def __enter__(self) -> "BizHawk":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def call(self, cmd: str, wait: Optional[float] = None, **args) -> dict:
        """Send one request to the bridge and return its reply. ``wait`` replaces the timeout."""
        if self._closed:
            raise BizHawkError("This connection to EmuHawk is closed.")
        self._next_id += 1
        request = json.dumps({"id": self._next_id, "cmd": cmd, **args}) + "\n"
        try:
            self._sock.settimeout(wait or self.timeout)
            self._sock.sendall(request.encode())
            while True:
                line = self._file.readline()
                if not line:
                    raise OSError("the connection closed")
                reply = json.loads(line)
                # The bridge also sends events, such as hotkey presses, which have no id.
                if reply.get("id") == self._next_id:
                    break
        except (OSError, ValueError) as error:
            self._closed = True
            self._sock.close()
            raise BizHawkError(f"EmuHawk didn't answer the {cmd} request: {error}") from None
        if "error" in reply:
            raise BizHawkError(reply["error"])
        return reply

    def read_many(self, ranges: Iterable[tuple[int, int]]) -> list[bytes]:
        reply = self.call("read", reads=[[address, length] for address, length in ranges])
        return [bytes(data) for data in reply["data"]]

    def read(self, address: int, length: int) -> bytes:
        return self.read_many([(address, length)])[0]

    def write_many(self, writes: Iterable[tuple[int, bytes]]) -> None:
        self.call("write", writes=[[address, list(data)] for address, data in writes])

    def write(self, address: int, data: bytes) -> None:
        self.write_many([(address, data)])

    def step(self, buttons: Iterable[str] = (), frames: int = 1) -> None:
        self.call("step", wait=self.timeout + frames / 20, buttons=list(check_buttons(buttons)),
                  frames=frames)

    def save(self) -> str:
        slot = f"{self._prefix}-{len(self._states)}-{self._next_id}"
        self.call("state_save", slot=slot)
        self._states.add(slot)
        return slot

    def load(self, state: str) -> None:
        self.call("state_load", slot=state)

    def drop(self, state: str) -> None:
        self.call("state_drop", slot=state)
        self._states.discard(state)

    def reset(self, keep_save: bool = False) -> None:
        """Reboot the game with a blank save, or with the save it has when ``keep_save`` is true.

        EmuHawk loses its saved states when the game reboots.
        """
        self.call("reboot", wait=max(self.timeout, 60.0), keep_save=keep_save)
        self._states.clear()

    def frame(self) -> int:
        """Return EmuHawk's frame counter, which a reset returns to 0."""
        return self.call("ping")["frame"]

    def speed(self, percent: Optional[int] = None, throttle: Optional[bool] = None,
              sound: Optional[bool] = None, frameskip: Optional[int] = None) -> None:
        """Change how fast EmuHawk runs frames. Each setting is left alone when it is None.

        ``percent`` is the speed that EmuHawk aims for, and ``throttle=False`` removes the limit
        altogether. ``frameskip`` is how many frames go undrawn for each one drawn. None of these
        changes what a frame does to the game. EmuHawk keeps each setting after the connection
        closes, apart from the limit that a ``fast`` connection restores.

        With the limit removed, an EmuHawk started by :func:`launch` runs about 900 frames a
        second in steps of many frames, and about 600 steps a second one frame at a time. Both
        were measured on the machine this was written on, and the room being played changes them
        by up to a third.
        """
        settings = {"percent": percent, "throttle": throttle, "sound": sound,
                    "frameskip": frameskip}
        self.call("speed", **{name: value for name, value in settings.items()
                              if value is not None})

    def close(self) -> None:
        if self._closed:
            return
        try:
            for state in list(self._states):
                self.drop(state)
            if self._fast:
                self.speed(throttle=True)
            self.call("hold", on=False)
        except BizHawkError:
            pass
        if not self._closed:
            self._closed = True
            self._sock.close()


def launch(emuhawk: str, rom_path: str, folder: str, port: int = PORT,
           minimized: bool = True, picture: bool = False) -> subprocess.Popen:
    """Start an EmuHawk for the search on the annotator's test ROM, and return its process.

    ``rom_path`` is the USA ROM, and the test ROM is built from it as the annotator does.
    ``folder`` receives everything this EmuHawk writes: the test ROM, a boot script, a
    configuration file and the game's save file. The save file is deleted first, so the game
    starts with a blank save.

    The configuration file is a copy of the one beside ``emuhawk`` with :data:`SETTINGS` applied,
    rewind switched off and the GBA save folder moved into ``folder``. The original is only read.

    The window starts minimized unless ``minimized`` is false, and the game runs either way. The
    window shows no picture unless ``picture`` is true, because drawing one in a visible window
    holds the game to under a third of its speed (see :data:`NO_PICTURE`). End the returned
    process when the work is done.
    """
    # Imported here because it loads the world, which no other part of this module needs.
    from ..annotator import emulator as annotator

    folder = os.path.abspath(folder)
    saves = os.path.join(folder, "SaveRAM")
    shutil.rmtree(saves, ignore_errors=True)
    os.makedirs(saves, exist_ok=True)
    rom = annotator.test_rom(rom_path, folder)

    config = {}
    original = os.path.join(os.path.dirname(emuhawk), "config.ini")
    if os.path.exists(original):
        with open(original, encoding="utf-8-sig") as handle:
            config = json.load(handle)
    config.update(SETTINGS)
    if not picture:
        config.update(NO_PICTURE)
    config.setdefault("Rewind", {})["Enabled"] = False
    paths = config.setdefault("PathEntries", {}).setdefault("Paths", [])
    paths[:] = [entry for entry in paths
                if (entry.get("System"), entry.get("Type")) != ("GBA", "Save RAM")]
    paths.append({"Type": "Save RAM", "Path": saves, "System": "GBA"})
    config_path = os.path.join(folder, "config.ini")
    with open(config_path, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)

    bridge = os.path.join(os.path.dirname(os.path.abspath(annotator.__file__)), "bridge.lua")
    boot = os.path.join(folder, "boot.lua")
    with open(boot, "w", encoding="utf-8") as handle:
        handle.write(f"ANNOTATOR_PORT = {port}\nANNOTATOR_AP_LUA = [[{annotator.lua_dir()}]]\n"
                     f"dofile([[{bridge}]])\n")

    startup = subprocess.STARTUPINFO()
    if minimized:
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = SW_SHOWMINNOACTIVE
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    # GDI drawing lets a second EmuHawk run beside one that uses the graphics card.
    return subprocess.Popen([emuhawk, "--gdi", f"--config={config_path}", f"--lua={boot}", rom],
                            cwd=os.path.dirname(emuhawk), creationflags=flags, close_fds=True,
                            startupinfo=startup)
