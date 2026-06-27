#include <Servo.h>
#include <U8g2lib.h>
#include <string.h>
#include <Adafruit_NeoPixel.h>

// ================= LED STRIP =================
// 12x12 grid = 144 LEDs on pin 12.
// Column layout (top-to-bottom direction): col 0=down, 1=up, 2=up, 3=down, 4=down, 5=up, ...
// Pattern: down, up, up, down, down, up  (repeating every 6 columns)
const byte LED_PIN       = 12;
const byte LED_COLS      = 12;
const byte LED_ROWS      = 12;
const int  LED_COUNT     = LED_COLS * LED_ROWS;

// true  = column runs top-to-bottom (first pixel = row 0)
// false = column runs bottom-to-top (first pixel = row 11)
// Pattern per col index 0-11: down up up down down up down up up down down up
const bool COL_DIR[LED_COLS] = {
  true, false, false, true, true, false,   // cols 0-5
  true, false, false, true, true, false    // cols 6-11
};

Adafruit_NeoPixel strip(LED_COUNT, LED_PIN, NEO_GRB + NEO_KHZ800);

// Returns the strip index for grid position (col, row) where row 0 = top.
int ledIndex(byte col, byte row) {
  byte physRow = COL_DIR[col] ? row : (LED_ROWS - 1 - row);
  return (int)col * LED_ROWS + (int)physRow;
}

void setAllLeds(uint32_t colour) {
  for (int i = 0; i < LED_COUNT; i++) {
    strip.setPixelColor(i, colour);
  }
  strip.show();
}

void clearAllLeds() {
  strip.clear();
  strip.show();
}

// ---- simple win animation state ----
bool ledWinActive = false;
unsigned long ledWinStart = 0;
const unsigned long LED_WIN_DURATION_MS = 3000;

void startLedWin() {
  ledWinActive = true;
  ledWinStart = millis();
}

void updateLedAnimation() {
  if (!ledWinActive) return;
  unsigned long elapsed = millis() - ledWinStart;
  if (elapsed >= LED_WIN_DURATION_MS) {
    clearAllLeds();
    ledWinActive = false;
    return;
  }
  // Alternate gold / off at ~4 Hz
  bool on = ((elapsed / 125) % 2) == 0;
  uint32_t colour = on ? strip.Color(255, 180, 0) : 0;
  setAllLeds(colour);
}

// ================= DISPLAY =================
// Page-buffer version saves RAM on Arduino Uno/Nano.
U8G2_ST7920_128X64_1_SW_SPI u8g2(U8G2_R0, 13, 11, 10, U8X8_PIN_NONE);

// ================= SERVO / ACTUATOR =================
Servo actuatorServo;

const byte SERVO_PIN = 9;
const int SERVO_MIN_ANGLE = 20;
const int SERVO_MAX_ANGLE = 160;
const int SERVO_STEPS = 100;
const unsigned long SERVO_STEP_DELAY_MS = 20;

int currentAngle = SERVO_MIN_ANGLE;
int startAngle = SERVO_MIN_ANGLE;
int targetAngle = SERVO_MIN_ANGLE;
int servoStep = 0;
unsigned long lastServoStepTime = 0;
bool boardRaised = false;
bool servoMoving = false;

void writeActuator(int angle) {
  actuatorServo.write(angle);
}

int easedAngle(int startA, int targetA, int stepNo) {
  long i = constrain(stepNo, 0, SERVO_STEPS);
  long numerator = 3L * i * i * SERVO_STEPS - 2L * i * i * i;
  long denominator = 1L * SERVO_STEPS * SERVO_STEPS * SERVO_STEPS;
  return startA + (long)(targetA - startA) * numerator / denominator;
}

void startServoMove(int newTarget) {
  startAngle = currentAngle;
  targetAngle = newTarget;
  servoStep = 0;
  servoMoving = true;
  lastServoStepTime = millis();
}

