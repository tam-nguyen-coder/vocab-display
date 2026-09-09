// vocab-display -- an always-on vocabulary card for the TTGO T-Display.
//
// Each entry is a two-sided card. The front shows the English term alone, in the largest
// type that fits; several seconds later it flips to the Vietnamese meaning and an example.
// Nothing needs pressing: the pause on the front is there to give you a moment to recall
// the answer before it appears, which is a better loop than showing both halves at once.
//
// Button 1 (GPIO 35) advances -- flip, or next card if already flipped.
// Button 2 (GPIO 0) marks the entry known so it never returns; hold it for the backlight.
//
// The deck compiled into flash is the floor, not the plan: the board shows cards on power
// alone, and Wi-Fi only adds to that. When the host answers, the board runs from batches
// it serves and reports progress back; when it does not, the board keeps going on the
// compiled deck. A sleeping Mac means no new entries, never a blank screen.

#include <Arduino.h>
#include <ArduinoJson.h>
#include <ESPmDNS.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <TFT_eSPI.h>
#include <WiFi.h>

// Only the debug-screenshot environment defines this, so the shipping build needs a
// default -- without one, the ternary in setup() simply fails to compile there.
#ifndef VOCAB_DEBUG
#define VOCAB_DEBUG 0
#endif

// 1 puts the USB port on the left; 3 is the same landscape flipped 180 degrees, for when
// the cable needs to leave the other way. Set it in config.h.
#ifndef SCREEN_ROTATION
#define SCREEN_ROTATION 1
#endif

// Flipping the image does not move the buttons, so after a 180 turn the one that was under
// your left thumb is under your right. Whether that wants swapping depends on how the
// thing sits on the desk, which is not something the firmware can know -- hence a separate
// switch rather than tying it to the rotation.
#ifndef SWAP_BUTTONS
#define SWAP_BUTTONS 0
#endif

#include "config.h"
#include "deck.h"
#include "fonts/font_meaning.h"
#include "fonts/font_small.h"
#include "fonts/font_term_large.h"
#include "fonts/font_term_small.h"

// ---------------------------------------------------------------------------- hardware

// GPIO 35 is input-only and relies on the board's own pull-up; GPIO 0 is the boot strap,
// so it needs INPUT_PULLUP. That difference is why the two are configured separately below
// and why swapping them is a matter of which pin gets which job, not of renaming.
static const int PIN_TOP = 35;
static const int PIN_BOTTOM = 0;
static const int BUTTON_ADVANCE = SWAP_BUTTONS ? PIN_BOTTOM : PIN_TOP;
static const int BUTTON_KNOWN = SWAP_BUTTONS ? PIN_TOP : PIN_BOTTOM;
static const int SCREEN_W = 240;
static const int SCREEN_H = 135;
static const int MARGIN = 8;

// How long each side stays up. The front is shorter because it is a prompt to recall,
// not something to read; the back carries two to four lines and needs the time. The first
// pass at 4s/6s read as hurried on the actual device -- these are the two numbers most
// worth tuning by feel rather than by argument.
static const uint32_t FRONT_MS = 6000;
static const uint32_t BACK_MS = 10000;

static const uint16_t COLOR_BG = 0x0000;
static const uint16_t COLOR_TERM = 0xFFFF;
static const uint16_t COLOR_MEANING = 0xFDA0;    // amber, the one warm note on the card
static const uint16_t COLOR_DIM = 0x8410;        // labels and the example
static const uint16_t COLOR_TRACK = 0x2124;      // the drained part of the timer bar
static const uint16_t COLOR_WORD = 0x2648;       // green accent for plain vocabulary
static const uint16_t COLOR_STRUCTURE = 0x059F;  // blue accent for sentence patterns

TFT_eSPI tft = TFT_eSPI();
TFT_eSprite canvas = TFT_eSprite(&tft);

// -------------------------------------------------------------------------------- cards

// Both sources produce the same shape, so rendering never learns where a card came from.
// Offline cards point straight at the compiled deck's flash strings; fetched ones point
// into the arena below.
//
// The host serves one fixed set per day, so this only has to be larger than the biggest
// daily dose anyone would configure, not a slice of the deck.
static const int MAX_BATCH = 40;

