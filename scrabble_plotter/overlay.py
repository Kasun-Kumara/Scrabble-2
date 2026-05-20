from __future__ import annotations

from .board import BOARD_SIZE
from .calibration import PlotterCalibration, board_corner_points


Point = tuple[float, float]
Segment = tuple[Point, Point]


def project_board_points(
    corners: list[list[float]] | list[tuple[float, float]],
    points: list[Point],
    board_size: int = BOARD_SIZE,
) -> list[Point]:
    if len(corners) != 4:
        raise ValueError("Exactly 4 board corners are required.")

    cv2 = _require_cv2()
    transform = cv2.getPerspectiveTransform(
        _to_float32(board_corner_points(board_size)),
        _to_float32([[float(x), float(y)] for x, y in corners]),
    )
    projected = cv2.perspectiveTransform(_to_float32([[list(point) for point in points]]), transform)
    return [(float(point[0]), float(point[1])) for point in projected[0]]


def grid_segments(
    corners: list[list[float]] | list[tuple[float, float]],
    board_size: int = BOARD_SIZE,
) -> list[Segment]:
    segment_points: list[Point] = []
    for index in range(board_size + 1):
        segment_points.extend(
            [
                (float(index), 0.0),
                (float(index), float(board_size)),
                (0.0, float(index)),
                (float(board_size), float(index)),
            ]
        )

    projected = project_board_points(corners, segment_points, board_size)
    return [
        (projected[index], projected[index + 1])
        for index in range(0, len(projected), 2)
    ]


def cell_label_positions(
    corners: list[list[float]] | list[tuple[float, float]],
    board_size: int = BOARD_SIZE,
) -> list[tuple[str, Point]]:
    labels: list[str] = []
    points: list[Point] = []
    for row in range(board_size):
        for col in range(board_size):
            labels.append(f"{chr(ord('A') + col)}{row + 1}")
            points.append((col + 0.5, row + 0.5))

    projected = project_board_points(corners, points, board_size)
    return list(zip(labels, projected))


def draw_board_overlay(frame, calibration: PlotterCalibration):  # type: ignore[no-untyped-def]
    if len(calibration.image_corners) != 4:
        return frame

    cv2 = _require_cv2()
    board_size = calibration.board_size
    overlay = frame.copy()

    for start, end in grid_segments(calibration.image_corners, board_size):
        cv2.line(overlay, _int_point(start), _int_point(end), (0, 255, 255), 1, cv2.LINE_AA)

    outline = project_board_points(
        calibration.image_corners,
        [(0.0, 0.0), (float(board_size), 0.0), (float(board_size), float(board_size)), (0.0, float(board_size))],
        board_size,
    )
    for index, point in enumerate(outline):
        cv2.line(overlay, _int_point(point), _int_point(outline[(index + 1) % 4]), (0, 200, 0), 3, cv2.LINE_AA)

    for label, point in cell_label_positions(calibration.image_corners, board_size):
        x, y = _int_point(point)
        cv2.putText(
            overlay,
            label,
            (x - 10, y + 4),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.35,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )

    return overlay


def _int_point(point: Point) -> tuple[int, int]:
    return (int(round(point[0])), int(round(point[1])))


def _to_float32(values):  # type: ignore[no-untyped-def]
    import numpy as np

    return np.array(values, dtype=np.float32)


def _require_cv2():
    try:
        import cv2  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "OpenCV is required for camera overlay. Install scrabble_plotter/requirements.txt."
        ) from exc
    return cv2
