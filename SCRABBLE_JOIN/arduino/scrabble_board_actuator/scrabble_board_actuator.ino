#include <Servo.h>
#include <U8g2lib.h>
#include <Adafruit_NeoPixel.h>
#include <string.h>

// ================= DISPLAY =================
// Page-buffer version saves RAM on Arduino Uno.
U8G2_ST7920_128X64_1_SW_SPI u8g2(U8G2_R0, 13, 11, 10, U8X8_PIN_NONE);

// ================= SERVO / ACTUATOR =================
// All 4 servo signal wires are connected to pin 9.
Servo actuatorServo;

const byte SERVO_PIN = 9;

const int SERVO_MIN_ANGLE = 20;   // board lowered
const int SERVO_MAX_ANGLE = 160;  // board lifted

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

// Smooth movement without using cos() to save memory.
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
const byte BUTTON_TOGGLE = 2;     // power button
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

// ================= LED STRIP =================
#define LED_PIN 12
#define LED_COUNT 10

Adafruit_NeoPixel strip(LED_COUNT, LED_PIN, NEO_GRB + NEO_KHZ800);

// ================= WORDS =================
const char* fallbackWords[] = {
  "APPLE",
  "BANANA",
  "CHERRY",
  "DATE",
  "ELDERBERRY"
};

const int fallbackWordCount = sizeof(fallbackWords) / sizeof(fallbackWords[0]);
int currentWordIndex = 0;
char runtimeWord[18] = "";
bool hasRuntimeWord = false;

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
char serialLine[80];
byte serialLength = 0;

// ================= WORD / LED FUNCTIONS =================
const char* currentWord() {
  if (hasRuntimeWord && runtimeWord[0] != '\0') {
    return runtimeWord;
  }
  return fallbackWords[currentWordIndex];
}

void showWordLengthLEDs(const char* word) {
  strip.clear();

  int len = strlen(word);
  if (len > LED_COUNT) {
    len = LED_COUNT;
  }

  for (int i = 0; i < len; i++) {
    strip.setPixelColor(i, strip.Color(255, 0, 0));
  }

  strip.show();
}

void clearLEDs() {
  strip.clear();
  strip.show();
}

void setRuntimeWord(const char* word) {
  byte outputIndex = 0;
  for (byte inputIndex = 0; word[inputIndex] != '\0' && outputIndex < sizeof(runtimeWord) - 1; inputIndex++) {
    char c = word[inputIndex];
    if (c >= 'A' && c <= 'Z') {
      runtimeWord[outputIndex++] = c;
    }
  }
  runtimeWord[outputIndex] = '\0';
  hasRuntimeWord = runtimeWord[0] != '\0';
}

// ================= SHARED ACTIONS =================
void resetPlayState() {
  inChallengeMode = false;
  wordChosen = false;
  showCountdown = false;
  clearLEDs();
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
  clearLEDs();
}

void stopCountdown() {
  showCountdown = false;
}

void startChallengeMode() {
  displayOn = true;
  inChallengeMode = true;
  wordChosen = false;
  showCountdown = false;
  clearLEDs();
}

void cancelChallengeMode() {
  inChallengeMode = false;
  wordChosen = false;
  showCountdown = false;
  clearLEDs();
}

void previousWord() {
  hasRuntimeWord = false;
  currentWordIndex = (currentWordIndex - 1 + fallbackWordCount) % fallbackWordCount;
}

void nextWord() {
  hasRuntimeWord = false;
  currentWordIndex = (currentWordIndex + 1) % fallbackWordCount;
}

void chooseCurrentWord() {
  displayOn = true;
  wordChosen = true;
  inChallengeMode = true;
  showCountdown = false;
  wordChosenTime = millis();
  showWordLengthLEDs(currentWord());
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
  Serial.print(F(" word="));
  Serial.println(currentWord());
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
  } else if (commandStartsWith(cmd, "WORD_SET")) {
    char* wordText = trimWhitespace(cmd + strlen("WORD_SET"));
    setRuntimeWord(wordText);
    if (!hasRuntimeWord) {
      Serial.println(F("err invalid word"));
      return;
    }
    displayOn = true;
    inChallengeMode = true;
    wordChosen = false;
    showCountdown = false;
    clearLEDs();
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
  } else if (strcmp(cmd, "LED_CLEAR") == 0) {
    clearLEDs();
    Serial.println(F("ok led clear"));
  } else if (strcmp(cmd, "DISPLAY_ON") == 0) {
    displayOn = true;
    Serial.println(F("ok display on"));
  } else if (strcmp(cmd, "DISPLAY_OFF") == 0) {
    displayOff();
    Serial.println(F("ok display off"));
  } else if (strcmp(cmd, "STATUS") == 0) {
    printStatus();
    Serial.println(F("ok status"));
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
      clearLEDs();
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
    u8g2.setCursor(0, 20);
    u8g2.print(F("Chosen Word:"));

    u8g2.setCursor(0, 40);
    u8g2.print(currentWord());
  } else if (inChallengeMode) {
    u8g2.setCursor(0, 20);
    u8g2.print(F("Challenge Mode"));

    u8g2.setCursor(0, 40);
    u8g2.print(F("Choose word:"));

    u8g2.setCursor(0, 60);
    u8g2.print(currentWord());
  } else {
    u8g2.setCursor(30, 32);
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
  updateDisplay();
}
