// vocab-display -- an always-on vocabulary card for the TTGO T-Display.
//
// Each entry is a two-sided card. The front shows the English term alone, in the largest
// type that fits; four seconds later it flips to the Vietnamese meaning and an example.
// Nothing needs pressing: the pause on the front is there to give you a moment to recall
// the answer before it appears, which is a better loop than showing both halves at once.
//
// Button 1 (GPIO 35) advances -- flip, or next card if already flipped.
// Button 2 (GPIO 0) marks the entry known so it never returns; hold it for the backlight.
//
// No Wi-Fi and no host: the deck is compiled in and progress lives in NVS.

#include <Arduino.h>
#include <Preferences.h>
#include <TFT_eSPI.h>

// Only the debug-screenshot environment defines this, so the shipping build needs a
// default -- without one, the ternary in setup() simply fails to compile there.
#ifndef VOCAB_DEBUG
#define VOCAB_DEBUG 0
#endif

#include "deck.h"
#include "fonts/font_meaning.h"
#include "fonts/font_small.h"
#include "fonts/font_term_large.h"
#include "fonts/font_term_small.h"

// ---------------------------------------------------------------------------- hardware

static const int BUTTON_ADVANCE = 35;  // input-only pin; the board provides the pull-up
static const int BUTTON_KNOWN = 0;     // shared with the boot strap, hence INPUT_PULLUP
static const int SCREEN_W = 240;
static const int SCREEN_H = 135;
static const int MARGIN = 8;

// How long each side stays up. The front is shorter because it is a prompt, not reading.
static const uint32_t FRONT_MS = 4000;
static const uint32_t BACK_MS = 6000;

static const uint16_t COLOR_BG = 0x0000;
static const uint16_t COLOR_TERM = 0xFFFF;
static const uint16_t COLOR_MEANING = 0xFDA0;   // amber, the one warm note on the card
static const uint16_t COLOR_DIM = 0x8410;       // labels and the example
static const uint16_t COLOR_TRACK = 0x2124;     // the drained part of the timer bar
static const uint16_t COLOR_WORD = 0x2648;      // green accent for plain vocabulary
static const uint16_t COLOR_STRUCTURE = 0x059F;  // blue accent for sentence patterns

TFT_eSPI tft = TFT_eSPI();
TFT_eSprite canvas = TFT_eSprite(&tft);

// ------------------------------------------------------------------------------- state

// One byte per entry: the top bit marks "known, never show again", the rest counts how
// many times it has come up. 122 entries is 122 bytes, so the whole thing is one small
// NVS blob rather than a key per entry.
static const uint8_t KNOWN_BIT = 0x80;
static const uint8_t SEEN_MASK = 0x7F;
uint8_t progress[DECK_COUNT];

Preferences prefs;
bool progressDirty = false;
uint32_t lastFlushMs = 0;
// Seen counts tick every few seconds; committing each one would mean thousands of flash
// writes a day for information nobody reads in real time. Marking an entry known flushes
// immediately, because that is the one change worth losing nothing over.
static const uint32_t FLUSH_INTERVAL_MS = 5 * 60 * 1000;

// A shuffled bag drains before any entry repeats, which spreads exposure evenly without
// needing dates or scheduling. Known entries are left out when the bag is refilled.
uint16_t bag[DECK_COUNT];
uint16_t bagSize = 0;
uint16_t bagPos = 0;

int16_t current = -1;
bool showingBack = false;
uint32_t sideStartedMs = 0;
bool needsRender = true;

bool backlightOn = true;
bool lastAdvanceButton = HIGH;
bool lastKnownButton = HIGH;
uint32_t knownPressedAt = 0;
static const uint32_t HOLD_MS = 600;

// ------------------------------------------------------------------------------ helpers

static bool isKnown(uint16_t index) { return progress[index] & KNOWN_BIT; }
static uint8_t seenCount(uint16_t index) { return progress[index] & SEEN_MASK; }

static void noteSeen(uint16_t index) {
  if (seenCount(index) < SEEN_MASK) {
    progress[index] = (progress[index] & KNOWN_BIT) | (seenCount(index) + 1);
    progressDirty = true;
  }
}

static uint16_t remainingCount() {
  uint16_t n = 0;
  for (uint16_t i = 0; i < DECK_COUNT; i++) {
    if (!isKnown(i)) n++;
  }
  return n;
}

static void saveProgress() {
  prefs.putBytes("progress", progress, sizeof(progress));
  progressDirty = false;
  lastFlushMs = millis();
}

