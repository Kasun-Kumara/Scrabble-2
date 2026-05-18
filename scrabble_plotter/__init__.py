"""Scrabble board image-to-G-code sender."""

from .board import BOARD_SIZE, Square, parse_square_label
from .calibration import PlotterCalibration

__all__ = ["BOARD_SIZE", "PlotterCalibration", "Square", "parse_square_label"]