struct Card {
  uint16_t id;
  uint8_t type;
  const char *front;
  const char *back;
  const char *example;
  int16_t deckIndex;  // -1 for a fetched card: only compiled entries have NVS progress
  uint8_t seen;       // this session, reported to the host and then cleared
  bool known;
};

Card cards[MAX_BATCH];
uint16_t cardCount = 0;
int16_t cardPos = -1;

// A bump allocator reset per batch. Fetched strings have to live somewhere for the life
// of the batch, and one arena avoids two dozen little heap allocations every refill.
static const size_t ARENA_SIZE = 10240;
char arena[ARENA_SIZE];
size_t arenaUsed = 0;

static const char *arenaCopy(const char *text) {
  size_t need = strlen(text) + 1;
  if (arenaUsed + need > ARENA_SIZE) return nullptr;
  char *out = arena + arenaUsed;
  memcpy(out, text, need);
  arenaUsed += need;
  return out;
}

// ------------------------------------------------------------------------ offline state

// One byte per compiled entry: the top bit marks "known, never show again", the rest
// counts how many times it has come up. 122 entries is 122 bytes, so the whole thing is
// one small NVS blob rather than a key per entry.
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
// needing dates or scheduling.
uint16_t bag[DECK_COUNT];
uint16_t bagSize = 0;
uint16_t bagPos = 0;

// ------------------------------------------------------------------------- online state

enum Source { SOURCE_OFFLINE, SOURCE_HOST };
Source source = SOURCE_OFFLINE;

IPAddress hostIp;
bool hostResolved = false;
uint16_t remainingReported = 0;
int fetchFailures = 0;
uint32_t lastFetchAttemptMs = 0;
// Set by the loop, acted on by nextCard(). Fetching where the timer notices it would
// swap the batch out from under a card that is still on screen; deferring to the next
// card boundary is the only place the source can change without cutting one short.
bool fetchPending = false;
// Long enough not to hammer a sleeping Mac, short enough that waking it is noticed within
// a card or two.
static const uint32_t FETCH_RETRY_MS = 20000;

// Progress is posted in small batches rather than per card: the host rewrites its whole
// store on every write, so one request per card would mean thousands of full-file writes
// a day for counts nobody watches that closely.
static const uint8_t REPORT_EVERY = 5;
uint8_t unreportedCards = 0;

char batchDate[12] = "";
// A day's set drains every few minutes, so a single failed refresh must not be treated as
// the host being gone -- otherwise one hiccup drops the board onto the full compiled deck
// and it starts showing words that are not in today's set at all.
int hostMisses = 0;
static const int HOST_MISS_LIMIT = 3;

// A failed report is retried on a cooldown rather than at the next opportunity. Against a
// host that refuses the connection the difference is invisible, but a *sleeping* Mac never
// answers the SYN at all, so every attempt burns the full connect timeout inside the
// render loop -- seven of those per batch froze the timer bar for about ten seconds.
uint32_t reportBackoffUntilMs = 0;
static const uint32_t REPORT_BACKOFF_MS = 60000;

// ------------------------------------------------------------------------------ display

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

static void noteSeenOffline(uint16_t index) {
  if (seenCount(index) < SEEN_MASK) {
    progress[index] = (progress[index] & KNOWN_BIT) | (seenCount(index) + 1);
    progressDirty = true;
  }
}

static uint16_t offlineRemaining() {
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
  if (prefs.getBytesLength("progress") == sizeof(progress)) {
    prefs.getBytes("progress", progress, sizeof(progress));
  } else {
    // Either a first run or the deck changed size. Offline progress is keyed by array
    // index, so a resized deck cannot be trusted -- start clean rather than mismatch.
    memset(progress, 0, sizeof(progress));
    saveProgress();
  }
}

// ------------------------------------------------------------------------- card sourcing

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

static void fillFromDeck() {
  arenaUsed = 0;
  cardCount = 0;
  for (int i = 0; i < MAX_BATCH; i++) {
    if (bagPos >= bagSize) refillBag();
    if (bagSize == 0) break;
    uint16_t index = bag[bagPos++];
    cards[cardCount++] = {DECK[index].id, DECK[index].type, DECK[index].front,
                          DECK[index].back, DECK[index].example, (int16_t)index, 0, false};
  }
  cardPos = -1;
  source = SOURCE_OFFLINE;
  remainingReported = offlineRemaining();
}

