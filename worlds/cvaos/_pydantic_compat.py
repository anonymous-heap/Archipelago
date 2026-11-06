"""Single import point for the pydantic v1 API.

The cvaos data models use the pydantic v1 API so the world can run on frozen Archipelago
installs. pydantic cannot be pip-installed there, and pydantic v2 cannot be vendored, because
zipimport cannot load its compiled pydantic_core extension from an .apworld.

The names come from the first of these that is available:

1. ``pydantic.v1``, the v1 compatibility namespace of an installed pydantic v2 (pydantic
   1.10.17 and later have it too).
2. An installed pydantic v1.
3. The pure-Python pydantic 1.10.x vendored in ``worlds/cvaos/vendor``.
"""

__all__ = [
    "BaseModel",
    "parse_obj_as",
    "validator",
]

try:
    from pydantic.v1 import BaseModel, parse_obj_as, validator
except ImportError:
    try:
        from pydantic import VERSION as _INSTALLED_VERSION
    except ImportError:
        # No pydantic in the environment (typical for a frozen Archipelago install):
        # fall back to the vendored copy.
        from ._vendor import ensure_vendor_on_sys_path
        ensure_vendor_on_sys_path()
        from pydantic import BaseModel, parse_obj_as, validator
    else:
        if not _INSTALLED_VERSION.startswith("1."):
            raise  # pydantic v2 without a working pydantic.v1 namespace: broken install
        from pydantic import BaseModel, parse_obj_as, validator
