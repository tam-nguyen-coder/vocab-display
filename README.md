<h1 align="center">vocab-display</h1>

<p align="center">
  <b>A vocabulary card that lives on your desk, not in another app you forget to open.</b>
</p>

<p align="center">
  One small set of English words and sentence patterns each day, with Vietnamese meanings,<br>
  on a $10 screen that is simply always on.
</p>

<p align="center">
  <img alt="ESP32" src="https://img.shields.io/badge/ESP32-TTGO%20T--Display-E8842A">
  <img alt="Firmware" src="https://img.shields.io/badge/firmware-PlatformIO-orange">
  <img alt="Host" src="https://img.shields.io/badge/host-Python%20%2B%20MongoDB-3776AB">
  <img alt="Run" src="https://img.shields.io/badge/run-uv-DE5FE9">
  <img alt="Flash" src="https://img.shields.io/badge/flash-38%25%20of%203MB-4c1">
</p>

<p align="center">
  <img src="resources/hero.png" alt="Two cards on the device: a word tagged v, n with its Vietnamese meaning and example, and a sentence pattern below it" width="760">
</p>

---

Reviewing vocabulary fails on the opening step: opening the app. This removes that step.
The screen sits next to the keyboard showing one word, pauses long enough for you to try
to recall the meaning, then reveals it — and keeps doing that all day whether or not you
were paying attention.

## Why it works the way it does

- 📅 **One set a day, repeated.** Twelve entries — eight words, four patterns, interleaved
  — cycle in about three minutes. A day at the desk shows each one dozens of times.
  Tomorrow draws a different set. The deck is deliberately *not* raced through.
- 🏷️ **Words carry their part of speech.** `approach` is stored `n,v` and shown as
  `noun, verb`, so the noun and the verb stop hiding behind one Vietnamese gloss — and the
  deck filters by word class.
- 🔤 **Real Vietnamese, not stripped accents.** `đã quen với việc`, not `da quen voi viec`.
  Tone marks stay legible down to 16px.
- 🧠 **It knows what you already know.** One button press marks an entry known and it never
  returns; a replacement joins the day so the dose stays the same.
- 🔌 **It works on power alone.** The starter deck is compiled into the firmware. No Wi-Fi,
  no host, no account needed to get value out of it.
- 🌐 **A web page when you want one.** Run the host and the deck becomes searchable,
  editable in place, and importable in bulk — one Python file, no npm and no build step.
- 🗄️ **The deck lives in MongoDB, and a copy lives on disk.** The database is the source of
  truth; `data/store.json` is a mirror refreshed on a timer, so a dead link leaves the deck
  browsable rather than blank.

## Try it in five minutes

```bash
cp include/config.example.h include/config.h    # Wi-Fi and host, optional
PY=python3 tools/gen_assets.sh                  # build the deck and the fonts
pio run -t upload
```

That is the whole offline device: 122 entries in flash, progress in NVS. **It needs no
host, no database and no network to be useful** — everything below is additive.

For the web UI, put a connection string in `.env` (or `atlas-credentials.env`; either name
is read, and both are gitignored):

```bash
MONGODB_URI = mongodb+srv://user:password@cluster.example.mongodb.net
MONGODB_DB  = vocab_display          # optional, this is the default
```

```bash
uv run host/server.py         # http://localhost:8788
```

