#include <Servo.h>
#include <Adafruit_NeoPixel.h>
#include <U8g2lib.h>
#include <string.h>

#define LED_PIN 12
#define LED_COUNT 144
#define GRID_SIZE 12

const byte ON_BUTTON_PIN = 2;
const byte TIMER_BUTTON_PIN = 3;
const byte CHALLENGE_BUTTON_PIN = 4;
const byte PREVIOUS_BUTTON_PIN = 5;
const byte NEXT_BUTTON_PIN = 6;
const byte ACTUATOR_SERVO_PIN = 9;
const int ACTUATOR_DOWN_ANGLE = 20;
const int ACTUATOR_UP_ANGLE = 160;

const unsigned long BUTTON_DEBOUNCE_MS = 35;
const unsigned long SERVO_STEP_INTERVAL_MS = 20;
const unsigned long ANIMATION_ROW_INTERVAL_MS = 120;
const unsigned long ANIMATION_FINAL_HOLD_MS = 300;

Adafruit_NeoPixel strip(LED_COUNT, LED_PIN, NEO_GRB + NEO_KHZ800);
Servo actuatorServo;
U8G2_ST7920_128X64_1_SW_SPI screen(U8G2_R0, 13, 11, 10, U8X8_PIN_NONE);

bool systemOn = false;
int player1Score = 0;
int player2Score = 0;
int countdownSeconds = 120;
bool displayDirty = true;

char serialLine[64];
byte serialLength = 0;

// Only Player 1's latest three words are available for challenge.
const byte MAX_CHALLENGE_WORDS = 3;
const byte MAX_WORD_LENGTH = 16;
const byte CELL_BYTES = (LED_COUNT + 7) / 8;
char challengeWords[MAX_CHALLENGE_WORDS][MAX_WORD_LENGTH + 1];
byte challengeWordCells[MAX_CHALLENGE_WORDS][CELL_BYTES];
byte challengeWordCount = 0;
byte selectedWordIndex = 0;
bool challengeMode = false;
bool challengeChoicePending = false;
bool challengeLightsActive = false;
bool timerButtonPressed = false;

bool buttonState = HIGH;
bool lastButtonReading = HIGH;
unsigned long lastButtonChangeTime = 0;

struct DebouncedButton {
  byte pin;
  bool stableState;
  bool lastReading;
  bool pressedEvent;
  unsigned long lastChangeTime;

  DebouncedButton(byte buttonPin)
    : pin(buttonPin), stableState(HIGH), lastReading(HIGH),
      pressedEvent(false), lastChangeTime(0) {}

  void begin() {
    pinMode(pin, INPUT_PULLUP);
    stableState = digitalRead(pin);
    lastReading = stableState;
  }

  void update() {
    pressedEvent = false;
    bool reading = digitalRead(pin);
    if (reading != lastReading) {
      lastChangeTime = millis();
      lastReading = reading;
    }
    if (millis() - lastChangeTime >= BUTTON_DEBOUNCE_MS && reading != stableState) {
      stableState = reading;
      if (stableState == LOW) {
        pressedEvent = true;
      }
    }
  }

  bool pressed() {
    return pressedEvent;
  }
};

DebouncedButton challengeButton(CHALLENGE_BUTTON_PIN);
DebouncedButton previousButton(PREVIOUS_BUTTON_PIN);
DebouncedButton nextButton(NEXT_BUTTON_PIN);
DebouncedButton timerButton(TIMER_BUTTON_PIN);

int currentServoAngle = ACTUATOR_DOWN_ANGLE;
int targetServoAngle = ACTUATOR_DOWN_ANGLE;
unsigned long lastServoStepTime = 0;

int glowingRow = 0;
int clearingRow = 0;
bool clearingAnimation = false;
bool animationActive = false;
unsigned long lastAnimationTime = 0;

