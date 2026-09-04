"""
A small THUMB assembler for this package's ROM hooks.

Every hook here ships as a hex blob in its .py module with the assembly it came from beside it in
a .s file, and until now nothing connected the two. The .s was documentation: editing it left the
blob untouched, and one of them went missing entirely without any test noticing. This module
closes that gap, so a test can assert a blob really is what its source says it is, and so revising
a hook is a source edit rather than a hand-laid literal pool and a page of recomputed offsets.

Deliberately NOT a general assembler. It implements the idioms these files use and nothing else:
``.equ`` constants, labels, ``ldr rN, =expr`` with an automatically placed literal pool, local
branches, and ``.word`` / ``.hword`` / ``.align`` / ``.pool``. Instruction encoding is delegated to
keystone; this module only does the parts keystone cannot, which are resolving symbols and laying
out the pool. Anything unrecognised raises :class:`AsmError` rather than being skipped, because a
silently dropped line would assemble into a blob that looks plausible and is wrong.

keystone is a dev-time dependency, imported lazily. Nothing at run time needs it, and the tests
that use this module skip when it is absent.

Two conventions worth knowing, both chosen to match the blobs that already shipped:

* the literal pool is emitted in first-use order, deduplicated by expression text (GAS
  deduplicates by value instead; for these files the two agree);
* ``.align n`` aligns to ``2**n`` bytes, as it does for GAS on ARM.
"""
from __future__ import annotations

import re
import struct
import sys
from typing import Dict, List, Optional, Tuple

#: Condition codes for the T1 conditional branch encoding.
_CONDITIONS = {
    "eq": 0x0, "ne": 0x1, "cs": 0x2, "hs": 0x2, "cc": 0x3, "lo": 0x3,
    "mi": 0x4, "pl": 0x5, "vs": 0x6, "vc": 0x7, "hi": 0x8, "ls": 0x9,
    "ge": 0xA, "lt": 0xB, "gt": 0xC, "le": 0xD,
}

#: Directives that carry no bytes and need no interpretation here.
_IGNORED_DIRECTIVES = frozenset(
    (".syntax", ".thumb", ".text", ".global", ".arch", ".thumb_func", ".code", ".type", ".size")
)

_IDENTIFIER = re.compile(r"(?<![\w$.])[A-Za-z_.$][A-Za-z_0-9.$]*")
_LABEL = re.compile(r"^([A-Za-z_.$][A-Za-z_0-9.$]*):\s*(.*)$")
_LDR_LITERAL = re.compile(r"^ldr\s+(r[0-9]|r1[0-5]|sp|lr|pc)\s*,\s*=\s*(.+)$", re.IGNORECASE)
_BRANCH = re.compile(r"^(b|bl|bx?)([a-z][a-z])?\s+([A-Za-z_.$][A-Za-z_0-9.$]*)\s*$", re.IGNORECASE)


class AsmError(Exception):
    """A line this assembler does not implement, or an operand that will not encode."""


def assemble(source: str, base: int) -> bytes:
    """Assemble ``source`` as THUMB linked at GBA address ``base``.

    ``base`` matters because these hooks are position-dependent: the literal pool holds absolute
    addresses and ``ldr rN, =x`` resolves against the program counter.
    """
    if base % 2:
        raise AsmError(f"base address {base:#x} is not halfword aligned")
    items, symbols = _parse(source)
    labels, pool_order = _layout(items, base)
    return _emit(items, _resolve(symbols, labels), labels, pool_order, base)


def assemble_file(path: str, base: int) -> bytes:
    """:func:`assemble` the .s file at ``path``."""
    with open(path, "r", encoding="ascii") as handle:
        return assemble(handle.read(), base)


def _print_listing(blob: bytes, base: int, code_size: int) -> None:
    """Disassemble ``blob`` so the result can be read back against the source.

    Worth doing when authoring a hook: it is the check that the bytes say what the source meant.
    capstone is optional, and its absence is reported rather than fatal.
    """
    try:
        from capstone import CS_ARCH_ARM, CS_MODE_THUMB, Cs
    except ImportError:
        print("(no listing: pip install capstone)", file=sys.stderr)
        return
    for instruction in Cs(CS_ARCH_ARM, CS_MODE_THUMB).disasm(blob[:code_size], base):
        print(f"  {instruction.address:#010x}  {instruction.bytes.hex():8s}  "
              f"{instruction.mnemonic} {instruction.op_str}")
    for offset in range(code_size, len(blob), 4):
        word = int.from_bytes(blob[offset:offset + 4], "little")
        print(f"  {base + offset:#010x}  {blob[offset:offset + 4].hex()}  .word {word:#010x}")


