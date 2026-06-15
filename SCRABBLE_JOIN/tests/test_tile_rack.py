from __future__ import annotations

import unittest

from scrabble_plotter.tile_rack import (
    can_build_word_from_rack,
    horizontal_word_squares,
    normalize_rack_letters,
    rack_slot_indices_for_word,
)


class TileRackTests(unittest.TestCase):
    def test_rack_letters_are_limited_to_seven(self) -> None:
        self.assertEqual(normalize_rack_letters("a b c d e f g h"), "ABCDEFG")

    def test_word_must_be_buildable_from_rack_letters(self) -> None:
        self.assertTrue(can_build_word_from_rack("CAT", "ACTRIDE"))
        self.assertFalse(can_build_word_from_rack("TREE", "TRACIDE"))

    def test_duplicate_letters_use_distinct_rack_slots(self) -> None:
        self.assertEqual(rack_slot_indices_for_word("TELL", "LTEALRS"), [1, 2, 0, 4])

    def test_horizontal_word_squares(self) -> None:
        self.assertEqual(horizontal_word_squares("F6", "CAT"), ["F6", "G6", "H6"])
        with self.assertRaises(ValueError):
            horizontal_word_squares("L12", "CAT")


if __name__ == "__main__":
    unittest.main()