int pixelIndex(int row, int column) {
  // The physical first LED is at the bottom, so reverse logical screen rows.
  int physicalRow = GRID_SIZE - 1 - row;

  // Serpentine columns: first column bottom-to-top, next top-to-bottom.
  if (column % 2 == 0) {
    return column * GRID_SIZE + physicalRow;
  }
  return column * GRID_SIZE + (GRID_SIZE - 1 - physicalRow);
}

void clearLights() {
  strip.clear();
  strip.show();
}

void drawTopToBottomGlow(int activeRow) {
  strip.clear();

  for (int row = 0; row <= activeRow; row++) {
    int distanceBehindGlow = activeRow - row;
    int brightness = max(255 - distanceBehindGlow * 18, 70);
    int red = 38 * brightness / 255;
    int green = 171 * brightness / 255;
    int blue = brightness;

    for (int column = 0; column < GRID_SIZE; column++) {
      strip.setPixelColor(
        pixelIndex(row, column),
        strip.Color(red, green, blue)
      );
    }
  }

  strip.show();
}

void startBlueAnimation() {
  glowingRow = 0;
  clearingRow = 0;
  clearingAnimation = false;
  animationActive = true;
  lastAnimationTime = millis();
  drawTopToBottomGlow(glowingRow);
}

void updateBlueAnimation() {
  if (!systemOn || !animationActive) {
    return;
  }

  unsigned long now = millis();
  if (!clearingAnimation &&
    glowingRow < GRID_SIZE - 1
    && now - lastAnimationTime >= ANIMATION_ROW_INTERVAL_MS
  ) {
    lastAnimationTime = now;
    glowingRow++;
    drawTopToBottomGlow(glowingRow);
  } else if (!clearingAnimation &&
    glowingRow == GRID_SIZE - 1
    && now - lastAnimationTime >= ANIMATION_FINAL_HOLD_MS
  ) {
    clearingAnimation = true;
    clearingRow = 0;
    lastAnimationTime = now;

    for (int column = 0; column < GRID_SIZE; column++) {
      strip.setPixelColor(pixelIndex(clearingRow, column), 0);
    }
    strip.show();
    clearingRow++;
  } else if (
    clearingAnimation
    && now - lastAnimationTime >= ANIMATION_ROW_INTERVAL_MS
  ) {
    lastAnimationTime = now;

    if (clearingRow < GRID_SIZE) {
      for (int column = 0; column < GRID_SIZE; column++) {
        strip.setPixelColor(pixelIndex(clearingRow, column), 0);
      }
      strip.show();
      clearingRow++;
    }

    if (clearingRow >= GRID_SIZE) {
      animationActive = false;
      clearingAnimation = false;
    }
  }
}

void updateActuator() {
  unsigned long now = millis();
  if (currentServoAngle == targetServoAngle) {
    if (actuatorServo.attached()) {
      actuatorServo.detach();
    }
    return;
  }
  if (now - lastServoStepTime < SERVO_STEP_INTERVAL_MS) {
    return;
  }

  lastServoStepTime = now;
  currentServoAngle += currentServoAngle < targetServoAngle ? 1 : -1;
  actuatorServo.write(currentServoAngle);

  // Stop sending correction pulses at the endpoint to prevent vibration.
  if (currentServoAngle == targetServoAngle) {
    actuatorServo.detach();
  }
}

void turnSystemOn() {
  systemOn = true;
  if (!actuatorServo.attached()) {
    actuatorServo.attach(ACTUATOR_SERVO_PIN);
  }
  targetServoAngle = ACTUATOR_UP_ANGLE;
  lastServoStepTime = 0;
  startBlueAnimation();
}

void turnSystemOff() {
  systemOn = false;
  animationActive = false;
  challengeLightsActive = false;
  if (!actuatorServo.attached()) {
    actuatorServo.attach(ACTUATOR_SERVO_PIN);
  }
  targetServoAngle = ACTUATOR_DOWN_ANGLE;
  lastServoStepTime = 0;
  clearLights();
}