static void loadProgress() {
  prefs.begin("vocab", false);
  size_t stored = prefs.getBytesLength("progress");
  if (stored == sizeof(progress)) {
    prefs.getBytes("progress", progress, sizeof(progress));
  } else {
    // Either a first run or the deck changed size. Progress is keyed by array index here,
    // so a resized deck cannot be trusted -- start clean rather than mismatch entries.
    memset(progress, 0, sizeof(progress));
    saveProgress();
  }
}

static void refillBag() {
  bagSize = 0;
  for (uint16_t i = 0; i < DECK_COUNT; i++) {
    if (!isKnown(i)) bag[bagSize++] = i;
  }
  for (uint16_t i = bagSize; i > 1; i--) {  // Fisher-Yates
    uint16_t j = esp_random() % i;
    uint16_t t = bag[i - 1];
    bag[i - 1] = bag[j];
    bag[j] = t;
  }
  bagPos = 0;
}

static void nextCard() {
  if (bagPos >= bagSize) refillBag();
  if (bagSize == 0) {  // everything marked known
    current = -1;
  } else {
    current = bag[bagPos++];
    noteSeen(current);
  }
  showingBack = false;
  sideStartedMs = millis();
  needsRender = true;
}

static void markKnown() {
  if (current < 0) return;
  progress[current] |= KNOWN_BIT;
  saveProgress();  // the one change worth an immediate flash write
  refillBag();     // drop it from the rotation straight away
  nextCard();
}

// ---------------------------------------------------------------------------- rendering

// Greedy word wrap measured with textWidth rather than guessed from character counts:
// these are proportional fonts, so "mm" is nearly twice "ll" and counting characters
// overflows on some strings while wasting space on others.
static int wrapText(const char *text, int maxWidth, char lines[][64], int maxLines) {
  int count = 0;
  const char *cursor = text;
  char line[64] = "";

  while (*cursor != '\0' && count < maxLines) {
    const char *end = cursor;
    while (*end != '\0' && *end != ' ') end++;

    size_t wordLength = (size_t)(end - cursor);
    if (wordLength > 63) wordLength = 63;
    char word[64];
    memcpy(word, cursor, wordLength);
    word[wordLength] = '\0';

    char candidate[128];
    if (line[0] == '\0') snprintf(candidate, sizeof(candidate), "%s", word);
    else snprintf(candidate, sizeof(candidate), "%s %s", line, word);

    if (canvas.textWidth(candidate) <= maxWidth) {
      strlcpy(line, candidate, sizeof(line));
    } else if (line[0] != '\0') {
      strlcpy(lines[count++], line, 64);
      strlcpy(line, word, sizeof(line));
    } else {
      strlcpy(lines[count++], word, 64);  // single word wider than the screen
      line[0] = '\0';
    }

    cursor = end;
    while (*cursor == ' ') cursor++;
  }
  if (line[0] != '\0' && count < maxLines) strlcpy(lines[count++], line, 64);
  return count;
}

// Only one smooth font can be loaded per drawing target at a time, so every text run
// below is bracketed by loadFont/unloadFont. That is cheap here because text is redrawn
// only when the card or its side changes -- a few times every four seconds, not per frame.
static void drawHeader(const Entry &entry) {
  canvas.loadFont(fontSmall);
  canvas.setTextDatum(TL_DATUM);
  canvas.setTextColor(entry.type == TYPE_STRUCTURE ? COLOR_STRUCTURE : COLOR_WORD, COLOR_BG);
  canvas.drawString(entry.type == TYPE_STRUCTURE ? "structure" : "word", MARGIN, 6);

  char counter[16];
  snprintf(counter, sizeof(counter), "%u left", remainingCount());
  canvas.setTextDatum(TR_DATUM);
  canvas.setTextColor(COLOR_DIM, COLOR_BG);
  canvas.drawString(counter, SCREEN_W - MARGIN, 6);
  canvas.unloadFont();
}

// The term is set in the largest of the two sizes that actually fits, dropping to two
// lines only when even the smaller size cannot hold it on one. Terms range from "afford"
// to "there's no point in + V-ing", so a single fixed size would either clip the long
// ones or waste most of the screen on the short ones.
static void drawTerm(const Entry &entry) {
  const int usable = SCREEN_W - 2 * MARGIN;

  canvas.loadFont(fontTermLarge);
  if (canvas.textWidth(entry.front) <= usable) {
    canvas.setTextDatum(MC_DATUM);
    canvas.setTextColor(COLOR_TERM, COLOR_BG);
    canvas.drawString(entry.front, SCREEN_W / 2, 68);
    canvas.unloadFont();
    return;
  }
  canvas.unloadFont();

  canvas.loadFont(fontTermSmall);
  canvas.setTextColor(COLOR_TERM, COLOR_BG);
  canvas.setTextDatum(MC_DATUM);
  char lines[2][64];
  int count = wrapText(entry.front, usable, lines, 2);
  if (count == 1) {
    canvas.drawString(lines[0], SCREEN_W / 2, 68);
  } else {
    canvas.drawString(lines[0], SCREEN_W / 2, 56);
    canvas.drawString(lines[1], SCREEN_W / 2, 84);
  }
  canvas.unloadFont();
}

