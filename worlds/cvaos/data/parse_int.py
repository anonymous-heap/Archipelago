__all__ = [
    "parse_int",
    "parse_dec",
    "parse_hex",
]

def parse_int(value: str | int | None, *, base: int = 10) -> int | None:
    """Parse a CSV cell as an integer in ``base``, returning None for None or a blank cell.

    An int passes through unchanged. A leading ``0x`` is removed before parsing in any base.
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text == "":
        return None
    if text.startswith(("0x", "0X")):
        text = text[2:]
    return int(text, base)


parse_dec = lambda v: parse_int(v, base=10)
parse_hex = lambda v: parse_int(v, base=16)
