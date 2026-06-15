from __future__ import annotations

import os
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .board import BOARD_SIZE, CELL_SIZE_MM, parse_square_label
from .calibration import PlotterCalibration
from .camera import open_camera_capture, read_camera_frame
from .gemini_agent import (
    GeminiPlotterAgent,
    PlotterAgentAction,
)
from .image_calibration import collect_board_corners_from_frame
from .overlay import draw_camera_ocr_overlay
from .scanner import (
    BoardScanResult,
    CameraLetterScanResult,
    CameraWordScanResult,
    board_square_from_image_point,
    format_camera_words_numbered,
    scan_board_image,
    scan_camera_letters,
    scan_camera_words,
    select_best_frame,
)
from .word_bank import format_words_by_direction, matched_matrix_words
from .scoring import (
    ScoreResult,
    normalize_letter,
    premium_from_short_label,
    premium_short_label,
    score_board,
)
from .tile_rack import (
    can_build_word_from_rack,
    horizontal_word_squares,
    normalize_rack_letters,
    normalize_word,
    rack_slot_indices_for_word,
)
from .serial_sender import (
    GCodeSender,
    SerialConfig,
    format_move_command,
    list_serial_ports,
)


APP_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CALIBRATION_PATH = APP_ROOT / "scrabble_plotter_calibration.json"
LIVE_LETTER_SCAN_INTERVAL_SECONDS = 4.0
LIVE_WORD_SCAN_INTERVAL_SECONDS = 4.0
BEST_CAPTURE_FRAME_COUNT = 18
BEST_CAPTURE_TIMEOUT_SECONDS = 1.0
BEST_CAPTURE_FRAME_DELAY_SECONDS = 0.025
CAMERA_READ_FAILURE_LIMIT = 10
PICK_DROP_Z_SETTLE_SECONDS = 0.8
PICK_DROP_MAGNET_SETTLE_SECONDS = 1.0
PICK_DROP_LIFT_RETRY_SECONDS = 0.35
Z_DOWN_COMMAND = "ZU"
Z_UP_COMMAND = "ZD"


class ScrabblePlotterApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Scrabble Join")
        self.root.geometry("1240x820")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._z_axis_controls_frame = None
        self.z_height_angle = tk.IntVar(value=80)
        self.root.after(500, self._add_z_axis_controls_to_move_section)

        self.calibration_path = tk.StringVar(value=str(DEFAULT_CALIBRATION_PATH.resolve()))
        self._calibration = PlotterCalibration.load(self.calibration_path.get())

        self.camera_index_var = tk.StringVar(value=str(self._calibration.camera_index))
        self.offset_x_var = tk.StringVar(value=str(self._calibration.offset_x_mm))
        self.offset_y_var = tk.StringVar(value=str(self._calibration.offset_y_mm))
        self.cell_size_var = tk.StringVar(value=str(self._calibration.cell_size_mm))
        self.x_steps_per_mm_var = tk.StringVar(value=str(self._calibration.x_steps_per_mm))
        self.y_steps_per_mm_var = tk.StringVar(value=str(self._calibration.y_steps_per_mm))
        self.cart_x_var = tk.StringVar(value=str(self._calibration.cart_x_mm))
        self.cart_y_var = tk.StringVar(value=str(self._calibration.cart_y_mm))
        self.ocr_confidence_threshold_var = tk.StringVar(value=str(self._calibration.ocr_confidence_threshold))
        self.ocr_cell_size_px_var = tk.StringVar(value=str(self._calibration.ocr_cell_size_px))
        self.square_var = tk.StringVar(value="H8")
        self.pick_square_var = tk.StringVar(value="A1")
        self.drop_square_var = tk.StringVar(value="H8")
        self.port_var = tk.StringVar()
        self.baud_var = tk.StringVar(value="115200")
        self.feed_rate_var = tk.StringVar(value="1500")
        self.command_var = tk.StringVar(value="G0")
        self.timeout_var = tk.StringVar(value="2.0")
        self.startup_g90_var = tk.BooleanVar(value=True)
        self.gemini_api_key_var = tk.StringVar(value=os.environ.get("GEMINI_API_KEY", ""))
        self.gemini_model_var = tk.StringVar(value="gemini-2.5-flash")
        self.gemini_objective_var = tk.StringVar(value="Choose the next board square.")
        self.gemini_include_camera_var = tk.BooleanVar(value=True)
        self.live_letter_scan_var = tk.BooleanVar(value=False)
        self.live_word_scan_var = tk.BooleanVar(value=True)
        self._last_agent_action: PlotterAgentAction | None = None
        self.blank_squares_var = tk.StringVar()
        self.captured_letters_var = tk.StringVar()
        self._letter_vars: list[list[tk.StringVar]] = [
            [tk.StringVar() for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)
        ]
        self._letter_entries: list[list[tk.Entry]] = []
        self._premium_vars: list[list[tk.StringVar]] = [
            [
                tk.StringVar(value=premium_short_label(self._calibration.premium_layout[row][col]))
                for col in range(BOARD_SIZE)
            ]
            for row in range(BOARD_SIZE)
        ]
        self._last_scan: BoardScanResult | None = None
        self._last_camera_letter_scan: CameraLetterScanResult | None = None
        self._last_camera_word_scan: CameraWordScanResult | None = None
        self._camera_word_scan_running = False
        self.camera_words_text: tk.Text | None = None

        self.status_var = tk.StringVar(
            value="Start the camera to find visible words."
        )
        self.preview_image = None
        self._preview_display_size = (0, 0)
        self._preview_source_size = (0, 0)
        self._camera = None
        self._camera_after_id: str | None = None
        self._latest_frame = None
        self._captured_photo_frame = None
        self._camera_failed_reads = 0
        self._camera_letter_scan_token = 0
        self._camera_word_scan_token = 0
        self._last_live_letter_scan_at = 0.0
        self._live_letter_scan_running = False
        self._last_live_letter_scan_error: str | None = None
        self._last_live_word_scan_at = 0.0
        self._last_live_word_scan_error: str | None = None
        self._sender: GCodeSender | None = None
        self._sender_key: tuple[str, int, float, bool] | None = None

        self._build_ui()
        self.refresh_ports()
        self.load_calibration_into_form()

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        frame = ttk.Frame(self.root, padding=12)
        frame.grid(sticky="nsew")
        frame.columnconfigure(0, weight=3)
        frame.columnconfigure(1, weight=2)
        frame.rowconfigure(0, weight=1)

        preview_frame = ttk.LabelFrame(frame, text="Live Camera", padding=10)
        preview_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        preview_frame.columnconfigure(4, weight=1)
        preview_frame.rowconfigure(2, weight=1)

        ttk.Label(preview_frame, text="Camera").grid(row=0, column=0, sticky="w")
        camera_selector = ttk.Combobox(
            preview_frame,
            textvariable=self.camera_index_var,
            values=[str(index) for index in range(6)],
            width=8,
        )
        camera_selector.grid(row=0, column=1, sticky="w", padx=(8, 8))
        ttk.Button(preview_frame, text="Start", command=self.start_camera).grid(row=0, column=2, padx=(0, 8))
        ttk.Button(preview_frame, text="Stop", command=self.stop_camera).grid(row=0, column=3, padx=(0, 8))
        ttk.Button(preview_frame, text="Calibrate Board", command=self.calibrate_board_from_camera).grid(
            row=0, column=4, sticky="w"
        )

        live_options = ttk.Frame(preview_frame)
        live_options.grid(row=1, column=0, columnspan=5, sticky="w", pady=(8, 0))
        ttk.Checkbutton(
            live_options,
            text="Live letters",
            variable=self.live_letter_scan_var,
        ).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(
            live_options,
            text="Live words",
            variable=self.live_word_scan_var,
        ).grid(row=0, column=1, sticky="w", padx=(14, 0))

        self.preview_label = ttk.Label(preview_frame, text="Camera stopped", anchor="center")
        self.preview_label.grid(row=2, column=0, columnspan=5, sticky="nsew", pady=(8, 0))
        self.preview_label.bind("<Button-1>", self._handle_camera_preview_click)

        controls_container = ttk.Frame(frame)
        controls_container.grid(row=0, column=1, sticky="nsew")
        controls_container.columnconfigure(0, weight=1)
        controls_container.rowconfigure(0, weight=1)

        controls_canvas = tk.Canvas(controls_container, highlightthickness=0)
        controls_canvas.grid(row=0, column=0, sticky="nsew")
        controls_scrollbar = ttk.Scrollbar(controls_container, orient="vertical", command=controls_canvas.yview)
        controls_scrollbar.grid(row=0, column=1, sticky="ns")
        controls_canvas.configure(yscrollcommand=controls_scrollbar.set)

        controls = ttk.Frame(controls_canvas)
        controls.columnconfigure(0, weight=1)
        controls_window = controls_canvas.create_window((0, 0), window=controls, anchor="nw")

        def sync_controls_scroll_region(event) -> None:  # type: ignore[no-untyped-def]
            controls_canvas.configure(scrollregion=controls_canvas.bbox("all"))

        def sync_controls_width(event) -> None:  # type: ignore[no-untyped-def]
            controls_canvas.itemconfigure(controls_window, width=event.width)

        def scroll_controls_with_wheel(event) -> str:  # type: ignore[no-untyped-def]
            delta = event.delta
            if delta == 0:
                return "break"
            controls_canvas.yview_scroll(int(-delta / 120), "units")
            return "break"

        controls.bind("<Configure>", sync_controls_scroll_region)
        controls_canvas.bind("<Configure>", sync_controls_width)
        controls_canvas.bind_all("<MouseWheel>", scroll_controls_with_wheel)

        calibration_box = ttk.LabelFrame(controls, text="Board Settings", padding=10)
        calibration_box.grid(row=0, column=0, sticky="ew")
        calibration_box.columnconfigure(1, weight=1)

        ttk.Label(calibration_box, text="Calibration File").grid(row=0, column=0, sticky="w")
        ttk.Entry(calibration_box, textvariable=self.calibration_path).grid(row=0, column=1, sticky="ew", padx=(8, 0))
        ttk.Button(calibration_box, text="Browse", command=self.choose_calibration_file).grid(row=0, column=2, padx=(8, 0))

        fields = [
            ("Offset X mm", self.offset_x_var),
            ("Offset Y mm", self.offset_y_var),
            ("Cell Size mm", self.cell_size_var),
            ("X Steps/mm", self.x_steps_per_mm_var),
            ("Y Steps/mm", self.y_steps_per_mm_var),
            ("Cart X mm", self.cart_x_var),
            ("Cart Y mm", self.cart_y_var),
            ("OCR Min Confidence", self.ocr_confidence_threshold_var),
            ("OCR Cell Size px", self.ocr_cell_size_px_var),
        ]
        for index, (label, variable) in enumerate(fields, start=1):
            ttk.Label(calibration_box, text=label).grid(row=index, column=0, sticky="w", pady=2)
            entry = ttk.Entry(calibration_box, textvariable=variable)
            entry.grid(row=index, column=1, columnspan=2, sticky="ew", padx=(8, 0))

        ttk.Button(calibration_box, text="Save Board Settings", command=self.save_board_settings).grid(
            row=len(fields) + 1, column=0, columnspan=3, sticky="ew", pady=(10, 6)
        )
        ttk.Button(calibration_box, text="Send Step Settings", command=self.send_step_settings).grid(
            row=len(fields) + 2, column=0, columnspan=3, sticky="ew"
        )

        move_box = ttk.LabelFrame(controls, text="Move Plotter", padding=10)
        move_box.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        move_box.columnconfigure(1, weight=1)

        move_fields = [
            ("Square", self.square_var),
            ("Pick Square", self.pick_square_var),
            ("Drop Square", self.drop_square_var),
            ("COM Port", self.port_var),
            ("Baud", self.baud_var),
            ("Feed Rate", self.feed_rate_var),
            ("G-code Command", self.command_var),
            ("Timeout", self.timeout_var),
        ]
        for index, (label, variable) in enumerate(move_fields):
            ttk.Label(move_box, text=label).grid(row=index, column=0, sticky="w", pady=2)
            ttk.Entry(move_box, textvariable=variable).grid(row=index, column=1, sticky="ew", padx=(8, 8))
            if label == "COM Port":
                ttk.Button(move_box, text="Refresh", command=self.refresh_ports).grid(row=index, column=2, sticky="ew")

        next_move_row = len(move_fields)
        ttk.Checkbutton(move_box, text="Send G90 before move", variable=self.startup_g90_var).grid(
            row=next_move_row, column=0, columnspan=3, sticky="w", pady=(8, 0)
        )
        ttk.Button(move_box, text="Preview G-code", command=self.preview_move).grid(
            row=next_move_row + 1, column=0, columnspan=3, sticky="ew", pady=(10, 6)
        )
        ttk.Button(move_box, text="Send Move", command=self.send_move).grid(
            row=next_move_row + 2, column=0, columnspan=3, sticky="ew"
        )
        ttk.Button(move_box, text="Reset To Start", command=self.reset_to_start).grid(
            row=next_move_row + 3, column=0, columnspan=3, sticky="ew", pady=(8, 0)
        )
        ttk.Button(move_box, text="Go To Cart", command=self.go_to_cart).grid(
            row=next_move_row + 4, column=0, columnspan=3, sticky="ew", pady=(8, 0)
        )
        ttk.Button(move_box, text="Pick And Drop", command=self.pick_and_drop).grid(
            row=next_move_row + 5, column=0, columnspan=3, sticky="ew", pady=(8, 0)
        )

        scan_box = ttk.LabelFrame(controls, text="Camera OCR", padding=10)
        scan_box.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        scan_box.columnconfigure(0, weight=1)

        scan_buttons = ttk.Frame(scan_box)
        scan_buttons.grid(row=0, column=0, sticky="ew")
        scan_buttons.columnconfigure(0, weight=1)
        scan_buttons.columnconfigure(1, weight=1)
        scan_buttons.columnconfigure(2, weight=1)
        ttk.Button(scan_buttons, text="Take Picture", command=self.take_picture_from_camera).grid(
            row=0, column=0, sticky="ew", padx=(0, 6)
        )
        ttk.Button(scan_buttons, text="Find Words", command=self.identify_words_with_easyocr).grid(
            row=0, column=1, sticky="ew", padx=(0, 6)
        )
        ttk.Button(scan_buttons, text="Live View", command=self.resume_live_camera).grid(
            row=0, column=2, sticky="ew"
        )
        ttk.Button(scan_buttons, text="Capture Letters", command=self.capture_letters_from_camera).grid(
            row=1, column=0, sticky="ew", padx=(0, 6), pady=(6, 0)
        )
        ttk.Button(scan_buttons, text="Scan Board", command=self.scan_board_from_camera).grid(
            row=1, column=1, sticky="ew", padx=(0, 6), pady=(6, 0)
        )
        ttk.Button(scan_buttons, text="Calculate Score", command=self.calculate_score_from_board).grid(
            row=1, column=2, sticky="ew", pady=(6, 0)
        )
        ttk.Button(scan_buttons, text="Tile Rack", command=self.open_tile_rack_window).grid(
            row=2, column=0, columnspan=3, sticky="ew", pady=(6, 0)
        )

        ttk.Label(scan_box, text="Captured Letters").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(scan_box, textvariable=self.captured_letters_var).grid(row=2, column=0, sticky="ew")

        board_grid = ttk.Frame(scan_box)
        board_grid.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        for col in range(BOARD_SIZE):
            ttk.Label(board_grid, text=chr(ord("A") + col), width=3, anchor="center").grid(row=0, column=col + 1)
        self._letter_entries = []
        for row in range(BOARD_SIZE):
            ttk.Label(board_grid, text=str(row + 1), width=3, anchor="e").grid(row=row + 1, column=0, padx=(0, 3))
            entry_row: list[tk.Entry] = []
            for col in range(BOARD_SIZE):
                entry = tk.Entry(
                    board_grid,
                    textvariable=self._letter_vars[row][col],
                    width=3,
                    justify="center",
                    relief="solid",
                    borderwidth=1,
                )
                entry.grid(row=row + 1, column=col + 1, padx=1, pady=1)
                entry.bind(
                    "<Button-1>",
                    lambda event, row=row, col=col: self._handle_ocr_board_cell_click(row, col),
                    add="+",
                )
                entry.bind("<FocusOut>", lambda event: self._normalize_board_entries())
                entry.bind("<Return>", lambda event: self.calculate_score_from_board())
                entry_row.append(entry)
            self._letter_entries.append(entry_row)

        ttk.Label(scan_box, text="Matched Words").grid(row=4, column=0, sticky="w", pady=(8, 0))
        self.camera_words_text = tk.Text(scan_box, height=6, wrap="word", state="disabled")
        self.camera_words_text.grid(row=5, column=0, sticky="ew")

        ttk.Label(scan_box, text="Blank Squares").grid(row=6, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(scan_box, textvariable=self.blank_squares_var).grid(row=7, column=0, sticky="ew")

        premium_box = ttk.LabelFrame(scan_box, text="Premium Layout (DL TL DW TW)", padding=6)
        premium_box.grid(row=8, column=0, sticky="ew", pady=(10, 0))
        for col in range(BOARD_SIZE):
            ttk.Label(premium_box, text=chr(ord("A") + col), width=3, anchor="center").grid(row=0, column=col + 1)
        for row in range(BOARD_SIZE):
            ttk.Label(premium_box, text=str(row + 1), width=3, anchor="e").grid(row=row + 1, column=0, padx=(0, 3))
            for col in range(BOARD_SIZE):
                entry = tk.Entry(
                    premium_box,
                    textvariable=self._premium_vars[row][col],
                    width=3,
                    justify="center",
                    relief="solid",
                    borderwidth=1,
                )
                entry.grid(row=row + 1, column=col + 1, padx=1, pady=1)
                entry.bind("<FocusOut>", lambda event: self._normalize_premium_entries())
        ttk.Button(scan_box, text="Save Scan Settings", command=self.save_board_settings).grid(
            row=9, column=0, sticky="ew", pady=(10, 0)
        )

        agent_box = ttk.LabelFrame(controls, text="Gemini Agent", padding=10)
        agent_box.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        agent_box.columnconfigure(1, weight=1)

        agent_fields = [
            ("API Key", self.gemini_api_key_var),
            ("Model", self.gemini_model_var),
            ("Objective", self.gemini_objective_var),
        ]
        for index, (label, variable) in enumerate(agent_fields):
            ttk.Label(agent_box, text=label).grid(row=index, column=0, sticky="w", pady=2)
            show = "*" if label == "API Key" else ""
            ttk.Entry(agent_box, textvariable=variable, show=show).grid(
                row=index, column=1, columnspan=2, sticky="ew", padx=(8, 0)
            )

        ttk.Checkbutton(agent_box, text="Use latest camera frame", variable=self.gemini_include_camera_var).grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(8, 0)
        )
        ttk.Button(agent_box, text="Ask Gemini", command=self.ask_gemini).grid(
            row=4, column=0, columnspan=3, sticky="ew", pady=(10, 6)
        )
        ttk.Button(agent_box, text="Run Gemini Action", command=self.run_gemini_action).grid(
            row=5, column=0, columnspan=3, sticky="ew"
        )

        log_box = ttk.LabelFrame(controls, text="Status", padding=10)
        log_box.grid(row=4, column=0, sticky="nsew", pady=(12, 0))
        log_box.columnconfigure(0, weight=1)
        log_box.rowconfigure(1, weight=1)
        controls.rowconfigure(4, weight=1)

        ttk.Label(log_box, textvariable=self.status_var, wraplength=380).grid(row=0, column=0, sticky="ew")
        self.log_text = tk.Text(log_box, height=14, wrap="word")
        self.log_text.grid(row=1, column=0, sticky="nsew", pady=(8, 0))

    def choose_calibration_file(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Choose calibration file",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            initialfile=Path(self.calibration_path.get()).name or "scrabble_plotter_calibration.json",
        )
        if not path:
            return
        self.calibration_path.set(path)
        self.load_calibration_into_form()

    def load_calibration_into_form(self) -> None:
        self._calibration = PlotterCalibration.load(self.calibration_path.get())
        self.camera_index_var.set(str(self._calibration.camera_index))
        self.offset_x_var.set(str(self._calibration.offset_x_mm))
        self.offset_y_var.set(str(self._calibration.offset_y_mm))
        self.cell_size_var.set(str(self._calibration.cell_size_mm))
        self.x_steps_per_mm_var.set(str(self._calibration.x_steps_per_mm))
        self.y_steps_per_mm_var.set(str(self._calibration.y_steps_per_mm))
        self.cart_x_var.set(str(self._calibration.cart_x_mm))
        self.cart_y_var.set(str(self._calibration.cart_y_mm))
        self.ocr_confidence_threshold_var.set(str(self._calibration.ocr_confidence_threshold))
        self.ocr_cell_size_px_var.set(str(self._calibration.ocr_cell_size_px))
        self._load_premium_layout_into_form()
        if len(self._calibration.image_corners) == 4:
            self._set_status("Loaded board calibration. Start the camera to find visible words.")
        else:
            self._set_status("Start the camera to find visible words.")

    def save_board_settings(self) -> None:
        try:
            self._calibration = self._calibration_from_form()
            self._calibration.save(self.calibration_path.get())
            self._set_status("Saved board settings.")
            self._log(
                f"Board offset saved: X={self._calibration.offset_x_mm:.3f}, "
                f"Y={self._calibration.offset_y_mm:.3f}; "
                f"steps/mm X={self._calibration.x_steps_per_mm:.3f}, "
                f"Y={self._calibration.y_steps_per_mm:.3f}; "
                f"cart X={self._calibration.cart_x_mm:.3f}, "
                f"Y={self._calibration.cart_y_mm:.3f}"
            )
        except Exception as exc:
            self._show_error(exc)

    def send_step_settings(self) -> None:
        try:
            calibration = self._calibration_from_form()
            calibration.save(self.calibration_path.get())
            sender = self._get_sender()
            command, responses = sender.send_step_config(
                calibration.x_steps_per_mm,
                calibration.y_steps_per_mm,
            )
            self._set_status("Sent step settings to the controller.")
            self._log(f"Sent: {command}")
            if responses:
                self._log("Responses: " + " | ".join(responses))
        except Exception as exc:
            self._show_error(exc)

    def start_camera(self) -> None:
        try:
            cv2 = _require_cv2()
            self.stop_camera(clear_preview=False)
            camera_index = int(self.camera_index_var.get())
            opened_camera = open_camera_capture(cv2, camera_index)

            self._camera = opened_camera.capture
            self._calibration = self._calibration_from_form()
            self._calibration.camera_index = camera_index
            self._calibration.save(self.calibration_path.get())
            self._latest_frame = opened_camera.first_frame.copy()
            self._captured_photo_frame = None
            self._camera_failed_reads = 0
            self._invalidate_camera_scans()
            self._last_live_letter_scan_at = 0.0
            self._last_live_letter_scan_error = None
            self._last_camera_letter_scan = None
            self._last_live_word_scan_at = 0.0
            self._last_live_word_scan_error = None
            self._last_camera_word_scan = None
            self.captured_letters_var.set("")
            self._set_camera_words_text("")
            self._show_frame(opened_camera.first_frame)
            self._set_status(f"Camera {camera_index} started ({opened_camera.backend_name}).")
            self._schedule_camera_update()
        except Exception as exc:
            self._show_error(exc)

    def stop_camera(self, clear_preview: bool = True) -> None:
        if self._camera_after_id is not None:
            self.root.after_cancel(self._camera_after_id)
            self._camera_after_id = None
        if self._camera is not None:
            self._camera.release()
            self._camera = None
        self._camera_failed_reads = 0
        if clear_preview:
            self._latest_frame = None
            self._captured_photo_frame = None
            self._invalidate_camera_scans()
            self.preview_label.configure(image="", text="Camera stopped")
            self.preview_image = None
            self._preview_display_size = (0, 0)
            self._preview_source_size = (0, 0)

    def calibrate_board_from_camera(self) -> None:
        if self._latest_frame is None:
            raise_user_error("Start the camera first.")
            return

        try:
            corners = collect_board_corners_from_frame(self._latest_frame.copy())
            self._calibration = self._calibration_from_form()
            self._calibration.set_camera_corners(corners)
            self._calibration.save(self.calibration_path.get())
            self._set_status("Saved board corners for board scanning.")
            self._log(f"Board corners: {corners}")
            self._show_frame(self._latest_frame)
        except Exception as exc:
            self._show_error(exc)

    def preview_move(self) -> None:
        try:
            calibration = self._calibration_from_form()
            calibration.validate_ready_for_move()
            square = parse_square_label(self.square_var.get())
            x, y = calibration.square_center_in_machine(square)
            gcode = format_move_command(x, y, self._optional_float(self.feed_rate_var.get()), self.command_var.get())
            self._set_status(f"{square.label} -> X={x:.3f}, Y={y:.3f}")
            self._log(gcode)
        except Exception as exc:
            self._show_error(exc)

    def send_move(self) -> None:
        try:
            self._send_square_move(self.square_var.get())
        except Exception as exc:
            self._show_error(exc)

    def pick_and_drop(self) -> None:
        try:
            self._send_pick_and_drop(self.pick_square_var.get(), self.drop_square_var.get())
        except Exception as exc:
            self._show_error(exc)

    def _handle_ocr_board_cell_click(self, row: int, col: int) -> None:
        square = f"{chr(ord('A') + col)}{row + 1}"
        self._move_plotter_to_clicked_square(square, source="OCR grid")

    def _handle_camera_preview_click(self, event) -> None:  # type: ignore[no-untyped-def]
        grid = self._current_camera_ocr_grid()
        if grid is None:
            self._set_status("No auto-aligned OCR grid is visible yet. Capture letters or find words first.")
            return

        point = self._preview_click_to_frame_point(int(event.x), int(event.y))
        if point is None:
            self._set_status("Click inside the camera image to choose a board square.")
            return

        square = board_square_from_image_point(grid.corners, point[0], point[1], board_size=grid.board_size)
        if square is None:
            self._set_status("Click inside the detected board grid to move the plotter.")
            return

        self._move_plotter_to_clicked_square(square, source="camera grid")

    def _move_plotter_to_clicked_square(self, square: str, source: str) -> None:
        try:
            self.square_var.set(square)
            self._send_square_move(square)
            self._log(f"Clicked {source} square {square}.")
        except Exception as exc:
            self._show_error(exc)

    def open_tile_rack_window(self) -> None:
        self._ensure_tile_rack_state()
        existing_window = getattr(self, "_tile_rack_window", None)
        if existing_window is not None and existing_window.winfo_exists():
            existing_window.lift()
            existing_window.focus_force()
            return

        window = tk.Toplevel(self.root)
        self._tile_rack_window = window
        window.title("Tile Rack")
        window.resizable(False, False)
        window.columnconfigure(1, weight=1)

        ttk.Label(window, text="Rack letters").grid(row=0, column=0, sticky="w", padx=10, pady=(10, 4))
        rack_entry = ttk.Entry(window, textvariable=self.tile_rack_letters_var, width=18)
        rack_entry.grid(row=0, column=1, sticky="ew", padx=10, pady=(10, 4))
        ttk.Button(window, text="Capture Rack", command=self.capture_tile_rack_from_camera).grid(
            row=0, column=2, sticky="ew", padx=10, pady=(10, 4)
        )

        ttk.Label(window, text="Word").grid(row=1, column=0, sticky="w", padx=10, pady=4)
        ttk.Entry(window, textvariable=self.tile_rack_word_var, width=18).grid(
            row=1, column=1, sticky="ew", padx=10, pady=4
        )
        ttk.Button(window, text="Suggest Word", command=self.suggest_tile_rack_word).grid(
            row=1, column=2, sticky="ew", padx=10, pady=4
        )

        ttk.Label(window, text="Start square").grid(row=2, column=0, sticky="w", padx=10, pady=4)
        ttk.Entry(window, textvariable=self.tile_rack_start_square_var, width=8).grid(
            row=2, column=1, sticky="w", padx=10, pady=4
        )
        ttk.Button(window, text="Check Word", command=self.check_tile_rack_word).grid(
            row=2, column=2, sticky="ew", padx=10, pady=4
        )

        motion_frame = ttk.LabelFrame(window, text="Rack pickup settings")
        motion_frame.grid(row=3, column=0, columnspan=3, sticky="ew", padx=10, pady=(8, 4))
        for column in range(6):
            motion_frame.columnconfigure(column, weight=1)

        ttk.Label(motion_frame, text="X").grid(row=0, column=0, sticky="w", padx=6, pady=(8, 2))
        ttk.Entry(motion_frame, textvariable=self.tile_rack_x_var, width=8).grid(row=0, column=1, padx=6, pady=(8, 2))
        ttk.Label(motion_frame, text="Y").grid(row=0, column=2, sticky="w", padx=6, pady=(8, 2))
        ttk.Entry(motion_frame, textvariable=self.tile_rack_y_var, width=8).grid(row=0, column=3, padx=6, pady=(8, 2))
        ttk.Label(motion_frame, text="Spacing").grid(row=0, column=4, sticky="w", padx=6, pady=(8, 2))
        ttk.Entry(motion_frame, textvariable=self.tile_rack_spacing_var, width=8).grid(row=0, column=5, padx=6, pady=(8, 2))

        ttk.Label(motion_frame, text="Safe Z").grid(row=1, column=0, sticky="w", padx=6, pady=2)
        ttk.Entry(motion_frame, textvariable=self.tile_rack_safe_z_var, width=8).grid(row=1, column=1, padx=6, pady=2)
        ttk.Label(motion_frame, text="Pick Z").grid(row=1, column=2, sticky="w", padx=6, pady=2)
        ttk.Entry(motion_frame, textvariable=self.tile_rack_pick_z_var, width=8).grid(row=1, column=3, padx=6, pady=2)
        ttk.Label(motion_frame, text="Place Z").grid(row=1, column=4, sticky="w", padx=6, pady=2)
        ttk.Entry(motion_frame, textvariable=self.tile_rack_place_z_var, width=8).grid(row=1, column=5, padx=6, pady=2)

        ttk.Label(motion_frame, text="XY feed").grid(row=2, column=0, sticky="w", padx=6, pady=2)
        ttk.Entry(motion_frame, textvariable=self.tile_rack_xy_feed_var, width=8).grid(row=2, column=1, padx=6, pady=2)
        ttk.Label(motion_frame, text="Z feed").grid(row=2, column=2, sticky="w", padx=6, pady=2)
        ttk.Entry(motion_frame, textvariable=self.tile_rack_z_feed_var, width=8).grid(row=2, column=3, padx=6, pady=2)
        ttk.Button(motion_frame, text="Go To Rack", command=self.move_to_tile_rack_start).grid(
            row=2, column=4, columnspan=2, sticky="ew", padx=6, pady=2
        )

        ttk.Label(motion_frame, text="Magnet on").grid(row=3, column=0, sticky="w", padx=6, pady=(2, 8))
        ttk.Entry(motion_frame, textvariable=self.tile_rack_magnet_on_var, width=10).grid(
            row=3, column=1, padx=6, pady=(2, 8)
        )
        ttk.Label(motion_frame, text="Magnet off").grid(row=3, column=2, sticky="w", padx=6, pady=(2, 8))
        ttk.Entry(motion_frame, textvariable=self.tile_rack_magnet_off_var, width=10).grid(
            row=3, column=3, padx=6, pady=(2, 8)
        )

        self.tile_rack_make_button = ttk.Button(
            window,
            text="Make Word On Board",
            command=self.make_tile_rack_word_on_board,
            state="disabled",
        )
        self.tile_rack_make_button.grid(row=4, column=0, columnspan=3, sticky="ew", padx=10, pady=(8, 4))

        ttk.Label(window, textvariable=self.tile_rack_status_var, wraplength=520).grid(
            row=5, column=0, columnspan=3, sticky="ew", padx=10, pady=(4, 10)
        )
        self._update_tile_rack_word_option()

    def _ensure_tile_rack_state(self) -> None:
        if getattr(self, "_tile_rack_state_ready", False):
            return

        default_feed = "1500"
        feed_var = getattr(self, "feed_rate_var", None)
        if feed_var is not None:
            try:
                default_feed = str(feed_var.get())
            except Exception:
                default_feed = "1500"

        self.tile_rack_letters_var = tk.StringVar(value="")
        self.tile_rack_word_var = tk.StringVar(value="")
        self.tile_rack_start_square_var = tk.StringVar(value="F6")
        self.tile_rack_x_var = tk.StringVar(value="0")
        self.tile_rack_y_var = tk.StringVar(value="0")
        self.tile_rack_spacing_var = tk.StringVar(value="25")
        self.tile_rack_safe_z_var = tk.StringVar(value="20")
        self.tile_rack_pick_z_var = tk.StringVar(value="0")
        self.tile_rack_place_z_var = tk.StringVar(value="0")
        self.tile_rack_xy_feed_var = tk.StringVar(value=default_feed)
        self.tile_rack_z_feed_var = tk.StringVar(value="600")
        self.tile_rack_magnet_on_var = tk.StringVar(value="M3")
        self.tile_rack_magnet_off_var = tk.StringVar(value="M5")
        self.tile_rack_status_var = tk.StringVar(
            value="Capture or type up to 7 rack letters, then enter a word to place."
        )
        self._tile_rack_normalizing = False
        self._tile_rack_word_normalizing = False
        self._tile_rack_state_ready = True

        self.tile_rack_letters_var.trace_add("write", lambda *_: self._normalize_tile_rack_letters())
        self.tile_rack_word_var.trace_add("write", lambda *_: self._normalize_tile_rack_word())
        self.tile_rack_start_square_var.trace_add("write", lambda *_: self._update_tile_rack_word_option())

    def _normalize_tile_rack_letters(self) -> None:
        if self._tile_rack_normalizing:
            return
        self._tile_rack_normalizing = True
        try:
            normalized = normalize_rack_letters(self.tile_rack_letters_var.get())
            if normalized != self.tile_rack_letters_var.get():
                self.tile_rack_letters_var.set(normalized)
        finally:
            self._tile_rack_normalizing = False
        self._update_tile_rack_word_option()

    def _normalize_tile_rack_word(self) -> None:
        if self._tile_rack_word_normalizing:
            return
        self._tile_rack_word_normalizing = True
        try:
            normalized = normalize_word(self.tile_rack_word_var.get())
            if normalized != self.tile_rack_word_var.get():
                self.tile_rack_word_var.set(normalized)
        finally:
            self._tile_rack_word_normalizing = False
        self._update_tile_rack_word_option()

    def capture_tile_rack_from_camera(self) -> None:
        self._ensure_tile_rack_state()
        try:
            if self._captured_photo_frame is None:
                frame, quality = self._capture_best_photo_for_ocr("tile rack")
                source = f"best camera frame (sharpness {quality.sharpness:.0f})"
            else:
                frame = self._captured_photo_frame.copy()
                source = "captured picture"
            self._set_tile_rack_status(f"Scanning rack letters from the {source}...")
            thread = threading.Thread(
                target=self._scan_tile_rack_worker,
                args=(frame.copy(),),
                daemon=True,
            )
            thread.start()
        except Exception as exc:
            self._show_error(exc)

    def _scan_tile_rack_worker(self, frame) -> None:  # type: ignore[no-untyped-def]
        try:
            scan = scan_camera_letters(frame)
        except Exception as exc:
            self.root.after(0, lambda exc=exc: self._show_error(exc))
            return
        self.root.after(0, lambda: self._handle_tile_rack_scan_result(scan))

    def _handle_tile_rack_scan_result(self, scan: CameraLetterScanResult) -> None:
        rack_letters = self._rack_letters_from_camera_scan(scan)
        self.tile_rack_letters_var.set(rack_letters)
        if rack_letters:
            message = f"Tile rack captured {len(rack_letters)} letter(s): {rack_letters}"
        else:
            message = "No rack letters were detected. Type the rack letters or capture a clearer rack image."
        self._set_tile_rack_status(message)
        self._refresh_camera_preview()

    def _rack_letters_from_camera_scan(self, scan: CameraLetterScanResult) -> str:
        detected = []
        for captured in sorted(scan.letters, key=self._camera_letter_sort_key):
            text = getattr(captured, "text", "")
            detected.extend(character for character in str(text).upper() if character.isalpha())
        return normalize_rack_letters("".join(detected))

    def _camera_letter_sort_key(self, captured) -> tuple[float, float]:  # type: ignore[no-untyped-def]
        center_x = getattr(captured, "center_x", None)
        center_y = getattr(captured, "center_y", None)
        if center_x is not None and center_y is not None:
            return (float(center_x), float(center_y))
        left = getattr(captured, "left", None)
        top = getattr(captured, "top", None)
        if left is not None and top is not None:
            return (float(left), float(top))
        points = getattr(captured, "points", None) or getattr(captured, "corners", None)
        if points:
            xs = [float(point[0]) for point in points]
            ys = [float(point[1]) for point in points]
            return (min(xs), min(ys))
        return (0.0, 0.0)

    def suggest_tile_rack_word(self) -> None:
        self._ensure_tile_rack_state()
        rack_letters = normalize_rack_letters(self.tile_rack_letters_var.get())
        candidates = self._tile_rack_word_candidates()
        playable = [
            normalize_word(candidate)
            for candidate in candidates
            if can_build_word_from_rack(str(candidate), rack_letters)
        ]
        playable = sorted({word for word in playable if word}, key=lambda word: (-len(word), word))
        if not playable:
            self._set_tile_rack_status(
                "No playable word was found in the loaded word list. Enter a word manually to check it."
            )
            return
        self.tile_rack_word_var.set(playable[0])
        self._set_tile_rack_status(f"Suggested word: {playable[0]}")

    def _tile_rack_word_candidates(self) -> list[str]:
        candidates: list[str] = []
        for name in (
            "word_bank",
            "_word_bank",
            "valid_words",
            "_valid_words",
            "dictionary_words",
            "_dictionary_words",
        ):
            source = getattr(self, name, None)
            if isinstance(source, dict):
                candidates.extend(str(key) for key in source.keys())
            elif isinstance(source, (list, set, tuple)):
                candidates.extend(str(value) for value in source)

        for name in ("detected_words_var", "camera_words_var", "words_var"):
            variable = getattr(self, name, None)
            if variable is None:
                continue
            try:
                value = variable.get()
            except Exception:
                continue
            candidates.extend(value.replace(",", " ").split())
        return candidates

    def check_tile_rack_word(self) -> None:
        self._ensure_tile_rack_state()
        self._update_tile_rack_word_option(show_status=True)

    def _update_tile_rack_word_option(self, show_status: bool = False) -> None:
        if not getattr(self, "_tile_rack_state_ready", False):
            return
        rack_letters = normalize_rack_letters(self.tile_rack_letters_var.get())
        word = normalize_word(self.tile_rack_word_var.get())
        can_make = can_build_word_from_rack(word, rack_letters)
        placement_error: Exception | None = None
        squares: list[str] = []
        if can_make:
            try:
                squares = horizontal_word_squares(self.tile_rack_start_square_var.get(), word)
            except Exception as exc:
                placement_error = exc
        button = getattr(self, "tile_rack_make_button", None)
        if button is not None:
            button.configure(state="normal" if can_make and placement_error is None else "disabled")
        if show_status:
            if can_make and placement_error is None:
                self._set_tile_rack_status(f"{word} can be made and will be placed at {' '.join(squares)}.")
            elif placement_error is not None:
                self._set_tile_rack_status(str(placement_error))
            elif not rack_letters:
                self._set_tile_rack_status("Capture or type rack letters first.")
            elif not word:
                self._set_tile_rack_status("Enter the word to build from the rack.")
            else:
                self._set_tile_rack_status(f"{word} cannot be made from rack letters {rack_letters}.")

    def make_tile_rack_word_on_board(self) -> None:
        self._ensure_tile_rack_state()
        try:
            rack_letters = normalize_rack_letters(self.tile_rack_letters_var.get())
            word = normalize_word(self.tile_rack_word_var.get())
            if not can_build_word_from_rack(word, rack_letters):
                raise ValueError(f"{word or 'That word'} cannot be made from rack letters {rack_letters}.")
            slot_indices = rack_slot_indices_for_word(word, rack_letters)
            target_squares = horizontal_word_squares(self.tile_rack_start_square_var.get(), word)
            settings = self._tile_rack_motion_settings()
            self._place_tile_rack_word(word, slot_indices, target_squares, settings)
        except Exception as exc:
            self._show_error(exc)

    def move_to_tile_rack_start(self) -> None:
        self._ensure_tile_rack_state()
        try:
            settings = self._tile_rack_motion_settings()
            self._send_tile_rack_z_move(float(settings["safe_z"]), float(settings["z_feed"]))
            self._send_tile_rack_xy_move(
                float(settings["rack_x"]),
                float(settings["rack_y"]),
                float(settings["xy_feed"]),
            )
            self._set_tile_rack_status("Plotter moved to tile rack slot 1.")
        except Exception as exc:
            self._show_error(exc)

    def _tile_rack_motion_settings(self) -> dict[str, float | str]:
        return {
            "rack_x": float(self.tile_rack_x_var.get()),
            "rack_y": float(self.tile_rack_y_var.get()),
            "slot_spacing": float(self.tile_rack_spacing_var.get()),
            "safe_z": float(self.tile_rack_safe_z_var.get()),
            "pick_z": float(self.tile_rack_pick_z_var.get()),
            "place_z": float(self.tile_rack_place_z_var.get()),
            "xy_feed": float(self.tile_rack_xy_feed_var.get()),
            "z_feed": float(self.tile_rack_z_feed_var.get()),
            "magnet_on": self.tile_rack_magnet_on_var.get().strip(),
            "magnet_off": self.tile_rack_magnet_off_var.get().strip(),
        }

    def _place_tile_rack_word(
        self,
        word: str,
        slot_indices: list[int],
        target_squares: list[str],
        settings: dict[str, float | str],
    ) -> None:
        self._set_tile_rack_status(f"Placing {word} from the tile rack...")
        self._log(f"Tile rack placement started for {word}.")
        self._send_tile_rack_z_move(float(settings["safe_z"]), float(settings["z_feed"]))

        for letter, slot_index, target_square in zip(word, slot_indices, target_squares):
            rack_x = float(settings["rack_x"]) + slot_index * float(settings["slot_spacing"])
            rack_y = float(settings["rack_y"])

            self._log(f"Picking {letter} from rack slot {slot_index + 1}.")
            self._send_tile_rack_xy_move(rack_x, rack_y, float(settings["xy_feed"]))
            self._send_tile_rack_z_move(float(settings["pick_z"]), float(settings["z_feed"]))
            self._send_tile_rack_command(str(settings["magnet_on"]))
            self._send_tile_rack_command("G4 P0.2")
            self._send_tile_rack_z_move(float(settings["safe_z"]), float(settings["z_feed"]))

            self._log(f"Placing {letter} on {target_square}.")
            self._send_square_move(target_square)
            self._send_tile_rack_z_move(float(settings["place_z"]), float(settings["z_feed"]))
            self._send_tile_rack_command(str(settings["magnet_off"]))
            self._send_tile_rack_command("G4 P0.2")
            self._send_tile_rack_z_move(float(settings["safe_z"]), float(settings["z_feed"]))

        self._set_tile_rack_status(f"Placed {word} horizontally from {target_squares[0]}.")
        self._log(f"Tile rack placement complete for {word}: {' '.join(target_squares)}.")

    def _send_tile_rack_xy_move(self, x: float, y: float, feed: float) -> None:
        self._send_tile_rack_command(f"G0 X{x:g} Y{y:g} F{feed:g}")

    def _send_tile_rack_z_move(self, z: float, feed: float) -> None:
        self._send_tile_rack_command(f"G0 Z{z:g} F{feed:g}")

    def _send_tile_rack_command(self, command: str) -> None:
        command = command.strip()
        if not command:
            return

        for method_name in (
            "_send_raw_command",
            "_send_manual_command",
            "_send_command",
            "_send_gcode_command",
        ):
            method = getattr(self, method_name, None)
            if callable(method):
                try:
                    method(command)
                    return
                except TypeError:
                    pass

        sender = getattr(self, "_sender", None) or getattr(self, "sender", None)
        if sender is not None:
            for method_name in ("send_command", "send", "write"):
                method = getattr(sender, method_name, None)
                if callable(method):
                    try:
                        method(command)
                        return
                    except TypeError:
                        pass
            send_commands = getattr(sender, "send_commands", None)
            if callable(send_commands):
                try:
                    send_commands([command])
                    return
                except TypeError:
                    pass

        raise RuntimeError(
            "No raw G-code sender was found for rack Z or magnet commands. "
            "Add a manual command sender or connect this method to the controller command path."
        )

    def _set_tile_rack_status(self, message: str) -> None:
        if getattr(self, "_tile_rack_state_ready", False):
            self.tile_rack_status_var.set(message)
        self._set_status(message)

    def reset_to_start(self) -> None:
        try:
            self._send_reset()
        except Exception as exc:
            self._show_error(exc)

    def go_to_cart(self) -> None:
        try:
            self._send_cart_move()
        except Exception as exc:
            self._show_error(exc)

    def scan_board_from_camera(self) -> None:
        try:
            calibration = self._calibration_from_form()
            calibration.validate_ready_for_scan()
            if self._captured_photo_frame is None:
                frame, quality = self._capture_best_photo_for_ocr("board scan")
                source = f"best camera frame (sharpness {quality.sharpness:.0f})"
            else:
                frame = self._captured_photo_frame.copy()
                source = "captured picture"
            self._set_status(f"Scanning the calibrated board from the {source}...")
            self._log(f"Calibrated board scan started from {source}.")
            thread = threading.Thread(
                target=self._scan_board_worker,
                args=(frame.copy(), calibration),
                daemon=True,
            )
            thread.start()
        except RuntimeError as exc:
            if str(exc) == "Start the camera first.":
                raise_user_error(str(exc))
            else:
                self._show_error(exc)
        except Exception as exc:
            self._show_error(exc)

    def take_picture_from_camera(self) -> None:
        try:
            _, quality = self._capture_best_photo_for_ocr("manual picture")
            self._set_status(
                f"Best picture captured. Sharpness {quality.sharpness:.0f}; click Find Words, Capture Letters, or Scan Board."
            )
        except RuntimeError as exc:
            if str(exc) == "Start the camera first.":
                raise_user_error(str(exc))
            else:
                self._show_error(exc)
        except Exception as exc:
            self._show_error(exc)

    def resume_live_camera(self) -> None:
        if self._captured_photo_frame is None:
            self._set_status("Live camera view is already active.")
            return

        self._captured_photo_frame = None
        self._invalidate_camera_scans()
        self._last_camera_letter_scan = None
        self._last_camera_word_scan = None
        self.captured_letters_var.set("")
        self._set_camera_words_text("")
        self._refresh_camera_preview()
        self._set_status("Live camera view resumed.")
        self._log("Live camera view resumed.")

    def _capture_best_photo_for_ocr(self, reason: str):  # type: ignore[no-untyped-def]
        frame, quality = self._capture_best_camera_frame()
        self._captured_photo_frame = frame.copy()
        self._invalidate_camera_scans()
        self._last_camera_letter_scan = None
        self._last_camera_word_scan = None
        self.captured_letters_var.set("")
        self._set_camera_words_text("")
        self._refresh_camera_preview()
        self._log(
            f"Selected best camera frame for {reason}: "
            f"score={quality.score:.0f}, sharpness={quality.sharpness:.0f}, "
            f"contrast={quality.contrast:.1f}, brightness={quality.brightness:.1f}."
        )
        return self._captured_photo_frame.copy(), quality

    def _capture_best_camera_frame(self):  # type: ignore[no-untyped-def]
        frames = []
        if self._latest_frame is not None:
            frames.append(self._latest_frame.copy())

        deadline = time.monotonic() + BEST_CAPTURE_TIMEOUT_SECONDS
        while (
            self._camera is not None
            and len(frames) < BEST_CAPTURE_FRAME_COUNT
            and time.monotonic() < deadline
        ):
            frame = read_camera_frame(self._camera)
            if frame is not None:
                self._camera_failed_reads = 0
                self._latest_frame = frame
                frames.append(frame.copy())
            else:
                self._camera_failed_reads += 1
                if self._camera_failed_reads >= CAMERA_READ_FAILURE_LIMIT:
                    break
            time.sleep(BEST_CAPTURE_FRAME_DELAY_SECONDS)

        if not frames:
            raise RuntimeError("Start the camera first.")

        frame, quality = select_best_frame(frames)
        return frame.copy(), quality

    def capture_letters_from_camera(self) -> None:
        try:
            confidence_threshold = self._ocr_confidence_threshold()
            if self._captured_photo_frame is None:
                frame, quality = self._capture_best_photo_for_ocr("letter capture")
                source = f"best camera frame (sharpness {quality.sharpness:.0f})"
            else:
                frame = self._captured_photo_frame.copy()
                source = "captured picture"
            scan_token = self._next_camera_letter_scan_token()
            self._set_status(f"Capturing letters from the {source}...")
            self._log(f"Camera letter capture started from {source}.")
            thread = threading.Thread(
                target=self._camera_letter_scan_worker,
                args=(frame.copy(), confidence_threshold, True, scan_token),
                daemon=True,
            )
            thread.start()
        except RuntimeError as exc:
            if str(exc) == "Start the camera first.":
                raise_user_error(str(exc))
            else:
                self._show_error(exc)
        except Exception as exc:
            self._show_error(exc)

    def identify_words_with_easyocr(self) -> None:
        if self._camera_word_scan_running:
            self._set_status("EasyOCR word detection is already running.")
            return

        try:
            confidence_threshold = self._ocr_confidence_threshold()
            if self._captured_photo_frame is None:
                frame, quality = self._capture_best_photo_for_ocr("word detection")
                source = f"best camera frame (sharpness {quality.sharpness:.0f})"
            else:
                frame = self._captured_photo_frame.copy()
                source = "captured picture"
            self._camera_word_scan_running = True
            scan_token = self._next_camera_word_scan_token()
            self._set_status(f"Finding words in the {source} with EasyOCR...")
            self._set_camera_words_text("Finding words...")
            self._log(f"EasyOCR word detection started from {source}.")
            thread = threading.Thread(
                target=self._camera_word_scan_worker,
                args=(frame.copy(), confidence_threshold, True, scan_token),
                daemon=True,
            )
            thread.start()
        except RuntimeError as exc:
            if str(exc) == "Start the camera first.":
                raise_user_error(str(exc))
            else:
                self._show_error(exc)
        except Exception as exc:
            self._camera_word_scan_running = False
            self._show_error(exc)

    def identify_words_with_paddleocr(self) -> None:
        self.identify_words_with_easyocr()

    def identify_words_with_gemini(self) -> None:
        self.identify_words_with_easyocr()

    def calculate_score_from_board(self) -> None:
        try:
            self._normalize_board_entries()
            calibration = self._calibration_from_form()
            board_letters = self._board_letters_from_form()
            score = score_board(
                board_letters,
                premium_layout=calibration.premium_layout,
                blank_squares=self._blank_squares_from_form(),
            )
            self._handle_score_result(score)
            self._set_camera_words_text(format_words_by_direction(matched_matrix_words(board_letters)))
        except Exception as exc:
            self._show_error(exc)

    def ask_gemini(self) -> None:
        try:
            calibration = self._calibration_from_form()
            calibration.validate_ready_for_move()
            image_jpeg = self._latest_camera_jpeg() if self.gemini_include_camera_var.get() else None
            self._set_status("Asking Gemini for the next plotter action...")
            self._log("Gemini request started.")
            thread = threading.Thread(
                target=self._ask_gemini_worker,
                args=(calibration, image_jpeg),
                daemon=True,
            )
            thread.start()
        except Exception as exc:
            self._show_error(exc)

    def run_gemini_action(self) -> None:
        try:
            if self._last_agent_action is None:
                self.ask_gemini()
                return
            self._execute_agent_action(self._last_agent_action)
            self._last_agent_action = None
        except Exception as exc:
            self._show_error(exc)

    def _scan_board_worker(self, frame, calibration: PlotterCalibration) -> None:  # type: ignore[no-untyped-def]
        try:
            scan = scan_board_image(frame, calibration)
            score = score_board(
                scan.board_letters(),
                premium_layout=calibration.premium_layout,
                blank_squares=scan.blank_squares(),
            )
            self.root.after(0, lambda: self._handle_scan_result(scan, score, announce=True))
        except Exception as exc:
            self.root.after(0, lambda exc=exc: self._show_error(exc))

    def _camera_letter_scan_worker(
        self,
        frame,
        confidence_threshold: float,
        announce: bool,
        scan_token: int,
    ) -> None:  # type: ignore[no-untyped-def]
        try:
            scan = scan_camera_letters(frame, confidence_threshold=confidence_threshold)
            self.root.after(
                0,
                lambda: self._handle_camera_letter_scan_result(scan, announce=announce, scan_token=scan_token),
            )
        except Exception as exc:
            self.root.after(
                0,
                lambda exc=exc: self._handle_camera_letter_scan_error(
                    exc,
                    announce=announce,
                    scan_token=scan_token,
                ),
            )

    def _camera_word_scan_worker(
        self,
        frame,
        confidence_threshold: float,
        announce: bool,
        scan_token: int,
    ) -> None:  # type: ignore[no-untyped-def]
        try:
            scan = scan_camera_words(frame, confidence_threshold=confidence_threshold)
            self.root.after(
                0,
                lambda: self._handle_camera_word_scan_result(scan, announce=announce, scan_token=scan_token),
            )
        except Exception as exc:
            self.root.after(
                0,
                lambda exc=exc: self._handle_camera_word_scan_error(
                    exc,
                    announce=announce,
                    scan_token=scan_token,
                ),
            )

    def _handle_camera_letter_scan_result(
        self,
        scan: CameraLetterScanResult,
        announce: bool = False,
        scan_token: int | None = None,
    ) -> None:
        if scan_token is not None and scan_token != self._camera_letter_scan_token:
            return
        self._live_letter_scan_running = False
        self._last_live_letter_scan_error = None
        self._last_camera_letter_scan = scan
        captured_text = scan.text()
        self.captured_letters_var.set(captured_text)
        aligned_count = self._apply_camera_ocr_grid_to_board_form(scan.grid) if announce else 0
        if captured_text:
            message = f"Captured {len(scan.letters)} letter group(s): {captured_text}"
        else:
            message = "No letters captured from the camera."
        if scan.grid is not None:
            message += f" Auto grid aligned {len(scan.grid.cells)} letter(s)."
        elif announce:
            message += " Board grid was not visible enough to align."
        self._set_status(message)
        self._refresh_camera_preview()
        if announce:
            if aligned_count:
                self._log(f"Camera OCR grid placed {aligned_count} letter(s) into the board matrix.")
            self._log(message)

    def _handle_camera_letter_scan_error(
        self,
        exc: Exception,
        announce: bool = False,
        scan_token: int | None = None,
    ) -> None:
        if scan_token is not None and scan_token != self._camera_letter_scan_token:
            return
        self._live_letter_scan_running = False
        message = str(exc)
        if announce or self._last_live_letter_scan_error != message:
            self._last_live_letter_scan_error = message
            self._set_status(f"Letter capture paused: {message}")
            self._log(f"Letter capture paused: {message}")
        if "easyocr" in message.lower():
            self.live_letter_scan_var.set(False)

    def _handle_camera_word_scan_result(
        self,
        scan: CameraWordScanResult,
        announce: bool = False,
        scan_token: int | None = None,
    ) -> None:
        if scan_token is not None and scan_token != self._camera_word_scan_token:
            return
        self._camera_word_scan_running = False
        self._last_live_word_scan_error = None
        self._last_camera_word_scan = scan
        aligned_count = self._apply_camera_ocr_grid_to_board_form(scan.grid) if announce else 0
        formatted_words = format_camera_words_numbered(scan.words)
        if scan.grid is not None and scan.grid.cells:
            grid_words = matched_matrix_words(scan.grid.board_letters())
            if grid_words:
                formatted_words = format_words_by_direction(grid_words)
        self._set_camera_words_text(formatted_words)
        if scan.words:
            message = f"EasyOCR matched {len(scan.words)} word(s) from {len(scan.text_boxes)} text box(es)."
            if announce:
                if aligned_count:
                    self._log(f"Camera OCR grid placed {aligned_count} letter(s) into the board matrix.")
                self._log("Matched words:\n" + formatted_words)
        else:
            message = f"EasyOCR found {len(scan.text_boxes)} text box(es), but no words matched the list."
            if announce:
                self._log("Matched words: none")
        if scan.grid is not None:
            message += f" Auto grid aligned {len(scan.grid.cells)} letter(s)."
        elif announce:
            message += " Board grid was not visible enough to align."
        self._set_status(message)
        self._refresh_camera_preview()

    def _handle_camera_word_scan_error(
        self,
        exc: Exception,
        announce: bool = False,
        scan_token: int | None = None,
    ) -> None:
        if scan_token is not None and scan_token != self._camera_word_scan_token:
            return
        self._camera_word_scan_running = False
        message = str(exc)
        if announce or self._last_live_word_scan_error != message:
            self._last_live_word_scan_error = message
            self._set_status(f"Word detection paused: {message}")
            self._log(f"Word detection paused: {message}")
        if "easyocr" in message.lower():
            self.live_word_scan_var.set(False)

    def _handle_scan_result(self, scan: BoardScanResult, score: ScoreResult, announce: bool = True) -> None:
        self._last_scan = scan
        try:
            threshold = float(self.ocr_confidence_threshold_var.get())
        except ValueError:
            threshold = self._calibration.ocr_confidence_threshold
        cells_by_square = {cell.square: cell for cell in scan.cells}
        for row in range(BOARD_SIZE):
            for col in range(BOARD_SIZE):
                cell = cells_by_square.get(f"{chr(ord('A') + col)}{row + 1}")
                letter = cell.letter if cell else ""
                self._letter_vars[row][col].set(letter)
                entry = self._letter_entries[row][col]
                if cell is not None and cell.occupied and (not cell.letter or cell.confidence < threshold):
                    entry.configure(bg="#fff2a8")
                elif cell is not None and cell.letter:
                    entry.configure(bg="#e8f7e8")
                else:
                    entry.configure(bg="white")
        self.blank_squares_var.set("")
        self._set_camera_words_text(format_words_by_direction(matched_matrix_words(scan.board_letters())))
        occupied = sum(1 for cell in scan.cells if cell.occupied)
        recognized = sum(1 for cell in scan.cells if cell.letter)
        if announce:
            self._handle_score_result(score)
            self._set_status(f"Scanned {recognized} letters from {occupied} occupied cells.")
            self._log(f"Scan complete: {recognized} recognized, {occupied} occupied.")
        else:
            self._set_status(
                f"Live scan: {recognized} letters from {occupied} occupied cells. Score {score.total_score}."
            )

    def _apply_camera_ocr_grid_to_board_form(self, grid) -> int:  # type: ignore[no-untyped-def]
        if grid is None:
            return 0

        board_letters = grid.board_letters()
        placed = 0
        for row in range(BOARD_SIZE):
            for col in range(BOARD_SIZE):
                letter = normalize_letter(board_letters[row][col])
                self._letter_vars[row][col].set(letter)
                entry = self._letter_entries[row][col]
                if letter:
                    placed += 1
                    entry.configure(bg="#e8f7e8")
                else:
                    entry.configure(bg="white")
        self._set_camera_words_text(format_words_by_direction(matched_matrix_words(board_letters)))
        return placed

    def _handle_score_result(self, score: ScoreResult) -> None:
        self._set_status(f"Board score: {score.total_score}")
        self._log(self._format_score_result(score))

    def _ask_gemini_worker(self, calibration: PlotterCalibration, image_jpeg: bytes | None) -> None:
        try:
            agent = GeminiPlotterAgent(
                api_key=self.gemini_api_key_var.get(),
                model=self.gemini_model_var.get(),
            )
            action = agent.decide(
                self.gemini_objective_var.get(),
                calibration,
                image_jpeg=image_jpeg,
                timeout=max(5.0, float(self.timeout_var.get()) * 10.0),
            )
            self.root.after(0, lambda: self._handle_agent_action(action))
        except Exception as exc:
            self.root.after(0, lambda exc=exc: self._show_error(exc))

    def _handle_agent_action(self, action: PlotterAgentAction) -> None:
        self._last_agent_action = action
        summary = self._format_agent_action(action)
        self._set_status(f"Gemini chose: {summary}")
        self._log(f"Gemini chose: {summary}")
        if action.reason:
            self._log(f"Gemini reason: {action.reason}")

    def _execute_agent_action(self, action: PlotterAgentAction) -> None:
        if action.action == "move_square" and action.square:
            self.square_var.set(action.square)
            self._send_square_move(action.square)
            return
        if action.action == "go_cart":
            self._send_cart_move()
            return
        if action.action == "reset":
            self._send_reset()
            return
        self._set_status("Gemini chose no movement.")
        self._log("Gemini action: none")

    def _format_agent_action(self, action: PlotterAgentAction) -> str:
        if action.action == "move_square" and action.square:
            return f"move to {action.square}"
        if action.action == "go_cart":
            return "go to cart"
        if action.action == "reset":
            return "reset to start"
        return "none"

    def _latest_camera_jpeg(self) -> bytes | None:
        frame = self._current_camera_ocr_frame()
        if frame is None:
            return None
        cv2 = _require_cv2()
        ok, encoded = cv2.imencode(".jpg", frame)
        if not ok:
            raise RuntimeError("Could not encode the current camera frame for Gemini.")
        return encoded.tobytes()

    def _send_square_move(self, square_label: str) -> None:
        calibration = self._calibration_from_form()
        calibration.validate_ready_for_move()
        square = parse_square_label(square_label)
        x, y = calibration.square_center_in_machine(square)
        sender = self._get_sender()
        gcode, responses = sender.send_move(
            x,
            y,
            feed_rate=self._optional_float(self.feed_rate_var.get()),
            command=self.command_var.get().strip() or "G0",
        )
        self._set_status(f"Sent {square.label} to {sender.config.port}")
        self._log(f"Sent: {gcode}")
        if responses:
            self._log("Responses: " + " | ".join(responses))

    def _send_cart_move(self) -> None:
        calibration = self._calibration_from_form()
        calibration.validate_ready_for_move()
        x, y = calibration.cart_position_in_machine()
        sender = self._get_sender()
        gcode, responses = sender.send_move(
            x,
            y,
            feed_rate=self._optional_float(self.feed_rate_var.get()),
            command=self.command_var.get().strip() or "G0",
        )
        self._set_status(f"Sent cart move to X={x:.3f}, Y={y:.3f}")
        self._log(f"Sent: {gcode}")
        if responses:
            self._log("Responses: " + " | ".join(responses))

    def _send_pick_and_drop(self, pick_square_label: str, drop_square_label: str) -> None:
        calibration = self._calibration_from_form()
        calibration.validate_ready_for_move()
        pick_square = parse_square_label(pick_square_label)
        drop_square = parse_square_label(drop_square_label)
        feed_rate = self._optional_float(self.feed_rate_var.get())
        command = self.command_var.get().strip() or "G0"
        pick_x, pick_y = calibration.square_center_in_machine(pick_square)
        drop_x, drop_y = calibration.square_center_in_machine(drop_square)

        sender = self._get_sender()
        self._send_pick_drop_move(sender, pick_x, pick_y, feed_rate, command)
        self._send_pick_drop_auxiliary(sender, Z_DOWN_COMMAND)
        self._pick_drop_wait(PICK_DROP_Z_SETTLE_SECONDS)
        self._send_pick_drop_auxiliary(sender, "M1")
        self._pick_drop_wait(PICK_DROP_MAGNET_SETTLE_SECONDS)
        self._send_pick_drop_auxiliary(sender, Z_UP_COMMAND)
        self._pick_drop_wait(PICK_DROP_LIFT_RETRY_SECONDS)
        self._send_pick_drop_auxiliary(sender, Z_UP_COMMAND)
        self._pick_drop_wait(PICK_DROP_Z_SETTLE_SECONDS)
        self._send_pick_drop_move(sender, drop_x, drop_y, feed_rate, command)
        self._send_pick_drop_auxiliary(sender, Z_DOWN_COMMAND)
        self._pick_drop_wait(PICK_DROP_Z_SETTLE_SECONDS)
        self._send_pick_drop_auxiliary(sender, "M0")

        self._set_status(f"Pick/drop complete: {pick_square.label} -> {drop_square.label}")

    def _send_pick_drop_move(
        self,
        sender: GCodeSender,
        x: float,
        y: float,
        feed_rate: float | None,
        command: str,
    ) -> None:
        gcode, responses = sender.send_move(x, y, feed_rate=feed_rate, command=command)
        self._log(f"Sent: {gcode}")
        if responses:
            self._log("Responses: " + " | ".join(responses))
        self._raise_for_pick_drop_error(gcode, responses)

    def _send_pick_drop_auxiliary(self, sender: GCodeSender, command: str) -> None:
        responses = sender.send_command(command, startup_g90=False)
        self._log(f"Sent auxiliary command: {command}")
        if responses:
            self._log("Responses: " + " | ".join(responses))
        self._raise_for_pick_drop_error(command, responses)

    def _raise_for_pick_drop_error(self, command: str, responses: list[str]) -> None:
        if not responses:
            raise RuntimeError(f"No Arduino response after {command}; stopped pick/drop sequence.")
        for response in responses:
            lowered = response.lower()
            if lowered.startswith("err") or lowered.startswith("error"):
                raise RuntimeError(f"Arduino rejected {command}: {response}")

    def _pick_drop_wait(self, seconds: float) -> None:
        time.sleep(seconds)

    def _send_reset(self) -> None:
        sender = self._get_sender()
        command, responses = sender.send_reset()
        self._set_status(f"Sent reset command to {sender.config.port}")
        self._log(f"Sent: {command}")
        if responses:
            self._log("Responses: " + " | ".join(responses))

    def refresh_ports(self) -> None:
        try:
            ports = list_serial_ports()
            if ports and not self.port_var.get().strip():
                self.port_var.set(ports[0])
            if ports:
                self._log("Available ports: " + ", ".join(ports))
            else:
                self._log("No serial ports detected automatically. You can still type one manually.")
        except Exception as exc:
            self._show_error(exc)

    def _schedule_camera_update(self) -> None:
        self._camera_after_id = self.root.after(30, self._update_camera_frame)

    def _update_camera_frame(self) -> None:
        if self._camera is None:
            return

        frame = read_camera_frame(self._camera)
        if frame is not None:
            self._camera_failed_reads = 0
            self._latest_frame = frame
            if self._captured_photo_frame is None:
                self._maybe_start_live_letter_scan(frame)
                self._maybe_start_live_word_scan(frame)
                self._show_frame(frame)
        else:
            self._camera_failed_reads += 1
            if self._camera_failed_reads >= CAMERA_READ_FAILURE_LIMIT:
                self._handle_camera_frame_loss()
                return
        self._schedule_camera_update()

    def _handle_camera_frame_loss(self) -> None:
        message = (
            "Camera stopped because no video frames were available. "
            "Try another camera number or close other apps using the webcam."
        )
        self.stop_camera(clear_preview=True)
        self.preview_label.configure(image="", text="Camera frame unavailable")
        self.preview_image = None
        self._preview_display_size = (0, 0)
        self._preview_source_size = (0, 0)
        self._set_status(message)
        self._log(message)

    def _maybe_start_live_letter_scan(self, frame) -> None:  # type: ignore[no-untyped-def]
        if not self.live_letter_scan_var.get() or self._live_letter_scan_running:
            return

        now = time.monotonic()
        if now - self._last_live_letter_scan_at < LIVE_LETTER_SCAN_INTERVAL_SECONDS:
            return
        self._last_live_letter_scan_at = now

        try:
            confidence_threshold = self._ocr_confidence_threshold()
        except Exception as exc:
            self._handle_camera_letter_scan_error(exc)
            return

        self._live_letter_scan_running = True
        scan_token = self._next_camera_letter_scan_token()
        thread = threading.Thread(
            target=self._camera_letter_scan_worker,
            args=(frame.copy(), confidence_threshold, False, scan_token),
            daemon=True,
        )
        thread.start()

    def _maybe_start_live_word_scan(self, frame) -> None:  # type: ignore[no-untyped-def]
        if not self.live_word_scan_var.get() or self._camera_word_scan_running:
            return

        now = time.monotonic()
        if now - self._last_live_word_scan_at < LIVE_WORD_SCAN_INTERVAL_SECONDS:
            return
        self._last_live_word_scan_at = now

        try:
            confidence_threshold = self._ocr_confidence_threshold()
        except Exception as exc:
            self._handle_camera_word_scan_error(exc)
            return

        self._camera_word_scan_running = True
        scan_token = self._next_camera_word_scan_token()
        thread = threading.Thread(
            target=self._camera_word_scan_worker,
            args=(frame.copy(), confidence_threshold, False, scan_token),
            daemon=True,
        )
        thread.start()

    def _current_camera_ocr_frame(self):  # type: ignore[no-untyped-def]
        if self._captured_photo_frame is not None:
            return self._captured_photo_frame
        return self._latest_frame

    def _current_camera_ocr_grid(self):  # type: ignore[no-untyped-def]
        if self._last_camera_word_scan is not None and self._last_camera_word_scan.grid is not None:
            return self._last_camera_word_scan.grid
        if self._last_camera_letter_scan is not None:
            return self._last_camera_letter_scan.grid
        return None

    def _preview_click_to_frame_point(self, click_x: int, click_y: int) -> tuple[float, float] | None:
        display_width, display_height = self._preview_display_size
        source_width, source_height = self._preview_source_size
        if display_width <= 0 or display_height <= 0 or source_width <= 0 or source_height <= 0:
            return None

        label_width = max(1, self.preview_label.winfo_width())
        label_height = max(1, self.preview_label.winfo_height())
        image_left = max(0.0, (label_width - display_width) / 2.0)
        image_top = max(0.0, (label_height - display_height) / 2.0)
        image_x = float(click_x) - image_left
        image_y = float(click_y) - image_top
        if image_x < 0 or image_y < 0 or image_x > display_width or image_y > display_height:
            return None

        return (
            image_x * source_width / display_width,
            image_y * source_height / display_height,
        )

    def _refresh_camera_preview(self) -> None:
        frame = self._current_camera_ocr_frame()
        if frame is not None:
            self._show_frame(frame)

    def _invalidate_camera_scans(self) -> None:
        self._camera_letter_scan_token += 1
        self._camera_word_scan_token += 1
        self._live_letter_scan_running = False
        self._camera_word_scan_running = False

    def _next_camera_letter_scan_token(self) -> int:
        self._camera_letter_scan_token += 1
        return self._camera_letter_scan_token

    def _next_camera_word_scan_token(self) -> int:
        self._camera_word_scan_token += 1
        return self._camera_word_scan_token

    def _show_frame(self, frame) -> None:  # type: ignore[no-untyped-def]
        from PIL import Image, ImageTk

        cv2 = _require_cv2()
        captured_letters = self._last_camera_letter_scan.letters if self._last_camera_letter_scan else []
        detected_words = self._last_camera_word_scan.words if self._last_camera_word_scan else []
        detected_tiles = self._last_camera_word_scan.tiles if self._last_camera_word_scan else []
        ocr_grid = self._current_camera_ocr_grid()
        display = draw_camera_ocr_overlay(
            frame.copy(),
            captured_letters,
            detected_words,
            detected_tiles,
            ocr_grid=ocr_grid,
        )
        rgb = cv2.cvtColor(display, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(rgb)
        self._preview_source_size = (int(display.shape[1]), int(display.shape[0]))
        width = self.preview_label.winfo_width()
        height = self.preview_label.winfo_height()
        image.thumbnail((max(width, 700), max(height, 520)))
        self._preview_display_size = image.size
        self.preview_image = ImageTk.PhotoImage(image)
        self.preview_label.configure(image=self.preview_image, text="")

    def _calibration_from_form(self) -> PlotterCalibration:
        calibration = PlotterCalibration.load(self.calibration_path.get())
        calibration.board_size = BOARD_SIZE
        calibration.cell_size_mm = float(self.cell_size_var.get())
        calibration.camera_index = int(self.camera_index_var.get())
        calibration.offset_x_mm = float(self.offset_x_var.get())
        calibration.offset_y_mm = float(self.offset_y_var.get())
        calibration.x_steps_per_mm = float(self.x_steps_per_mm_var.get())
        calibration.y_steps_per_mm = float(self.y_steps_per_mm_var.get())
        calibration.cart_x_mm = float(self.cart_x_var.get())
        calibration.cart_y_mm = float(self.cart_y_var.get())
        calibration.ocr_confidence_threshold = float(self.ocr_confidence_threshold_var.get())
        calibration.ocr_cell_size_px = int(self.ocr_cell_size_px_var.get())
        calibration.premium_layout = self._premium_layout_from_form()
        return calibration

    def _load_premium_layout_into_form(self) -> None:
        for row in range(BOARD_SIZE):
            for col in range(BOARD_SIZE):
                self._premium_vars[row][col].set(premium_short_label(self._calibration.premium_layout[row][col]))

    def _premium_layout_from_form(self) -> list[list[str]]:
        self._normalize_premium_entries()
        return [
            [premium_from_short_label(self._premium_vars[row][col].get()) for col in range(BOARD_SIZE)]
            for row in range(BOARD_SIZE)
        ]

    def _board_letters_from_form(self) -> list[list[str]]:
        return [
            [normalize_letter(self._letter_vars[row][col].get()) for col in range(BOARD_SIZE)]
            for row in range(BOARD_SIZE)
        ]

    def _blank_squares_from_form(self) -> set[str]:
        raw = self.blank_squares_var.get().replace(",", " ").split()
        blanks: set[str] = set()
        for label in raw:
            blanks.add(parse_square_label(label).label)
        return blanks

    def _normalize_board_entries(self) -> None:
        for row in range(BOARD_SIZE):
            for col in range(BOARD_SIZE):
                self._letter_vars[row][col].set(normalize_letter(self._letter_vars[row][col].get()))

    def _normalize_premium_entries(self) -> None:
        for row in range(BOARD_SIZE):
            for col in range(BOARD_SIZE):
                code = premium_from_short_label(self._premium_vars[row][col].get())
                self._premium_vars[row][col].set(premium_short_label(code))

    def _format_score_result(self, score: ScoreResult) -> str:
        if not score.words:
            return "Score: 0 (no words found)"

        lines = [f"Score: {score.total_score}"]
        for word in score.words:
            direction = "H" if word.direction == "horizontal" else "V"
            squares = "-".join(word.squares)
            multiplier = f" x{word.word_multiplier}" if word.word_multiplier != 1 else ""
            lines.append(f"{word.word} {direction} {squares}: {word.base_score}{multiplier} = {word.score}")
        return "\n".join(lines)

    def _set_camera_words_text(self, text: str) -> None:
        if self.camera_words_text is None:
            return
        self.camera_words_text.configure(state="normal")
        self.camera_words_text.delete("1.0", "end")
        self.camera_words_text.insert("1.0", text)
        self.camera_words_text.configure(state="disabled")

    def _ocr_confidence_threshold(self) -> float:
        threshold = float(self.ocr_confidence_threshold_var.get())
        if threshold < 0 or threshold > 100:
            raise ValueError("OCR confidence threshold must be from 0 to 100.")
        return threshold

    def _optional_float(self, raw: str) -> float | None:
        value = raw.strip()
        return float(value) if value else None

    def _serial_config(self) -> SerialConfig:
        config = SerialConfig(
            port=self.port_var.get().strip(),
            baud=int(self.baud_var.get()),
            timeout=float(self.timeout_var.get()),
            startup_g90=self.startup_g90_var.get(),
        )
        if not config.port:
            raise ValueError("Enter a COM port such as COM3.")
        return config

    def _get_sender(self) -> GCodeSender:
        config = self._serial_config()
        sender_key = (config.port, config.baud, config.timeout, config.startup_g90)
        if self._sender is not None and self._sender_key == sender_key:
            return self._sender

        if self._sender is not None:
            self._sender.close()

        self._sender = GCodeSender(config)
        self._sender.open()
        self._sender_key = sender_key
        self._log(f"Connected to {config.port} at {config.baud} baud.")
        return self._sender

    def _on_close(self) -> None:
        self.stop_camera(clear_preview=False)
        if self._sender is not None:
            self._sender.close()
        self.root.destroy()

    def _set_status(self, message: str) -> None:
        self.status_var.set(message)

    def _log(self, message: str) -> None:
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")

    def _add_z_axis_controls_to_move_section(self) -> None:
        if self._z_axis_controls_frame is not None and self._z_axis_controls_frame.winfo_exists():
            return

        parent = self._find_move_plotter_section() or self.root
        controls = ttk.LabelFrame(parent, text="Z Movement / Relay Magnet", padding=10)
        self._z_axis_controls_frame = controls
        controls.columnconfigure(1, weight=1)

        ttk.Label(controls, text="Down height").grid(row=0, column=0, sticky="w", padx=(0, 8))
        tk.Spinbox(
            controls,
            from_=0,
            to=180,
            increment=1,
            textvariable=self.z_height_angle,
            width=6,
        ).grid(row=0, column=1, sticky="ew")
        ttk.Button(controls, text="Set Height", command=self._send_z_height_command).grid(
            row=0, column=2, sticky="ew", padx=(8, 0)
        )

        ttk.Scale(
            controls,
            from_=0,
            to=180,
            variable=self.z_height_angle,
            orient="horizontal",
        ).grid(row=1, column=0, columnspan=3, sticky="ew", pady=(8, 0))

        ttk.Button(controls, text="Z Down", command=lambda: self._send_auxiliary_command(Z_DOWN_COMMAND)).grid(
            row=2, column=0, sticky="ew", padx=(0, 6), pady=(10, 0)
        )
        ttk.Button(controls, text="Z Up", command=lambda: self._send_auxiliary_command(Z_UP_COMMAND)).grid(
            row=2, column=1, sticky="ew", padx=(0, 6), pady=(10, 0)
        )
        ttk.Button(
            controls,
            text="Magnet ON",
            command=lambda: self._send_auxiliary_command("M1"),
        ).grid(row=3, column=0, sticky="ew", padx=(0, 6), pady=(8, 0))
        ttk.Button(
            controls,
            text="Magnet OFF",
            command=lambda: self._send_auxiliary_command("M0"),
        ).grid(row=3, column=1, sticky="ew", padx=(0, 6), pady=(8, 0))
        ttk.Label(controls, text="A2 controls the relay input").grid(
            row=3, column=2, sticky="w", pady=(8, 0)
        )

        self._place_z_axis_controls(parent, controls)

    def _find_move_plotter_section(self):
        candidates = []
        for widget in self._walk_widgets(self.root):
            text = self._widget_text(widget).lower()
            if "move plotter" in text:
                return widget if self._looks_like_container(widget) else self._nearest_container(widget)
            if "plotter" in text and "move" in text:
                section = widget if self._looks_like_container(widget) else self._nearest_container(widget)
                if section is not None:
                    candidates.append((0, section))
            elif "move" in text:
                section = widget if self._looks_like_container(widget) else self._nearest_container(widget)
                if section is not None:
                    candidates.append((1, section))
            elif "send move" in text:
                section = self._nearest_container(widget)
                if section is not None:
                    candidates.append((0, section))

        if candidates:
            candidates.sort(key=lambda item: item[0])
            return candidates[0][1]
        return None

    def _walk_widgets(self, widget):
        yield widget
        for child in widget.winfo_children():
            yield from self._walk_widgets(child)

    def _widget_text(self, widget) -> str:
        try:
            return str(widget.cget("text"))
        except Exception:
            return ""

    def _looks_like_container(self, widget) -> bool:
        class_name = widget.winfo_class().lower()
        return "frame" in class_name or "notebook" in class_name or "pane" in class_name

    def _nearest_container(self, widget):
        parent = getattr(widget, "master", None)
        while parent is not None and parent is not self.root:
            text = self._widget_text(parent).lower()
            if "move" in text or "plotter" in text:
                return parent
            parent = getattr(parent, "master", None)
        return getattr(widget, "master", None)

    def _place_z_axis_controls(self, parent, controls) -> None:
        managers = [child.winfo_manager() for child in parent.winfo_children() if child is not controls]
        if "grid" in managers:
            rows = []
            for child in parent.winfo_children():
                if child is controls or child.winfo_manager() != "grid":
                    continue
                try:
                    rows.append(int(child.grid_info().get("row", 0)))
                except Exception:
                    pass
            controls.grid(row=(max(rows) + 1 if rows else 0), column=0, columnspan=3, sticky="ew", pady=(10, 0))
            try:
                parent.columnconfigure(0, weight=1)
            except Exception:
                pass
        elif "pack" in managers:
            controls.pack(fill="x", pady=(10, 0))
        else:
            controls.grid(row=0, column=0, sticky="ew", pady=(10, 0))

    def _send_z_height_command(self) -> None:
        angle = int(float(self.z_height_angle.get()))
        angle = max(0, min(180, angle))
        self.z_height_angle.set(angle)
        self._send_auxiliary_command(f"ZH{angle}")

    def _send_auxiliary_command(self, command: str) -> None:
        try:
            responses = self._write_auxiliary_serial_line(command)
            log = getattr(self, "_log", None)
            if callable(log):
                log(f"Sent auxiliary command: {command}")
                if responses:
                    log("Responses: " + " | ".join(responses))
            set_status = getattr(self, "_set_status", None)
            if callable(set_status):
                set_status(f"Sent {command}")
        except Exception as exc:
            self._show_error(exc)

    def _write_auxiliary_serial_line(self, command: str) -> list[str]:
        sender = self._get_sender()
        return sender.send_command(command, startup_g90=False)

    def _show_error(self, exc: Exception) -> None:
        message = self._format_user_error(exc)
        self._set_status(message)
        self._log("Error: " + message)
        messagebox.showerror("Scrabble Join", message)

    def _format_user_error(self, exc: Exception) -> str:
        message = str(exc)
        lowered = message.lower()
        if (
            "cannot configure port" in lowered
            or "permissionerror(13" in lowered
            or "a device attached to the system is not functioning" in lowered
        ):
            return (
                message
                + "\n\n"
                + "This means Windows could not open the selected Arduino COM port. "
                + "Close Arduino IDE Serial Monitor/Plotter and any other program using the board, "
                + "unplug and reconnect the USB cable, press Refresh Ports, then select the Arduino COM port again. "
                + "Use 115200 baud for the updated Arduino sketch. If the same COM port still fails, check Device Manager "
                + "and reinstall the CH340/Arduino USB driver or try another USB cable/port."
            )
        return message


def _require_cv2():
    try:
        import cv2  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "OpenCV is required for live camera preview. Install scrabble_plotter/requirements.txt."
        ) from exc
    return cv2


def raise_user_error(message: str) -> None:
    messagebox.showwarning("Scrabble Join", message)


def launch_gui() -> None:
    root = tk.Tk()
    ttk.Style(root).theme_use("clam")
    app = ScrabblePlotterApp(root)
    app._log("Ready.")
    root.mainloop()