There is no install step: the dependency block at the top of `host/server.py` is
[PEP 723](https://peps.python.org/pep-0723/) inline metadata, and `uv` builds the
environment on the first run. Coming from the JSON-file version, move the deck across
once — it verifies every field against the file before it says it is done, and never
modifies the file:

```bash
uv run tools/migrate_to_mongo.py            # dry run, says what it would do
uv run tools/migrate_to_mongo.py --apply
```

<p align="center">
  <img src="resources/autosize.png" alt="A short pattern in the large face beside a long one wrapped in the smaller face" width="720">
</p>

<p align="center"><sub>Terms range from <code>afford</code> to <code>there's no point in + V-ing</code>, so the
type size is chosen per card — the largest face that fits, dropping a size and wrapping only when it has to.</sub></p>

## One set a day

The deck is not worked through front to back. Each day gets one fixed set and the board
repeats it all day; tomorrow draws another, chosen from whatever has been practised least.

`GET /api/batch` is idempotent for exactly this reason: the board re-asks every few minutes
and every answer has to be the same set, or the day stops being a day. The day boundary is
the host's local date, since the host runs on the same machine as the person.

**`days` is the only progress number, and that is on purpose.** An earlier version also
counted showings, which passed a hundred within hours and distinguished nothing: a card
stared at forty times in one afternoon has been practised once, not forty times. `days`
counts the distinct days an entry has appeared, is incremented by the host exactly once
when it builds a set, and is what tomorrow's set is chosen by. Nothing counts screen time.

Marking an entry known pulls it out of today's set and a replacement is drawn, so the day
keeps its size. Changing the dose reshapes the current day immediately rather than waiting
for tomorrow — quotas are honoured per type, which is not the same as honouring the total:
filling from one mixed pool turned a requested 8 + 4 into 9 + 3.

## Where the data lives

MongoDB holds two collections. `entries` uses the entry id as `_id`, so "ids are never
reused" stops being a rule the code has to remember and becomes one the database enforces.
`meta` is a single document holding the API token, the daily quotas, and today's set.

| Field | |
|-------|---|
| `_id` / `id` | Stable integer, never reused — progress is keyed off it |
| `type` `level` `pos` `front` `back` `example` | The card itself |
| `days` | Distinct days this entry has appeared. The only progress count kept |
| `known` | Sticky. Set by the board or the web UI, cleared only by the web UI |
| `updated` | Last write, ISO-8601 UTC |

`data/store.json` mirrors all of it every `VOCAB_MIRROR_SECONDS` (default 300) and only
when something actually changed, so a quiet host writes nothing. **The mirror is read-only
insurance**: if the database cannot be reached the host serves the deck from it and answers
`503` to anything that would write, rather than accepting an edit it cannot keep.

## Vietnamese needs a real font

TFT_eSPI's built-in fonts are ASCII only: no `ă â ê ô ơ ư đ`, no tone marks. Vietnamese
renders as blanks or boxes. The fix is a smooth **VLW** font converted from a TTF with the
Vietnamese range included.

TFT_eSPI's own converter is a Processing sketch — a Java IDE to produce one binary file —
so [`tools/ttf2vlw.py`](tools/ttf2vlw.py) does the same job from the command line, and
regenerating a font after changing a size stays part of the normal build.

| Size | Character set | Used for | Cost |
|-----:|---------------|----------|-----:|
| 42px | ASCII | the term, when it fits | 61 KB |
| 26px | ASCII | long terms, wrapped | 25 KB |
| 22px | + Vietnamese | the meaning | 57 KB |
| 15px | + Vietnamese | labels and the example | 30 KB |

Fronts are always English, so those sizes carry 95 glyphs instead of 229 — which is where
most of the saving comes from. `TTF=` in
[`tools/gen_assets.sh`](tools/gen_assets.sh) selects the source font; something designed
for Vietnamese, like Be Vietnam Pro, places tone marks better at small sizes than a
general-purpose face.

## The deck

[`data/seed.tsv`](data/seed.tsv) is the starting knowledge base — 122 entries of
high-frequency English with Vietnamese meanings, and the only ones compiled into the
firmware. The live deck is larger and lives in the database; see
[Importing a word list](#importing-a-word-list). Tab-separated because Vietnamese meanings
are full of commas and quoting them in CSV buys nothing.

| Column | Meaning |
|--------|---------|
| `id` | Stable integer. **Never reuse or renumber** — progress is keyed off it |
| `type` | `word` or `structure` (patterns like `used to + V`, `end up + V-ing`) |
| `level` | `A2` / `B1` / `B2` |
| `pos` | Part of speech, words only. Stored short, shown in full |
| `front` | The English term or pattern |
| `back` | Vietnamese meaning |
| `example` | One short English sentence |

It covers both plain vocabulary and the patterns that trip learners up more than single
words do — `be used to + V-ing` versus `used to + V`, `so ... that`,
`there's no point in + V-ing`.

**A word can carry more than one part of speech**, comma-separated, because plenty of them
do and the meaning column had been quietly absorbing the difference: `approach` is
`cách tiếp cận; tiến lại gần`, which is a noun and a verb sharing one cell. Tagging it
`n,v` puts that on the card instead of leaving it to be inferred, and a word tagged `n,v`
answers to a filter for either. A structure carries no part of speech at all — `used to + V`
is a pattern, not a word class — so the column is always empty for one, whatever gets
typed into it.

**Stored short, shown in full.** `n,v` is what the database keeps, what a filter matches
and what the firmware links into flash; `noun, verb` is what a person ever reads, on the
panel and in the table alike. Both forms are accepted on input, so `noun` typed into the
web UI and `prep., adv.` pasted out of a word list land on the same value. The tags are
`n` `v` `adj` `adv` `prep` `conj` `det` `pron` `num` `exclam` `art` `phr v` `modal v`
`aux v` — the set Oxford's own lists use, so an import arrives with nowhere to lose a
category.

## Importing a word list

The starter deck is 122 hand-written entries. Anything larger arrives as a TSV of
`front  pos  level  back  example` and goes into the database:

```bash
uv run tools/import_tsv.py data/oxford-b1.tsv            # dry run
uv run tools/import_tsv.py data/oxford-b1.tsv --apply
```

Ids continue from the highest in use and are never reused, entries already in the deck are
skipped by term, and every new row is read back and compared before the tool reports
success. **Imports do not touch `data/seed.tsv`.** The compiled deck is the board's offline
floor and is deliberately small: growing it changes `DECK_COUNT`, and `loadProgress()`
wipes every saved mark whenever that number moves.

Four TSVs in `data/` came from the published Oxford lists, which give a term, a part of
speech and a CEFR level — and nothing else. **The two columns that make a card are the
work**: every Vietnamese meaning and every example sentence was written for this deck.

| File | From | Added |
|------|------|------:|
| `oxford-a2.tsv` | Oxford 3000, A2 | 773 |
| `oxford-b1.tsv` | Oxford 3000, B1 | 778 |
| `oxford-b2.tsv` | Oxford 3000, B2 | 621 |
| `oxford-phrases.tsv` | Oxford Phrase List, A1–C1 | 747 |

Oxford lists a headword once per band, so 52 B2 terms collided with an A2 or B1 entry
already in the deck — `amount` the noun against `amount` the verb, `patient` the noun
against `patient` the adjective. Those were merged rather than skipped: the part of speech
gains the second tag and the meaning gains the second sense, which is exactly the `n,v`
shape the deck already used for `approach`.

The phrase list needed its own parser. Its PDF is four columns of headwords with
collocations indented under them, and a plain text extraction interleaves the two into one
alphabetical-looking soup; reading word positions instead recovers 751 headwords against
the 750 the document claims.

## How the board and host share the work

```
   browser ──▶ host (Python + pymongo) ──▶  MongoDB          the source of truth
                    ▲                └──▶  data/store.json  a mirror, on a timer
                    │  GET  /api/batch     today's set
                    │  POST /api/progress  what was marked known
                    │
                  board ────  122 entries compiled into flash = the floor
```

**The compiled deck is the floor, not a fallback bolted on afterwards.** The board renders
its first card before Wi-Fi is even attempted, because joining a network takes seconds and
resolving a host takes more, and none of that belongs between power and the first word.

**A failed refresh replays today's set rather than abandoning it.** A day's set drains
every few minutes, so treating one miss as "the host is gone" would drop the board onto the
full compiled deck and start showing words outside today's set. It takes three consecutive
misses to fall back.

**Source changes land on card boundaries.** The loop only raises a flag; `nextCard()` does
the fetching. Fetching where the timer notices it would swap the batch out from under a
card still on screen.

**The board reports marks, and nothing else.** A card going past is not news — the host
already knows which entries are in today's set, and it is the host that decides a day has
happened. So `/api/progress` carries only "this one is known", which makes a repeated
report exactly idempotent: a reply that gets lost can be re-sent without counting anything
twice. A failed report backs off for a minute, because a refused connection answers
instantly but a sleeping Mac never answers at all, and seven attempts per cycle froze the
timer bar for about ten seconds.

**`known` is never un-set by the board.** The board sets it, the host stores it, and only
the web UI can take it back — otherwise un-ticking an entry in the browser just saw the
board assert it again a few cards later.

**A write that cannot reach the database fails loudly.** The host answers `503` rather than
pretending; the board counts that as a miss and falls back to its compiled deck after three
of them. A stale day served from a mirror would be worse than an honest gap.

**The store is cached in the process, and only what changed is written back.** Every
handler works on the whole store, so every request once pulled every document out of
Atlas: unnoticeable at 124 entries, about a second and a half at 900. This host is the only
writer, so its own copy stands between writes, with a five-second TTL as a backstop for
anything else touching the same database. Writes diff against that copy — a board report
reshapes a dozen entries and used to rewrite nine hundred.

A dot appears in the corner while running from the compiled deck. Deliberately not a word:
offline is a normal state, not a fault, and does not deserve a banner.

## Why the board's memory is not the constraint

The obvious worry is whether an ESP32 can hold a growing knowledge base plus progress.
Measured against the real seed file, one entry averages **70 bytes**:

| Entries | Content | Progress | Fits in board RAM? |
|--------:|--------:|---------:|:-------------------|
| 122 | 8 KB | 1.0 KB | yes — this is the compiled floor |
| 3,043 | 208 KB | 24 KB | no — and it does not need to |
| 10,000 | 682 KB | 78 KB | no |

The deck passed that line the moment the Oxford lists went in, and nothing about the board
changed. **A day's set of twelve costs under a kilobyte**, so storage size stopped being a
firmware problem and became a host problem: 3,043 entries live in MongoDB, of which 122 are
compiled into flash as the floor the board drops to when the host is gone.

## Measured on the device

| | |
|---|---|
| Flash | 38% of 3 MB (`huge_app`), of which 173 KB is font data |
| Free heap | 165 KB with Wi-Fi up, flat over a long run |
| Vietnamese | legible from 16px; tone marks and `đ` stay distinct |
| Part of speech | `claim` reads `v, n` in the header where `word` used to be; a pattern still reads `structure` |
| Longest term | `there's no point in + V-ing` drops a size and wraps to two lines |
| Longest meaning | `có đủ khả năng (tài chính) để làm gì` wraps to two lines and still leaves room for a two-line example |
| Progress | survives a reboot — marked 3 known, power-cycled, still 119 remaining |
| Daily set | idempotent across repeated requests; a simulated rollover drew 12 fresh entries with zero overlap |
| Host loss | two drained cycles replay today's set, the third falls back to the compiled deck |
| Database loss | host serves the deck read-only from the mirror; every write answers `503` |
| Deck at 3,043 | first request loads the whole store in ~2s, every one after it in ~8ms |

Wi-Fi, HTTPClient, mDNS and ArduinoJson together put the app at 92% of the default 1.31 MB
partition — too little headroom to add anything — so `platformio.ini` switches to
`huge_app`, trading the second OTA slot for a ~3 MB single app partition. That costs nothing
on a board flashed over USB.

## Working on the layout

135 pixels of height goes fast, and squinting at a 1.14" panel is a poor way to find out
whether a long term overflowed. The `debug-screenshot` build streams the framebuffer back
over serial, so every screenshot in this README is real pixels off the device:

```bash
pio run -e debug-screenshot -t upload
uv run tools/screenshot.py /dev/cu.usbserial-XXXX 'card-{}.png' --sides front,back
```

Debug console: `f`/`b` pick the side, `n` next card, `j###` jumps to a position in the set,
`h` forces a fetch, `p` forces a report, `o` forces offline, `k` marks known, `v` rotates
the screen, `r` clears progress, `q` reports state and free heap, `s` dumps the framebuffer.

Capture everything you want in one invocation — opening the port resets the board on this
hardware, so a separate run per shot means a reboot per shot and most land on a device
still joining the network. The tool waits for the boot banner and fails loudly rather than
handing back a picture of a half-booted screen.

## The host API

Browser requests come from the machine itself and carry no token; the board's two endpoints
do. The token exists so nothing else on the Wi-Fi can read or rewrite the deck — it travels
in cleartext over HTTP on a home network and is not real authentication.

| Endpoint | Who | Does |
|----------|-----|------|
| `GET /api/batch?n=` | board | today's set — the same answer all day |
| `POST /api/progress` | board | reports what it showed and what was marked known |
| `GET /api/entries` | browser | search, multi-value filters, sort, paged |
| `PUT`/`DELETE /api/entries/<id>` | browser | edit in place, or remove |
| `POST /api/entries` | browser | add one |
| `POST /api/import` | browser | paste tab- or pipe-separated lines in bulk (`front`, `back`, `example`, `pos`) |
| `POST /api/config` | browser | how many words and patterns a day holds |
| `POST /api/rebuild-daily` | browser | discard today's set and draw another |
| `GET /api/stats` | browser | counts, today's set, and the token |

Entry ids are the MongoDB `_id`, which is what makes "never reuse an id" a rule the
database enforces rather than one the code remembers: progress is keyed off them, and a
recycled id would silently inherit the history of a deleted entry.

The mirror is written through a temp file and renamed. A half-written mirror is worse than
a stale one — it is the only thing standing between a dead link and a blank page — and
rename is the only atomic step.

## The web UI

Filters combine: several types, several parts of speech, several levels, several statuses
at once, plus a today's-set toggle. Any column can be sorted from its header. Page size is 25 to 200.

**The whole view lives in the URL.** Filters, sort, page and page size are query
parameters, so a view survives a reload, can be bookmarked, and gets browser back and
forward for free — which `localStorage` would not have given.

Search is accent- and case-insensitive, so `tiep can` finds `tiếp cận`; typing Vietnamese
diacritics to look something up is exactly the friction this project exists to remove.

| Syntax | Does |
|--------|------|
| `word another` | both must match — adding a word narrows |
| `front:used` | scope to one column (`back:`, `ex:`, `pos:` too) |
| `"the more"` | keep a phrase together |
| `-fair` | exclude |

Matches are highlighted in place, respecting the scope: `front:used` does not mark up the
example column, because the query never looked there. Cells edit in place — they read as
plain text until focused — and commit on blur or Enter, with Escape to abandon.

## Hardware

A [TTGO T-Display](https://www.espboards.dev/esp32/lilygo-ttgo-t-display-1-14/) — ESP32
with a 1.14" 135×240 ST7789. Around $10.

It has three buttons: two large ones on the face, and a smaller **RST** on the side edge.

| Button | Tap | Hold |
|--------|-----|------|
| GPIO 35 | advance — front reveals the back, back moves to the next card | 1.5s: **rotate the screen 180°** |
| GPIO 0 | toggle the backlight | 0.8s: **mark known** — the entry never returns |
| RST (side edge) | reboot | — |

Left alone the board paces itself: six seconds on the front, ten on the back. The advance
button only moves faster than that; it is not the only way through the set.

Both buttons act on *release*, not press — that is what lets one button carry both a tap
and a hold, and the delay is imperceptible for a tap. A press shorter than 40ms is
discarded as electrical noise rather than read as a very fast finger, and nothing is acted
on at all for the first 1.5 seconds after boot: a strap pin shared with the bootloader is
not a clean button and does not deserve to be trusted while the board is still settling.

Marking known sits behind a hold, not a tap, because GPIO 0 is the boot-strap pin and is
driven by the USB bridge's DTR line. Opening a serial port produces a pulse on it that
looks exactly like a deliberate press, and it silently deleted words from the deck until
the mapping was inverted. Pulse-length guards were tried first and one still got through;
what fixed it was putting the harmless action on the easy gesture, so the worst a phantom
pulse can now do is blink the backlight.

**GPIO 0 is also the BOOT strap pin, and that is worth knowing before it costs you an
afternoon.** Hold it while the board resets — pressing it along with RST, or having it
down as the USB cable goes in — and the ESP32 comes up in the ROM's UART download mode
instead of running the app: black screen, both buttons dead, nothing at all on serial. It
looks precisely like a dead board and is not one. Press RST on its own, or replug the
cable without touching the face buttons, and it boots normally. `esptool` talks to the
chip in either state, which is the fastest way to tell a board in download mode from one
that has actually failed.

The rotation is remembered, so it survives a reboot and a reflash. `SCREEN_ROTATION` in
`config.h` only sets the starting value. Flipping the image does not move the buttons, so
`SWAP_BUTTONS` exists separately for when the one under your thumb changed sides.

It runs from USB. A 3.7V LiPo with a **protection circuit** and a JST 1.25 connector makes
it portable — the board's TP4054 only charges and has no low-voltage cutoff, so a bare cell
will be drained past the point of damage. Check the connector polarity with a meter before
plugging anything in; it is not standardised, and reversed polarity kills the board.

## Sibling project

[claude-tracker](https://github.com/tam-nguyen-coder/claude-tracker) drives the same board
as a Claude Code status light. The firmware here borrows its TFT setup, text wrapping and
screenshot tooling; they are separate repos because they share hardware, not purpose.
