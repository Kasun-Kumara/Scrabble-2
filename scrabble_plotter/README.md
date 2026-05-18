# Scrabble Plotter Sender

This standalone Python utility maps Scrabble board squares from an image to
machine XY coordinates and sends raw G-code commands to an Arduino-based XY
plotter.

## Install

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r scrabble_plotter\requirements.txt
```

## Commands

Open the desktop GUI.

```bash
python -m scrabble_plotter gui
```

The GUI lets you:
- Upload and preview the board image
- Save image corner calibration
- Enter 2 machine reference squares and coordinates
- Enter a target square and COM port
- Preview G-code and send the move

CLI commands are still available too.

Calibrate the board image by clicking corners in this order: top-left,
top-right, bottom-right, bottom-left.

```bash
python -m scrabble_plotter calibrate-image --image board.jpg --calibration board.json
```

Calibrate machine coordinates using two known squares and their XY positions.

```bash
python -m scrabble_plotter calibrate-machine --calibration board.json --square1 A1 --x1 0 --y1 0 --square2 O15 --x2 140 --y2 140
```

Move the plotter to a square.

```bash
python -m scrabble_plotter move --calibration board.json --square H8 --port COM3 --baud 115200 --feed-rate 1500 --startup-g90
```

Open an interactive prompt for repeated moves.

```bash
python -m scrabble_plotter interactive --calibration board.json --port COM3 --baud 115200 --feed-rate 1500 --startup-g90
```