void updateServoSystem() {
  if (!servoMoving) {
    return;
  }

  unsigned long now = millis();
  if (now - lastServoStepTime >= SERVO_STEP_DELAY_MS) {
    lastServoStepTime = now;
    servoStep++;
    currentAngle = easedAngle(startAngle, targetAngle, servoStep);
    writeActuator(currentAngle);

    if (servoStep >= SERVO_STEPS) {
      currentAngle = targetAngle;
      writeActuator(currentAngle);
      servoMoving = false;
    }
  }
}

// ================= BUTTON PINS =================
const byte BUTTON_TOGGLE = 2;
const byte BUTTON_COUNTDOWN = 3;
const byte BUTTON_CHALLENGE = 4;
const byte BUTTON_PREV = 5;
const byte BUTTON_NEXT = 6;

// ================= BUTTON DEBOUNCE =================
struct Button {
  byte pin;
  bool stableState;
  bool lastReading;
  bool fellEdge;
  unsigned long lastChangeTime;

  Button(byte p) {
    pin = p;
    stableState = HIGH;
    lastReading = HIGH;
    fellEdge = false;
    lastChangeTime = 0;
  }

  void begin() {
    pinMode(pin, INPUT_PULLUP);
    stableState = digitalRead(pin);
    lastReading = stableState;
  }

  void update() {
    fellEdge = false;
    bool reading = digitalRead(pin);

    if (reading != lastReading) {
      lastChangeTime = millis();
      lastReading = reading;
    }

    if ((millis() - lastChangeTime) > 35) {
      if (reading != stableState) {
        stableState = reading;
        if (stableState == LOW) {
          fellEdge = true;
        }
      }
    }
  }

  bool pressed() {
    return fellEdge;
  }
};

Button btnToggle(BUTTON_TOGGLE);
Button btnCountdown(BUTTON_COUNTDOWN);
Button btnChallenge(BUTTON_CHALLENGE);
Button btnPrev(BUTTON_PREV);
Button btnNext(BUTTON_NEXT);


// ================= WORDS =================
int currentWordIndex = 0;
const byte MAX_RUNTIME_WORDS = 10;
const byte MAX_WORD_LENGTH = 16;
char runtimeWords[MAX_RUNTIME_WORDS][MAX_WORD_LENGTH + 1];
byte runtimeWordCount = 0;
int player1Score = 0;
int player2Score = 0;
bool challengeChoicePending = false;
int chosenWordIndex = -1;
char chosenWord[MAX_WORD_LENGTH + 1];

// ================= STATES =================
bool displayOn = false;
bool inChallengeMode = false;
bool wordChosen = false;
bool showCountdown = false;

unsigned long countdownStart = 0;
unsigned long countdownDurationMs = 30000;
unsigned long wordChosenTime = 0;
const unsigned long DISPLAY_REFRESH_TIME_MS = 80;
unsigned long lastDisplayRefresh = 0;

// ================= SERIAL =================
char serialLine[200];
byte serialLength = 0;

// ================= WORD / LED FUNCTIONS =================
int activeWordCount() {
  return runtimeWordCount;
}

const char* currentWord() {
  int count = activeWordCount();
  if (count <= 0) {
    return "";
  }
  if (currentWordIndex < 0 || currentWordIndex >= count) {
    currentWordIndex = 0;
  }
  return runtimeWords[currentWordIndex];
}


bool copyCleanWord(char* destination, const char* word) {
  byte outputIndex = 0;
  for (byte inputIndex = 0; word[inputIndex] != '\0' && outputIndex < MAX_WORD_LENGTH; inputIndex++) {
    char c = word[inputIndex];
    if (c >= 'A' && c <= 'Z') {
      destination[outputIndex++] = c;
    }
  }
  destination[outputIndex] = '\0';
  return outputIndex > 0;
}

