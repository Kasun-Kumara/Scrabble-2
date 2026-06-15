from __future__ import annotations

from collections import Counter


DEFAULT_RACK_SIZE = 7
DEFAULT_BOARD_SIZE = 12


def normalize_rack_letters(value: str, max_letters: int = DEFAULT_RACK_SIZE) -> str:
    letters = [character.upper() for character in str(value) if character.isalpha()]
    return "".join(letters[:max_letters])


def normalize_word(value: str) -> str:
    return "".join(character.upper() for character in str(value) if character.isalpha())


def can_build_word_from_rack(word: str, rack_letters: str) -> bool:
    word = normalize_word(word)
    rack_letters = normalize_rack_letters(rack_letters)
    if not word:
        return False
    word_counts = Counter(word)
    rack_counts = Counter(rack_letters)
    return all(rack_counts[letter] >= count for letter, count in word_counts.items())


def rack_slot_indices_for_word(word: str, rack_letters: str) -> list[int]:
    word = normalize_word(word)
    rack_letters = normalize_rack_letters(rack_letters)
    used_slots: set[int] = set()
    indices: list[int] = []

    for letter in word:
        for index, rack_letter in enumerate(rack_letters):
            if index not in used_slots and rack_letter == letter:
                used_slots.add(index)
                indices.append(index)
                break
        else:
            raise ValueError(f"The tile rack does not contain enough '{letter}' tiles.")

    return indices


def horizontal_word_squares(
    start_square: str,
    word: str,
    board_size: int = DEFAULT_BOARD_SIZE,
) -> list[str]:
    word = normalize_word(word)
    if not word:
        raise ValueError("Enter a word before placing rack tiles.")

    start_square = str(start_square).strip().upper()
    if len(start_square) < 2 or not start_square[0].isalpha() or not start_square[1:].isdigit():
        raise ValueError("Enter a valid starting square, for example F6.")

    start_col = ord(start_square[0]) - ord("A")
    start_row = int(start_square[1:]) - 1
    if start_row < 0 or start_row >= board_size or start_col < 0 or start_col >= board_size:
        raise ValueError("The starting square is outside the board.")
    if start_col + len(word) > board_size:
        raise ValueError("The word does not fit horizontally from the starting square.")

    return [
        f"{chr(ord('A') + start_col + index)}{start_row + 1}"
        for index in range(len(word))
    ]
