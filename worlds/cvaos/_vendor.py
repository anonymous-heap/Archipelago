"""sys.path bootstrap for pure-Python dependencies vendored in ``worlds/cvaos/vendor``.

Works both as a loose world folder and inside a zipped .apworld: zipimport accepts
sys.path entries that point inside a zip archive, as long as the path is normalized
(no ``..`` segments).
"""

import importlib.machinery
import importlib.util
import os
import sys

_VENDOR_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))


def ensure_vendor_on_sys_path() -> None:
    """Make the vendored packages importable (idempotent).

    The vendor dir is appended rather than prepended so that packages installed in the
    environment win; the vendored copies are a fallback for frozen Archipelago
    installs, which cannot pip-install anything.
    """
    if _VENDOR_DIR not in sys.path:
        sys.path.append(_VENDOR_DIR)


def prefer_vendored(package: str) -> None:
    """Re-resolve ``package`` from the vendor dir even though an (older) copy is installed.

    ``ensure_vendor_on_sys_path`` appends, so an environment install always wins the
    first resolution. When that install is too old for this world, the caller uses this
    to evict it from ``sys.modules`` and load the vendored copy under the same name.

    The override is scoped to ``package`` alone: rather than promoting the whole vendor
    dir to the front of ``sys.path`` (which would also shadow every *other* vendored
    package — e.g. hand pydantic imports the vendored 1.10 in an environment that has
    pydantic 2), the package is located on the vendor path explicitly. Submodule imports
    then resolve through the loaded package's own ``__path__``, which points into the
    vendor dir. Safe only while no other module holds references to the evicted classes,
    i.e. when called from the package's single import point.
    """
    for name in [m for m in sys.modules if m == package or m.startswith(package + ".")]:
        del sys.modules[name]
    spec = importlib.machinery.PathFinder.find_spec(package, [_VENDOR_DIR])
    if spec is None or spec.loader is None:
        raise ImportError(f"vendored copy of {package!r} not found under {_VENDOR_DIR}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[package] = module  # registered first: the package imports itself during exec
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(package, None)
        raise