void updateOnButton() {
  bool reading = digitalRead(ON_BUTTON_PIN);

  if (reading != lastButtonReading) {
    lastButtonChangeTime = millis();
    lastButtonReading = reading;
  }

  if (millis() - lastButtonChangeTime >= BUTTON_DEBOUNCE_MS && reading != buttonState) {
    buttonState = reading;

    if (buttonState == LOW) {
      if (systemOn) {
        turnSystemOff();
      } else {
        turnSystemOn();
      }
    }
  }
}

const char* selectedChallengeWord() {
  if (challengeWordCount == 0) {
    return "";
  }
  if (selectedWordIndex >= challengeWordCount) {
    selectedWordIndex = 0;
  }
  return challengeWords[selectedWordIndex];
}

void clearChallengeWords() {
  if (challengeLightsActive) {
    clearLights();
  }
  challengeWordCount = 0;
  selectedWordIndex = 0;
  challengeMode = false;
  challengeChoicePending = false;
  challengeLightsActive = false;
  memset(challengeWordCells, 0, sizeof(challengeWordCells));
  displayDirty = true;
}

bool addChallengeWord(const char* word) {
  if (challengeWordCount >= MAX_CHALLENGE_WORDS || word[0] == '\0') {
    return false;
  }
  strncpy(challengeWords[challengeWordCount], word, MAX_WORD_LENGTH);
  challengeWords[challengeWordCount][MAX_WORD_LENGTH] = '\0';
  challengeWordCount++;
  displayDirty = true;
  return true;
}

void selectPreviousWord() {
  if (challengeWordCount == 0) {
    return;
  }
  selectedWordIndex = (selectedWordIndex + challengeWordCount - 1) % challengeWordCount;
  displayDirty = true;
}

void selectNextWord() {
  if (challengeWordCount == 0) {
    return;
  }
  selectedWordIndex = (selectedWordIndex + 1) % challengeWordCount;
  displayDirty = true;
}

void setChallengeCell(byte wordIndex, int pixel) {
  if (wordIndex >= MAX_CHALLENGE_WORDS || pixel < 0 || pixel >= LED_COUNT) {
    return;
  }
  challengeWordCells[wordIndex][pixel / 8] |= (1 << (pixel % 8));
}

bool challengeCellIsSet(byte wordIndex, int pixel) {
  if (wordIndex >= challengeWordCount || pixel < 0 || pixel >= LED_COUNT) {
    return false;
  }
  return challengeWordCells[wordIndex][pixel / 8] & (1 << (pixel % 8));
}

void showSelectedWordInRed() {
  strip.clear();
  for (int pixel = 0; pixel < LED_COUNT; pixel++) {
    if (challengeCellIsSet(selectedWordIndex, pixel)) {
      strip.setPixelColor(pixel, strip.Color(255, 0, 0));
    }
  }
  strip.show();
  challengeLightsActive = true;
}

bool storeWordCells(char* payload) {
  char* cellsStart = NULL;
  long wordIndex = strtol(payload, &cellsStart, 10);
  if (cellsStart == payload || wordIndex < 0 || wordIndex >= MAX_CHALLENGE_WORDS) {
    return false;
  }

  while (*cellsStart == ' ') {
    cellsStart++;
  }
  memset(challengeWordCells[wordIndex], 0, CELL_BYTES);

  char* square = strtok(cellsStart, ",");
  while (square != NULL) {
    if (square[0] >= 'A' && square[0] < 'A' + GRID_SIZE) {
      int row = atoi(square + 1) - 1;
      int column = square[0] - 'A';
      if (row >= 0 && row < GRID_SIZE) {
        // Board labels count from the opposite edge to the animation rows.
        // Undo the animation flip so A2 lights physical A2, not A11.
        int boardRow = GRID_SIZE - 1 - row;
        setChallengeCell((byte)wordIndex, pixelIndex(boardRow, column));
      }
    }
    square = strtok(NULL, ",");
  }
  return true;
}