void clearRuntimeWords() {
  runtimeWordCount = 0;
  currentWordIndex = 0;
  for (byte index = 0; index < MAX_RUNTIME_WORDS; index++) {
    runtimeWords[index][0] = '\0';
  }
}

bool runtimeWordExists(const char* word) {
  for (byte index = 0; index < runtimeWordCount; index++) {
    if (strcmp(runtimeWords[index], word) == 0) {
      return true;
    }
  }
  return false;
}

bool appendRuntimeWord(const char* word) {
  if (runtimeWordCount >= MAX_RUNTIME_WORDS) {
    return false;
  }

  char cleaned[MAX_WORD_LENGTH + 1];
  if (!copyCleanWord(cleaned, word) || runtimeWordExists(cleaned)) {
    return false;
  }

  strcpy(runtimeWords[runtimeWordCount], cleaned);
  runtimeWordCount++;
  return true;
}

bool setRuntimeWord(const char* word) {
  char cleaned[MAX_WORD_LENGTH + 1];
  if (!copyCleanWord(cleaned, word)) {
    return false;
  }

  clearRuntimeWords();
  strcpy(runtimeWords[0], cleaned);
  runtimeWordCount = 1;
  return true;
}

bool setRuntimeWordList(char* words) {
  clearRuntimeWords();

  char* tokenStart = words;
  for (char* cursor = words; ; cursor++) {
    bool atEnd = *cursor == '\0';
    bool separator = *cursor == ',' || *cursor == ';' || *cursor == '|' || *cursor == ' ' || *cursor == '\t';

    if (atEnd || separator) {
      char saved = *cursor;
      *cursor = '\0';
      appendRuntimeWord(tokenStart);

      if (atEnd || runtimeWordCount >= MAX_RUNTIME_WORDS) {
        break;
      }

      *cursor = saved;
      tokenStart = cursor + 1;
    }
  }

  currentWordIndex = 0;
  return runtimeWordCount > 0;
}

// ================= SHARED ACTIONS =================
void resetPlayState() {
  inChallengeMode = false;
  wordChosen = false;
  showCountdown = false;
}

void raiseBoard() {
  boardRaised = true;
  displayOn = true;
  resetPlayState();
  startServoMove(SERVO_MAX_ANGLE);
}

void lowerBoard() {
  boardRaised = false;
  displayOn = false;
  resetPlayState();
  startServoMove(SERVO_MIN_ANGLE);
}

void toggleBoard() {
  if (boardRaised) {
    lowerBoard();
  } else {
    raiseBoard();
  }
}

void startCountdown(unsigned int seconds) {
  displayOn = true;
  showCountdown = true;
  countdownStart = millis();
  countdownDurationMs = (unsigned long)seconds * 1000UL;
  inChallengeMode = false;
  wordChosen = false;
}

void stopCountdown() {
  showCountdown = false;
}

void startChallengeMode() {
  displayOn = true;
  inChallengeMode = true;
  wordChosen = false;
  showCountdown = false;
}

void cancelChallengeMode() {
  inChallengeMode = false;
  wordChosen = false;
  showCountdown = false;
}

void previousWord() {
  int count = activeWordCount();
  if (count <= 0) {
    return;
  }
  currentWordIndex = (currentWordIndex - 1 + count) % count;
}

void nextWord() {
  int count = activeWordCount();
  if (count <= 0) {
    return;
  }
  currentWordIndex = (currentWordIndex + 1) % count;
}

void chooseCurrentWord() {
  displayOn = true;
  wordChosen = true;
  inChallengeMode = true;
  showCountdown = false;
  wordChosenTime = millis();
}

void displayOff() {
  displayOn = false;
  resetPlayState();
}

