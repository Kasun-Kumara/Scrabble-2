from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .board import BOARD_SIZE, CELL_SIZE_MM, parse_square_label
from .calibration import PlotterCalibration
from .image_calibration import collect_board_corners_from_frame
from .overlay import draw_board_overlay
from .serial_sender import (
    GCodeSender,
    SerialConfig,
    format_move_command,
    list_serial_ports,
)


class ScrabblePlotterApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Scrabble Plotter Sender")
        self.root.geometry("1180x780")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.calibration_path = tk.StringVar(value=str(Path("scrabble_plotter_calibration.json").resolve()))
        self._calibration = PlotterCalibration.load(self.calibration_path.get())

        self.camera_index_var = tk.StringVar(value=str(self._calibration.camera_index))
        self.offset_x_var = tk.StringVar(value=str(self._calibration.offset_x_mm))
        self.offset_y_var = tk.StringVar(value=str(self._calibration.offset_y_mm))
        self.cell_size_var = tk.StringVar(value=str(self._calibration.cell_size_mm))
        self.x_steps_per_mm_var = tk.StringVar(value=str(self._calibration.x_steps_per_mm))
        self.y_steps_per_mm_var = tk.StringVar(value=str(self._calibration.y_steps_per_mm))
        self.cart_x_var = tk.StringVar(value=str(self._calibration.cart_x_mm))
        self.cart_y_var = tk.StringVar(value=str(self._calibration.cart_y_mm))
        self.square_var = tk.StringVar(value="H8")
        self.port_var = tk.StringVar()
        self.baud_var = tk.StringVar(value="115200")
        self.feed_rate_var = tk.StringVar(value="1500")
        self.command_var = tk.StringVar(value="G0")
        self.timeout_var = tk.StringVar(value="2.0")
        self.startup_g90_var = tk.BooleanVar(value=True)

        self.status_var = tk.StringVar(
            value="Start the camera, calibrate the board corners, then enter a square and COM port."
        )
        self.preview_image = None
        self._camera = None
        self._camera_after_id: str | None = None
        self._latest_frame = None
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
        preview_frame.rowconfigure(1, weight=1)

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

        self.preview_label = ttk.Label(preview_frame, text="Camera stopped", anchor="center")
        self.preview_label.grid(row=1, column=0, columnspan=5, sticky="nsew", pady=(8, 0))

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

        ttk.Checkbutton(move_box, text="Send G90 before move", variable=self.startup_g90_var).grid(
            row=6, column=0, columnspan=3, sticky="w", pady=(8, 0)
        )
        ttk.Button(move_box, text="Preview G-code", command=self.preview_move).grid(
            row=7, column=0, columnspan=3, sticky="ew", pady=(10, 6)
        )
        ttk.Button(move_box, text="Send Move", command=self.send_move).grid(
            row=8, column=0, columnspan=3, sticky="ew"
        )
        ttk.Button(move_box, text="Reset To Start", command=self.reset_to_start).grid(
            row=9, column=0, columnspan=3, sticky="ew", pady=(8, 0)
        )
        ttk.Button(move_box, text="Go To Cart", command=self.go_to_cart).grid(
            row=10, column=0, columnspan=3, sticky="ew", pady=(8, 0)
        )

        log_box = ttk.LabelFrame(controls, text="Status", padding=10)
        log_box.grid(row=2, column=0, sticky="nsew", pady=(12, 0))
        log_box.columnconfigure(0, weight=1)
        log_box.rowconfigure(1, weight=1)
        controls.rowconfigure(2, weight=1)

        ttk.Label(log_box, textvariable=self.status_var, wraplength=380).grid(row=0, column=0, sticky="ew")
        self.log_text = tk.Text(log_box, height=12, wrap="word")
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
        if len(self._calibration.image_corners) == 4:
            self._set_status("Loaded board calibration. Start the camera to see the overlay.")
        else:
            self._set_status("Start the camera and calibrate the board corners.")

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
            camera = cv2.VideoCapture(camera_index, cv2.CAP_DSHOW)
            if not camera.isOpened():
                camera.release()
                camera = cv2.VideoCapture(camera_index)
            if not camera.isOpened():
                raise RuntimeError(f"Unable to open camera {camera_index}.")

            self._camera = camera
            self._calibration = self._calibration_from_form()
            self._calibration.camera_index = camera_index
            self._calibration.save(self.calibration_path.get())
            self._set_status(f"Camera {camera_index} started.")
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
        if clear_preview:
            self.preview_label.configure(image="", text="Camera stopped")
            self.preview_image = None

    def calibrate_board_from_camera(self) -> None:
        if self._latest_frame is None:
            raise_user_error("Start the camera first.")
            return

        try:
            corners = collect_board_corners_from_frame(self._latest_frame.copy())
            self._calibration = self._calibration_from_form()
            self._calibration.set_camera_corners(corners)
            self._calibration.save(self.calibration_path.get())
            self._set_status("Saved board corners. Overlay is now active in the live preview.")
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
            calibration = self._calibration_from_form()
            calibration.validate_ready_for_move()
            square = parse_square_label(self.square_var.get())
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
        except Exception as exc:
            self._show_error(exc)

    def reset_to_start(self) -> None:
        try:
            sender = self._get_sender()
            command, responses = sender.send_reset()
            self._set_status(f"Sent reset command to {sender.config.port}")
            self._log(f"Sent: {command}")
            if responses:
                self._log("Responses: " + " | ".join(responses))
        except Exception as exc:
            self._show_error(exc)

    def go_to_cart(self) -> None:
        try:
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
        except Exception as exc:
            self._show_error(exc)

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

        ok, frame = self._camera.read()
        if ok:
            self._latest_frame = frame
            self._show_frame(frame)
        self._schedule_camera_update()

    def _show_frame(self, frame) -> None:  # type: ignore[no-untyped-def]
        from PIL import Image, ImageTk

        cv2 = _require_cv2()
        display = draw_board_overlay(frame.copy(), self._calibration)
        rgb = cv2.cvtColor(display, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(rgb)
        width = self.preview_label.winfo_width()
        height = self.preview_label.winfo_height()
        image.thumbnail((max(width, 700), max(height, 520)))
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
        return calibration

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

    def _show_error(self, exc: Exception) -> None:
        self._set_status(str(exc))
        self._log("Error: " + str(exc))
        messagebox.showerror("Scrabble Plotter Sender", str(exc))


def _require_cv2():
    try:
        import cv2  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "OpenCV is required for live camera preview. Install scrabble_plotter/requirements.txt."
        ) from exc
    return cv2


def raise_user_error(message: str) -> None:
    messagebox.showwarning("Scrabble Plotter Sender", message)


def launch_gui() -> None:
    root = tk.Tk()
    ttk.Style(root).theme_use("clam")
    app = ScrabblePlotterApp(root)
    app._log("Ready.")
    root.mainloop()