static bool resolveHost() {
  IPAddress literal;
  if (literal.fromString(VOCAB_HOST)) {  // an IP was configured; nothing to look up
    hostIp = literal;
    return true;
  }
  // Strip a trailing ".local" -- queryHost wants the bare name.
  char name[64];
  strlcpy(name, VOCAB_HOST, sizeof(name));
  char *dot = strstr(name, ".local");
  if (dot) *dot = '\0';
  IPAddress found = MDNS.queryHost(name, 2000);
  if (found == IPAddress()) return false;
  hostIp = found;
  Serial.printf("[mdns] %s -> %s\n", name, hostIp.toString().c_str());
  return true;
}

static bool httpRequest(const char *path, const char *body, JsonDocument *out) {
  HTTPClient http;
  char url[96];
  snprintf(url, sizeof(url), "http://%s:%u%s", hostIp.toString().c_str(), VOCAB_PORT, path);
  http.setConnectTimeout(1500);
  http.setTimeout(3000);
  http.setReuse(false);
  if (!http.begin(url)) return false;
  http.addHeader("X-Vocab-Token", VOCAB_TOKEN);

  int code;
  if (body) {
    http.addHeader("Content-Type", "application/json");
    code = http.POST((uint8_t *)body, strlen(body));
  } else {
    code = http.GET();
  }

  bool ok = false;
  if (code == 200) {
    ok = !out || !deserializeJson(*out, http.getStream());
    if (!ok) Serial.println("[host] response was not valid json");
  } else if (code == 401) {
    // A wrong token is a config mistake, not a flaky network. Say so once rather than
    // retrying forever and blaming the Wi-Fi.
    Serial.println("[host] token rejected -- check VOCAB_TOKEN against the host");
  } else {
    Serial.printf("[host] %s -> http %d\n", path, code);
  }
  http.end();
  return ok;
}

// Sends what has been shown since the last report. Merged host-side, so a lost response
// only means the same delta is sent again later.
static void reportProgress() {
  if (source != SOURCE_HOST || WiFi.status() != WL_CONNECTED || !hostResolved) return;
  if (reportBackoffUntilMs && (int32_t)(millis() - reportBackoffUntilMs) < 0) return;

  JsonDocument doc;
  JsonArray list = doc["cards"].to<JsonArray>();
  for (uint16_t i = 0; i < cardCount; i++) {
    if (cards[i].seen == 0 && !cards[i].known) continue;
    JsonObject item = list.add<JsonObject>();
    item["id"] = cards[i].id;
    item["seen"] = cards[i].seen;
    if (cards[i].known) item["known"] = true;
  }
  if (list.size() == 0) return;

  char body[1024];
  serializeJson(doc, body, sizeof(body));
  JsonDocument reply;
  if (httpRequest("/api/progress", body, &reply)) {
    remainingReported = reply["remaining"] | remainingReported;
    unreportedCards = 0;
    reportBackoffUntilMs = 0;
    // Cleared only on success, so an unsent delta is re-sent next time. The host merges,
    // taking the larger seen count and never un-setting known, so repeating a delta is
    // harmless -- but a mark made while the host is asleep is lost if the board reboots
    // before it syncs. The word simply comes back around to be marked again.
    for (uint16_t i = 0; i < cardCount; i++) cards[i].seen = 0;
  } else {
    reportBackoffUntilMs = millis() + REPORT_BACKOFF_MS;
  }
}

