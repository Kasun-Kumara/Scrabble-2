# Scrabble Plotter Sender

This standalone Python utility maps a fixed 12x12 board to plotter XY
coordinates and sends simple G-code style commands to an Arduino-based XY
plotter.

The camera is used for live preview, whole-frame letter capture, live
PaddleOCR word detection, board calibration, and offline board scanning.
Plotter movement uses a fixed 30 mm cell size plus the saved top-left board
offset.

## Install

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r scrabble_plotter\requirements.txt
```

## Desktop GUI

Open the desktop GUI.

```bash
python -m scrabble_plotter gui
```

The GUI lets you:
- Select and start a live camera
- Dynamically identify and display visible letters from the camera feed
- Capture camera letters without detecting the board area
- Use PaddleOCR on the latest camera frame and display detected words as a numbered list
- Save the board top-left plotter offset in millimeters
- Save and send X/Y stepper scale values in steps per millimeter
- Scan the board with local OCR and correct any uncertain cells
- Configure 12x12 letter/word premium squares and calculate the board score
- Enter a target square from A1 to L12
- Preview G-code and send moves over a COM port

## PaddleOCR word detection

Start the camera and leave Live words enabled, or click Find Words to run one
manual pass. The app first detects the four corners of each visible tile, warps
each tile into its own crop, reads the printed letter inside that tile with
PaddleOCR, and then assembles words from the tile positions. It lists only
words that read horizontally from right to left or vertically from top to
bottom. The first PaddleOCR run may take longer while OCR models are loaded.

The Gemini Agent panel is still available for optional plotter-action requests,
but camera word detection does not need a Gemini API key.

Movement uses this formula:

```text
X = offset_x_mm + 15 + column * 30
Y = offset_y_mm + 15 + row * 30
```

For example, with offset `0,0`, A1 moves to `X15 Y15`, A2 moves to
`X15 Y45`, and L12 moves to `X345 Y345`.

## CLI

Save the board top-left plotter offset.

```bash
python -m scrabble_plotter set-offset --calibration scrabble_plotter_calibration.json --x 0 --y 0
```

Calibrate a saved image by clicking corners in this order: top-left,
top-right, bottom-right, bottom-left. This is mostly useful for testing; the GUI
uses the live camera.

```bash
python -m scrabble_plotter calibrate-image --image board.jpg --calibration scrabble_plotter_calibration.json
```

Scan and score a saved board image using the saved camera corners.

```bash
python -m scrabble_plotter scan-image --image board.jpg --calibration scrabble_plotter_calibration.json
```

Preview a move without opening the serial port.

```bash
python -m scrabble_plotter move --calibration scrabble_plotter_calibration.json --square A2 --port COM3 --feed-rate 1500 --dry-run
```

Move the plotter to a square.

```bash
python -m scrabble_plotter move --calibration scrabble_plotter_calibration.json --square H8 --port COM3 --baud 115200 --feed-rate 1500 --startup-g90
```

Open an interactive prompt for repeated moves.

```bash
python -m scrabble_plotter interactive --calibration scrabble_plotter_calibration.json --port COM3 --baud 115200 --feed-rate 1500 --startup-g90
```

## Arduino

The custom STEP/DIR Arduino sketch is here:

```text
arduino\scrabble_plotter_controller\scrabble_plotter_controller.ino
```

It accepts:
- `G90`
- `G0 X... Y... F...`
- `G1 X... Y... F...`
- `STEPS X... Y...`
- `HOMEZERO`

`HOMEZERO` sets the current position to `X0 Y0`; it does not move the motors.
`STEPS X... Y...` updates the X and Y steps-per-millimeter values at runtime.