def _main(argv: List[str]) -> int:
    """Print a source's blob as hex, for pasting into its module.

    Run as a plain script rather than with ``-m``: this module imports nothing from the package,
    so a script run skips loading every Archipelago world.
    """
    listing = "--listing" in argv
    argv = [argument for argument in argv if argument != "--listing"]
    if len(argv) != 2:
        print("usage: python thumb_assembler.py [--listing] <hook.s> <link address>",
              file=sys.stderr)
        print("example (from this directory):", file=sys.stderr)
        print("    python thumb_assembler.py received_item_box.s 0x087D0300", file=sys.stderr)
        return 2
    base = int(argv[1], 0)
    try:
        blob = assemble_file(argv[0], base)
    except AsmError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(blob.hex())
    if listing:
        # The pool is the tail; everything before it is code.
        items, symbols = _parse(open(argv[0], encoding="ascii").read())
        _, pool_order = _layout(items, base)
        _print_listing(blob, base, len(blob) - 4 * len(pool_order))
    return 0



# --- parsing -------------------------------------------------------------------------------

# One entry per source line that produces bytes or a label, in order. The kinds are:
#   ("label", name)            ("insn", text)          ("ldr_literal", register, expression)
#   ("branch", mnemonic, cond, target)                 ("bl", target)
#   ("word", expression)       ("hword", expression)   ("align", n)        ("pool",)
Item = Tuple


def _strip_comment(line: str) -> str:
    return line.split("@", 1)[0].rstrip()


def _parse(source: str) -> Tuple[List[Item], Dict[str, str]]:
    """Source to items plus the ``.equ`` table (values stay as text and are evaluated later)."""
    items: List[Item] = []
    symbols: Dict[str, str] = {}
    for number, raw in enumerate(source.splitlines(), start=1):
        line = _strip_comment(raw).strip()
        while line:
            match = _LABEL.match(line)
            if not match:
                break        # a directive, or an instruction: only a label carries the colon
            items.append(("label", match.group(1)))
            line = match.group(2).strip()
        if not line:
            continue
        try:
            items.extend(_parse_statement(line, symbols))
        except AsmError as error:
            raise AsmError(f"line {number}: {error}\n    {raw.strip()}") from None
    return items, symbols


def _parse_statement(line: str, symbols: Dict[str, str]) -> List[Item]:
    lowered = line.lower()
    if line.startswith("."):
        directive = lowered.split()[0]
        rest = line[len(directive):].strip()
        if directive == ".equ":
            name, _, value = rest.partition(",")
            if not value.strip():
                raise AsmError(".equ needs a name and a value")
            symbols[name.strip()] = value.strip()
            return []
        if directive in (".pool", ".ltorg"):
            return [("pool",)]
        if directive == ".align":
            return [("align", int(rest or "2", 0))]
        if directive in (".word", ".long"):
            return [("word", piece.strip()) for piece in rest.split(",")]
        if directive in (".hword", ".short"):
            return [("hword", piece.strip()) for piece in rest.split(",")]
        if directive in _IGNORED_DIRECTIVES:
            return []
        raise AsmError(f"unimplemented directive {directive}")

    literal = _LDR_LITERAL.match(line)
    if literal:
        return [("ldr_literal", literal.group(1).lower(), literal.group(2).strip())]

    branch = _BRANCH.match(line)
    if branch:
        mnemonic, condition, target = (part.lower() if part else part for part in branch.groups())
        if mnemonic == "bx":
            return [("insn", line)]                       # bx takes a register, not a label
        if mnemonic == "bl":
            if condition:
                raise AsmError("conditional bl is not available on ARMv4T")
            return [("bl", branch.group(3))]
        if condition and condition not in _CONDITIONS:
            raise AsmError(f"unknown condition code {condition!r}")
        return [("branch", mnemonic, condition, branch.group(3))]

    return [("insn", line)]


# --- layout --------------------------------------------------------------------------------


def _item_size(item: Item, pool_size: int) -> int:
    kind = item[0]
    if kind == "label":
        return 0
    if kind in ("insn", "ldr_literal", "branch", "hword"):
        return 2
    if kind in ("bl", "word"):
        return 4
    if kind in ("align", "pool"):
        return pool_size            # resolved by the caller, which knows the address
    raise AsmError(f"unsized item {kind}")


