from __future__ import annotations

import time
from dataclasses import dataclass


def _require_serial():
    try:
        import serial  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "pyserial is required for serial communication. Install scrabble_plotter/requirements.txt."
        ) from exc
    return serial


def list_serial_ports() -> list[str]:
    serial = _require_serial()
    try:
        from serial.tools import list_ports  # type: ignore
    except ImportError:
        return []
    return [port.device for port in list_ports.comports()]


def _format_float(value: float) -> str:
    text = f"{value:.3f}"
    return text.rstrip("0").rstrip(".")


def format_move_command(x: float, y: float, feed_rate: float | None = None, command: str = "G0") -> str:
    parts = [command.upper(), f"X{_format_float(x)}", f"Y{_format_float(y)}"]
    if feed_rate is not None:
        parts.append(f"F{_format_float(feed_rate)}")
    return " ".join(parts)


@dataclass
class SerialConfig:
    port: str
    baud: int
    timeout: float = 2.0
    startup_g90: bool = False


class GCodeSender:
    def __init__(self, config: SerialConfig):
        self.config = config

    def send_move(
        self,
        x: float,
        y: float,
        feed_rate: float | None = None,
        command: str = "G0",
    ) -> tuple[str, list[str]]:
        gcode = format_move_command(x, y, feed_rate=feed_rate, command=command)
        responses = self.send_commands([gcode])
        return gcode, responses

    def send_commands(self, commands: list[str]) -> list[str]:
        serial = _require_serial()
        with serial.Serial(self.config.port, self.config.baud, timeout=self.config.timeout) as connection:
            time.sleep(2)
            if self.config.startup_g90:
                self._write_line(connection, "G90")
                self._read_responses(connection)

            responses: list[str] = []
            for command in commands:
                self._write_line(connection, command)
                responses.extend(self._read_responses(connection))
            return responses

    def _write_line(self, connection, line: str) -> None:  # type: ignore[no-untyped-def]
        connection.write((line.strip() + "\n").encode("utf-8"))
        connection.flush()

    def _read_responses(self, connection) -> list[str]:  # type: ignore[no-untyped-def]
        deadline = time.monotonic() + self.config.timeout
        responses: list[str] = []
        while time.monotonic() < deadline:
            raw = connection.readline()
            if not raw:
                if responses:
                    break
                continue
            message = raw.decode("utf-8", errors="replace").strip()
            if not message:
                continue
            responses.append(message)
            if message.lower() == "ok":
                break
        return responses