void updateChallengeButtons() {
  challengeButton.update();
  previousButton.update();
  nextButton.update();

  if (challengeMode && previousButton.pressed()) {
    selectPreviousWord();
  }
  if (challengeMode && nextButton.pressed()) {
    selectNextWord();
  }
  if (challengeButton.pressed()) {
    if (!challengeMode) {
      if (challengeWordCount > 0) {
        selectedWordIndex = 0;
        challengeMode = true;
        challengeChoicePending = false;
        displayDirty = true;
      }
    } else {
      challengeMode = false;
      challengeChoicePending = true;
      showSelectedWordInRed();
      displayDirty = true;
    }
  }
}

void updateTimerButton() {
  timerButton.update();
  if (timerButton.pressed()) {
    timerButtonPressed = true;
  }
}

void drawScreen() {
  char timerText[8];
  int minutes = countdownSeconds / 60;
  int seconds = countdownSeconds % 60;
  snprintf(timerText, sizeof(timerText), "%d:%02d", minutes, seconds);

  screen.firstPage();
  do {
    if (challengeMode || challengeChoicePending) {
      screen.setFont(u8g2_font_ncenB08_tr);
      screen.setCursor(0, 11);
      if (challengeChoicePending) {
        screen.print(F("CHALLENGED"));
      } else {
        screen.print(F("PLAYER 1 WORD "));
        screen.print(selectedWordIndex + 1);
        screen.print(F("/"));
        screen.print(challengeWordCount);
      }
      screen.drawHLine(0, 16, 128);

      screen.setFont(u8g2_font_ncenB14_tr);
      const char* word = selectedChallengeWord();
      int wordWidth = screen.getStrWidth(word);
      screen.setCursor(max(0, (128 - wordWidth) / 2), 42);
      screen.print(word);

      screen.setFont(u8g2_font_5x7_tr);
      screen.setCursor(4, 62);
      screen.print(challengeMode ? F("PREV   NEXT   CHALLENGE") : F("SENDING TO GUI..."));
      continue;
    }

    screen.setFont(u8g2_font_ncenB08_tr);
    screen.setCursor(0, 12);
    screen.print(F("P1: "));
    screen.print(player1Score);
    screen.setCursor(70, 12);
    screen.print(F("P2: "));
    screen.print(player2Score);
    screen.drawHLine(0, 18, 128);

    screen.setFont(u8g2_font_logisoso24_tn);
    int timerWidth = screen.getStrWidth(timerText);
    screen.setCursor((128 - timerWidth) / 2, 54);
    screen.print(timerText);
  } while (screen.nextPage());
}

void updateScreen() {
  if (!displayDirty) {
    return;
  }
  displayDirty = false;
  drawScreen();
}