static void renderFront(const Entry &entry) {
  canvas.fillRect(0, 0, SCREEN_W, SCREEN_H, COLOR_BG);
  drawHeader(entry);
  drawTerm(entry);
}

static void renderBack(const Entry &entry) {
  canvas.fillRect(0, 0, SCREEN_W, SCREEN_H, COLOR_BG);
  const int usable = SCREEN_W - 2 * MARGIN;

  // The term stays on screen, small, so the answer never appears without its question.
  canvas.loadFont(fontSmall);
  canvas.setTextDatum(TL_DATUM);
  canvas.setTextColor(COLOR_DIM, COLOR_BG);
  canvas.drawString(entry.front, MARGIN, 6);
  canvas.unloadFont();

  // Meaning and example share what is left, so the meaning is laid out first and the
  // example takes whatever rows remain. A one-line meaning leaves room for two lines of
  // example; a three-line meaning leaves none, and dropping the example is the right
  // sacrifice.
  canvas.loadFont(fontMeaning);
  canvas.setTextDatum(TL_DATUM);
  canvas.setTextColor(COLOR_MEANING, COLOR_BG);
  char lines[3][64];
  int count = wrapText(entry.back, usable, lines, 3);
  int y = 30;
  for (int i = 0; i < count; i++) {
    canvas.drawString(lines[i], MARGIN, y);
    y += 26;
  }
  canvas.unloadFont();

  canvas.loadFont(fontSmall);
  canvas.setTextColor(COLOR_DIM, COLOR_BG);
  char example[2][64];
  int exampleCount = wrapText(entry.example, usable, example, 2);
  y += 6;
  for (int i = 0; i < exampleCount && y + 18 <= SCREEN_H - 6; i++) {
    canvas.drawString(example[i], MARGIN, y);
    y += 18;
  }
  canvas.unloadFont();
}

static void renderEmpty() {
  canvas.fillRect(0, 0, SCREEN_W, SCREEN_H, COLOR_BG);
  canvas.loadFont(fontMeaning);
  canvas.setTextDatum(MC_DATUM);
  canvas.setTextColor(COLOR_MEANING, COLOR_BG);
  canvas.drawString("Xong het roi", SCREEN_W / 2, 60);
  canvas.unloadFont();
  canvas.loadFont(fontSmall);
  canvas.setTextColor(COLOR_DIM, COLOR_BG);
  canvas.drawString("moi tu da duoc danh dau", SCREEN_W / 2, 88);
  canvas.unloadFont();
}

// Drawn straight to the panel after the sprite is pushed, so the bar can animate without
// re-rendering the text -- which would mean reloading fonts ten times a second.
static void drawTimerBar(uint32_t elapsed, uint32_t total) {
  const int h = 3, y = SCREEN_H - h;
  int filled = total ? (int)((uint64_t)SCREEN_W * elapsed / total) : 0;
  if (filled > SCREEN_W) filled = SCREEN_W;
  tft.fillRect(0, y, SCREEN_W - filled, h, COLOR_TRACK);
  if (filled > 0) tft.fillRect(SCREEN_W - filled, y, filled, h, COLOR_BG);
}

static void renderCard() {
  if (current < 0) {
    renderEmpty();
  } else if (showingBack) {
    renderBack(DECK[current]);
  } else {
    renderFront(DECK[current]);
  }
  canvas.pushSprite(0, 0);
}

// ------------------------------------------------------------------------------ buttons

static void readButtons() {
  bool advance = digitalRead(BUTTON_ADVANCE);
  if (lastAdvanceButton == HIGH && advance == LOW) {
    if (!showingBack && current >= 0) {
      showingBack = true;
      sideStartedMs = millis();
      needsRender = true;
    } else {
      nextCard();
    }
    delay(180);  // crude debounce; nothing else needs to happen mid-press
  }
  lastAdvanceButton = advance;

  bool known = digitalRead(BUTTON_KNOWN);
  if (lastKnownButton == HIGH && known == LOW) {
    knownPressedAt = millis();
  } else if (lastKnownButton == LOW && known == HIGH) {
    if (millis() - knownPressedAt >= HOLD_MS) {
      backlightOn = !backlightOn;
      digitalWrite(TFT_BL, backlightOn ? TFT_BACKLIGHT_ON : !TFT_BACKLIGHT_ON);
    } else {
      markKnown();
    }
  }
  lastKnownButton = known;
}