static bool fetchBatch() {
  if (WiFi.status() != WL_CONNECTED) return false;
  lastFetchAttemptMs = millis();
  if (!hostResolved) {
    hostResolved = resolveHost();
    if (!hostResolved) return false;
  }

  char path[48];
  snprintf(path, sizeof(path), "/api/batch?n=%d", MAX_BATCH);
  JsonDocument doc;
  if (!httpRequest(path, nullptr, &doc)) {
    if (++fetchFailures >= 3) {
      // Three misses and the address is assumed stale -- re-resolve next time, which is
      // how a host that moved to a new DHCP lease is picked up without a reflash.
      hostResolved = false;
      fetchFailures = 0;
    }
    return false;
  }

  fetchFailures = 0;
  arenaUsed = 0;
  cardCount = 0;
  strlcpy(batchDate, doc["date"] | "", sizeof(batchDate));
  for (JsonObject item : doc["cards"].as<JsonArray>()) {
    if (cardCount >= MAX_BATCH) break;
    const char *front = arenaCopy(item["f"] | "");
    const char *back = arenaCopy(item["b"] | "");
    const char *example = arenaCopy(item["e"] | "");
    if (!front || !back || !example) break;  // arena full: keep what fits
    cards[cardCount++] = {(uint16_t)(item["id"] | 0), (uint8_t)(item["t"] | 0),
                          front, back, example, -1, 0, false};
  }

  if (cardCount == 0) return false;
  remainingReported = doc["remaining"] | cardCount;
  cardPos = -1;
  source = SOURCE_HOST;
  unreportedCards = 0;
  hostMisses = 0;
  Serial.printf("[host] %s: %u cards, %u remaining\n", batchDate, cardCount,
                remainingReported);
  return true;
}

static void nextCard() {
  if (cardPos >= 0 && cardPos < (int16_t)cardCount) {
    cards[cardPos].seen++;  // reset per card on a successful report, so this is a delta
    if (cards[cardPos].deckIndex >= 0) noteSeenOffline(cards[cardPos].deckIndex);
    if (source == SOURCE_HOST && ++unreportedCards >= REPORT_EVERY) reportProgress();
  }

  cardPos++;
  bool drained = cardPos >= (int16_t)cardCount;
  if (drained || (source == SOURCE_OFFLINE && fetchPending)) {
    if (source == SOURCE_HOST) reportProgress();
    fetchPending = false;
    if (fetchBatch()) {
      cardPos = 0;
    } else if (source == SOURCE_HOST && ++hostMisses < HOST_MISS_LIMIT) {
      // Replay today's set rather than abandoning it. The cards are still in the arena
      // and still correct; only the refresh failed.
      cardPos = 0;
      Serial.printf("[host] refresh missed (%d/%d), replaying %s\n", hostMisses,
                    HOST_MISS_LIMIT, batchDate);
    } else if (drained) {
      fillFromDeck();
      cardPos = 0;
      hostMisses = 0;
    }
    // A failed fetch mid-batch while already offline changes nothing: the offline batch is
    // still good and cardPos has already advanced into it.
  }

  showingBack = false;
  sideStartedMs = millis();
  needsRender = true;
}

