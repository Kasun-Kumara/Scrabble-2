from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .board import parse_square_label
from .calibration import MachineCalibration, PlotterCalibration
from .image_calibration import collect_board_corners
from .serial_sender import GCodeSender, SerialConfig, format_move_command, list_serial_ports


class ScrabblePlotterApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Scrabble Plotter Sender")
        self.root.geometry("1100x760")

        self.calibration_path = tk.StringVar(value=str(Path("scrabble_plotter_calibration.json").resolve()))
        self.image_path = tk.StringVar()
        self.square_var = tk.StringVar(value="H8")
        self.port_var = tk.StringVar()
        self.baud_var = tk.StringVar(value="115200")
        self.feed_rate_var = tk.StringVar(value="1500")
        self.command_var = tk.StringVar(value="G0")
        self.timeout_var = tk.StringVar(value="2.0")
        self.startup_g90_var = tk.BooleanVar(value=True)

        self.ref1_square_var = tk.StringVar(value="A1")
        self.ref1_x_var = tk.StringVar(value="0")
        self.ref1_y_var = tk.StringVar(value="0")
        self.ref2_square_var = tk.StringVar(value="O15")
        self.ref2_x_var = tk.StringVar(value="140")
        self.ref2_y_var = tk.StringVar(value="140")

        self.status_var = tk.StringVar(value="Choose an image, calibrate, then enter a square and COM port.")
        self.preview_image = None
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

        preview_frame = ttk.LabelFrame(frame, text="Board Image", padding=10)
        preview_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(1, weight=1)

        file_row = ttk.Frame(preview_frame)
        file_row.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        file_row.columnconfigure(1, weight=1)
        ttk.Button(file_row, text="Upload Image", command=self.choose_image).grid(row=0, column=0, padx=(0, 8))
        ttk.Entry(file_row, textvariable=self.image_path).grid(row=0, column=1, sticky="ew")

        self.preview_label = ttk.Label(preview_frame, text="No image selected", anchor="center")
        self.preview_label.grid(row=1, column=0, sticky="nsew")

        controls = ttk.Frame(frame)
        controls.grid(row=0, column=1, sticky="nsew")
        controls.columnconfigure(0, weight=1)

        calibration_box = ttk.LabelFrame(controls, text="Calibration", padding=10)
        calibration_box.grid(row=0, column=0, sticky="ew")
        calibration_box.columnconfigure(1, weight=1)

        ttk.Label(calibration_box, text="Calibration File").grid(row=0, column=0, sticky="w")
        ttk.Entry(calibration_box, textvariable=self.calibration_path).grid(row=0, column=1, sticky="ew", padx=(8, 0))
        ttk.Button(calibration_box, text="Browse", command=self.choose_calibration_file).grid(row=0, column=2, padx=(8, 0))

        ttk.Button(calibration_box, text="Calibrate Image Corners", command=self.calibrate_image).grid(
            row=1, column=0, columnspan=3, sticky="ew", pady=(10, 6)
        )

        fields = [
            ("Ref 1 Square", self.ref1_square_var),
            ("Ref 1 X", self.ref1_x_var),
            ("Ref 1 Y", self.ref1_y_var),
            ("Ref 2 Square", self.ref2_square_var),
            ("Ref 2 X", self.ref2_x_var),
            ("Ref 2 Y", self.ref2_y_var),
        ]
        for index, (label, variable) in enumerate(fields, start=2):
            ttk.Label(calibration_box, text=label).grid(row=index, column=0, sticky="w", pady=2)
            ttk.Entry(calibration_box, textvariable=variable).grid(row=index, column=1, columnspan=2, sticky="ew", padx=(8, 0))

        ttk.Button(calibration_box, text="Save Machine Calibration", command=self.save_machine_calibration).grid(
            row=8, column=0, columnspan=3, sticky="ew", pady=(10, 0)
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

        log_box = ttk.LabelFrame(controls, text="Status", padding=10)
        log_box.grid(row=2, column=0, sticky="nsew", pady=(12, 0))
        log_box.columnconfigure(0, weight=1)
        log_box.rowconfigure(1, weight=1)
        controls.rowconfigure(2, weight=1)

        ttk.Label(log_box, textvariable=self.status_var, wraplength=360).grid(row=0, column=0, sticky="ew")
        self.log_text = tk.Text(log_box, height=12, wrap="word")
        self.log_text.grid(row=1, column=0, sticky="nsew", pady=(8, 0))

    def choose_image(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose board image",
            filetypes=[
                ("Image files", "*.png *.jpg *.jpeg *.bmp *.webp"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        self.image_path.set(path)
        self._load_image_preview(path)
        self._set_status(f"Loaded image {path}")

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
        calibration = PlotterCalibration.load(self.calibration_path.get())
        if calibration.image_path and not self.image_path.get():
            self.image_path.set(calibration.image_path)
            self._load_image_preview(calibration.image_path)
        if calibration.machine:
            machine = calibration.machine
            self.ref1_square_var.set(machine.reference_square)
            self.ref1_x_var.set(str(machine.reference_x))
            self.ref1_y_var.set(str(machine.reference_y))
            self.ref2_square_var.set(machine.second_reference_square)
            self.ref2_x_var.set(str(machine.second_reference_x))
            self.ref2_y_var.set(str(machine.second_reference_y))

    def calibrate_image(self) -> None:
        image_path = self.image_path.get().strip()
        if not image_path:
            raise_user_error("Choose an image first.")
            return
        try:
            corners = collect_board_corners(image_path)
            calibration = PlotterCalibration.load(self.calibration_path.get())
            calibration.set_image_corners(image_path, corners)
            calibration.save(self.calibration_path.get())
            self._set_status("Saved image corner calibration.")
            self._log(f"Image corners: {corners}")
        except Exception as exc:
            self._show_error(exc)

    def save_machine_calibration(self) -> None:
        try:
            calibration = PlotterCalibration.load(self.calibration_path.get())
            machine = MachineCalibration.from_two_points(
                parse_square_label(self.ref1_square_var.get()),
                float(self.ref1_x_var.get()),
                float(self.ref1_y_var.get()),
                parse_square_label(self.ref2_square_var.get()),
                float(self.ref2_x_var.get()),
                float(self.ref2_y_var.get()),
            )
            calibration.set_machine_calibration(machine)
            if self.image_path.get().strip() and not calibration.image_path:
                calibration.image_path = self.image_path.get().strip()
            calibration.save(self.calibration_path.get())
            self._set_status("Saved machine calibration.")
            self._log(
                f"Machine calibration saved with {machine.reference_square} and {machine.second_reference_square}."
            )
        except Exception as exc:
            self._show_error(exc)

    def preview_move(self) -> None:
        try:
            calibration = PlotterCalibration.load(self.calibration_path.get())
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
            calibration = PlotterCalibration.load(self.calibration_path.get())
            calibration.validate_ready_for_move()
            square = parse_square_label(self.square_var.get())
            x, y = calibration.square_center_in_machine(square)
            config = SerialConfig(
                port=self.port_var.get().strip(),
                baud=int(self.baud_var.get()),
                timeout=float(self.timeout_var.get()),
                startup_g90=self.startup_g90_var.get(),
            )
            if not config.port:
                raise ValueError("Enter a COM port such as COM3.")
            sender = GCodeSender(config)
            gcode, responses = sender.send_move(
                x,
                y,
                feed_rate=self._optional_float(self.feed_rate_var.get()),
                command=self.command_var.get().strip() or "G0",
            )
            self._set_status(f"Sent {square.label} to {config.port}")
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

    def _load_image_preview(self, path: str) -> None:
        try:
            from PIL import Image, ImageTk
        except ImportError as exc:
            raise RuntimeError(
                "Pillow is required for image preview. Install scrabble_plotter/requirements.txt."
            ) from exc

        image = Image.open(path)
        image.thumbnail((650, 650))
        self.preview_image = ImageTk.PhotoImage(image)
        self.preview_label.configure(image=self.preview_image, text="")

    def _optional_float(self, raw: str) -> float | None:
        value = raw.strip()
        return float(value) if value else None

    def _set_status(self, message: str) -> None:
        self.status_var.set(message)

    def _log(self, message: str) -> None:
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")

    def _show_error(self, exc: Exception) -> None:
        self._set_status(str(exc))
        self._log("Error: " + str(exc))
        messagebox.showerror("Scrabble Plotter Sender", str(exc))


def raise_user_error(message: str) -> None:
    messagebox.showwarning("Scrabble Plotter Sender", message)


def launch_gui() -> None:
    root = tk.Tk()
    ttk.Style(root).theme_use("clam")
    app = ScrabblePlotterApp(root)
    app._log("Ready.")
    root.mainloop()
