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

## Measured on the device

| | |
|---|---|
| Flash | 40% of 1.31 MB, of which 173 KB is font data |
| Free heap | 283 KB, flat |
| Vietnamese | legible from 16px; tone marks and `đ` stay distinct |
| Longest term | `there's no point in + V-ing` drops to the 26px face and wraps to two lines |
| Longest meaning | `có đủ khả năng (tài chính) để làm gì` wraps to two lines and still leaves room for a two-line example |
| Progress | survives a reboot — marked 3 known, power-cycled, still 119 remaining |

Flash sits at 40% rather than claude-tracker's 78% because there is no Wi-Fi or TLS stack
here, which is also where the extra 111 KB of free heap comes from.

## Status

The offline device works: the deck is compiled in, progress persists in NVS, cards flip on
their own, and both worst-case layouts render without clipping.

Not built: the web UI for editing the deck, and the host that would serve it. That was
deliberately left until after the font question was answered — if Vietnamese had not been
legible at these sizes, the layout and possibly the card model would have had to change,
and a web form built first would have been wasted work.