// -------------------------------------------------------------------------- debug console
#if VOCAB_DEBUG
static void dumpFramebuffer() {
  Serial.printf("<<<SCREEN %d %d\n", SCREEN_W, SCREEN_H);
  for (int y = 0; y < SCREEN_H; y++) {
    for (int x = 0; x < SCREEN_W; x++) Serial.printf("%04X", canvas.readPixel(x, y));
    Serial.println();
  }
}

static void debugSerial() {
  if (!Serial.available()) return;
  switch (Serial.read()) {
    case 'f': showingBack = false; needsRender = true; break;
    case 'b': showingBack = true;  needsRender = true; break;
    case 'n': nextCard(); break;
    case 'k': markKnown(); break;  // the button press, reachable from the host
    case 'r':
      // Wipes progress. Needed to undo test runs, and genuinely useful for starting the
      // deck over.
      memset(progress, 0, sizeof(progress));
      saveProgress();
      refillBag();
      nextCard();
      Serial.println("[reset] progress cleared");
      break;
    case 'q':
      Serial.printf("[state] card=%d id=%u side=%s left=%u heap=%u\n", current,
                    current >= 0 ? DECK[current].id : 0, showingBack ? "back" : "front",
                    remainingCount(), ESP.getFreeHeap());
      break;
    case 'j': {
      // Jump straight to a deck index, sent as three ASCII digits. The rotation is
      // shuffled, so without this there is no way to aim a capture at a specific entry --
      // and the entries worth capturing are the extremes, not whatever comes up next.
      char digits[4] = {0};
      for (int i = 0; i < 3; i++) {
        uint32_t deadline = millis() + 500;
        while (!Serial.available() && millis() < deadline) delay(1);
        digits[i] = Serial.available() ? Serial.read() : '0';
      }
      int index = atoi(digits);
      if (index >= 0 && index < DECK_COUNT) {
        current = index;
        showingBack = false;
        sideStartedMs = millis();
        needsRender = true;
      }
      break;
    }
    case 's':
      renderCard();
      // Draw the bar into the sprite for the dump only. In normal running it goes to the
      // panel after the sprite is pushed, so it can animate without reloading fonts ten
      // times a second -- but that also means it is absent from a plain capture.
      canvas.fillRect(0, SCREEN_H - 3, SCREEN_W / 3, 3, COLOR_TRACK);
      dumpFramebuffer();
      break;
    default: break;
  }
}
#endif

// -------------------------------------------------------------------------------- setup

void setup() {
  Serial.begin(VOCAB_DEBUG ? 460800 : 115200);

  pinMode(BUTTON_ADVANCE, INPUT);
  pinMode(BUTTON_KNOWN, INPUT_PULLUP);
  pinMode(TFT_BL, OUTPUT);
  digitalWrite(TFT_BL, TFT_BACKLIGHT_ON);

  tft.init();
  tft.setRotation(1);
  tft.fillScreen(COLOR_BG);

  canvas.setColorDepth(16);
  if (canvas.createSprite(SCREEN_W, SCREEN_H) == nullptr) {
    Serial.println("[boot] sprite allocation failed");
  }

  loadProgress();
  refillBag();
  nextCard();

  Serial.printf("[boot] vocab-display, %u entries, %u remaining, free heap=%u\n",
                DECK_COUNT, remainingCount(), ESP.getFreeHeap());
}

void loop() {
  readButtons();
#if VOCAB_DEBUG
  debugSerial();
#endif

  uint32_t now = millis();
  uint32_t total = showingBack ? BACK_MS : FRONT_MS;
  uint32_t elapsed = now - sideStartedMs;

  if (current >= 0 && elapsed >= total) {
    if (showingBack) {
      nextCard();
    } else {
      showingBack = true;
      sideStartedMs = now;
      needsRender = true;
    }
    elapsed = 0;
  }

  if (needsRender) {
    renderCard();
    needsRender = false;
  }
  drawTimerBar(elapsed, total);

  if (progressDirty && now - lastFlushMs >= FLUSH_INTERVAL_MS) saveProgress();

  delay(40);
}
