from __future__ import annotations

import json
import unittest

from scrabble_plotter.board import parse_square_label
from scrabble_plotter.calibration import MachineCalibration, PlotterCalibration
from scrabble_plotter.main import build_parser
from scrabble_plotter.serial_sender import format_move_command, format_reset_command


class SquareParserTests(unittest.TestCase):
    def test_valid_labels(self) -> None:
        square = parse_square_label("H8")
        self.assertEqual(square.col, 7)
        self.assertEqual(square.row, 7)
        self.assertEqual(square.label, "H8")

    def test_invalid_labels(self) -> None:
        for label in ("P1", "A0", "A16", "11A", ""):
            with self.assertRaises(ValueError):
                parse_square_label(label)


class MachineCalibrationTests(unittest.TestCase):
    def test_two_point_calibration_maps_expected_squares(self) -> None:
        machine = MachineCalibration.from_two_points(
            parse_square_label("A1"),
            0.0,
            0.0,
            parse_square_label("O15"),
            140.0,
            140.0,
        )

        self.assertEqual(machine.square_to_machine(parse_square_label("A1")), (0.0, 0.0))
        x, y = machine.square_to_machine(parse_square_label("H8"))
        self.assertAlmostEqual(x, 70.0, places=6)
        self.assertAlmostEqual(y, 70.0, places=6)

    def test_persistence_round_trip(self) -> None:
        calibration = PlotterCalibration()
        calibration.set_image_corners("board.jpg", [(0.0, 0.0), (150.0, 0.0), (150.0, 150.0), (0.0, 150.0)])
        calibration.set_machine_calibration(
            MachineCalibration.from_two_points(
                parse_square_label("A1"),
                10.0,
                20.0,
                parse_square_label("A2"),
                10.0,
                30.0,
            )
        )

        payload = json.loads(json.dumps(calibration.to_dict()))
        loaded = PlotterCalibration.from_dict(payload)
        x, y = loaded.square_center_in_machine(parse_square_label("A3"))
        self.assertAlmostEqual(x, 10.0, places=6)
        self.assertAlmostEqual(y, 40.0, places=6)
        self.assertEqual(loaded.image_path, "board.jpg")
        self.assertEqual(len(loaded.image_corners), 4)


class GCodeFormattingTests(unittest.TestCase):
    def test_gcode_formatting(self) -> None:
        self.assertEqual(format_move_command(10.0, 20.5, 1500.0), "G0 X10 Y20.5 F1500")

    def test_reset_command_formatting(self) -> None:
        self.assertEqual(format_reset_command(), "HOMEZERO")


class CliTests(unittest.TestCase):
    def test_gui_command_exists(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["gui"])
        self.assertEqual(args.command, "gui")


if __name__ == "__main__":
    unittest.main()