// ================= BUTTON ACTIONS =================
void updateButtons() {
  btnToggle.update();
  btnCountdown.update();
  btnChallenge.update();
  btnPrev.update();
  btnNext.update();

  if (btnToggle.pressed()) {
    toggleBoard();
  }

  if (btnCountdown.pressed() && displayOn) {
    startCountdown((unsigned int)(countdownDurationMs / 1000UL));
  }

  if (btnChallenge.pressed() && displayOn) {
    if (inChallengeMode && !wordChosen) {
      chooseCurrentWord();
    } else {
      startChallengeMode();
    }
  }

  if (displayOn && inChallengeMode && !wordChosen) {
    if (btnPrev.pressed()) {
      previousWord();
    }

    if (btnNext.pressed()) {
      nextWord();
    }
  }
}

// ================= SERIAL COMMANDS =================
void readSerialCommand() {
  while (Serial.available() > 0) {
    char c = Serial.read();

    if (c == '\n' || c == '\r') {
      if (serialLength > 0) {
        serialLine[serialLength] = '\0';
        handleSerialCommand(serialLine);
        serialLength = 0;
      }
    } else if (serialLength < sizeof(serialLine) - 1) {
      serialLine[serialLength++] = c;
    }
  }
}

char* trimWhitespace(char* text) {
  while (*text == ' ' || *text == '\t') {
    text++;
  }

  int len = strlen(text);
  while (len > 0 && (text[len - 1] == ' ' || text[len - 1] == '\t')) {
    text[len - 1] = '\0';
    len--;
  }

  return text;
}

void uppercaseAscii(char* text) {
  for (byte index = 0; text[index] != '\0'; index++) {
    if (text[index] >= 'a' && text[index] <= 'z') {
      text[index] = text[index] - 'a' + 'A';
    }
  }
}

bool commandStartsWith(const char* cmd, const char* prefix) {
  int len = strlen(prefix);
  return strncmp(cmd, prefix, len) == 0 && (cmd[len] == '\0' || cmd[len] == ' ');
}

void printStatus() {
  Serial.print(F("state raised="));
  Serial.print(boardRaised ? 1 : 0);
  Serial.print(F(" moving="));
  Serial.print(servoMoving ? 1 : 0);
  Serial.print(F(" display="));
  Serial.print(displayOn ? 1 : 0);
  Serial.print(F(" challenge="));
  Serial.print(inChallengeMode ? 1 : 0);
  Serial.print(F(" chosen="));
  Serial.print(wordChosen ? 1 : 0);
  Serial.print(F(" countdown="));
  Serial.print(showCountdown ? 1 : 0);
  Serial.print(F(" word_count="));
  Serial.print(activeWordCount());
  Serial.print(F(" word="));
  Serial.println(currentWord());
}

bool parseScoreCommand(char* text) {
  int p1 = player1Score;
  int p2 = player2Score;
  char* token = strtok(text, " \t");
  while (token != NULL) {
    if (strcmp(token, "P1") == 0) {
      char* value = strtok(NULL, " \t");
      if (value == NULL) {
        break;
      }
      p1 = atoi(value);
    } else if (strcmp(token, "P2") == 0) {
      char* value = strtok(NULL, " \t");
      if (value == NULL) {
        break;
      }
      p2 = atoi(value);
    }
    token = strtok(NULL, " \t");
  }
  player1Score = max(0, p1);
  player2Score = max(0, p2);
  displayOn = true;
  return true;
}