def _layout(items: List[Item], base: int) -> Tuple[Dict[str, int], List[str]]:
    """Assign an address to every label and fix the pool's contents and order.

    The pool is collected here, in first-use order, so its size is known before anything after it
    is placed. Pool entries are keyed by expression text, which needs no label addresses and so
    avoids a circular dependency between the pool's size and the labels it may refer to.
    """
    pool_order: List[str] = []
    for item in items:
        if item[0] == "ldr_literal" and item[2] not in pool_order:
            pool_order.append(item[2])

    labels: Dict[str, int] = {}
    address = base
    emitted_pool = False
    for item in items:
        if item[0] == "label":
            if item[1] in labels:
                raise AsmError(f"label {item[1]!r} defined twice")
            labels[item[1]] = address
            continue
        if item[0] == "align":
            step = 1 << item[1]
            address += -address % step
            continue
        if item[0] == "pool":
            address += -address % 4
            if not emitted_pool:
                address += 4 * len(pool_order)
                emitted_pool = True
            continue
        address += _item_size(item, 0)

    if pool_order and not emitted_pool:
        raise AsmError(
            f"{len(pool_order)} literal(s) need a pool, but the source has no .pool or .ltorg"
        )
    return labels, pool_order


# --- emission ------------------------------------------------------------------------------


def _resolve(symbols: Dict[str, str], labels: Dict[str, int]) -> Dict[str, int]:
    """The whole symbol table as integers: label addresses plus every ``.equ``.

    A ``.equ`` value may name other symbols, so names resolve on demand with the chain in
    progress tracked. That turns a circular definition into a clear error rather than a stack
    overflow.
    """
    table: Dict[str, int] = dict(labels)
    chain: List[str] = []

    def resolve(name: str) -> int:
        if name in table:
            return table[name]
        if name in chain:
            raise AsmError(f"circular .equ definition: {' -> '.join(chain + [name])}")
        chain.append(name)
        try:
            table[name] = _evaluate_with(symbols[name], resolve)
        finally:
            chain.pop()
        return table[name]

    for name in symbols:
        if name in labels:
            raise AsmError(f"{name!r} is both a label and an .equ")
        resolve(name)
    return table


def _evaluate_with(expression: str, resolve) -> int:
    """Evaluate a constant expression, looking each name it mentions up through ``resolve``.

    Names are substituted as literals rather than passed in a scope, because a GAS local label
    like ``.Ldone`` is not a Python identifier and would be a syntax error.
    """
    unresolved: List[str] = []

    def replace(match: "re.Match[str]") -> str:
        name = match.group(0)
        try:
            return f"({resolve(name)})"
        except KeyError:
            unresolved.append(name)
            return name

    text = _IDENTIFIER.sub(replace, expression)
    if unresolved:
        raise AsmError(f"undefined symbol {', '.join(sorted(set(unresolved)))} in {expression!r}")
    try:
        value = eval(text, {"__builtins__": {}}, {})              # noqa: S307 - our own sources
    except AsmError:
        raise
    except Exception as error:                                    # noqa: BLE001
        raise AsmError(f"cannot evaluate {expression!r} (as {text!r}): {error}") from None
    if not isinstance(value, int):
        raise AsmError(f"{expression!r} is not an integer")
    return value


def _evaluate(expression: str, table: Dict[str, int]) -> int:
    return _evaluate_with(expression, lambda name: table[name])


def _expand_symbols(text: str, table: Dict[str, int]) -> str:
    """Substitute symbol names with their values, so keystone never sees one.

    Only names in the table are touched, and register names are never in it.
    """
    def replace(match: "re.Match[str]") -> str:
        name = match.group(0)
        return str(table[name]) if name in table else name

    return _IDENTIFIER.sub(replace, text)


def _encode_branch(mnemonic: str, condition: Optional[str], target: int, address: int) -> bytes:
    offset = target - (address + 4)
    if offset % 2:
        raise AsmError(f"branch to {target:#x} is not halfword aligned")
    if condition:
        if not -256 <= offset <= 254:
            raise AsmError(f"conditional branch out of range ({offset} bytes; limit -256..254)")
        return struct.pack("<H", 0xD000 | (_CONDITIONS[condition] << 8) | ((offset >> 1) & 0xFF))
    if not -2048 <= offset <= 2046:
        raise AsmError(f"branch out of range ({offset} bytes; limit -2048..2046)")
    return struct.pack("<H", 0xE000 | ((offset >> 1) & 0x7FF))


