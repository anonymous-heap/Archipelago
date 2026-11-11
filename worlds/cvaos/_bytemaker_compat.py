"""Single import point for bytemaker.

bytemaker is pure Python, so a copy is vendored in ``worlds/cvaos/vendor`` for frozen
Archipelago installs, which cannot pip-install. Its bitarray dependency is an optional
``[speedups]`` extra, and without bitarray it uses a pure-Python ``BitVector``.

An installed bytemaker takes precedence over the vendored copy, unless it is too old for
this world. This world needs the ``bytemaker.spaces`` module, which only bytemaker 0.13 and
later have, so importing that module is the version check:

- When no bytemaker is installed, the import falls back to the vendored copy by appending
  the vendor directory to ``sys.path``.
- When the installed bytemaker is too old, its modules are removed from ``sys.modules`` and
  the vendored copy is loaded in their place (see ``_vendor.prefer_vendored``).

Checking the version number instead would not work, because a vendored copy has no
distribution metadata.

Below Python 3.13, bytemaker 0.13 and later require ``typing_extensions``. It is not vendored
here, because Archipelago's root requirements include it and the vendored pydantic needs it
too. Frozen installs therefore rely on the copy that Archipelago ships.

Type checkers read the vendored copy through the ``TYPE_CHECKING`` branch below, so pyright
and Pylance resolve every name without configuration. mypy does not. The vendored package's
own absolute imports, such as ``bytemaker.structs``, need the vendor directory on mypy's
search path, but with it there mypy finds each file under two module names and reports an
error. To check this world with mypy, install bytemaker 0.13 or later in the checking
environment. Against the vendored copy alone, mypy treats the bytemaker names as ``Any``.
"""

__all__ = [
    # records and their layout
    "Struct",
    "array",
    "field",
    "offset_of",
    "sizeof",
    # field/codec types
    "Buffer",
    "UInt8",
    "s16",
    "u4",
    "u8",
    "u16",
    "u32",
    # the address-space layer
    "Entry",
    "Patch",
    "PatchConflict",
    "PatchVerifyError",
    "Ptr",
    "Space",
    "count",
    "through",
    "unknown",
    "until",
]

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # A type checker cannot follow the runtime resolution below, so it reads the vendored copy
    # directly: that is the version this world is written against, whichever bytemaker an
    # environment happens to install. ``vendor`` is a namespace package, so the relative import
    # resolves without an ``__init__.py``.
    from .vendor.bytemaker import (Buffer, Struct, UInt8, array, field, offset_of, s16, sizeof, u4, u8, u16,
                                   u32)
    from .vendor.bytemaker.spaces import (Entry, Patch, PatchConflict, PatchVerifyError, Ptr, Space, count,
                                          through, unknown, until)
else:
    try:
        # The version check: only bytemaker 0.13 and later have this module.
        import bytemaker.spaces  # noqa: F401
    except ImportError:
        import sys

        from ._vendor import ensure_vendor_on_sys_path, prefer_vendored

        if "bytemaker" in sys.modules:
            # An installed bytemaker resolved, but it is too old for this world: replace it
            # with the vendored copy before anything else can hold a reference to it.
            prefer_vendored("bytemaker")
        else:
            ensure_vendor_on_sys_path()
        # Check again. A failure now is a real error rather than a missing or outdated install.
        import bytemaker.spaces  # noqa: F401,F811

    from bytemaker import Buffer, Struct, UInt8, array, field, offset_of, s16, sizeof, u4, u8, u16, u32
    from bytemaker.spaces import (Entry, Patch, PatchConflict, PatchVerifyError, Ptr, Space, count, through,
                                  unknown, until)
