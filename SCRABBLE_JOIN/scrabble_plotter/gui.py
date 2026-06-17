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
        self.root.after(800, self._send_startup_z_up)
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
        ttk.Button(scan_buttons, text="Tile Rack Position", command=self.open_tile_rack_position_window).grid(
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
            self._send_move_target(self.square_var.get())
        except Exception as exc:
            self._show_error(exc)

    def _send_move_target(self, target: str) -> None:
        target = str(target).strip().upper()
        if self._is_tile_rack_target(target):
            self._send_tile_rack_target_move(target)
            return
        self._send_square_move(target)

    def _is_tile_rack_target(self, target: str) -> bool:
        return (
            len(target) >= 3
            and target[:2] == "TR"
            and target[2:].isdigit()
            and 1 <= int(target[2:]) <= 7
        )

    def _send_tile_rack_target_move(self, target: str) -> None:
        self._ensure_tile_rack_move_state()
        self._send_pick_drop_aux_command("ZU")
        slot_index = int(target[2:]) - 1
        x, y = self._tile_rack_slot_position(slot_index)
        feed = float(self.tile_rack_feed_var.get())
        command = f"G0 X{x:g} Y{y:g} F{feed:g}"
        self._send_tile_rack_move_command(command)
        self._set_status(f"Moved to {target} at X{x:g} Y{y:g}.")
        self._log(command)

    def _move_to_tile_rack_target_from_button(self, target: str) -> None:
        try:
            self._send_tile_rack_target_move(target)
        except Exception as exc:
            self._show_error(exc)

    def _tile_rack_slot_position(self, slot_index: int) -> tuple[float, float]:
        if slot_index < 0 or slot_index >= 7:
            raise ValueError("Tile rack target must be TR1 through TR7.")
        rack_x = float(self.tile_rack_tr1_x_var.get())
        rack_y = float(self.tile_rack_tr1_y_var.get())
        tile_size = float(self.tile_rack_tile_size_var.get())
        return rack_x, rack_y + tile_size * slot_index

    def _ensure_tile_rack_move_state(self) -> None:
        if getattr(self, "_tile_rack_move_state_ready", False):
            return

        default_feed = "1500"
        feed_var = getattr(self, "feed_rate_var", None)
        if feed_var is not None:
            try:
                default_feed = str(feed_var.get())
            except Exception:
                default_feed = "1500"

        self.tile_rack_tr1_x_var = tk.StringVar(value="335")
        self.tile_rack_tr1_y_var = tk.StringVar(value="30")
        self.tile_rack_tile_size_var = tk.StringVar(value="10")
        self.tile_rack_feed_var = tk.StringVar(value=default_feed)
        self.tile_rack_status_var = tk.StringVar(value="Enter TR1 to TR7 in Move Plotter to go to rack slots.")
        self._tile_rack_move_state_ready = True

    def _send_tile_rack_move_command(self, command: str) -> None:
        for method_name in (
            "_send_commands",
            "_send_serial_commands",
            "_send_controller_commands",
            "_send_gcode_commands",
            "_send_command_lines",
            "_send_gcode_lines",
            "_send_to_plotter",
            "_send_to_controller",
            "_send_serial",
            "_send_gcode",
            "_send_plotter_command",
            "_send_controller_command",
            "_send_raw_command",
            "_send_manual_command",
            "_send_command",
            "_send_gcode_command",
            "send_commands",
            "send_serial_commands",
            "send_controller_commands",
            "send_gcode_commands",
            "send_command_lines",
            "send_gcode_lines",
            "send_to_plotter",
            "send_to_controller",
            "send_serial",
            "send_gcode",
            "send_plotter_command",
            "send_controller_command",
            "send_raw_command",
            "send_manual_command",
            "send_command",
            "send_gcode_command",
        ):
            method = getattr(self, method_name, None)
            if not callable(method):
                continue
            if "commands" in method_name or "lines" in method_name:
                payloads = (
                    ([command],),
                    ([command], "tile rack move"),
                    (command,),
                    (command, "tile rack move"),
                )
            else:
                payloads = (
                    (command,),
                    (command, "tile rack move"),
                )
            for payload in payloads:
                try:
                    method(*payload)
                    return
                except TypeError:
                    continue

        for method_name in dir(self):
            lowered = method_name.lower()
            if (
                method_name in {
                    "send_move",
                    "reset_to_start",
                    "_send_square_move",
                    "_send_move_target",
                    "_send_reset",
                    "_send_tile_rack_move_command",
                    "_send_tile_rack_move_with_gcode_sender",
                    "_send_tile_rack_target_move",
                    "_move_to_tile_rack_target_from_button",
                }
                or ("send" not in lowered and "command" not in lowered and "gcode" not in lowered)
            ):
                continue
            method = getattr(self, method_name, None)
            if not callable(method):
                continue
            if "commands" in lowered or "lines" in lowered:
                payloads = (([command],), (command,))
            else:
                payloads = ((command,),)
            for payload in payloads:
                try:
                    method(*payload)
                    return
                except TypeError:
                    continue
                except Exception:
                    continue

        raise RuntimeError("Could not find the plotter command sender for tile rack movement.")

    def _pick_and_drop_from_tile_rack_target(self) -> bool:
        rack_target, board_target = self._tile_rack_pick_drop_targets()
        if rack_target is None:
            return False
        if board_target is None:
            raise ValueError("Enter the board drop square as A1 to L12.")

        self._set_status(f"Picking from {rack_target} and dropping on {board_target}...")
        self._log(f"Rack pickup through normal pick/drop: {rack_target} -> {board_target}")
        self._send_pick_drop_aux_command("ZU")
        self._send_tile_rack_target_move(rack_target)
        self._send_pick_drop_aux_command("ZD")
        self._send_pick_drop_aux_command("R1")
        self._send_pick_drop_aux_command("ZU")
        self._send_square_move(board_target)
        self._send_pick_drop_aux_command("ZD")
        self._send_pick_drop_aux_command("R0")
        self._send_pick_drop_aux_command("ZU")
        self._set_status(f"Picked from {rack_target} and dropped on {board_target}.")
        return True

    def _tile_rack_pick_drop_targets(self) -> tuple[str | None, str | None]:
        values: list[tuple[str, str]] = []
        for name, source in vars(self).items():
            if not hasattr(source, "get"):
                continue
            try:
                value = source.get()
            except Exception:
                continue
            text = str(value).strip()
            if text:
                values.append((name.lower(), text))

        source_words = ("pick", "pickup", "from", "source")
        drop_words = ("drop", "to", "target", "destination", "place")

        rack_target = None
        for name, text in values:
            if any(word in name for word in source_words) and self._is_tile_rack_target(text.upper()):
                rack_target = text.upper()
                break
        if rack_target is None:
            for _name, text in values:
                if self._is_tile_rack_target(text.upper()):
                    rack_target = text.upper()
                    break
        if rack_target is None:
            return None, None

        board_target = None
        for name, text in values:
            if any(word in name for word in drop_words) and self._looks_like_board_square(text):
                board_target = text.upper()
                break
        if board_target is None:
            for name, text in values:
                if "rack" not in name and self._looks_like_board_square(text):
                    board_target = text.upper()
                    break
        return rack_target, board_target

    def _looks_like_board_square(self, value: object) -> bool:
        text = str(value).strip().upper()
        if len(text) < 2:
            return False
        column = text[0]
        row = text[1:]
        return column in "ABCDEFGHIJKL" and row.isdigit() and 1 <= int(row) <= 12

    def _send_pick_drop_aux_command(self, command: str) -> None:
        writer = getattr(self, "_write_auxiliary_serial_line", None)
        if callable(writer):
            writer(command)
            self._log(f"Sent auxiliary command: {command}")
            return

        sender = getattr(self, "_send_auxiliary_command", None)
        if callable(sender):
            sender(command)
            return

        raise RuntimeError("Could not find the Z movement or relay command sender.")

    def _send_startup_z_up(self) -> None:
        try:
            self._send_pick_drop_aux_command("ZU")
            self._log("Startup Z up command sent.")
        except Exception as exc:
            log = getattr(self, "_log", None)
            if callable(log):
                log(f"Startup Z up command skipped: {exc}")

    def pick_and_drop(self) -> None:
        try:
            if self._pick_and_drop_from_tile_rack_target():
                return
            previous_force_z_up = getattr(self, "_force_z_up_before_pick_drop_move", False)
            self._force_z_up_before_pick_drop_move = True
            try:
                self._pick_and_drop_board_only()
                self._send_pick_drop_aux_command("ZU")
            finally:
                self._force_z_up_before_pick_drop_move = previous_force_z_up
        except Exception as exc:
            self._show_error(exc)

    def _pick_and_drop_board_only(self) -> None:
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

    def _send_tile_rack_move_with_gcode_sender(self, command: str) -> bool:
        return False

        try:
            config = self._tile_rack_move_serial_config()
        except Exception:
            return False

        if not self._tile_rack_move_config_has_port(config):
            return False

        try:
            sender = GCodeSender(config)
            if hasattr(sender, "__enter__") and hasattr(sender, "__exit__"):
                with sender as active_sender:
                    return self._send_tile_rack_move_to_sender(active_sender, command)
            try:
                return self._send_tile_rack_move_to_sender(sender, command)
            finally:
                close = getattr(sender, "close", None)
                if callable(close):
                    close()
        except Exception as exc:
            raise RuntimeError(str(exc)) from exc

    def _send_tile_rack_move_to_sender(self, sender, command: str) -> bool:  # type: ignore[no-untyped-def]
        for method_name in ("send_commands", "send_command", "send", "write"):
            method = getattr(sender, method_name, None)
            if not callable(method):
                continue
            payloads = ([command], command) if method_name == "send_commands" else (command, [command])
            for payload in payloads:
                try:
                    method(payload)
                    return True
                except TypeError:
                    continue
        return False

    def _tile_rack_move_serial_config(self) -> SerialConfig:
        import dataclasses
        import inspect

        for method_name in (
            "_serial_config_from_form",
            "_serial_config_from_inputs",
            "_get_serial_config",
            "serial_config_from_form",
            "get_serial_config",
        ):
            method = getattr(self, method_name, None)
            if not callable(method):
                continue
            try:
                config = method()
            except Exception:
                continue
            if isinstance(config, SerialConfig):
                return config

        if dataclasses.is_dataclass(SerialConfig):
            field_names = [field.name for field in dataclasses.fields(SerialConfig)]
        else:
            signature = inspect.signature(SerialConfig)
            field_names = [name for name in signature.parameters if name != "self"]

        values = {}
        for name in field_names:
            value = self._tile_rack_move_serial_value(name)
            if value is not None:
                values[name] = value
        return SerialConfig(**values)

    def _tile_rack_move_serial_value(self, name: str):  # type: ignore[no-untyped-def]
        key = name.lower()
        if key in {"port", "serial_port", "com_port"}:
            return self._tile_rack_selected_port()
        if key in {"baud", "baudrate", "baud_rate"}:
            return int(self._tile_rack_read_gui_value("baud_var", "baudrate_var", "baud_rate_var", default="115200"))
        if "timeout" in key:
            return float(self._tile_rack_read_gui_value("timeout_var", "serial_timeout_var", default="2.0"))
        if key in {"dry_run", "dryrun"}:
            value = self._tile_rack_read_gui_value("dry_run_var", "dryrun_var", default=False)
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in {"1", "true", "yes", "on"}
        if "delay" in key or "settle" in key:
            return float(self._tile_rack_read_gui_value("settle_seconds_var", "startup_delay_var", default="2.0"))
        return None

    def _tile_rack_move_config_has_port(self, config: SerialConfig) -> bool:
        for name in ("port", "serial_port", "com_port"):
            if hasattr(config, name):
                value = getattr(config, name)
                if value is not None and str(value).strip():
                    return True
        return False

    def _tile_rack_selected_port(self) -> str | None:
        port = self._tile_rack_read_gui_value(
            "port_var",
            "serial_port_var",
            "com_port_var",
            "plotter_port_var",
            "selected_port_var",
            "port_combo",
            "serial_port_combo",
            "com_port_combo",
        )
        if port is None:
            return None
        text = str(port).strip()
        if not text:
            return None
        if text.upper().startswith("COM"):
            return text.split()[0].rstrip(":,;")
        if text.startswith("/dev/") or text.lower().startswith("usb"):
            return text
        for name, source in vars(self).items():
            lowered = name.lower()
            if "port" not in lowered and "com" not in lowered and "serial" not in lowered:
                continue
            if not hasattr(source, "get"):
                continue
            try:
                value = source.get()
            except Exception:
                continue
            if value is None:
                continue
            text = str(value).strip()
            if not text:
                continue
            if text.upper().startswith("COM"):
                return text.split()[0].rstrip(":,;")
            if text.startswith("/dev/") or text.lower().startswith("usb"):
                return text
        return text

    def _tile_rack_read_gui_value(self, *names: str, default=None):  # type: ignore[no-untyped-def]
        for name in names:
            source = getattr(self, name, None)
            if source is None:
                continue
            try:
                value = source.get() if hasattr(source, "get") else source
            except Exception:
                continue
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            return value
        return default

    def open_tile_rack_position_window(self, force_open: bool = False) -> None:
        self._ensure_tile_rack_move_state()
        existing_window = getattr(self, "_tile_rack_position_window", None)
        if existing_window is not None and existing_window.winfo_exists():
            existing_window.lift()
            existing_window.focus_force()
            return

        window = tk.Toplevel(self.root)
        self._tile_rack_position_window = window
        window.title("Tile Rack Position")
        window.resizable(False, False)
        window.columnconfigure(1, weight=1)

        ttk.Label(window, text="TR1 X").grid(row=0, column=0, sticky="w", padx=10, pady=(10, 4))
        ttk.Entry(window, textvariable=self.tile_rack_tr1_x_var, width=12).grid(
            row=0, column=1, sticky="ew", padx=10, pady=(10, 4)
        )

        ttk.Label(window, text="TR1 Y").grid(row=1, column=0, sticky="w", padx=10, pady=4)
        ttk.Entry(window, textvariable=self.tile_rack_tr1_y_var, width=12).grid(
            row=1, column=1, sticky="ew", padx=10, pady=4
        )

        ttk.Label(window, text="Tile size").grid(row=2, column=0, sticky="w", padx=10, pady=4)
        ttk.Entry(window, textvariable=self.tile_rack_tile_size_var, width=12).grid(
            row=2, column=1, sticky="ew", padx=10, pady=4
        )

        ttk.Label(window, text="Feed rate").grid(row=3, column=0, sticky="w", padx=10, pady=4)
        ttk.Entry(window, textvariable=self.tile_rack_feed_var, width=12).grid(
            row=3, column=1, sticky="ew", padx=10, pady=4
        )

        buttons = ttk.LabelFrame(window, text="Rack Slots")
        buttons.grid(row=4, column=0, columnspan=2, sticky="ew", padx=10, pady=(8, 4))
        for index in range(7):
            target = f"TR{index + 1}"
            ttk.Button(
                buttons,
                text=target,
                command=lambda target=target: self._move_to_tile_rack_target_from_button(target),
                width=5,
            ).grid(row=index // 4, column=index % 4, sticky="ew", padx=2, pady=2)

        ttk.Label(window, textvariable=self.tile_rack_status_var, wraplength=280).grid(
            row=5, column=0, columnspan=2, sticky="ew", padx=10, pady=(4, 10)
        )
        return

        self._ensure_tile_rack_position_state()
        if not force_open and not getattr(self, "_tile_rack_manual_corners", None):
            self.select_tile_rack_corners(open_position_after=True)
            return

        existing_window = getattr(self, "_tile_rack_position_window", None)
        if existing_window is not None and existing_window.winfo_exists():
            existing_window.lift()
            existing_window.focus_force()
            return

        window = tk.Toplevel(self.root)
        self._tile_rack_position_window = window
        window.title("Tile Rack Position")
        window.resizable(False, False)
        window.columnconfigure(1, weight=1)

        ttk.Label(window, text="Rack X").grid(row=0, column=0, sticky="w", padx=10, pady=(10, 4))
        ttk.Entry(window, textvariable=self.tile_rack_position_x_var, width=12).grid(
            row=0, column=1, sticky="ew", padx=10, pady=(10, 4)
        )

        ttk.Label(window, text="Rack Y").grid(row=1, column=0, sticky="w", padx=10, pady=4)
        ttk.Entry(window, textvariable=self.tile_rack_position_y_var, width=12).grid(
            row=1, column=1, sticky="ew", padx=10, pady=4
        )

        ttk.Label(window, text="Slot gap").grid(row=2, column=0, sticky="w", padx=10, pady=4)
        ttk.Entry(window, textvariable=self.tile_rack_position_gap_var, width=12).grid(
            row=2, column=1, sticky="ew", padx=10, pady=4
        )

        ttk.Label(window, text="Feed rate").grid(row=3, column=0, sticky="w", padx=10, pady=4)
        ttk.Entry(window, textvariable=self.tile_rack_position_feed_var, width=12).grid(
            row=3, column=1, sticky="ew", padx=10, pady=4
        )

        ttk.Button(window, text="Move To Tile Rack", command=self.move_to_tile_rack_position).grid(
            row=4, column=0, columnspan=2, sticky="ew", padx=10, pady=(8, 4)
        )
        tr_frame = ttk.LabelFrame(window, text="Rack Positions")
        tr_frame.grid(row=5, column=0, columnspan=2, sticky="ew", padx=10, pady=4)
        self._build_tile_rack_move_buttons(tr_frame)

        ttk.Button(window, text="Detect Rack Letters", command=self.detect_tile_rack_position_letters).grid(
            row=6, column=0, columnspan=2, sticky="ew", padx=10, pady=4
        )
        ttk.Button(window, text="Select Rack Corners", command=self.select_tile_rack_corners).grid(
            row=7, column=0, columnspan=2, sticky="ew", padx=10, pady=4
        )
        rack_ocr_frame = ttk.LabelFrame(window, text="Tile Rack OCR")
        rack_ocr_frame.grid(row=8, column=0, columnspan=2, sticky="ew", padx=10, pady=4)
        self._build_tile_rack_ocr_grid(rack_ocr_frame)

        ttk.Label(window, textvariable=self.tile_rack_position_status_var, wraplength=300).grid(
            row=9, column=0, columnspan=2, sticky="ew", padx=10, pady=(4, 10)
        )

    def _ensure_tile_rack_position_state(self) -> None:
        if getattr(self, "_tile_rack_position_state_ready", False):
            return

        default_feed = "1500"
        feed_var = getattr(self, "feed_rate_var", None)
        if feed_var is not None:
            try:
                default_feed = str(feed_var.get())
            except Exception:
                default_feed = "1500"

        self.tile_rack_position_x_var = tk.StringVar(value="335")
        self.tile_rack_position_y_var = tk.StringVar(value="30")
        self.tile_rack_position_gap_var = tk.StringVar(value="10")
        self.tile_rack_position_feed_var = tk.StringVar(value=default_feed)
        self.tile_rack_position_status_var = tk.StringVar(value="Set the tile rack X/Y position, then move to it.")
        self.tile_rack_ocr_letter_vars = [tk.StringVar(value="") for _ in range(7)]
        self._tile_rack_manual_corners = getattr(self, "_tile_rack_manual_corners", None)
        self._tile_rack_position_state_ready = True

    def _build_tile_rack_move_buttons(self, parent) -> None:  # type: ignore[no-untyped-def]
        for index in range(7):
            target = f"TR{index + 1}"
            ttk.Button(
                parent,
                text=target,
                command=lambda target=target: self._move_to_tile_rack_label(target),
                width=5,
            ).grid(row=index // 4, column=index % 4, sticky="ew", padx=2, pady=2)

    def _move_to_tile_rack_label(self, target: str) -> None:
        try:
            self._send_move_target(target)
        except Exception as exc:
            self._show_error(exc)

    def _build_tile_rack_ocr_grid(self, parent) -> None:  # type: ignore[no-untyped-def]
        self._ensure_tile_rack_position_state()
        for index, variable in enumerate(self.tile_rack_ocr_letter_vars):
            ttk.Label(parent, text=f"TR{index + 1}").grid(row=index, column=0, sticky="e", padx=(6, 4), pady=1)
            entry = ttk.Entry(parent, textvariable=variable, width=4, justify="center")
            entry.grid(row=index, column=1, sticky="w", padx=(0, 6), pady=1)
            entry.configure(state="readonly")

    def move_to_tile_rack_position(self) -> None:
        self._ensure_tile_rack_position_state()
        try:
            x = float(self.tile_rack_position_x_var.get())
            y = float(self.tile_rack_position_y_var.get())
            feed = float(self.tile_rack_position_feed_var.get())
            command = self._send_absolute_plotter_move(x, y, feed)
            message = f"Moved to tile rack position X{x:g} Y{y:g}."
            self.tile_rack_position_status_var.set(message)
            self._set_status(message)
            self._log(command)
        except Exception as exc:
            self._show_error(exc)

    def detect_tile_rack_position_letters(self) -> None:
        import time

        self._ensure_tile_rack_position_state()
        try:
            rack_x = float(self.tile_rack_position_x_var.get())
            rack_y = float(self.tile_rack_position_y_var.get())
            slot_gap = float(self.tile_rack_position_gap_var.get())
            lines = []
            detected_count = 0
            for variable in self.tile_rack_ocr_letter_vars:
                variable.set("")
            for index in range(7):
                slot_x = rack_x
                slot_y = rack_y + slot_gap * index
                self._set_status(f"Scanning tile rack slot {index + 1} at X{slot_x:g} Y{slot_y:g}...")
                self._send_absolute_plotter_move(slot_x, slot_y, float(self.tile_rack_position_feed_var.get()))
                time.sleep(0.8)
                frame, _quality = self._capture_best_photo_for_ocr(f"tile rack slot {index + 1}")
                scan = scan_camera_letters(frame)
                letter = self._tile_rack_position_single_letter_from_scan(scan)
                self.tile_rack_ocr_letter_vars[index].set("" if letter == "-" else letter)
                if letter != "-":
                    detected_count += 1
                lines.append(f"Slot {index + 1}: {letter}  X{slot_x:g} Y{slot_y:g}")

            result = "\n".join(lines)
            self.tile_rack_position_status_var.set(result)
            self._set_status(f"Detected {detected_count} tile rack letter(s) separately.")
            self._log("Tile rack letters:\n" + result)
            self._refresh_camera_preview()
        except Exception as exc:
            self._show_error(exc)

    def _tile_rack_position_single_letter_from_scan(self, scan: CameraLetterScanResult) -> str:
        letters = self._tile_rack_position_letters_from_scan(scan)
        return letters[0] if letters else "-"

    def _tile_rack_position_letters_from_scan(self, scan: CameraLetterScanResult) -> list[str]:
        letters: list[str] = []
        for captured in sorted(self._tile_rack_scan_items(scan), key=self._tile_rack_position_letter_sort_key):
            text = self._tile_rack_item_text(captured)
            for character in str(text).upper():
                if character.isalpha():
                    letters.append(character)
                    if len(letters) >= 7:
                        return letters
        return letters

    def _tile_rack_scan_items(self, scan) -> list[object]:  # type: ignore[no-untyped-def]
        items: list[object] = []
        for name in ("letters", "tiles", "text_boxes"):
            values = getattr(scan, name, None)
            if values:
                items.extend(list(values))
        return items

    def _tile_rack_item_text(self, item) -> str:  # type: ignore[no-untyped-def]
        for name in ("text", "letter"):
            value = getattr(item, name, None)
            if value:
                return str(value)
        return ""

    def _tile_rack_position_letter_sort_key(self, captured) -> tuple[float, float]:  # type: ignore[no-untyped-def]
        center = self._tile_rack_position_letter_center(captured)
        if center is None:
            return (0.0, 0.0)
        return (center[1], center[0])

    def _tile_rack_position_letter_center(self, captured) -> tuple[float, float] | None:  # type: ignore[no-untyped-def]
        center_x = getattr(captured, "center_x", None)
        center_y = getattr(captured, "center_y", None)
        if center_x is not None and center_y is not None:
            return (float(center_x), float(center_y))

        left = getattr(captured, "left", None)
        top = getattr(captured, "top", None)
        if left is not None and top is not None:
            width = getattr(captured, "width", 0) or 0
            height = getattr(captured, "height", 0) or 0
            return (float(left) + float(width) / 2.0, float(top) + float(height) / 2.0)

        points = getattr(captured, "points", None) or getattr(captured, "corners", None)
        if points:
            xs = [float(point[0]) for point in points]
            ys = [float(point[1]) for point in points]
            return (sum(xs) / len(xs), sum(ys) / len(ys))

        return None

    def select_tile_rack_corners(self, open_position_after: bool = False) -> None:
        self._ensure_tile_rack_position_state()
        try:
            from PIL import Image, ImageTk

            frame = self._current_camera_ocr_frame()
            if frame is None:
                frame, _quality = self._capture_best_photo_for_ocr("tile rack corner selection")
        except Exception as exc:
            self._show_error(exc)
            return

        try:
            cv2 = _require_cv2()
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB) if len(frame.shape) == 3 else frame
            image = Image.fromarray(rgb)
        except Exception as exc:
            self._show_error(exc)
            return

        max_width = 760
        max_height = 520
        scale = min(max_width / image.width, max_height / image.height, 1.0)
        display_size = (max(1, int(image.width * scale)), max(1, int(image.height * scale)))
        display_image = image.resize(display_size)

        window = tk.Toplevel(self.root)
        window.title("Select Tile Rack Corners")
        ttk.Label(window, text="Click the four tile rack corners: top-left, top-right, bottom-right, bottom-left.").grid(
            row=0, column=0, sticky="ew", padx=10, pady=(10, 4)
        )
        canvas = tk.Canvas(window, width=display_size[0], height=display_size[1], cursor="crosshair")
        canvas.grid(row=1, column=0, padx=10, pady=4)
        photo = ImageTk.PhotoImage(display_image)
        canvas.create_image(0, 0, anchor="nw", image=photo)
        canvas.image = photo

        clicked: list[tuple[float, float]] = []

        def redraw_points() -> None:
            canvas.delete("corner")
            for index, point in enumerate(clicked):
                x = point[0] * scale
                y = point[1] * scale
                canvas.create_oval(x - 5, y - 5, x + 5, y + 5, outline="lime", width=2, tags="corner")
                canvas.create_text(x + 12, y - 10, text=str(index + 1), fill="lime", tags="corner")
            if len(clicked) > 1:
                scaled = [(point[0] * scale, point[1] * scale) for point in clicked]
                for start, end in zip(scaled, scaled[1:]):
                    canvas.create_line(start[0], start[1], end[0], end[1], fill="lime", width=2, tags="corner")
                if len(clicked) == 4:
                    canvas.create_line(scaled[-1][0], scaled[-1][1], scaled[0][0], scaled[0][1], fill="lime", width=2, tags="corner")

        def handle_click(event) -> None:  # type: ignore[no-untyped-def]
            if len(clicked) >= 4:
                return
            clicked.append((float(event.x) / scale, float(event.y) / scale))
            redraw_points()
            if len(clicked) == 4:
                self._tile_rack_manual_corners = clicked.copy()
                self._last_tile_rack_camera_rect = None
                self.tile_rack_position_status_var.set("Tile rack corners selected. The green rack grid will use these corners.")
                self._set_status("Tile rack corners selected.")
                self._refresh_camera_preview()
                if open_position_after:
                    window.after(400, lambda: (window.destroy(), self.open_tile_rack_position_window(force_open=True)))
                else:
                    window.after(400, window.destroy)

        def clear_points() -> None:
            clicked.clear()
            self._tile_rack_manual_corners = None
            self._last_tile_rack_camera_rect = None
            canvas.delete("corner")
            self.tile_rack_position_status_var.set("Tile rack corner selection cleared.")
            self._refresh_camera_preview()

        canvas.bind("<Button-1>", handle_click)
        ttk.Button(window, text="Clear Rack Corners", command=clear_points).grid(row=2, column=0, sticky="ew", padx=10, pady=(4, 10))

    def _update_tile_rack_ocr_from_current_frame(self, scan: CameraLetterScanResult) -> None:
        if not getattr(self, "_tile_rack_position_state_ready", False):
            return
        frame = self._current_camera_ocr_frame()
        if frame is None:
            return
        letters = self._tile_rack_letters_from_brown_grid(frame, scan)
        self._set_tile_rack_ocr_letters(letters)

    def _set_tile_rack_ocr_letters(self, letters: list[str]) -> None:
        self._ensure_tile_rack_position_state()
        for index in range(7):
            letter = letters[index] if index < len(letters) and letters[index] else ""
            self.tile_rack_ocr_letter_vars[index].set(letter)

    def _clear_tile_rack_ocr_letters(self) -> None:
        self._ensure_tile_rack_position_state()
        for variable in self.tile_rack_ocr_letter_vars:
            variable.set("")

    def detect_tile_rack_position_letters(self) -> None:
        self._ensure_tile_rack_position_state()
        try:
            if self._captured_photo_frame is None:
                frame, quality = self._capture_best_photo_for_ocr("tile rack OCR grid")
                source = f"best camera frame, sharpness {quality.sharpness:.0f}"
            else:
                frame = self._captured_photo_frame.copy()
                source = "captured picture"

            scan = scan_camera_letters(frame)
            letters = self._tile_rack_letters_from_brown_grid(frame, scan)
            self._clear_tile_rack_ocr_letters()
            self._set_tile_rack_ocr_letters(letters)
            rack_x = float(self.tile_rack_position_x_var.get())
            rack_y = float(self.tile_rack_position_y_var.get())
            slot_gap = float(self.tile_rack_position_gap_var.get())
            lines = []
            detected_count = 0
            for index in range(7):
                letter = letters[index] if index < len(letters) and letters[index] else "-"
                if letter != "-":
                    detected_count += 1
                slot_x = rack_x
                slot_y = rack_y + slot_gap * index
                lines.append(f"TR{index + 1}: {letter}  X{slot_x:g} Y{slot_y:g}")

            result = "\n".join(lines)
            self.tile_rack_position_status_var.set(result)
            self._set_status(f"Detected {detected_count} tile rack letter(s) from the {source}.")
            self._log("Tile rack OCR grid:\n" + result)
            self._refresh_camera_preview()
        except Exception as exc:
            self._show_error(exc)

    def _tile_rack_letters_from_brown_grid(self, frame, scan: CameraLetterScanResult) -> list[str]:  # type: ignore[no-untyped-def]
        manual_corners = getattr(self, "_tile_rack_manual_corners", None)
        if manual_corners and len(manual_corners) == 4:
            return self._tile_rack_letters_from_corner_grid(manual_corners, scan)

        rect = self._detect_tile_rack_brown_rect(frame)
        letters = [""] * 7
        self._last_tile_rack_camera_rect = rect
        if rect is None:
            fallback_letters = self._tile_rack_position_letters_from_scan(scan)
            for index, letter in enumerate(fallback_letters[:7]):
                letters[index] = letter
            return letters

        for captured in self._tile_rack_scan_items(scan):
            center = self._tile_rack_position_letter_center(captured)
            if center is None:
                continue
            slot_index = self._tile_rack_slot_index_from_point(rect, center[0], center[1])
            if slot_index is None or letters[slot_index]:
                continue
            text = self._tile_rack_item_text(captured)
            for character in str(text).upper():
                if character.isalpha():
                    letters[slot_index] = character
                    break
        return letters

    def _tile_rack_letters_from_corner_grid(self, corners, scan: CameraLetterScanResult) -> list[str]:  # type: ignore[no-untyped-def]
        letters = [""] * 7
        for captured in self._tile_rack_scan_items(scan):
            center = self._tile_rack_position_letter_center(captured)
            if center is None:
                continue
            slot_index = self._tile_rack_slot_index_from_corner_grid(corners, center[0], center[1])
            if slot_index is None or letters[slot_index]:
                continue
            text = self._tile_rack_item_text(captured)
            for character in str(text).upper():
                if character.isalpha():
                    letters[slot_index] = character
                    break
        return letters

    def _tile_rack_slot_index_from_corner_grid(self, corners, x: float, y: float) -> int | None:  # type: ignore[no-untyped-def]
        cv2 = _require_cv2()
        import numpy as np

        source = np.array(corners, dtype=np.float32)
        destination = np.array([(0.0, 0.0), (1.0, 0.0), (1.0, 7.0), (0.0, 7.0)], dtype=np.float32)
        transform = cv2.getPerspectiveTransform(source, destination)
        point = np.array([[[float(x), float(y)]]], dtype=np.float32)
        mapped = cv2.perspectiveTransform(point, transform)[0][0]
        grid_x = float(mapped[0])
        grid_y = float(mapped[1])
        if grid_x < -0.05 or grid_x > 1.05 or grid_y < -0.05 or grid_y > 7.05:
            return None
        return max(0, min(6, int(grid_y)))

    def _detect_tile_rack_brown_rect(self, frame):  # type: ignore[no-untyped-def]
        cv2 = _require_cv2()
        import numpy as np

        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV) if len(frame.shape) == 3 else cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        if len(frame.shape) != 3:
            hsv = cv2.cvtColor(hsv, cv2.COLOR_BGR2HSV)

        lower_brown = np.array([5, 35, 25], dtype=np.uint8)
        upper_brown = np.array([35, 255, 210], dtype=np.uint8)
        mask = cv2.inRange(hsv, lower_brown, upper_brown)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None

        frame_area = float(frame.shape[0] * frame.shape[1])
        best_rect = None
        best_score = 0.0
        for contour in contours:
            x, y, width, height = cv2.boundingRect(contour)
            area = float(width * height)
            if area < frame_area * 0.002:
                continue
            if height < width * 1.4:
                continue
            score = area * (height / max(width, 1))
            if score > best_score:
                best_rect = (int(x), int(y), int(width), int(height))
                best_score = score
        return best_rect

    def _tile_rack_slot_index_from_point(self, rect, x: float, y: float) -> int | None:  # type: ignore[no-untyped-def]
        rect_x, rect_y, rect_width, rect_height = rect
        if x < rect_x or x > rect_x + rect_width or y < rect_y or y > rect_y + rect_height:
            return None
        slot_height = rect_height / 7.0
        if slot_height <= 0:
            return None
        return max(0, min(6, int((y - rect_y) / slot_height)))

    def _draw_tile_rack_green_grid_overlay(self, frame):  # type: ignore[no-untyped-def]
        cv2 = _require_cv2()
        manual_corners = getattr(self, "_tile_rack_manual_corners", None)
        if manual_corners and len(manual_corners) == 4:
            return self._draw_tile_rack_corner_grid_overlay(frame, manual_corners)

        rect = getattr(self, "_last_tile_rack_camera_rect", None) or self._detect_tile_rack_brown_rect(frame)
        if rect is None:
            return frame

        x, y, width, height = rect
        green = (0, 255, 0)
        overlay = frame.copy()
        cv2.rectangle(overlay, (x, y), (x + width, y + height), green, 3, cv2.LINE_AA)
        for index in range(1, 7):
            line_y = int(round(y + height * index / 7.0))
            cv2.line(overlay, (x, line_y), (x + width, line_y), green, 2, cv2.LINE_AA)

        for index in range(7):
            center_y = int(round(y + height * (index + 0.5) / 7.0))
            letter = ""
            if getattr(self, "_tile_rack_position_state_ready", False):
                letter = self.tile_rack_ocr_letter_vars[index].get()
            if letter:
                cv2.putText(
                    overlay,
                    letter,
                    (x + max(8, width // 2 - 10), center_y + 12),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.9,
                    green,
                    2,
                    cv2.LINE_AA,
                )
        return overlay

    def _draw_tile_rack_corner_grid_overlay(self, frame, corners):  # type: ignore[no-untyped-def]
        cv2 = _require_cv2()
        import numpy as np

        overlay = frame.copy()
        green = (0, 255, 0)
        source = np.array([(0.0, 0.0), (1.0, 0.0), (1.0, 7.0), (0.0, 7.0)], dtype=np.float32)
        destination = np.array(corners, dtype=np.float32)
        transform = cv2.getPerspectiveTransform(source, destination)

        def project(point):  # type: ignore[no-untyped-def]
            mapped = cv2.perspectiveTransform(np.array([[point]], dtype=np.float32), transform)[0][0]
            return int(round(float(mapped[0]))), int(round(float(mapped[1])))

        outline = [project(point) for point in [(0.0, 0.0), (1.0, 0.0), (1.0, 7.0), (0.0, 7.0)]]
        for index, point in enumerate(outline):
            cv2.line(overlay, point, outline[(index + 1) % 4], green, 3, cv2.LINE_AA)

        for index in range(1, 7):
            left = project((0.0, float(index)))
            right = project((1.0, float(index)))
            cv2.line(overlay, left, right, green, 2, cv2.LINE_AA)

        for index in range(7):
            letter_point = project((0.48, index + 0.5))
            if getattr(self, "_tile_rack_position_state_ready", False):
                letter = self.tile_rack_ocr_letter_vars[index].get()
                if letter:
                    cv2.putText(overlay, letter, letter_point, cv2.FONT_HERSHEY_SIMPLEX, 0.9, green, 2, cv2.LINE_AA)
        return overlay

    def _send_tile_rack_position_command(self, command: str) -> None:
        self._send_plotter_raw_command(command)
        return

        sender = getattr(self, "_send_tile_rack_command", None)
        if callable(sender):
            sender(command, prefer_arduino=False)
            return

        for method_name in (
            "_send_raw_command",
            "_send_manual_command",
            "_send_command",
            "_send_gcode_command",
            "send_raw_command",
            "send_manual_command",
            "send_command",
            "send_gcode_command",
        ):
            method = getattr(self, method_name, None)
            if not callable(method):
                continue
            try:
                method(command)
                return
            except TypeError:
                continue

        raise RuntimeError("Could not find the plotter command sender for the tile rack position.")

    def open_tile_rack_window(self) -> None:
        raise_user_error("The tile rack feature has been removed.")
        return

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

        ttk.Label(motion_frame, text="Left rack X").grid(row=0, column=0, sticky="w", padx=6, pady=(8, 2))
        ttk.Entry(motion_frame, textvariable=self.tile_rack_x_var, width=8).grid(row=0, column=1, padx=6, pady=(8, 2))
        ttk.Label(motion_frame, text="Top Y").grid(row=0, column=2, sticky="w", padx=6, pady=(8, 2))
        ttk.Entry(motion_frame, textvariable=self.tile_rack_y_var, width=8).grid(row=0, column=3, padx=6, pady=(8, 2))
        ttk.Label(motion_frame, text="Vertical gap").grid(row=0, column=4, sticky="w", padx=6, pady=(8, 2))
        ttk.Entry(motion_frame, textvariable=self.tile_rack_spacing_var, width=8).grid(row=0, column=5, padx=6, pady=(8, 2))

        ttk.Label(motion_frame, text="MG995").grid(row=1, column=0, sticky="w", padx=6, pady=2)
        ttk.Label(motion_frame, text="Automatic lift/lower").grid(row=1, column=1, columnspan=3, sticky="w", padx=6, pady=2)
        ttk.Label(motion_frame, text="Servo wait").grid(row=1, column=4, sticky="w", padx=6, pady=2)
        ttk.Label(motion_frame, text=f"{DEFAULT_MG995_SERVO_WAIT_SECONDS:g}s").grid(row=1, column=5, sticky="w", padx=6, pady=2)

        ttk.Label(motion_frame, text="XY feed").grid(row=2, column=0, sticky="w", padx=6, pady=2)
        ttk.Entry(motion_frame, textvariable=self.tile_rack_xy_feed_var, width=8).grid(row=2, column=1, padx=6, pady=2)
        ttk.Label(motion_frame, text="Arduino").grid(row=2, column=2, sticky="w", padx=6, pady=2)
        ttk.Label(motion_frame, text="Uses selected controller port").grid(row=2, column=3, sticky="w", padx=6, pady=2)
        ttk.Button(motion_frame, text="Go To Rack", command=self.move_to_tile_rack_start).grid(
            row=2, column=4, columnspan=2, sticky="ew", padx=6, pady=2
        )

        ttk.Label(motion_frame, text="Move wait").grid(row=3, column=0, sticky="w", padx=6, pady=2)
        ttk.Label(motion_frame, text=f"{DEFAULT_TILE_RACK_MOVE_WAIT_SECONDS:g}s").grid(row=3, column=1, sticky="w", padx=6, pady=2)

        ttk.Label(motion_frame, text="Magnet").grid(row=4, column=0, sticky="w", padx=6, pady=(2, 8))
        ttk.Label(motion_frame, text="Automatic on/off").grid(row=4, column=1, columnspan=3, sticky="w", padx=6, pady=(2, 8))

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
        self.tile_rack_start_square_var = tk.StringVar(value="G6")
        self.tile_rack_x_var = tk.StringVar(value="0")
        self.tile_rack_y_var = tk.StringVar(value="40")
        self.tile_rack_spacing_var = tk.StringVar(value="25")
        self.tile_rack_xy_feed_var = tk.StringVar(value=default_feed)
        self.tile_rack_servo_up_var = tk.StringVar(value="SERVO_UP")
        self.tile_rack_servo_down_var = tk.StringVar(value="SERVO_DOWN")
        self.tile_rack_servo_wait_var = tk.StringVar(value="0.4")
        self.tile_rack_move_wait_var = tk.StringVar(value="1.0")
        self.tile_rack_arduino_port_var = tk.StringVar(value="")
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
        frame_shape = getattr(frame, "shape", None)
        frame_width = int(frame_shape[1]) if frame_shape is not None and len(frame_shape) >= 2 else 0
        self.root.after(0, lambda scan=scan, frame_width=frame_width: self._handle_tile_rack_scan_result(scan, frame_width))

    def _handle_tile_rack_scan_result(self, scan: CameraLetterScanResult, frame_width: int = 0) -> None:
        rack_letters = self._rack_letters_from_camera_scan(scan, frame_width)
        self.tile_rack_letters_var.set(rack_letters)
        if rack_letters:
            message = f"Left-side vertical tile rack captured {len(rack_letters)} letter(s): {rack_letters}"
        else:
            message = "No left-side rack letters were detected. Type the rack letters or capture a clearer rack image."
        self._set_tile_rack_status(message)
        self._refresh_camera_preview()

    def _rack_letters_from_camera_scan(self, scan: CameraLetterScanResult, frame_width: int = 0) -> str:
        rack_letters = self._filter_left_side_rack_letters(scan, frame_width)
        detected = []
        for captured in sorted(rack_letters, key=self._camera_letter_vertical_sort_key):
            text = getattr(captured, "text", "")
            detected.extend(character for character in str(text).upper() if character.isalpha())
        return normalize_rack_letters("".join(detected))

    def _filter_left_side_rack_letters(self, scan: CameraLetterScanResult, frame_width: int) -> list[object]:
        letters = list(scan.letters)
        if not letters:
            return []

        board_left = None
        if scan.grid is not None and getattr(scan.grid, "corners", None):
            board_left = min(float(point[0]) for point in scan.grid.corners)
        else:
            calibration = getattr(self, "_calibration", None)
            image_corners = getattr(calibration, "image_corners", None)
            if image_corners and len(image_corners) == 4:
                board_left = min(float(point[0]) for point in image_corners)

        filtered = []
        for captured in letters:
            center = self._camera_letter_center(captured)
            if center is None:
                continue
            x, _ = center
            if board_left is not None:
                if x < board_left:
                    filtered.append(captured)
            elif frame_width > 0 and x <= frame_width * 0.40:
                filtered.append(captured)

        if filtered:
            return filtered[:7]
        if board_left is not None or frame_width > 0:
            return []
        return letters[:7]

    def _camera_letter_center(self, captured) -> tuple[float, float] | None:  # type: ignore[no-untyped-def]
        center_x = getattr(captured, "center_x", None)
        center_y = getattr(captured, "center_y", None)
        if center_x is not None and center_y is not None:
            return (float(center_x), float(center_y))
        left = getattr(captured, "left", None)
        top = getattr(captured, "top", None)
        if left is not None and top is not None:
            width = getattr(captured, "width", 0) or 0
            height = getattr(captured, "height", 0) or 0
            return (float(left) + float(width) / 2.0, float(top) + float(height) / 2.0)
        points = getattr(captured, "points", None) or getattr(captured, "corners", None)
        if points:
            xs = [float(point[0]) for point in points]
            ys = [float(point[1]) for point in points]
            return (sum(xs) / len(xs), sum(ys) / len(ys))
        return None

    def _camera_letter_vertical_sort_key(self, captured) -> tuple[float, float]:  # type: ignore[no-untyped-def]
        center = self._camera_letter_center(captured)
        if center is None:
            return (0.0, 0.0)
        return (center[1], center[0])

    def suggest_tile_rack_word(self) -> None:
        self._ensure_tile_rack_state()
        rack_letters = normalize_rack_letters(self.tile_rack_letters_var.get())
        word = self._best_tile_rack_word(rack_letters)
        if not word:
            self._set_tile_rack_status(
                "No playable word was found in the loaded word list. Enter a word manually to check it."
            )
            return
        self.tile_rack_word_var.set(word)
        self._set_tile_rack_status(f"Suggested word: {word}")

    def _best_tile_rack_word(self, rack_letters: str) -> str:
        candidates = self._tile_rack_word_candidates()
        playable = [
            normalize_word(candidate)
            for candidate in candidates
            if can_build_word_from_rack(str(candidate), rack_letters)
        ]
        playable = sorted({word for word in playable if word}, key=lambda word: (-len(word), word))
        return playable[0] if playable else ""

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
                squares = horizontal_word_squares(self.tile_rack_start_square_var.get(), word, direction="left")
            except Exception as exc:
                placement_error = exc
        button = getattr(self, "tile_rack_make_button", None)
        if button is not None:
            button.configure(state="normal")
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
            if not rack_letters:
                rack_letters = self._capture_tile_rack_letters_for_placement()
            word = normalize_word(self.tile_rack_word_var.get())
            if not word:
                word = self._best_tile_rack_word(rack_letters)
                if word:
                    self.tile_rack_word_var.set(word)
            if not can_build_word_from_rack(word, rack_letters):
                raise ValueError(f"{word or 'That word'} cannot be made from rack letters {rack_letters}.")
            slot_indices = rack_slot_indices_for_word(word, rack_letters)
            target_squares = horizontal_word_squares(self.tile_rack_start_square_var.get(), word, direction="left")
            settings = self._tile_rack_motion_settings()
            self._place_tile_rack_word(word, slot_indices, target_squares, settings)
        except Exception as exc:
            self._show_error(exc)

    def _capture_tile_rack_letters_for_placement(self) -> str:
        if self._captured_photo_frame is None:
            frame, _quality = self._capture_best_photo_for_ocr("tile rack placement")
        else:
            frame = self._captured_photo_frame.copy()

        scan = scan_camera_letters(frame)
        frame_shape = getattr(frame, "shape", None)
        frame_width = int(frame_shape[1]) if frame_shape is not None and len(frame_shape) >= 2 else 0
        rack_letters = self._rack_letters_from_camera_scan(scan, frame_width)
        self.tile_rack_letters_var.set(rack_letters)
        if not rack_letters:
            raise ValueError("No left-side rack letters were detected for placement.")
        self._set_tile_rack_status(f"Detected rack letters for placement: {rack_letters}")
        return rack_letters

    def move_to_tile_rack_start(self) -> None:
        self._ensure_tile_rack_state()
        try:
            settings = self._tile_rack_motion_settings()
            self._send_tile_rack_servo_up(settings)
            self._send_tile_rack_xy_move(
                float(settings["rack_x"]),
                float(settings["rack_y"]),
                float(settings["xy_feed"]),
            )
            self._set_tile_rack_status("Plotter moved to tile rack slot 1 with the magnet lifted.")
        except Exception as exc:
            self._show_error(exc)

    def _tile_rack_motion_settings(self) -> dict[str, float | str]:
        return {
            "rack_x": float(self.tile_rack_x_var.get()),
            "rack_y": float(self.tile_rack_y_var.get()),
            "slot_spacing": float(self.tile_rack_spacing_var.get()),
            "xy_feed": float(self.tile_rack_xy_feed_var.get()),
            "servo_up_commands": MG995_SERVO_UP_COMMANDS,
            "servo_down_commands": MG995_SERVO_DOWN_COMMANDS,
            "servo_wait": DEFAULT_MG995_SERVO_WAIT_SECONDS,
            "move_wait": DEFAULT_TILE_RACK_MOVE_WAIT_SECONDS,
            "arduino_port": "",
            "magnet_on_commands": MAGNET_ON_COMMANDS,
            "magnet_off_commands": MAGNET_OFF_COMMANDS,
        }

    def _place_tile_rack_word(
        self,
        word: str,
        slot_indices: list[int],
        target_squares: list[str],
        settings: dict[str, float | str],
    ) -> None:
        with self._tile_rack_serial_session():
            self._set_tile_rack_status(f"Placing {word} from the tile rack...")
            self._log(f"Tile rack placement started for {word}.")
            self._send_tile_rack_servo_up(settings)
            self._send_tile_rack_magnet_off(settings)

            for letter, slot_index, target_square in zip(word, slot_indices, target_squares):
                rack_x, rack_y = vertical_rack_slot_position(
                    float(settings["rack_x"]),
                    float(settings["rack_y"]),
                    slot_index,
                    float(settings["slot_spacing"]),
                )

                self._log(f"Picking {letter} from rack slot {slot_index + 1}.")
                self._send_tile_rack_servo_up(settings)
                self._send_tile_rack_xy_move(rack_x, rack_y, float(settings["xy_feed"]))
                self._wait_after_tile_rack_move(settings)
                self._send_tile_rack_servo_down(settings)
                self._send_tile_rack_magnet_on(settings)
                self._send_tile_rack_delay(settings)
                self._send_tile_rack_servo_up(settings)

                self._log(f"Placing {letter} on {target_square}.")
                self._send_tile_rack_square_move(target_square, settings)
                self._wait_after_tile_rack_move(settings)
                self._send_tile_rack_servo_down(settings)
                self._send_tile_rack_magnet_off(settings)
                self._send_tile_rack_delay(settings)
                self._send_tile_rack_servo_up(settings)

            self._set_tile_rack_status(f"Placed {word} right-to-left from {target_squares[0]}.")
            self._log(f"Tile rack placement complete for {word}: {' '.join(target_squares)}.")

    def _send_tile_rack_xy_move(self, x: float, y: float, feed: float) -> None:
        self._send_tile_rack_command(f"G0 X{x:g} Y{y:g} F{feed:g}", prefer_arduino=False)

    def _send_tile_rack_square_move(self, square: str, settings: dict[str, float | str]) -> None:
        command = self._tile_rack_square_move_command(square, float(settings["xy_feed"]))
        if command:
            self._send_tile_rack_command(command, prefer_arduino=False)
            return
        self._send_square_move(square)

    def _tile_rack_square_move_command(self, square: str, feed: float) -> str:
        calibration = None
        for method_name in ("_calibration_from_form", "_calibration"):
            source = getattr(self, method_name, None)
            try:
                calibration = source() if callable(source) else source
            except Exception:
                calibration = None
            if calibration is not None:
                break

        if calibration is not None:
            for args in (
                (calibration, square, feed),
                (square, calibration, feed),
                (calibration, square),
                (square, calibration),
            ):
                try:
                    command = format_move_command(*args)
                    if command:
                        return str(command)
                except TypeError:
                    continue
                except Exception:
                    continue

            for method_name in (
                "square_center",
                "center_for_square",
                "coordinates_for_square",
                "square_to_xy",
                "xy_for_square",
                "square_center_mm",
                "plotter_coordinates_for_square",
                "square_to_plotter_xy",
                "board_square_to_plotter_position",
            ):
                method = getattr(calibration, method_name, None)
                if not callable(method):
                    continue
                try:
                    x, y = method(square)
                except Exception:
                    continue
                for args in ((x, y, feed), (x, y), (float(x), float(y), feed)):
                    try:
                        command = format_move_command(*args)
                        if command:
                            return str(command)
                    except TypeError:
                        continue
                    except Exception:
                        continue
                return f"G0 X{float(x):g} Y{float(y):g} F{feed:g}"
        return ""

    def _send_tile_rack_z_move(self, z: float, feed: float) -> None:
        self._send_tile_rack_command(f"G0 Z{z:g} F{feed:g}", prefer_arduino=False)

    def _send_tile_rack_servo_up(self, settings: dict[str, float | str]) -> None:
        self._send_tile_rack_aux_command_sequence(settings["servo_up_commands"])
        self._send_tile_rack_delay(settings)

    def _send_tile_rack_servo_down(self, settings: dict[str, float | str]) -> None:
        self._send_tile_rack_aux_command_sequence(settings["servo_down_commands"])
        self._send_tile_rack_delay(settings)

    def _send_tile_rack_magnet_on(self, settings: dict[str, float | str]) -> None:
        self._send_tile_rack_aux_command_sequence(settings["magnet_on_commands"])

    def _send_tile_rack_magnet_off(self, settings: dict[str, float | str]) -> None:
        self._send_tile_rack_aux_command_sequence(settings["magnet_off_commands"])

    def _send_tile_rack_delay(self, settings: dict[str, float | str]) -> None:
        import time

        wait_seconds = max(0.0, float(settings["servo_wait"]))
        if wait_seconds > 0:
            time.sleep(wait_seconds)

    def _wait_after_tile_rack_move(self, settings: dict[str, float | str]) -> None:
        import time

        wait_seconds = max(0.0, float(settings["move_wait"]))
        if wait_seconds > 0:
            time.sleep(wait_seconds)

    def _send_tile_rack_aux_command(self, command: str) -> None:
        self._send_tile_rack_command(command, prefer_arduino=False)

    def _send_tile_rack_aux_command_sequence(self, commands) -> None:  # type: ignore[no-untyped-def]
        last_error: Exception | None = None
        sent_any = False
        for command in commands:
            try:
                self._send_tile_rack_aux_command(str(command))
                sent_any = True
            except Exception as exc:
                last_error = exc
        if not sent_any and last_error is not None:
            raise last_error

    def _tile_rack_serial_session(self):  # type: ignore[no-untyped-def]
        import contextlib

        return contextlib.nullcontext()

        if getattr(self, "_tile_rack_active_sender", None) is not None:
            return contextlib.nullcontext()

        try:
            config = self._tile_rack_serial_config(prefer_arduino=False)
        except Exception:
            return contextlib.nullcontext()
        if not self._serial_config_has_port(config, prefer_arduino=False):
            return contextlib.nullcontext()

        try:
            sender = GCodeSender(config)
        except TypeError:
            return contextlib.nullcontext()

        @contextlib.contextmanager
        def session():
            active_sender = sender
            aux_sender = None
            active_aux_sender = None
            entered = False
            aux_entered = False
            try:
                if hasattr(sender, "__enter__") and hasattr(sender, "__exit__"):
                    active_sender = sender.__enter__()
                    entered = True
                self._tile_rack_active_sender = active_sender
                plotter_port = self._serial_config_port(config)
                aux_port = self._selected_tile_rack_serial_port(prefer_arduino=True)
                if aux_port and aux_port != plotter_port:
                    aux_config = self._tile_rack_serial_config(prefer_arduino=True)
                    aux_sender = GCodeSender(aux_config)
                    active_aux_sender = aux_sender
                    if hasattr(aux_sender, "__enter__") and hasattr(aux_sender, "__exit__"):
                        active_aux_sender = aux_sender.__enter__()
                        aux_entered = True
                    self._tile_rack_active_aux_sender = active_aux_sender
                else:
                    self._tile_rack_active_aux_sender = active_sender
                yield
            finally:
                self._tile_rack_active_sender = None
                self._tile_rack_active_aux_sender = None
                if aux_sender is not None:
                    if aux_entered:
                        aux_sender.__exit__(None, None, None)
                    else:
                        close = getattr(aux_sender, "close", None)
                        if callable(close):
                            close()
                if entered:
                    sender.__exit__(None, None, None)
                else:
                    close = getattr(sender, "close", None)
                    if callable(close):
                        close()

        return session()

    def _send_tile_rack_command(self, command: str, prefer_arduino: bool = False) -> None:
        command = command.strip()
        if not command:
            return

        active_sender = (
            getattr(self, "_tile_rack_active_aux_sender", None)
            if prefer_arduino
            else getattr(self, "_tile_rack_active_sender", None)
        )
        if active_sender is not None:
            if self._send_tile_rack_command_with_gcode_sender(command, prefer_arduino=prefer_arduino):
                return
            raise RuntimeError(f"Could not send tile rack command: {command}")

        if prefer_arduino and self._tile_rack_arduino_port():
            if self._send_tile_rack_command_with_gcode_sender(command, prefer_arduino=True):
                return
            raise RuntimeError("Could not send the tile rack MG995/magnet command to the Arduino port.")

        for method_name in (
            "_send_commands",
            "_send_serial_commands",
            "_send_controller_commands",
            "_send_gcode_commands",
            "send_commands",
            "send_serial_commands",
            "send_controller_commands",
            "send_gcode_commands",
            "_send_raw_command",
            "_send_manual_command",
            "_send_command",
            "_send_gcode_command",
            "send_raw_command",
            "send_manual_command",
            "send_command",
            "send_gcode_command",
        ):
            method = getattr(self, method_name, None)
            if callable(method):
                if self._try_tile_rack_sender_method(method, command, prefer_list="commands" in method_name):
                    return

        sender = (
            getattr(self, "_sender", None)
            or getattr(self, "sender", None)
            or getattr(self, "_gcode_sender", None)
            or getattr(self, "gcode_sender", None)
        )
        if sender is not None:
            for method_name in ("send_commands", "send_command", "send", "write"):
                method = getattr(sender, method_name, None)
                if callable(method):
                    if self._try_tile_rack_sender_method(method, command, prefer_list="commands" in method_name):
                        return

        if self._send_tile_rack_command_with_gcode_sender(command, prefer_arduino=prefer_arduino):
            return

        if not self._selected_tile_rack_serial_port(prefer_arduino=prefer_arduino):
            raise RuntimeError("Select the plotter COM port before using the tile rack movement.")

        raise RuntimeError(
            "No raw G-code sender was found for rack Z or magnet commands. "
            "Add a manual command sender or connect this method to the controller command path."
        )

    def _try_tile_rack_sender_method(self, method, command: str, prefer_list: bool = False) -> bool:  # type: ignore[no-untyped-def]
        list_payloads = (
            ([command],),
            ([command], "tile rack"),
            ([command], "Tile rack command"),
        )
        string_payloads = (
            (command,),
            (command, "tile rack"),
            (command, "Tile rack command"),
        )
        payloads = list_payloads + string_payloads if prefer_list else string_payloads + list_payloads
        for payload in payloads:
            try:
                method(*payload)
                return True
            except TypeError:
                continue
        return False

    def _send_tile_rack_command_with_gcode_sender(self, command: str, prefer_arduino: bool = False) -> bool:
        active_sender = (
            getattr(self, "_tile_rack_active_aux_sender", None)
            if prefer_arduino
            else getattr(self, "_tile_rack_active_sender", None)
        )
        if active_sender is not None:
            return self._send_tile_rack_command_to_sender(active_sender, command)

        try:
            config = self._tile_rack_serial_config(prefer_arduino=prefer_arduino)
        except Exception:
            return False
        if not self._serial_config_has_port(config, prefer_arduino=prefer_arduino):
            return False

        try:
            sender = GCodeSender(config)
        except TypeError:
            return False

        context_sender = sender
        if hasattr(sender, "__enter__") and hasattr(sender, "__exit__"):
            with sender as active_sender:
                context_sender = active_sender
                return self._send_tile_rack_command_to_sender(context_sender, command)
        return self._send_tile_rack_command_to_sender(context_sender, command)

    def _send_tile_rack_command_to_sender(self, sender, command: str) -> bool:  # type: ignore[no-untyped-def]
        for method_name in ("send_commands", "send_command", "send", "write"):
            method = getattr(sender, method_name, None)
            if callable(method) and self._try_tile_rack_sender_method(method, command, prefer_list="commands" in method_name):
                return True
        return False

    def _tile_rack_serial_config(self, prefer_arduino: bool = False) -> SerialConfig:
        import dataclasses
        import inspect

        for method_name in (
            "_serial_config_from_form",
            "_serial_config_from_inputs",
            "_serial_config",
            "_get_serial_config",
            "serial_config_from_form",
            "get_serial_config",
        ):
            method = getattr(self, method_name, None)
            if not callable(method):
                continue
            try:
                config = method()
            except Exception:
                continue
            if isinstance(config, SerialConfig):
                return self._tile_rack_config_with_selected_port(config, prefer_arduino=prefer_arduino)

        names = []
        if dataclasses.is_dataclass(SerialConfig):
            names = [field.name for field in dataclasses.fields(SerialConfig)]
        else:
            signature = inspect.signature(SerialConfig)
            names = [name for name in signature.parameters if name != "self"]

        values = {}
        for name in names:
            value = self._tile_rack_serial_config_value(name, prefer_arduino=prefer_arduino)
            if value is not None:
                values[name] = value
        return self._tile_rack_config_with_selected_port(SerialConfig(**values), prefer_arduino=prefer_arduino)

    def _tile_rack_config_with_selected_port(self, config: SerialConfig, prefer_arduino: bool = False) -> SerialConfig:
        import dataclasses

        selected_port = self._selected_tile_rack_serial_port(prefer_arduino=prefer_arduino)
        if not selected_port:
            return config

        for name in ("port", "serial_port", "com_port"):
            if not hasattr(config, name):
                continue
            current = getattr(config, name)
            if current is not None and str(current).strip() and not prefer_arduino:
                return config
            if dataclasses.is_dataclass(config):
                return dataclasses.replace(config, **{name: selected_port})
            try:
                setattr(config, name, selected_port)
            except Exception:
                return config
            return config
        return config

    def _serial_config_has_port(self, config: SerialConfig, prefer_arduino: bool = False) -> bool:
        for name in ("port", "serial_port", "com_port"):
            if hasattr(config, name):
                value = getattr(config, name)
                if value is not None and str(value).strip():
                    return True
        return bool(self._selected_tile_rack_serial_port(prefer_arduino=prefer_arduino))

    def _serial_config_port(self, config: SerialConfig) -> str | None:
        for name in ("port", "serial_port", "com_port"):
            if hasattr(config, name):
                value = getattr(config, name)
                if value is not None and str(value).strip():
                    return self._normalize_serial_port_name(value)
        return None

    def _tile_rack_serial_config_value(self, name: str, prefer_arduino: bool = False):  # type: ignore[no-untyped-def]
        key = name.lower()
        if key in {"port", "serial_port", "com_port"}:
            return self._selected_tile_rack_serial_port(prefer_arduino=prefer_arduino) or self._read_tile_rack_gui_value(
                "port_var",
                "serial_port_var",
                "com_port_var",
                "plotter_port_var",
                "selected_port_var",
            )
        if key in {"baud", "baudrate", "baud_rate"}:
            return int(
                self._read_tile_rack_gui_value(
                    "baud_var",
                    "baudrate_var",
                    "baud_rate_var",
                    default="115200",
                )
            )
        if key == "timeout" or "timeout" in key:
            return float(self._read_tile_rack_gui_value("timeout_var", "serial_timeout_var", default="2.0"))
        if key in {"dry_run", "dryrun"}:
            value = self._read_tile_rack_gui_value("dry_run_var", "dryrun_var", default=False)
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in {"1", "true", "yes", "on"}
        if "settle" in key or "delay" in key:
            return float(self._read_tile_rack_gui_value("settle_seconds_var", "startup_delay_var", default="2.0"))
        return None

    def _tile_rack_arduino_port(self) -> str | None:
        source = getattr(self, "tile_rack_arduino_port_var", None)
        if source is None:
            source_value = None
        else:
            try:
                source_value = source.get()
            except Exception:
                source_value = None
        if self._looks_like_serial_port(source_value):
            return self._normalize_serial_port_name(source_value)
        return None

    def _detect_tile_rack_mg995_port(self) -> str | None:
        import time

        try:
            import serial
        except Exception:
            return None

        try:
            ports = list_serial_ports()
        except Exception:
            return None

        port_names: list[str] = []
        selected_plotter_port = self._selected_tile_rack_serial_port(prefer_arduino=False)
        if selected_plotter_port:
            port_names.append(selected_plotter_port)

        for port in ports:
            name = getattr(port, "device", None) or getattr(port, "name", None) or str(port)
            normalized_name = self._normalize_serial_port_name(name)
            if self._looks_like_serial_port(normalized_name) and normalized_name not in port_names:
                port_names.append(normalized_name)

        for port_name in port_names:
            try:
                with serial.Serial(port_name, 115200, timeout=0.35, write_timeout=0.35) as connection:
                    time.sleep(1.8)
                    connection.reset_input_buffer()
                    connection.write(b"MG995_PING\n")
                    connection.flush()
                    deadline = time.monotonic() + 1.2
                    response = ""
                    while time.monotonic() < deadline:
                        line = connection.readline().decode(errors="ignore")
                        response += line
                        if "SCRABBLE_MG995_A1_READY" in response or "MG995" in response:
                            return port_name
            except Exception:
                continue
        return None

    def _selected_tile_rack_serial_port(self, prefer_arduino: bool = False) -> str | None:
        if prefer_arduino:
            arduino_port = self._tile_rack_arduino_port()
            if arduino_port:
                return arduino_port

        port = self._read_tile_rack_gui_value(
            "port_var",
            "serial_port_var",
            "com_port_var",
            "plotter_port_var",
            "selected_port_var",
            "port_combo",
            "serial_port_combo",
            "com_port_combo",
        )
        if self._looks_like_serial_port(port):
            return self._normalize_serial_port_name(port)

        for name, source in vars(self).items():
            lowered = name.lower()
            if not prefer_arduino and "arduino" in lowered:
                continue
            if "port" not in lowered and "com" not in lowered and "serial" not in lowered:
                continue
            if not hasattr(source, "get"):
                continue
            try:
                value = source.get()
            except Exception:
                continue
            if self._looks_like_serial_port(value):
                return self._normalize_serial_port_name(value)
        return None

    def _looks_like_serial_port(self, value) -> bool:  # type: ignore[no-untyped-def]
        if value is None:
            return False
        text = str(value).strip()
        if not text:
            return False
        lowered = text.lower()
        return lowered.startswith("com") or lowered.startswith("/dev/") or lowered.startswith("usb")

    def _normalize_serial_port_name(self, value) -> str:  # type: ignore[no-untyped-def]
        text = str(value).strip()
        if text.upper().startswith("COM"):
            token = text.split()[0].rstrip(":,;")
            return token
        return text

    def _read_tile_rack_gui_value(self, *names: str, default=None):  # type: ignore[no-untyped-def]
        for name in names:
            source = getattr(self, name, None)
            if source is None:
                continue
            try:
                value = source.get() if hasattr(source, "get") else source
            except Exception:
                continue
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            return value
        return default

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
        if getattr(self, "_force_z_up_before_pick_drop_move", False):
            self._send_pick_drop_aux_command("ZU")

        target = str(square_label).strip().upper()
        if self._is_tile_rack_target(target):
            self._send_tile_rack_target_move(target)
            return

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
