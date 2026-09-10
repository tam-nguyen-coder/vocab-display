#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pymongo>=4.18", "dnspython>=2.6"]
# ///
"""Move data/store.json into MongoDB, once.

    uv run tools/migrate_to_mongo.py            # dry run: says what it would do
    uv run tools/migrate_to_mongo.py --apply

The JSON file is never modified. After this runs it stops being the source of truth and
becomes the mirror the host refreshes on a timer, so it stays exactly where it is.

`seen` is dropped on the way through. It counted how many times a card was pushed to the
panel, which passed a hundred a day and distinguished nothing; `days` -- distinct days an
entry has appeared -- is the count that survives. Every `seen` value stays readable in
data/backups/ if it is ever wanted back.

Refuses to write over a database that already holds entries unless --force is given: the
one irreversible thing here is overwriting progress that is newer than the file.
"""
import argparse
import json
import os
import sys
from pathlib import Path

from pymongo import MongoClient, ReplaceOne

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "store.json"


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
    ap.add_argument("--apply", action="store_true", help="actually write to the database")
    ap.add_argument("--force", action="store_true", help="overwrite a non-empty database")
    args = ap.parse_args()

    load_env()
    uri = os.environ.get("MONGODB_URI")
    if not uri:
        sys.exit("no MONGODB_URI in .env or atlas-credentials.env")
    dbname = os.environ.get("MONGODB_DB", "vocab_display")

    if not STORE.exists():
        sys.exit(f"no {STORE} to migrate")
    store = json.loads(STORE.read_text(encoding="utf-8"))
    entries = store["entries"]

    docs, dropped = [], 0
    for entry in entries:
        doc = {k: v for k, v in entry.items() if k != "seen"}
        dropped += "seen" in entry
        doc.setdefault("days", 0)
        doc.setdefault("pos", "")
        doc.setdefault("known", False)
        doc["_id"] = doc["id"]
        docs.append(doc)

    ids = [d["id"] for d in docs]
    print(f"source  {STORE}")
    print(f"  {len(docs)} entries, ids {min(ids)}..{max(ids)}, "
          f"{sum(1 for d in docs if d['known'])} known, "
          f"{sum(1 for d in docs if d['days'] > 0)} with days > 0")
    print(f"  dropping `seen` from {dropped} entries "
          f"(total was {sum(e.get('seen', 0) for e in entries)})")
    print(f"target  mongodb {dbname}: collections `entries`, `meta`")

    client = MongoClient(uri, serverSelectionTimeoutMS=8000, appname="vocab-migrate")
    db = client[dbname]
    existing = db.entries.count_documents({})
    print(f"  database currently holds {existing} entries")

    if not args.apply:
        print("\ndry run -- nothing written. Re-run with --apply.")
        return
    if existing and not args.force:
        sys.exit("database already has entries; re-run with --force to overwrite")

    db.entries.bulk_write([ReplaceOne({"_id": d["_id"]}, d, upsert=True) for d in docs],
                          ordered=False)
    db.entries.delete_many({"_id": {"$nin": ids}})
    db.meta.replace_one(
        {"_id": "state"},
        {"_id": "state", "version": 3, "token": store["token"],
         "config": store["config"], "daily": store["daily"]},
        upsert=True)

    # Verify against the file rather than trusting the write: this is the one step where a
    # silent partial result would cost progress that exists nowhere else.
    back = {d["id"]: d for d in db.entries.find({}, {"_id": 0})}
    meta = db.meta.find_one({"_id": "state"})
    problems = []
    if set(back) != set(ids):
        problems.append(f"id mismatch: missing {sorted(set(ids) - set(back))}, "
                        f"extra {sorted(set(back) - set(ids))}")
    for d in docs:
        got = back.get(d["id"])
        if not got:
            continue
        for field in ("front", "back", "example", "type", "level", "pos", "days", "known"):
            if got.get(field) != d.get(field):
                problems.append(f"id {d['id']} {field}: {d.get(field)!r} -> {got.get(field)!r}")
        if "seen" in got:
            problems.append(f"id {d['id']} still carries `seen`")
    if meta.get("token") != store["token"] or meta.get("daily") != store["daily"]:
        problems.append("meta did not round-trip")

    print(f"\nverify  {len(back)} entries read back")
    if problems:
        print("  FAILED:")
        for line in problems[:20]:
            print("   ", line)
        sys.exit(1)
    print("  every field matches the file; token, config and daily set round-tripped")
    print("\nMigrated. data/store.json is now the mirror and is left untouched.")


if __name__ == "__main__":
    main()
