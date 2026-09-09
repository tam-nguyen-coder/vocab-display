#!/bin/bash
# Regenerates every compiled asset: the deck header and the four VLW fonts.
# Run after editing data/seed.tsv or changing a font size.
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PY:-python3}"
TTF="${TTF:-/System/Library/Fonts/Supplemental/Arial.ttf}"

$PY tools/gen_deck.py data/seed.tsv include/deck.h

# The front of a card is always English, so those sizes carry ASCII only -- 95 glyphs
# instead of 229, which is where most of the flash saving comes from.
$PY tools/ttf2vlw.py "$TTF" --size 42 --charset ascii \
    --name fontTermLarge --out-header include/fonts/font_term_large.h
$PY tools/ttf2vlw.py "$TTF" --size 26 --charset ascii \
    --name fontTermSmall --out-header include/fonts/font_term_small.h
$PY tools/ttf2vlw.py "$TTF" --size 22 --charset vietnamese \
    --name fontMeaning --out-header include/fonts/font_meaning.h
$PY tools/ttf2vlw.py "$TTF" --size 15 --charset vietnamese \
    --name fontSmall --out-header include/fonts/font_small.h
