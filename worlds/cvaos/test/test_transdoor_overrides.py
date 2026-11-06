"""
The transdoor override file: a row that removes or adds a crossing must name its doors exactly,
and a removal must match a crossing in the default table.

    python -m pytest worlds/cvaos/test/test_transdoor_overrides.py -v
"""
from __future__ import annotations

import unittest
from unittest import mock

from ..data import routing_info

HEADER = {"from_entrance": "", "to_entrance": "", "does_exist": "", "is_override": ""}


def load_with_overrides(*rows: tuple[str, str, str, str]):
    """Load the transdoor table with ``rows`` as the whole override file."""
    override_rows = [dict(zip(HEADER, row)) for row in rows]
    real = routing_info.open_csv_if_exists

    def fake(package, filename):
        if filename == "override_transdoor_entrance_connections.csv":
            return iter(override_rows)
        return real(package, filename)

    with mock.patch.object(routing_info, "open_csv_if_exists", fake):
        return routing_info._load_transdoor_connections()


class OverrideTest(unittest.TestCase):
    def test_a_removal_takes_the_crossing_out(self):
        crossing = routing_info.transdoor_connection_rows[0]
        pair = (crossing.from_entrance, crossing.to_entrance)
        loaded = load_with_overrides((*pair, "FALSE", "FALSE"))
        self.assertNotIn(pair, {(c.from_entrance, c.to_entrance) for c in loaded})

    def test_a_removal_that_matches_no_crossing_fails_at_load(self):
        with self.assertRaisesRegex(ValueError, "match no crossing"):
            load_with_overrides(("000:001", "001:000", "FALSE", "FALSE"))

    def test_a_row_that_changes_the_table_must_name_its_doors_exactly(self):
        # Rooms 103 and 104 share two doors, so "103:104" alone names neither.
        for does_exist, is_override in (("FALSE", "FALSE"), ("TRUE", "TRUE")):
            with self.subTest(does_exist=does_exist, is_override=is_override):
                with self.assertRaisesRegex(ValueError, "names two different doors"):
                    load_with_overrides(("103:104", "104:103", does_exist, is_override))

    def test_a_row_that_only_restates_a_crossing_is_not_checked(self):
        # The file restates many crossings with untagged names; such rows change nothing.
        load_with_overrides(("103:104", "104:103", "TRUE", "FALSE"))


if __name__ == "__main__":
    unittest.main()
