# Vendored dependencies

Pure-Python packages bundled so the world works on frozen Archipelago installs, which
cannot pip-install anything. Compiled extensions cannot live here: zipimport cannot
load `.pyd`/`.so` files from a zipped `.apworld`.

This directory is added to `sys.path` (appended, so environment installs win) by
`worlds/cvaos/_vendor.py`, on demand from `worlds/cvaos/_pydantic_compat.py` and
`worlds/cvaos/_bytemaker_compat.py`.

Note: the directory is named `vendor` rather than `lib` because the repo .gitignore
excludes any `lib/` directory as a build artifact.

| Package | Version | Source | License |
|---|---|---|---|
| pydantic | 1.10.26 | `pydantic-1.10.26-py3-none-any.whl` from PyPI, unmodified | MIT (see `pydantic/LICENSE`) |
| bytemaker | 0.13.0.dev0 | source tree of the `experimental-spaces` branch at `a1dd090` (2026-09-02), `__pycache__` stripped, line endings normalized | MIT (see `bytemaker/LICENSE`) |

Without bitarray installed (frozen installs), bytemaker uses its pure-Python
`BitVector`; with bitarray (`pip install bytemaker[speedups]`) it uses the C-backed
fast path automatically.

**bytemaker requires `typing_extensions` on Python < 3.13** (its `typing_redirect`
imports `Buffer` unconditionally there). This is not an extra vendoring burden:
Archipelago's root `requirements.txt` already pins `typing_extensions`, and the vendored
pydantic needs it too — but it is load-bearing for a frozen install, so keep that pin.

`_bytemaker_compat.py` probes for the `bytemaker.spaces` surface (which only >= 0.13
has) rather than a version number, because a vendored copy carries no dist metadata:
`bytemaker.__version__` is resolved from whatever `bytemaker` distribution *is* installed
in the environment, so with a stale 0.11 install present it reports `0.11.0` for the
vendored 0.13 code (observed 2026-09-01), and with none present it falls back to a
literal. It cannot be trusted to identify the vendored copy. An installed-but-stale
bytemaker is instead detected by the surface probe and superseded by the vendored copy
(`_vendor.prefer_vendored`) rather than silently shadowing it.

To update **bytemaker**: replace the `bytemaker/` package directory wholesale from a
released `py3-none-any` wheel once 0.13.0 ships to PyPI (currently a dev build vendored
from source), strip any `__pycache__`, and copy the fork's `LICENCE.md` to
`bytemaker/LICENSE`. Then bump the version above and, when the release is on PyPI, bump
`worlds/cvaos/requirements.txt` to `bytemaker>=0.13`.

To update **pydantic**: download the new `py3-none-any` wheel from PyPI, extract, and
replace the package directory wholesale. Stay on the 1.10.x line — pydantic v2 requires
the compiled `pydantic_core`.
