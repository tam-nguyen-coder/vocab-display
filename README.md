# vocab-display

An always-on vocabulary display for a TTGO T-Display (ESP32 + 135×240 ST7789). It sits on
the desk and cycles through English words and sentence patterns with Vietnamese meanings,
skipping whatever you've already marked as known.

Sibling project to [claude-tracker](https://github.com/tam-nguyen-coder/claude-tracker),
which drives the same board as a Claude Code status light. The firmware borrows that
project's TFT setup, gradient backdrop, text-wrapping and screenshot tooling; the two are
deliberately separate repos because they share hardware, not purpose.

## The deck

`data/seed.tsv` is the starting knowledge base — 122 entries of high-frequency English
with Vietnamese meanings. Tab-separated because Vietnamese meanings contain commas and
quoting them in CSV is a needless trap.

| Column | Meaning |
|--------|---------|
| `id` | Stable integer. **Never reuse or renumber** — per-entry progress is keyed off it |
| `type` | `word` or `structure` (patterns like `used to + V`, `end up + V-ing`) |
| `level` | `A2` / `B1` / `B2`, for filtering later |
| `front` | The English word or pattern |
| `back` | Vietnamese meaning |
| `example` | One short English sentence |

It covers both plain vocabulary and the sentence patterns that trip learners up more than
individual words do — `be used to + V-ing` versus `used to + V`, `so ... that`,
`there's no point in + V-ing`.

## Why the board's memory is not the constraint

The obvious worry is whether an ESP32 can hold a growing knowledge base plus per-entry
progress. Measured against the real seed file, one entry averages **70 bytes**:

| Entries | Content | Progress state | Fits in board RAM? |
|--------:|--------:|---------------:|:-------------------|
| 122 | 8 KB | 1.0 KB | yes |
| 1,000 | 68 KB | 7.8 KB | yes, but eats 40% of free RAM |
| 3,000 | 205 KB | 23 KB | no |
| 10,000 | 682 KB | 78 KB | no |

Free RAM while running is about **172 KB** (measured on the same board under
claude-tracker, with Wi-Fi up and a full-screen sprite allocated).

So a few thousand entries genuinely will not fit — but they never need to. **A working
window of 30 entries costs 2 KB.** The board fetches a batch, shows it, and asks for
more. Storage size stops being a firmware problem and becomes a host problem, where a
plain file is the whole answer.

Progress state is 8 bytes per entry (`id`, times seen, status, last-seen date). Even
10,000 entries is 78 KB of state — small enough to keep in one file, versioned in git.

## The card flips, on a timer

Each entry is shown as a two-sided card rather than one crowded screen:

```
front  ~4s  --auto-->  back  ~6s  -->  next card
(term)                 (meaning + example)
```

Showing everything at once means the term itself only gets about 26px of height, because
the meaning and example need the rest. Flipping gives each side the whole screen, which
roughly doubles the type size available to both — the term gets ~44px, the meaning ~24px.

Flipping on a timer rather than on a button press keeps the device passive: it runs
unattended like a clock. The four seconds of front-only turn out to be the useful part —
they are a pause to recall the meaning before the answer appears, which beats reading both
halves simultaneously. A thin bar drains along the bottom edge so the reveal is paced
rather than sudden.

Buttons only override the timer:

| Input | Action |
|-------|--------|
| Button 1 | Advance: flip if showing the front, next card if showing the back |
| Button 2 | Mark known — never show this entry again |
| Button 2 (hold) | Toggle the backlight |

### Vertical budget for the back, the tighter side

| Height | Element |
|-------:|---------|
| 8 px | type label and counter |
| 16 px | the term again, small, so you don't lose track of the question |
| 48 px | meaning, 24px over two lines |
| 28 px | example, 14px over two lines |

With gaps that lands near 126 px of 135. It fits, but the figures come from assuming a
character advance of about half the font height; they have to be **re-measured against real
pixels** once the VLW font is in. The longest meaning in the seed deck,
`có đủ khả năng (tài chính) để làm gì` at 36 characters, is the worst case to test with.

## Vietnamese needs a real font

TFT_eSPI's built-in fonts are ASCII only: no `ă â ê ô ơ ư đ`, no tone marks. Vietnamese
meanings render as blanks or boxes. The fix is a smooth **VLW** font converted from a TTF
with the Vietnamese range included — supported by the library, roughly 30 KB of flash per
size. This is a build step, not an afterthought.

## Building it

```bash
PY=path/to/python tools/gen_assets.sh   # regenerate deck.h and the four fonts
pio run -t upload
```

`tools/gen_assets.sh` is the only step to repeat after editing `data/seed.tsv` or changing
a font size. `tools/ttf2vlw.py` and `tools/gen_deck.py` can also be run individually.

### Checking a layout

135 pixels of height goes fast, and squinting at a 1.14" panel is a poor way to find out
whether a long term overflowed. The `debug-screenshot` build streams the framebuffer back
over serial:

```bash
pio run -e debug-screenshot -t upload
python3 tools/screenshot.py /dev/cu.usbserial-XXXX 'card-{}.png' --sides front,back
```

Debug console: `f`/`b` pick the side, `n` next card, `j###` jumps to a deck index,
`k` marks known, `r` clears all progress, `q` reports state and free heap, `s` dumps the
framebuffer. Aim captures with `j` — the rotation is shuffled, and the entries worth
looking at are the extremes rather than whatever comes up next.

## One set a day

The deck is not worked through front to back. Each day gets one fixed set — by default
eight words and four sentence patterns, interleaved — and the board repeats that set all
day. Tomorrow draws a different one, chosen from whatever has been practised least.

Twelve entries cycle in about three minutes at the device's pacing, so a day at the desk
shows each one dozens of times. That repetition is the point; it is the reason the deck is
not raced through.

`GET /api/batch` is idempotent for exactly this reason: the board re-asks every few
minutes and every answer has to be the same set, or the day stops being a day. The day
boundary is the host's local date, since the host runs on the same machine as the person.

**`days` is the number that means something.** With one set repeated all day, `seen`
climbs past a hundred within hours and stops distinguishing anything. `days` counts the
distinct days an entry has appeared, is incremented by the host exactly once when it
builds a set, and is what tomorrow's set is chosen by.

Marking an entry known pulls it out of today's set and a replacement is drawn, so the day
keeps its size. Changing the dose reshapes the current day immediately rather than waiting
for tomorrow — quotas are honoured per type, which is not the same as honouring the total:
filling from one mixed pool turned a requested 8 + 4 into 9 + 3.

## How the board and host share the work

The board fetches a batch of 24, shows it, and reports back. Three details carry most of
the behaviour:

**The compiled deck is the floor, not a fallback bolted on afterwards.** The board renders
its first card before Wi-Fi is even attempted, because joining a network takes seconds and
resolving a host takes more, and none of that belongs between power and the first word.

**Source changes land on card boundaries.** The loop only raises a flag; `nextCard()` does
the fetching. Fetching where the timer notices it would swap the batch out from under a
card still on screen.

**A failed refresh replays today's set rather than abandoning it.** A day's set drains
every few minutes, so treating one miss as "the host is gone" would drop the board onto
the full compiled deck and start showing words that are not in today's set. It takes three
consecutive misses to fall back.

**Progress goes out every five cards, not every card.** The host rewrites its whole store
per write, so per-card reporting would mean thousands of full-file writes a day for counts
nobody watches that closely. Marking an entry known reports immediately — that is the one
change worth a round trip of its own.

A dot appears in the corner while running from the compiled deck. Deliberately not a word:
offline is a normal state, not a fault, and does not deserve a banner.

## Measured on the device

| | |
|---|---|
| Flash | 38% of 3 MB (`huge_app`), of which 173 KB is font data |
| Free heap | 167 KB with Wi-Fi up, flat over a long run |
| Vietnamese | legible from 16px; tone marks and `đ` stay distinct |
| Longest term | `there's no point in + V-ing` drops to the 26px face and wraps to two lines |
| Longest meaning | `có đủ khả năng (tài chính) để làm gì` wraps to two lines and still leaves room for a two-line example |
| Progress | survives a reboot — marked 3 known, power-cycled, still 119 remaining |
| Daily set | idempotent across repeated requests; a simulated rollover drew 12 fresh entries with zero overlap |
| Host loss | two drained cycles replay today's set, the third falls back to the compiled deck |

Wi-Fi, HTTPClient, mDNS and ArduinoJson together put the app at 92% of the default 1.31 MB
partition -- too little headroom to add anything -- so `platformio.ini` switches to
`huge_app`, which trades the second OTA slot for a ~3 MB single app partition. That costs
nothing on a board flashed over USB.

The link was verified by pulling the plug on it: host up gives `src=host`, killing the
host drops the board to `src=deck` without a blank screen, and restarting the host brings
it back to `src=host` on its own.

## The host and its web UI

`data/seed.tsv` is only the seed. The living deck is `data/store.json`, edited through a
web UI served by a standard-library Python server — no npm, no pip, no build step:

```bash
python3 host/server.py     # http://localhost:8788
```

It prints the shared token on startup; that goes into the firmware's `config.h`. Browser
requests come from the machine itself and carry no token; the board's two endpoints do.
The token is there so nothing else on the Wi-Fi can read or rewrite the deck — it travels
in cleartext over HTTP on a home network and is not real authentication.

| Endpoint | Who | Does |
|----------|-----|------|
| `GET /api/batch?n=` | board | today's set — the same answer all day |
| `POST /api/progress` | board | reports what it showed and what was marked known |
| `GET /api/entries` | browser | list, search, filter by type and status |
| `PUT`/`DELETE /api/entries/<id>` | browser | edit in place, or remove |
| `POST /api/import` | browser | paste tab- or pipe-separated lines in bulk |
| `POST /api/config` | browser | how many words and patterns a day holds |
| `POST /api/rebuild-daily` | browser | discard today's set and draw another |
| `GET /api/stats` | browser | counts, today's set, and the token |

Two decisions worth recording:

**Progress accumulates, and `known` is never un-set.** The board reports deltas, so
showings add up across the day; `known` only turns on, so a board can never un-know
something.

Adding is not idempotent -- a report whose response is lost gets re-sent and
double-counts. That is deliberate: with one set repeated all day, `seen` climbs past a
hundred and stops carrying much meaning, so a few extra do no harm. The number to trust is
`days`, which the host increments exactly once per day per entry when it builds the set,
never taking the board's word for it.

**The store is written through a temp file and renamed.** A half-written store is worse
than a stale one — the deck and every bit of progress live in that one file, and rename is
the only step that is atomic.

Ids are never reused. Board progress is keyed off them, so a recycled id would silently
inherit the history of a deleted entry.

## Status

The offline device works: the deck is compiled in, progress persists in NVS, cards flip on
their own, and both worst-case layouts render without clipping.

The host and its web UI work: the deck can be searched, filtered, edited in place,
imported in bulk, and entries marked known, and both board endpoints serve and merge
correctly.

Not built yet: the firmware side of that link. The board still runs entirely from its
compiled-in deck. Wiring it to the host is additive by design — the compiled deck stays as
the offline fallback, so a sleeping Mac means no new entries rather than a blank screen.
