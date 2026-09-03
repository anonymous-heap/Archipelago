"""Single import point for bytemaker.

bytemaker is pure Python (bitarray is only an optional ``[speedups]`` extra with a
pure-Python ``BitVector`` fallback), so it can be vendored for frozen Archipelago
installs, which cannot pip-install. An installed bytemaker wins over the copy vendored
in ``worlds/cvaos/vendor`` — unless it is too old for this world.

This world needs the ``bytemaker.spaces`` surface (>= 0.13), so that import doubles as
the version probe: no bytemaker at all falls back to the vendored copy the usual way
(vendor dir appended to ``sys.path``), while an installed-but-stale bytemaker is
*detected* rather than silently preferred — its modules are evicted and the vendored
copy is loaded in their place (see ``_vendor.prefer_vendored``). A version-number check
would be worse: a vendored copy carries no dist metadata, so the probe asks for the
surface itself.

Note for frozen installs: bytemaker >= 0.13 hard-imports ``typing_extensions`` below
Python 3.13. Archipelago's root requirements pin it (and the vendored pydantic needs it
too), so nothing extra is vendored here — but it is load-bearing.

Type checkers read the vendored copy through the ``TYPE_CHECKING`` branch below, so
pyright/Pylance resolve every name without configuration. mypy cannot: the vendored
package's own absolute imports (``bytemaker.structs`` and so on) need the vendor directory
on its search path, and with it there mypy sees each file under two module names and
refuses. To mypy-check this world, install bytemaker >= 0.13 in the checking environment;
against the vendored copy alone mypy treats the bytemaker names as ``Any``.
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
        import bytemaker.spaces  # noqa: F401  — the probe: a surface only bytemaker >= 0.13 has
    except ImportError:
        import sys

        from ._vendor import ensure_vendor_on_sys_path, prefer_vendored

        if "bytemaker" in sys.modules:
            # An installed bytemaker resolved, but it is too old for this world: replace it
            # with the vendored copy before anything else can hold a reference to it.
            prefer_vendored("bytemaker")
        else:
            ensure_vendor_on_sys_path()
        import bytemaker.spaces  # noqa: F401,F811  — re-probe; a failure here is a real error

    from bytemaker import Buffer, Struct, UInt8, array, field, offset_of, s16, sizeof, u4, u8, u16, u32
    from bytemaker.spaces import (Entry, Patch, PatchConflict, PatchVerifyError, Ptr, Space, count, through,
                                  unknown, until)
