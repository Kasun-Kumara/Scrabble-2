from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .board import BOARD_SIZE, Square, parse_square_label


def _require_cv2():
    try:
        import cv2  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "OpenCV is required for image calibration. Install scrabble_plotter/requirements.txt."
        ) from exc
    return cv2


@dataclass
class MachineCalibration:
    reference_square: str
    reference_x: float
    reference_y: float
    unit_col_x: float
    unit_col_y: float
    unit_row_x: float
    unit_row_y: float
    square_pitch: float
    rotation_degrees: float
    second_reference_square: str
    second_reference_x: float
    second_reference_y: float

    def square_to_machine(self, square: Square) -> tuple[float, float]:
        reference = parse_square_label(self.reference_square)
        delta_col = square.col - reference.col
        delta_row = square.row - reference.row
        x = self.reference_x + delta_col * self.unit_col_x + delta_row * self.unit_row_x
        y = self.reference_y + delta_col * self.unit_col_y + delta_row * self.unit_row_y
        return (x, y)

    @classmethod
    def from_two_points(
        cls,
        square1: Square,
        x1: float,
        y1: float,
        square2: Square,
        x2: float,
        y2: float,
    ) -> "MachineCalibration":
        board_dx = square2.col - square1.col
        board_dy = square2.row - square1.row
        if board_dx == 0 and board_dy == 0:
            raise ValueError("Reference squares must be different.")

        machine_dx = x2 - x1
        machine_dy = y2 - y1
        board_distance = math.hypot(board_dx, board_dy)
        machine_distance = math.hypot(machine_dx, machine_dy)
        if machine_distance == 0:
            raise ValueError("Reference machine points must be different.")

        square_pitch = machine_distance / board_distance
        rotation_radians = math.atan2(machine_dy, machine_dx) - math.atan2(board_dy, board_dx)
        unit_col_x = square_pitch * math.cos(rotation_radians)
        unit_col_y = square_pitch * math.sin(rotation_radians)
        unit_row_x = -square_pitch * math.sin(rotation_radians)
        unit_row_y = square_pitch * math.cos(rotation_radians)

        return cls(
            reference_square=square1.label,
            reference_x=x1,
            reference_y=y1,
            unit_col_x=unit_col_x,
            unit_col_y=unit_col_y,
            unit_row_x=unit_row_x,
            unit_row_y=unit_row_y,
            square_pitch=square_pitch,
            rotation_degrees=math.degrees(rotation_radians),
            second_reference_square=square2.label,
            second_reference_x=x2,
            second_reference_y=y2,
        )


@dataclass
class PlotterCalibration:
    board_size: int = BOARD_SIZE
    label_orientation: str = "A1-top-left"
    image_path: str | None = None
    image_corners: list[list[float]] = field(default_factory=list)
    machine: MachineCalibration | None = None

    def set_image_corners(self, image_path: str, corners: list[tuple[float, float]]) -> None:
        if len(corners) != 4:
            raise ValueError("Exactly 4 corners are required.")
        self.image_path = image_path
        self.image_corners = [[float(x), float(y)] for x, y in corners]

    def set_machine_calibration(self, machine: MachineCalibration) -> None:
        self.machine = machine

    def validate_ready_for_move(self) -> None:
        if len(self.image_corners) != 4:
            raise ValueError("Image calibration is missing. Run calibrate-image first.")
        if self.machine is None:
            raise ValueError("Machine calibration is missing. Run calibrate-machine first.")

    def square_center_in_image(self, square: Square) -> tuple[float, float]:
        if len(self.image_corners) != 4:
            raise ValueError("Image calibration is missing.")

        cv2 = _require_cv2()
        src = cv2.getPerspectiveTransform(
            _to_float32(
                [
                    [0.0, 0.0],
                    [float(self.board_size), 0.0],
                    [float(self.board_size), float(self.board_size)],
                    [0.0, float(self.board_size)],
                ]
            ),
            _to_float32(self.image_corners),
        )
        center = _to_float32([[list(square.center_in_board_space())]])
        transformed = cv2.perspectiveTransform(center, src)
        return (float(transformed[0][0][0]), float(transformed[0][0][1]))

    def square_center_in_machine(self, square: Square) -> tuple[float, float]:
        if self.machine is None:
            raise ValueError("Machine calibration is missing.")
        return self.machine.square_to_machine(square)

    def to_dict(self) -> dict[str, Any]:
        return {
            "board_size": self.board_size,
            "label_orientation": self.label_orientation,
            "image_path": self.image_path,
            "image_corners": self.image_corners,
            "machine": asdict(self.machine) if self.machine else None,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PlotterCalibration":
        machine_payload = payload.get("machine")
        machine = MachineCalibration(**machine_payload) if machine_payload else None
        return cls(
            board_size=payload.get("board_size", BOARD_SIZE),
            label_orientation=payload.get("label_orientation", "A1-top-left"),
            image_path=payload.get("image_path"),
            image_corners=payload.get("image_corners", []),
            machine=machine,
        )

    @classmethod
    def load(cls, path: str | Path) -> "PlotterCalibration":
        calibration_path = Path(path)
        if not calibration_path.exists():
            return cls()
        return cls.from_dict(json.loads(calibration_path.read_text(encoding="utf-8")))

    def save(self, path: str | Path) -> None:
        calibration_path = Path(path)
        calibration_path.parent.mkdir(parents=True, exist_ok=True)
        calibration_path.write_text(
            json.dumps(self.to_dict(), indent=2),
            encoding="utf-8",
        )


def _to_float32(values: list[list[float]] | list[list[list[float]]]):
    import numpy as np

    return np.array(values, dtype=np.float32)