void handleSerialCommand(char* rawCommand) {
  char* cmd = trimWhitespace(rawCommand);
  uppercaseAscii(cmd);

  if (cmd[0] == '\0') {
    return;
  }

  if (strcmp(cmd, "PING") == 0) {
    Serial.println(F("ok board actuator"));
  } else if (strcmp(cmd, "BOARD_UP") == 0 || strcmp(cmd, "UP") == 0 || strcmp(cmd, "RAISE") == 0) {
    raiseBoard();
    Serial.println(F("ok board up"));
  } else if (strcmp(cmd, "BOARD_DOWN") == 0 || strcmp(cmd, "DOWN") == 0 || strcmp(cmd, "LOWER") == 0) {
    lowerBoard();
    Serial.println(F("ok board down"));
  } else if (strcmp(cmd, "BOARD_TOGGLE") == 0) {
    toggleBoard();
    Serial.println(F("ok board toggle"));
  } else if (commandStartsWith(cmd, "COUNTDOWN")) {
    char* secondsText = trimWhitespace(cmd + strlen("COUNTDOWN"));
    unsigned int seconds = countdownDurationMs / 1000UL;
    if (secondsText[0] != '\0') {
      long parsed = atol(secondsText);
      if (parsed <= 0 || parsed > 3600) {
        Serial.println(F("err invalid countdown"));
        return;
      }
      seconds = (unsigned int)parsed;
    }
    startCountdown(seconds);
    Serial.print(F("ok countdown "));
    Serial.println(seconds);
  } else if (strcmp(cmd, "COUNTDOWN_STOP") == 0) {
    stopCountdown();
    Serial.println(F("ok countdown stop"));
  } else if (strcmp(cmd, "CHALLENGE_START") == 0) {
    startChallengeMode();
    Serial.println(F("ok challenge start"));
  } else if (strcmp(cmd, "CHALLENGE_CANCEL") == 0) {
    cancelChallengeMode();
    Serial.println(F("ok challenge cancel"));
  } else if (strcmp(cmd, "WORD_CLEAR") == 0) {
    clearRuntimeWords();
    Serial.println(F("ok word clear"));
  } else if (commandStartsWith(cmd, "WORD_LIST")) {
    char* wordsText = trimWhitespace(cmd + strlen("WORD_LIST"));
    if (!setRuntimeWordList(wordsText)) {
      Serial.println(F("err invalid word list"));
      return;
    }
    displayOn = true;
    inChallengeMode = true;
    wordChosen = false;
    showCountdown = false;
    Serial.print(F("ok word list "));
    Serial.println(runtimeWordCount);
  } else if (commandStartsWith(cmd, "WORD_SET")) {
    char* wordText = trimWhitespace(cmd + strlen("WORD_SET"));
    if (!setRuntimeWord(wordText)) {
      Serial.println(F("err invalid word"));
      return;
    }
    displayOn = true;
    inChallengeMode = true;
    wordChosen = false;
    showCountdown = false;
    Serial.print(F("ok word set "));
    Serial.println(currentWord());
  } else if (strcmp(cmd, "WORD_PREV") == 0) {
    previousWord();
    Serial.print(F("ok word prev "));
    Serial.println(currentWord());
  } else if (strcmp(cmd, "WORD_NEXT") == 0) {
    nextWord();
    Serial.print(F("ok word next "));
    Serial.println(currentWord());
  } else if (strcmp(cmd, "WORD_CHOOSE") == 0) {
    chooseCurrentWord();
    Serial.print(F("ok word choose "));
    Serial.println(currentWord());
  } else if (commandStartsWith(cmd, "SCORE")) {
    char* scoreText = trimWhitespace(cmd + strlen("SCORE"));
    if (!parseScoreCommand(scoreText)) {
      Serial.println(F("err invalid score"));
      return;
    }
    Serial.print(F("ok score p1 "));
    Serial.print(player1Score);
    Serial.print(F(" p2 "));
    Serial.println(player2Score);
  } else if (strcmp(cmd, "DISPLAY_ON") == 0) {
    displayOn = true;
    Serial.println(F("ok display on"));
  } else if (strcmp(cmd, "DISPLAY_OFF") == 0) {
    displayOff();
    Serial.println(F("ok display off"));
  } else if (strcmp(cmd, "STATUS") == 0) {
    printStatus();
    Serial.println(F("ok status"));
  } else if (strcmp(cmd, "LED_RED") == 0) {
    ledWinActive = false;
    setAllLeds(strip.Color(255, 0, 0));
    Serial.println(F("ok led red"));
  } else if (strcmp(cmd, "LED_OFF") == 0) {
    ledWinActive = false;
    clearAllLeds();
    Serial.println(F("ok led off"));
  } else if (strcmp(cmd, "LED_WIN") == 0) {
    startLedWin();
    Serial.println(F("ok led win"));
  } else {
    Serial.print(F("err unknown command "));
    Serial.println(cmd);
  }
}