static void markKnown() {
  if (cardPos < 0 || cardCount == 0) return;
  Card &card = cards[cardPos];
  card.known = true;
  if (card.deckIndex >= 0) {
    progress[card.deckIndex] |= KNOWN_BIT;
    saveProgress();
    refillBag();
  }
  if (source == SOURCE_HOST) reportProgress();  // the one change worth an immediate round trip
  if (remainingReported > 0) remainingReported--;

  // Drop it from the rest of this batch so it cannot come back before the next refill.
  for (uint16_t i = cardPos + 1; i < cardCount; i++) cards[i - 1] = cards[i];
  cardCount--;
  cardPos--;
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
// only when the card or its side changes -- a few times every six seconds, not per frame.
static void drawHeader(const Card &card) {
  canvas.loadFont(fontSmall);
  canvas.setTextDatum(TL_DATUM);
  canvas.setTextColor(card.type == TYPE_STRUCTURE ? COLOR_STRUCTURE : COLOR_WORD, COLOR_BG);
  canvas.drawString(card.type == TYPE_STRUCTURE ? "structure" : "word", MARGIN, 6);

  char counter[16];
  snprintf(counter, sizeof(counter), "%u left", remainingReported);
  canvas.setTextDatum(TR_DATUM);
  canvas.setTextColor(COLOR_DIM, COLOR_BG);
  canvas.drawString(counter, SCREEN_W - MARGIN, 6);
  canvas.unloadFont();

  // A dot in the corner when running from the compiled deck. Deliberately not a word:
  // running offline is a normal state, not a fault, and does not deserve a banner.
  if (source == SOURCE_OFFLINE) {
    canvas.fillSmoothCircle(SCREEN_W - 5, SCREEN_H - 11, 2, COLOR_TRACK, COLOR_BG);
  }
}

// The term is set in the largest of the two sizes that actually fits, dropping to two
// lines only when even the smaller size cannot hold it on one. Terms range from "afford"
// to "there's no point in + V-ing", so a single fixed size would either clip the long
// ones or waste most of the screen on the short ones.
static void drawTerm(const Card &card) {
  const int usable = SCREEN_W - 2 * MARGIN;

  canvas.loadFont(fontTermLarge);
  if (canvas.textWidth(card.front) <= usable) {
    canvas.setTextDatum(MC_DATUM);
    canvas.setTextColor(COLOR_TERM, COLOR_BG);
    canvas.drawString(card.front, SCREEN_W / 2, 68);
    canvas.unloadFont();
    return;
  }
  canvas.unloadFont();

  canvas.loadFont(fontTermSmall);
  canvas.setTextColor(COLOR_TERM, COLOR_BG);
  canvas.setTextDatum(MC_DATUM);
  char lines[2][64];
  int count = wrapText(card.front, usable, lines, 2);
  if (count == 1) {
    canvas.drawString(lines[0], SCREEN_W / 2, 68);
  } else {
    canvas.drawString(lines[0], SCREEN_W / 2, 56);
    canvas.drawString(lines[1], SCREEN_W / 2, 84);
  }
  canvas.unloadFont();
}

static void renderFront(const Card &card) {
  canvas.fillRect(0, 0, SCREEN_W, SCREEN_H, COLOR_BG);
  drawHeader(card);
  drawTerm(card);
}

static void renderBack(const Card &card) {
  canvas.fillRect(0, 0, SCREEN_W, SCREEN_H, COLOR_BG);
  const int usable = SCREEN_W - 2 * MARGIN;

  // The term stays on screen, small, so the answer never appears without its question.
  canvas.loadFont(fontSmall);
  canvas.setTextDatum(TL_DATUM);
  canvas.setTextColor(COLOR_DIM, COLOR_BG);
  canvas.drawString(card.front, MARGIN, 6);
  canvas.unloadFont();

  // Meaning and example share what is left, so the meaning is laid out first and the
  // example takes whatever rows remain. A one-line meaning leaves room for two lines of
  // example; a three-line meaning leaves none, and dropping the example is the right
  // sacrifice.
  canvas.loadFont(fontMeaning);
  canvas.setTextDatum(TL_DATUM);
  canvas.setTextColor(COLOR_MEANING, COLOR_BG);
  char lines[3][64];
  int count = wrapText(card.back, usable, lines, 3);
  int y = 30;
  for (int i = 0; i < count; i++) {
    canvas.drawString(lines[i], MARGIN, y);
    y += 26;
  }
  canvas.unloadFont();

  canvas.loadFont(fontSmall);
  canvas.setTextColor(COLOR_DIM, COLOR_BG);
  char example[2][64];
  int exampleCount = wrapText(card.example, usable, example, 2);
  y += 6;
  for (int i = 0; i < exampleCount && y + 18 <= SCREEN_H - 6; i++) {
    canvas.drawString(example[i], MARGIN, y);
    y += 18;
  }
  canvas.unloadFont();

  if (source == SOURCE_OFFLINE) {
    canvas.fillSmoothCircle(SCREEN_W - 5, SCREEN_H - 11, 2, COLOR_TRACK, COLOR_BG);
  }
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
  if (cardPos < 0 || cardCount == 0) {
    renderEmpty();
  } else if (showingBack) {
    renderBack(cards[cardPos]);
  } else {
    renderFront(cards[cardPos]);
  }
  canvas.pushSprite(0, 0);
}

// ------------------------------------------------------------------------------ buttons

static void readButtons() {
  bool advance = digitalRead(BUTTON_ADVANCE);
  if (lastAdvanceButton == HIGH && advance == LOW) {
    if (!showingBack && cardCount > 0) {
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

// ------------------------------------------------------------------------ debug console
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
    case 'k': markKnown(); break;
    case 'h': Serial.printf("[fetch] %s\n", fetchBatch() ? "ok" : "failed"); needsRender = true; break;
    case 'p': reportProgress(); Serial.println("[report] sent"); break;
    case 'o': fillFromDeck(); nextCard(); Serial.println("[source] forced offline"); break;
    case 'r':
      // Wipes offline progress. Needed to undo test runs, and useful for starting over.
      memset(progress, 0, sizeof(progress));
      saveProgress();
      refillBag();
      fillFromDeck();
      nextCard();
      Serial.println("[reset] offline progress cleared");
      break;
    case 'q':
      Serial.printf("[state] src=%s date=%s pos=%d/%u id=%u side=%s left=%u misses=%d "
                    "wifi=%d ip=%s heap=%u\n",
                    source == SOURCE_HOST ? "host" : "deck",
                    batchDate[0] ? batchDate : "-", cardPos, cardCount,
                    cardPos >= 0 && cardCount ? cards[cardPos].id : 0,
                    showingBack ? "back" : "front", remainingReported, hostMisses,
                    WiFi.status(), WiFi.localIP().toString().c_str(), ESP.getFreeHeap());
      break;
    case 'j': {
      // Jump straight to a batch position, sent as three ASCII digits. Captures are worth
      // aiming, and the rotation is shuffled.
      char digits[4] = {0};
      for (int i = 0; i < 3; i++) {
        uint32_t deadline = millis() + 500;
        while (!Serial.available() && millis() < deadline) delay(1);
        digits[i] = Serial.available() ? Serial.read() : '0';
      }
      int index = atoi(digits);
      if (index >= 0 && index < (int)cardCount) {
        cardPos = index;
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

  pinMode(PIN_TOP, INPUT);            // input-only, pulled up on the board
  pinMode(PIN_BOTTOM, INPUT_PULLUP);  // boot strap pin, needs the internal pull-up
  pinMode(TFT_BL, OUTPUT);
  digitalWrite(TFT_BL, TFT_BACKLIGHT_ON);

  tft.init();
  tft.setRotation(SCREEN_ROTATION);
  tft.fillScreen(COLOR_BG);

  canvas.setColorDepth(16);
  if (canvas.createSprite(SCREEN_W, SCREEN_H) == nullptr) {
    Serial.println("[boot] sprite allocation failed");
  }

  loadProgress();
  refillBag();
  // Show a card before touching the network. Joining Wi-Fi takes seconds and resolving
  // the host takes more; none of that should stand between power and the first word.
  fillFromDeck();
  nextCard();

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  MDNS.begin("vocab-display");

  Serial.printf("[boot] vocab-display, %u compiled entries, %u remaining, "
                "rotation=%d swap=%d free heap=%u\n",
                DECK_COUNT, offlineRemaining(), SCREEN_ROTATION, SWAP_BUTTONS,
                ESP.getFreeHeap());
}

void loop() {
  readButtons();
#if VOCAB_DEBUG
  debugSerial();
#endif

  uint32_t now = millis();
  uint32_t total = showingBack ? BACK_MS : FRONT_MS;
  uint32_t elapsed = now - sideStartedMs;

  if (cardCount > 0 && elapsed >= total) {
    if (showingBack) {
      nextCard();
    } else {
      showingBack = true;
      sideStartedMs = now;
      needsRender = true;
    }
    elapsed = 0;
  }

  // While offline, keep asking -- but only raise the flag here. nextCard() does the
  // fetching, so the switch lands on a card boundary instead of mid-flip.
  if (source == SOURCE_OFFLINE && WiFi.status() == WL_CONNECTED && !fetchPending &&
      now - lastFetchAttemptMs >= FETCH_RETRY_MS) {
    lastFetchAttemptMs = now;
    fetchPending = true;
  }

  if (needsRender) {
    renderCard();
    needsRender = false;
  }
  drawTimerBar(elapsed, total);

  if (progressDirty && now - lastFlushMs >= FLUSH_INTERVAL_MS) saveProgress();

  delay(40);
}