def _encode_bl(target: int, address: int) -> bytes:
    offset = target - (address + 4)
    if offset % 2:
        raise AsmError(f"bl to {target:#x} is not halfword aligned")
    if not -(1 << 22) <= offset < (1 << 22):
        raise AsmError(f"bl out of range ({offset} bytes; limit +/-4MB)")
    return struct.pack("<HH", 0xF000 | ((offset >> 12) & 0x7FF), 0xF800 | ((offset >> 1) & 0x7FF))


def _encode_ldr_literal(register: str, slot: int, address: int) -> bytes:
    # LDR (literal) reads from a PC-relative slot, and the PC it uses is the instruction's own
    # address plus 4, rounded DOWN to a word boundary.
    offset = slot - ((address + 4) & ~3)
    if offset < 0 or offset > 1020 or offset % 4:
        raise AsmError(f"literal slot {slot:#x} is not reachable from {address:#x} (offset {offset})")
    number = int(register[1:]) if register.startswith("r") else -1
    if not 0 <= number <= 7:
        raise AsmError(f"ldr {register}, =x needs a low register (r0-r7)")
    return struct.pack("<H", 0x4800 | (number << 8) | (offset >> 2))


def _keystone():
    try:
        import keystone
    except ImportError as error:                              # pragma: no cover - dev-time only
        raise AsmError(
            "keystone is needed to assemble (pip install keystone-engine); it is a dev-time "
            "dependency, so tests that assemble skip without it"
        ) from error
    return keystone.Ks(keystone.KS_ARCH_ARM, keystone.KS_MODE_THUMB)


def _emit(items: List[Item], table: Dict[str, int], labels: Dict[str, int],
          pool_order: List[str], base: int) -> bytes:
    assembler = None            # keystone, built on the first instruction that needs it
    slots = {}                      # expression -> address, filled in when the pool is placed
    out = bytearray()
    emitted_pool = False

    def address() -> int:
        return base + len(out)

    # The pool's address is needed before the instructions that read from it are encoded, so walk
    # the layout once more to find it.
    pool_at = None
    cursor = base
    for item in items:
        if item[0] == "label":
            continue
        if item[0] == "align":
            cursor += -cursor % (1 << item[1])
        elif item[0] == "pool":
            cursor += -cursor % 4
            if pool_at is None:
                pool_at = cursor
                cursor += 4 * len(pool_order)
        else:
            cursor += _item_size(item, 0)
    if pool_at is not None:
        slots = {expression: pool_at + 4 * index for index, expression in enumerate(pool_order)}

    for item in items:
        kind = item[0]
        if kind == "label":
            if labels[item[1]] != address():
                raise AsmError(f"internal: label {item[1]!r} moved between layout and emission")
            continue
        if kind == "align":
            out.extend(b"\x00" * (-address() % (1 << item[1])))
            continue
        if kind == "pool":
            out.extend(b"\x00" * (-address() % 4))
            if not emitted_pool:
                for expression in pool_order:
                    out.extend(struct.pack("<I", _evaluate(expression, table) & 0xFFFFFFFF))
                emitted_pool = True
            continue
        if kind == "word":
            out.extend(struct.pack("<I", _evaluate(item[1], table) & 0xFFFFFFFF))
            continue
        if kind == "hword":
            out.extend(struct.pack("<H", _evaluate(item[1], table) & 0xFFFF))
            continue
        if kind == "ldr_literal":
            out.extend(_encode_ldr_literal(item[1], slots[item[2]], address()))
            continue
        if kind == "branch":
            target = _evaluate(item[3], table)
            out.extend(_encode_branch(item[1], item[2], target, address()))
            continue
        if kind == "bl":
            out.extend(_encode_bl(_evaluate(item[1], table), address()))
            continue
        if kind == "insn":
            if assembler is None:
                assembler = _keystone()
            text = _expand_symbols(item[1], table)
            try:
                encoding, _count = assembler.asm(text, address())
            except Exception as error:                        # noqa: BLE001 - keystone's own type
                raise AsmError(f"keystone rejected {item[1]!r} (as {text!r}): {error}") from None
            if encoding is None:
                raise AsmError(f"keystone could not encode {item[1]!r} (as {text!r})")
            if len(encoding) != 2:
                raise AsmError(
                    f"{item[1]!r} assembled to {len(encoding)} bytes; ARMv4T THUMB is all "
                    f"halfwords, so this is probably a THUMB-2 instruction the GBA cannot run"
                )
            out.extend(encoding)
            continue
        raise AsmError(f"internal: unhandled item {kind}")

    return bytes(out)


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