void handleSerialCommand(char* command) {
  int newPlayer1Score = 0;
  int newPlayer2Score = 0;

  if (sscanf(command, "SCORE P1 %d P2 %d", &newPlayer1Score, &newPlayer2Score) == 2) {
    player1Score = newPlayer1Score;
    player2Score = newPlayer2Score;
    if (challengeLightsActive) {
      clearLights();
      challengeLightsActive = false;
    }
    displayDirty = true;
    Serial.println(F("ok score"));
  } else if (strncmp(command, "TIMER ", 6) == 0) {
    countdownSeconds = max(0, atoi(command + 6));
    displayDirty = true;
    Serial.println(F("ok timer"));
  } else if (strcmp(command, "TIMER_TAKE") == 0) {
    if (timerButtonPressed) {
      timerButtonPressed = false;
      Serial.println(F("ok timer pressed"));
    } else {
      Serial.println(F("ok timer none"));
    }
  } else if (strcmp(command, "WORD_CLEAR") == 0) {
    clearChallengeWords();
    Serial.println(F("ok word clear"));
  } else if (strncmp(command, "WORD_ADD ", 9) == 0) {
    if (addChallengeWord(command + 9)) {
      Serial.println(F("ok word add"));
    } else {
      Serial.println(F("err word list full"));
    }
  } else if (strncmp(command, "WORD_LIST ", 10) == 0) {
    clearChallengeWords();
    char* word = strtok(command + 10, ",");
    while (word != NULL && challengeWordCount < MAX_CHALLENGE_WORDS) {
      addChallengeWord(word);
      word = strtok(NULL, ",");
    }
    Serial.println(F("ok word list"));
  } else if (strncmp(command, "WORD_CELLS ", 11) == 0) {
    if (storeWordCells(command + 11)) {
      Serial.println(F("ok word cells"));
    } else {
      Serial.println(F("err invalid word cells"));
    }
  } else if (strcmp(command, "WORD_PREV") == 0) {
    selectPreviousWord();
    Serial.println(F("ok word prev"));
  } else if (strcmp(command, "WORD_NEXT") == 0) {
    selectNextWord();
    Serial.println(F("ok word next"));
  } else if (strcmp(command, "CHALLENGE_START") == 0) {
    if (challengeWordCount > 0) {
      selectedWordIndex = 0;
      challengeMode = true;
      challengeChoicePending = false;
      displayDirty = true;
      Serial.println(F("ok challenge start"));
    } else {
      Serial.println(F("err no player 1 words"));
    }
  } else if (strcmp(command, "CHALLENGE_CANCEL") == 0) {
    challengeMode = false;
    challengeChoicePending = false;
    clearLights();
    challengeLightsActive = false;
    displayDirty = true;
    Serial.println(F("ok challenge cancel"));
  } else if (strcmp(command, "CHALLENGE_TAKE") == 0) {
    if (challengeChoicePending) {
      Serial.print(F("ok challenge chosen "));
      Serial.print(selectedWordIndex);
      Serial.print(F(" "));
      Serial.println(selectedChallengeWord());
      challengeChoicePending = false;
      displayDirty = true;
    } else {
      Serial.println(F("ok challenge none"));
    }
  } else if (strcmp(command, "BOARD_UP") == 0) {
    turnSystemOn();
    Serial.println(F("ok board up"));
  } else if (strcmp(command, "BOARD_DOWN") == 0) {
    turnSystemOff();
    Serial.println(F("ok board down"));
  } else if (strcmp(command, "LED_TEST") == 0) {
    turnSystemOn();
    Serial.println(F("ok led test"));
  } else if (strcmp(command, "LED_CLEAR") == 0) {
    turnSystemOff();
    Serial.println(F("ok led clear"));
  } else if (strcmp(command, "PING") == 0) {
    Serial.println(F("ok board actuator"));
  } else if (command[0] != '\0') {
    Serial.print(F("err unknown command "));
    Serial.println(command);
  }
}

void readSerialCommands() {
  while (Serial.available() > 0) {
    char incoming = Serial.read();

    if (incoming == '\n' || incoming == '\r') {
      if (serialLength > 0) {
        serialLine[serialLength] = '\0';
        handleSerialCommand(serialLine);
        serialLength = 0;
      }
    } else if (serialLength < sizeof(serialLine) - 1) {
      serialLine[serialLength++] = incoming;
    }
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(ON_BUTTON_PIN, INPUT_PULLUP);
  timerButton.begin();
  challengeButton.begin();
  previousButton.begin();
  nextButton.begin();

  strip.begin();
  strip.setBrightness(80);
  clearLights();

  actuatorServo.attach(ACTUATOR_SERVO_PIN);
  actuatorServo.write(ACTUATOR_DOWN_ANGLE);
  delay(400);
  actuatorServo.detach();

  screen.begin();
  drawScreen();
  displayDirty = false;

  Serial.println(F("SCRABBLE_BOARD_ACTUATOR_READY"));
  Serial.println(F("ok ready"));
}

void loop() {
  readSerialCommands();
  updateOnButton();
  updateTimerButton();
  updateChallengeButtons();
  updateActuator();
  updateBlueAnimation();
  updateScreen();
}