// ================= TIMED STATES =================
void updateTimedStates() {
  if (showCountdown) {
    unsigned long elapsed = millis() - countdownStart;
    if (elapsed > countdownDurationMs) {
      showCountdown = false;
    }
  }

  if (wordChosen) {
    if (millis() - wordChosenTime >= 5000) {
      wordChosen = false;
      inChallengeMode = false;
    }
  }
}

// ================= DISPLAY DRAWING =================
void drawDisplayContent() {
  if (!displayOn) {
    return;
  }

  u8g2.setFont(u8g2_font_ncenB08_tr);

  if (showCountdown) {
    unsigned long elapsed = millis() - countdownStart;
    int remaining = 0;
    if (elapsed <= countdownDurationMs) {
      remaining = (countdownDurationMs - elapsed) / 1000UL;
    }

    u8g2.setCursor(0, 20);
    u8g2.print(F("Countdown: "));
    u8g2.print(remaining);
    u8g2.print(F("s"));
  } else if (wordChosen) {
    u8g2.setCursor(0, 10);
    u8g2.print(F("P1:"));
    u8g2.print(player1Score);
    u8g2.print(F("  P2:"));
    u8g2.print(player2Score);
    u8g2.setCursor(0, 20);
    u8g2.print(F("Chosen Word:"));
    u8g2.setCursor(0, 40);
    if (activeWordCount() > 0) {
      u8g2.print(currentWord());
    } else {
      u8g2.print(F("No camera words"));
    }
  } else if (inChallengeMode) {
    u8g2.setCursor(0, 10);
    u8g2.print(F("P1:"));
    u8g2.print(player1Score);
    u8g2.print(F("  P2:"));
    u8g2.print(player2Score);
    u8g2.setCursor(0, 24);
    u8g2.print(F("Challenge Mode"));
    if (activeWordCount() > 0) {
      u8g2.setCursor(0, 42);
      u8g2.print(F("Choose word:"));
      u8g2.setCursor(0, 60);
      u8g2.print(currentWord());
    } else {
      u8g2.setCursor(0, 40);
      u8g2.print(F("Use camera scan"));
      u8g2.setCursor(0, 60);
      u8g2.print(F("then challenge"));
    }
  } else {
    u8g2.setCursor(0, 20);
    u8g2.print(F("P1:"));
    u8g2.print(player1Score);
    u8g2.print(F("  P2:"));
    u8g2.print(player2Score);
    u8g2.setCursor(30, 44);
    u8g2.print(F("SCRABLIFY"));
  }
}

void updateDisplay() {
  unsigned long now = millis();
  if (now - lastDisplayRefresh >= DISPLAY_REFRESH_TIME_MS) {
    lastDisplayRefresh = now;
    u8g2.firstPage();
    do {
      drawDisplayContent();
    } while (u8g2.nextPage());
  }
}

// ================= SETUP =================
void setup() {
  Serial.begin(115200);

  btnToggle.begin();
  btnCountdown.begin();
  btnChallenge.begin();
  btnPrev.begin();
  btnNext.begin();

  u8g2.begin();

  strip.begin();
  strip.setBrightness(180);
  strip.clear();
  strip.show();

  actuatorServo.attach(SERVO_PIN);
  writeActuator(SERVO_MIN_ANGLE);

  Serial.println(F("SCRABBLE_BOARD_ACTUATOR_READY"));
  Serial.println(F("ok ready"));
}

// ================= LOOP =================
void loop() {
  readSerialCommand();
  updateServoSystem();
  updateButtons();
  updateTimedStates();
  updateLedAnimation();
  updateDisplay();
}
