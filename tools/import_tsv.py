#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pymongo>=4.18", "dnspython>=2.6"]
# ///
"""Add entries from a TSV straight into MongoDB, skipping anything already in the deck.

    uv run tools/import_tsv.py data/oxford-b1.tsv            # dry run
    uv run tools/import_tsv.py data/oxford-b1.tsv --apply

Columns: front, pos, level, back, example -- the same names data/seed.tsv uses, minus the
id, which is assigned here. An optional `type` column overrides the inference the web UI's
paste box does, which is what a list of phrases needs: "find out" is a pattern but carries
none of the markers that give a pattern away.

Imported entries go to the database and **not** to data/seed.tsv. The compiled deck is the
board's offline floor and is deliberately kept small: growing it changes DECK_COUNT, and
loadProgress() wipes every saved mark whenever that number moves. Storage size is a host
problem, which is the whole reason there is a host.

Ids continue from the highest already in use and are never reused, because progress is
keyed off them and a recycled id would inherit a deleted entry's history.
"""
import argparse
import csv
import os
import re
import sys
from pathlib import Path

from pymongo import MongoClient, ReplaceOne

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "host"))

LEVELS = ("A1", "A2", "B1", "B2", "C1")


def load_env():
    for name in (".env", "atlas-credentials.env"):
        path = ROOT / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tsv")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    # Imported here rather than at the top so the normaliser is the server's own: one
    # definition of what a part of speech is, not a second copy that drifts.
    from server import clean_pos, now_iso, spell_pos

    load_env()
    uri = os.environ.get("MONGODB_URI")
    if not uri:
        sys.exit("no MONGODB_URI in .env or atlas-credentials.env")
    db = MongoClient(uri, serverSelectionTimeoutMS=8000,
                     appname="vocab-import")[os.environ.get("MONGODB_DB", "vocab_display")]

    existing = list(db.entries.find({}, {"_id": 0, "id": 1, "front": 1}))
    have = {e["front"].strip().lower() for e in existing}
    next_id = max((e["id"] for e in existing), default=0) + 1

    docs, skipped, bad = [], [], []
    seen_in_file = set()
    with open(args.tsv, encoding="utf-8") as handle:
        for line_no, row in enumerate(csv.DictReader(handle, delimiter="\t"), 2):
            front = (row.get("front") or "").strip()
            back = (row.get("back") or "").strip()
            if not front or not back:
                bad.append(f"line {line_no}: front and back are both required")
                continue
            key = front.lower()
            if key in have or key in seen_in_file:
                skipped.append(front)
                continue
            seen_in_file.add(key)
            level = (row.get("level") or "B1").strip().upper()
            if level not in LEVELS:
                bad.append(f"line {line_no}: unknown level {level!r}")
                continue
            # An explicit `type` column wins; otherwise the paste box's rule applies, where
            # a "+" or an ellipsis means a pattern. Phrase lists are all patterns and say
            # so in the column, because "find out" carries neither marker.
            kind = (row.get("type") or "").strip().lower()
            if kind not in ("word", "structure"):
                kind = "structure" if re.search(r"\+|\.\.\.", front) else "word"
            docs.append({
                "_id": next_id, "id": next_id, "type": kind, "level": level,
                "pos": clean_pos(row.get("pos", ""), kind), "front": front, "back": back,
                "example": (row.get("example") or "").strip(),
                "days": 0, "known": False, "updated": now_iso(),
            })
            next_id += 1

    print(f"source  {args.tsv}")
    print(f"  {len(docs)} new, {len(skipped)} already in the deck, {len(bad)} rejected")
    if bad:
        for line in bad[:10]:
            print("   ", line)
    if skipped:
        print(f"  skipped: {', '.join(skipped[:8])}{' …' if len(skipped) > 8 else ''}")
    if docs:
        print(f"  ids {docs[0]['id']}..{docs[-1]['id']}, "
              f"{sum(1 for d in docs if d['type'] == 'structure')} structures")
        no_pos = [d["front"] for d in docs if d["type"] == "word" and not d["pos"]]
        print(f"  words with no part of speech: {len(no_pos)}"
              f"{' -> ' + ', '.join(no_pos[:8]) if no_pos else ''}")
        print(f"  first: {docs[0]['front']} [{spell_pos(docs[0]['pos'])}] = {docs[0]['back']}")
    print(f"target  database already holds {len(existing)} entries")

    if not args.apply:
        print("\ndry run -- nothing written. Re-run with --apply.")
        return
    if not docs:
        print("\nnothing to add.")
        return

    db.entries.bulk_write([ReplaceOne({"_id": d["_id"]}, d, upsert=True) for d in docs],
                          ordered=False)

    # Read back rather than trust the write: a partial bulk result would leave the deck
    # quietly short, and this is the only moment the file and the database can be compared.
    back = {d["id"]: d for d in db.entries.find({"_id": {"$gte": docs[0]["_id"]}}, {"_id": 0})}
    problems = [f"id {d['id']} ({d['front']}) missing or altered"
                for d in docs
                if back.get(d["id"], {}).get("front") != d["front"]
                or back.get(d["id"], {}).get("back") != d["back"]]
    total = db.entries.count_documents({})
    print(f"\nverify  {len(back)} read back, {total} entries in the deck now")
    if problems:
        print("  FAILED:")
        for line in problems[:20]:
            print("   ", line)
        sys.exit(1)
    print(f"  every new entry round-tripped; {total - len(existing)} added")


if __name__ == "__main__":
    main()
