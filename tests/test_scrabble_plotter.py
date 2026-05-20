from __future__ import annotations

import json
from pathlib import Path
import unittest

from scrabble_plotter.board import BOARD_SIZE, CELL_SIZE_MM, parse_square_label
from scrabble_plotter.calibration import PlotterCalibration, path_for_storage, resolve_stored_path
from scrabble_plotter.main import build_parser
from scrabble_plotter.overlay import cell_label_positions, grid_segments, project_board_points
from scrabble_plotter.serial_sender import format_move_command, format_reset_command, format_steps_command


class SquareParserTests(unittest.TestCase):
    def test_valid_labels(self) -> None:
        examples = {
            "A1": (0, 0, "A1"),
            "a1": (0, 0, "A1"),
            "H8": (7, 7, "H8"),
            "L12": (11, 11, "L12"),
        }

        for raw, expected in examples.items():
            with self.subTest(raw=raw):
                square = parse_square_label(raw)
                self.assertEqual((square.col, square.row, square.label), expected)

    def test_invalid_labels(self) -> None:
        for label in ("M1", "A0", "A13", "O15", "11A", ""):
            with self.subTest(label=label), self.assertRaises(ValueError):
                parse_square_label(label)


class PlotterCalibrationTests(unittest.TestCase):
    def test_fixed_coordinate_mapping_with_default_offset(self) -> None:
        calibration = PlotterCalibration()

        expected = {
            "A1": (15.0, 15.0),
            "A2": (15.0, 45.0),
            "B1": (45.0, 15.0),
            "L12": (345.0, 345.0),
        }

        for label, target in expected.items():
            with self.subTest(label=label):
                self.assertEqual(calibration.square_center_in_machine(parse_square_label(label)), target)

    def test_fixed_coordinate_mapping_with_board_corner_offset(self) -> None:
        calibration = PlotterCalibration(offset_x_mm=10.0, offset_y_mm=20.0)

        self.assertEqual(calibration.square_center_in_machine(parse_square_label("A1")), (25.0, 35.0))
        self.assertEqual(calibration.square_center_in_machine(parse_square_label("L12")), (355.0, 365.0))

    def test_fixed_coordinate_mapping_with_custom_cell_size(self) -> None:
        calibration = PlotterCalibration(offset_x_mm=10.0, offset_y_mm=20.0, cell_size_mm=25.0)

        self.assertEqual(calibration.square_center_in_machine(parse_square_label("A1")), (22.5, 32.5))
        self.assertEqual(calibration.square_center_in_machine(parse_square_label("A2")), (22.5, 57.5))

    def test_persistence_round_trip(self) -> None:
        calibration = PlotterCalibration(
            camera_index=2,
            offset_x_mm=12.5,
            offset_y_mm=24.5,
            cell_size_mm=25.0,
            cart_x_mm=111.0,
            cart_y_mm=222.0,
            image_corners=[[0.0, 0.0], [360.0, 0.0], [360.0, 360.0], [0.0, 360.0]],
        )

        payload = json.loads(json.dumps(calibration.to_dict()))
        loaded = PlotterCalibration.from_dict(payload)

        self.assertEqual(loaded.board_size, BOARD_SIZE)
        self.assertEqual(loaded.cell_size_mm, 25.0)
        self.assertEqual(loaded.camera_index, 2)
        self.assertEqual(loaded.offset_x_mm, 12.5)
        self.assertEqual(loaded.offset_y_mm, 24.5)
        self.assertEqual(loaded.x_steps_per_mm, 80.0)
        self.assertEqual(loaded.y_steps_per_mm, 80.0)
        self.assertEqual(loaded.cart_x_mm, 111.0)
        self.assertEqual(loaded.cart_y_mm, 222.0)
        self.assertEqual(len(loaded.image_corners), 4)

    def test_persistence_round_trip_includes_step_settings(self) -> None:
        calibration = PlotterCalibration(
            x_steps_per_mm=100.0,
            y_steps_per_mm=200.0,
        )

        payload = json.loads(json.dumps(calibration.to_dict()))
        loaded = PlotterCalibration.from_dict(payload)

        self.assertEqual(loaded.x_steps_per_mm, 100.0)
        self.assertEqual(loaded.y_steps_per_mm, 200.0)

    def test_image_path_can_be_stored_relative_to_calibration_file(self) -> None:
        calibration_path = Path("project/calibration/board.json")
        image_path = Path("project/images/board.jpg")

        stored_path = path_for_storage(image_path, calibration_path)

        self.assertEqual(Path(stored_path), Path("../images/board.jpg"))
        self.assertEqual(resolve_stored_path(stored_path, calibration_path), image_path.resolve())

    def test_square_center_in_image_uses_camera_corners(self) -> None:
        calibration = PlotterCalibration(
            image_corners=[[0.0, 0.0], [360.0, 0.0], [360.0, 360.0], [0.0, 360.0]],
        )

        self.assertEqual(calibration.square_center_in_image(parse_square_label("A1")), (15.0, 15.0))
        self.assertEqual(calibration.square_center_in_image(parse_square_label("L12")), (345.0, 345.0))

    def test_cart_position_in_machine(self) -> None:
        calibration = PlotterCalibration(cart_x_mm=90.0, cart_y_mm=140.0)

        self.assertEqual(calibration.cart_position_in_machine(), (90.0, 140.0))


class OverlayTests(unittest.TestCase):
    def test_project_board_points(self) -> None:
        corners = [(0.0, 0.0), (360.0, 0.0), (360.0, 360.0), (0.0, 360.0)]

        points = project_board_points(corners, [(0.5, 0.5), (11.5, 11.5)])

        self.assertEqual(points[0], (15.0, 15.0))
        self.assertEqual(points[1], (345.0, 345.0))

    def test_grid_segments_and_labels(self) -> None:
        corners = [(0.0, 0.0), (360.0, 0.0), (360.0, 360.0), (0.0, 360.0)]

        self.assertEqual(len(grid_segments(corners)), (BOARD_SIZE + 1) * 2)
        labels = dict(cell_label_positions(corners))
        self.assertEqual(labels["A1"], (15.0, 15.0))
        self.assertEqual(labels["L12"], (345.0, 345.0))


class GCodeFormattingTests(unittest.TestCase):
    def test_gcode_formatting(self) -> None:
        self.assertEqual(format_move_command(10.0, 20.5, 1500.0), "G0 X10 Y20.5 F1500")

    def test_reset_command_formatting(self) -> None:
        self.assertEqual(format_reset_command(), "HOMEZERO")

    def test_steps_command_formatting(self) -> None:
        self.assertEqual(format_steps_command(80.0, 100.5), "STEPS X80 Y100.5")


class CliTests(unittest.TestCase):
    def test_gui_command_exists(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["gui"])
        self.assertEqual(args.command, "gui")

    def test_set_offset_command_exists(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["set-offset", "--calibration", "board.json", "--x", "10", "--y", "20"])
        self.assertEqual(args.command, "set-offset")
        self.assertEqual(args.x, 10.0)
        self.assertEqual(args.y, 20.0)

    def test_move_gcode_command_does_not_replace_subcommand(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "move",
                "--calibration",
                "board.json",
                "--square",
                "A1",
                "--port",
                "COM3",
                "--command",
                "G1",
                "--dry-run",
            ]
        )
        self.assertEqual(args.command, "move")
        self.assertEqual(args.gcode_command, "G1")


if __name__ == "__main__":
    unittest.main()
