#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pymongo>=4.18", "dnspython>=2.6"]
# ///
"""The vocab-display host: a MongoDB store, a small API, and the web UI that edits it.

Runs with `uv run host/server.py` -- the dependency block above is all the setup there is,
and uv builds the environment on the first run.

MongoDB is the source of truth. `data/store.json` is kept as a mirror of it, refreshed on
a timer, and is what the host falls back to reading when the database cannot be reached --
the deck stays browsable on a dead link even though nothing can be written to it. The
mirror is also the format this project used before, so it stays hand-readable and
hand-editable if this program is ever in the way.

The deck is not worked through front to back. Each day gets one fixed set -- words and
sentence patterns interleaved -- and the board repeats that set all day; tomorrow gets a
different one. Repetition inside a day is the point, which is why nothing counts showings:
a card seen forty times in one afternoon has been practised once. `days`, the count of
distinct days an entry has appeared, is the only progress number kept, and is what
tomorrow's set is chosen by.

Two audiences share the API:

  the board   GET  /api/batch      today's set, the same answer all day
              POST /api/progress   what it has shown and what was marked known
  the browser GET  /api/entries    search, multi-value filters, sort, paged
              PUT  /api/entries/<id>, DELETE /api/entries/<id>
              POST /api/import     paste TSV in bulk
              GET  /api/stats

Board requests carry a shared token; browser requests come from localhost and do not. The
token exists so that nothing else on the Wi-Fi can read or rewrite the deck, not as real
authentication -- it travels in cleartext over HTTP on a home network.
"""
import copy
import json
import os
import unicodedata
import random
import re
import secrets
import sys
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pymongo import MongoClient, ReplaceOne
from pymongo.errors import PyMongoError

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "store.json"   # mirror of the database, not the source of truth
SEED = ROOT / "data" / "seed.tsv"
STATIC = Path(__file__).resolve().parent / "static"
PORT = int(os.environ.get("VOCAB_PORT", 8788))
# How often the mirror is refreshed from the database. Long, because the mirror exists to
# survive a dead link rather than to be current: the board reports every few minutes and
# rewriting the file more often than that would be churn for its own sake.
MIRROR_INTERVAL_S = int(os.environ.get("VOCAB_MIRROR_SECONDS", 300))


def load_env():
    """Read KEY = VALUE lines from the first credentials file that exists.

    Hand-rolled rather than pulled from python-dotenv: it is fifteen lines, and the
    dependency list is worth keeping to the one thing that cannot be written by hand.
    Real environment variables win, so a shell export can override the file.
    """
    for name in (".env", "atlas-credentials.env"):
        path = ROOT / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()
MONGO_URI = os.environ.get("MONGODB_URI", "")
MONGO_DB = os.environ.get("MONGODB_DB", "vocab_display")


class StorageOffline(RuntimeError):
    """The database is unreachable, so this request cannot be served from the mirror."""

TYPES = ("word", "structure")
# A1 and C1 exist so an Oxford list can be imported without its ends being clipped;
# the starter deck itself only spans A2 to B2.
LEVELS = ("A1", "A2", "B1", "B2", "C1")
# Parts of speech, for words only. Stored abbreviated because that is what filters, sorts
# and the firmware's flash all want to be short; every surface that shows one to a person
# spells it out. The set covers what Oxford's own lists tag words with, so an import does
# not arrive carrying categories there is nowhere to put.
POS_FULL = {
    "n": "noun", "v": "verb", "adj": "adjective", "adv": "adverb",
    "prep": "preposition", "conj": "conjunction", "det": "determiner",
    "pron": "pronoun", "num": "number", "exclam": "exclamation",
    "art": "article", "phr v": "phrasal verb", "modal v": "modal verb",
    "aux v": "auxiliary verb",
}
POS_TAGS = tuple(POS_FULL)
# Both directions are accepted on input, so "noun" typed into the web UI and "n." pasted
# out of a word list land on the same stored value.
POS_ALIASES = {full: tag for tag, full in POS_FULL.items()}
POS_ALIASES.update({"indefinite article": "art", "definite article": "art"})


def spell_pos(value):
    '''"n,v" -> "noun, verb". The one place on the host where a tag becomes a word.'''
    return ", ".join(POS_FULL.get(t, t) for t in value.split(",") if t)


def clean_pos(value, kind="word"):
    """Normalise a part-of-speech field to a comma-separated list of known tags.

    A structure has no part of speech -- "used to + V" is a pattern, not a word class --
    so it is always cleared rather than left to whatever a caller sent. Unknown tags are
    dropped instead of rejecting the write: this arrives from a paste box and a bad tag
    is not worth losing the entry over.
    """
    if kind == "structure":
        return ""
    tags = []
    for tag in str(value or "").lower().replace(";", ",").replace("/", ",").split(","):
        tag = " ".join(tag.split()).rstrip(".")
        tag = POS_ALIASES.get(tag, tag)
        if tag in POS_FULL and tag not in tags:
            tags.append(tag)
    return ",".join(tags)

_lock = threading.Lock()


# ------------------------------------------------------------------------------- store


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_seed():
    """First run only: import seed.tsv so the store is never empty."""
    rows = []
    lines = SEED.read_text(encoding="utf-8").splitlines()
    header = lines[0].split("\t")
    for line in lines[1:]:
        if not line.strip():
            continue
        row = dict(zip(header, line.split("\t")))
        rows.append({
            "id": int(row["id"]),
            "type": row["type"],
            "level": row["level"],
            "pos": clean_pos(row.get("pos", ""), row["type"]),
            "front": row["front"],
            "back": row["back"],
            "example": row.get("example", ""),
            "days": 0,
            "known": False,
            "updated": now_iso(),
        })
    return rows


# Twelve entries cycle in about three minutes at the device's pacing, so a desk-bound day
# shows each one dozens of times. That is the intended dose: ambient repetition of a small
# set, not a march through the deck.
DEFAULT_CONFIG = {"daily_words": 8, "daily_structures": 4}


def default_store():
    return {
        "version": 3,
        # Copied into the firmware's config.h. Regenerating it locks out any board still
        # using the old one, which is the point.
        "token": secrets.token_hex(8),
        "config": dict(DEFAULT_CONFIG),
        "daily": {"date": "", "ids": []},
        "entries": load_seed() if SEED.exists() else [],
    }


# --------------------------------------------------------------------------- the database

# One client for the process. pymongo pools connections itself and is thread-safe, which
# is what makes it safe to share across ThreadingHTTPServer's request threads.
_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000,
                      appname="vocab-display") if MONGO_URI else None
_db = _client[MONGO_DB] if _client is not None else None
_mirror_dirty = threading.Event()

# Every handler works on the whole store, which meant every request pulled every document
# out of Atlas: fine at 124 entries, about a second and a half at 900, and worse from
# there. This process is the only writer, so its own copy is authoritative between writes;
# the TTL is only a backstop for a second host or a migration script touching the same
# database behind its back.
STORE_CACHE_TTL_S = 5.0
_cache = {"store": None, "at": 0.0}
PERSISTED = ("id", "type", "level", "pos", "front", "back", "example",
             "days", "known", "updated")


def db_alive():
    if _db is None:
        return False
    try:
        _db.client.admin.command("ping")
        return True
    except PyMongoError:
        return False


def normalise(entry):
    """One entry in the shape the rest of the program expects, whatever it was stored as.

    Documents written before a field existed are just that field short, so defaults are
    applied on the way out rather than by a migration pass -- the same in-place approach
    the JSON store used, and the reason adding a column has never needed a schema step.
    """
    entry.setdefault("days", 0)
    entry.setdefault("pos", "")
    entry.setdefault("known", False)
    entry.setdefault("example", "")
    # `seen` counted how many times a card was pushed to the panel, which climbed past a
    # hundred a day and distinguished nothing. Dropped in favour of `days`; documents that
    # still carry it are read without it rather than being rewritten.
    entry.pop("seen", None)
    return entry


def read_store():
    """The whole store as one dict, from the database, or from the mirror if it is down.

    Returning the same shape the JSON file had is deliberate: every handler below works on
    that dict and none of them had to change when the storage underneath it did. Callers
    get a copy, so a handler mutating what it was handed cannot corrupt the cache.
    """
    if _cache["store"] is not None and time.monotonic() - _cache["at"] < STORE_CACHE_TTL_S:
        return copy.deepcopy(_cache["store"])
    if _db is not None:
        try:
            meta = _db.meta.find_one({"_id": "state"})
            entries = [normalise(e) for e in _db.entries.find({}, {"_id": 0})]
            if meta is None and not entries:
                store = default_store()
                write_store(store)
                return store
            meta = meta or {}
            store = {
                "version": meta.get("version", 3),
                "token": meta.get("token") or secrets.token_hex(8),
                "config": meta.get("config") or dict(DEFAULT_CONFIG),
                "daily": meta.get("daily") or {"date": "", "ids": []},
                "entries": sorted(entries, key=lambda e: e["id"]),
            }
            _cache["store"], _cache["at"] = copy.deepcopy(store), time.monotonic()
            return store
        except PyMongoError as err:
            sys.stderr.write(f"[db] read failed, falling back to the mirror: {err}\n")

    if not STORE.exists():
        raise StorageOffline("no database and no mirror to fall back to")
    store = json.loads(STORE.read_text(encoding="utf-8"))
    store.setdefault("config", dict(DEFAULT_CONFIG))
    store.setdefault("daily", {"date": "", "ids": []})
    store["entries"] = [normalise(e) for e in store["entries"]]
    store["offline"] = True   # read-only: write_store will refuse
    return store


def write_store(store):
    """Persist the whole store to MongoDB, then flag the mirror as stale.

    Writing everything on every call keeps the contract the JSON file had, and at a few
    hundred documents one bulk upsert costs less than the round trip that carries it.
    Deletes are handled by removing whatever ids are no longer present, because an entry
    that vanished from the dict has to vanish from the collection too.
    """
    if store.get("offline") or _db is None:
        raise StorageOffline("the database is unreachable, so nothing can be written")
    try:
        ids = [e["id"] for e in store["entries"]]
        # Only what actually changed. The board reports every few minutes and reshapes the
        # daily set each time, which touched a dozen entries and rewrote nine hundred.
        # Compared on the persisted fields only: /api/entries decorates what it hands back
        # with `status` and `today`, and an entry that came back through a PUT would
        # otherwise look changed on every request.
        before = {e["id"]: {f: e.get(f) for f in PERSISTED}
                  for e in (_cache["store"] or {}).get("entries", [])}
        changed = [e for e in store["entries"]
                   if before.get(e["id"]) != {f: e.get(f) for f in PERSISTED}]
        if changed:
            _db.entries.bulk_write(
                [ReplaceOne({"_id": e["id"]}, {"_id": e["id"], **normalise(dict(e))},
                            upsert=True) for e in changed],
                ordered=False)
        if before and set(before) - set(ids):
            _db.entries.delete_many({"_id": {"$nin": ids}})
        elif not before:
            _db.entries.delete_many({"_id": {"$nin": ids}})
        _db.meta.replace_one(
            {"_id": "state"},
            {"_id": "state", "version": store.get("version", 3), "token": store["token"],
             "config": store["config"], "daily": store["daily"]},
            upsert=True)
    except PyMongoError as err:
        _cache["store"] = None          # what is in the database is no longer known
        raise StorageOffline(f"write failed: {err}") from err
    _cache["store"] = copy.deepcopy({k: v for k, v in store.items() if k != "offline"})
    _cache["at"] = time.monotonic()
    _mirror_dirty.set()


def write_mirror(store):
    """Snapshot the store to data/store.json through a temp file, then rename.

    A half-written mirror is worse than a stale one -- it is the only thing standing
    between a dead database and a blank web UI -- and rename is the only atomic step.
    """
    STORE.parent.mkdir(parents=True, exist_ok=True)
    snapshot = {k: v for k, v in store.items() if k != "offline"}
    snapshot["mirrored"] = now_iso()
    fd, tmp = tempfile.mkstemp(dir=STORE.parent, prefix=".store-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STORE)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def mirror_loop():
    """Refresh the mirror on a timer, and only when something actually changed.

    A daemon thread rather than a write-through on every save: the board reports every few
    minutes and rewriting the file on each one would be churn. Waiting on the flag means a
    quiet host writes nothing at all.
    """
    while True:
        _mirror_dirty.wait(MIRROR_INTERVAL_S)
        if not _mirror_dirty.is_set():
            continue
        _mirror_dirty.clear()
        try:
            with _lock:
                store = read_store()
            if not store.get("offline"):
                write_mirror(store)
        except Exception as err:                       # a mirror is a nicety, never fatal
            sys.stderr.write(f"[mirror] skipped: {err}\n")


def next_id(entries):
    """Ids are never reused: board progress is keyed off them, so a recycled id would
    silently inherit the history of a deleted entry."""
    return max((e["id"] for e in entries), default=0) + 1


# ------------------------------------------------------------------------------ selection


def local_date():
    """The day boundary is the user's, not UTC's -- the host runs on their own machine."""
    return datetime.now().strftime("%Y-%m-%d")


def by_priority(pool):
    """Fewest days first, shuffled within a tier.

    Shuffling before the sort is what keeps ties unpredictable, and it does more work now
    than it used to: with showings no longer counted there is no second key, so most of
    the deck sits in one tier on nought days and the shuffle alone decides who is drawn.
    """
    random.shuffle(pool)
    pool.sort(key=lambda e: e["days"])
    return pool


def build_daily(store, date):
    """Choose one day's set: words and patterns interleaved, least-practised first."""
    wants = daily_wants(store["config"])
    want_words, want_structures = wants["word"], wants["structure"]

    eligible = [e for e in store["entries"] if not e["known"]]
    words = by_priority([e for e in eligible if e["type"] == "word"])
    structures = by_priority([e for e in eligible if e["type"] == "structure"])

    chosen = words[:want_words] + structures[:want_structures]
    # If one pool cannot fill its share -- few structures left, say -- make the shortfall
    # up from the other rather than handing back a short day.
    shortfall = (want_words + want_structures) - len(chosen)
    if shortfall > 0:
        rest = by_priority(words[want_words:] + structures[want_structures:])
        chosen += rest[:shortfall]

    # Counted once per day per entry, which is what makes `days` mean what it says.
    for entry in chosen:
        entry["days"] += 1
        entry["updated"] = now_iso()

    # Interleave, so the device alternates between vocabulary and patterns instead of
    # showing all the words and then all the structures.
    random.shuffle(chosen)
    return {"date": date, "ids": [e["id"] for e in chosen]}


def ensure_daily(store):
    """Today's set, built on first request of the day and then fixed.

    Idempotent on purpose: the board re-asks every few minutes, and every one of those
    requests must return the same set or the day stops being a day.
    """
    today = local_date()
    if store["daily"].get("date") != today:
        store["daily"] = build_daily(store, today)
        return True
    return False


def daily_wants(config):
    return {"word": max(0, int(config.get("daily_words", 8))),
            "structure": max(0, int(config.get("daily_structures", 4)))}


def reshape_daily(store):
    """Brings today's set back to the configured shape, both directions and per type.

    Entries leave it when marked known or deleted -- without refilling, marking three in
    the morning would leave a nine-entry day. It also has to shrink: lowering the dose and
    watching the set stay put would read as a dead input.

    Quotas are honoured per type rather than on the total. Filling from one mixed pool
    looked equivalent and was not: asking for 8 words and 4 patterns produced 9 and 3,
    because whichever type happened to sort first took the free slots.

    Entries already in the set are kept rather than re-picked, so marking one known does
    not reshuffle the day. An entry trimmed on the same day it was added keeps its
    incremented `days`; undoing that would mean tracking whether it was ever actually
    shown, which is more bookkeeping than a count of days deserves.
    """
    by_id = {e["id"]: e for e in store["entries"]}
    alive = [i for i in store["daily"]["ids"] if i in by_id and not by_id[i]["known"]]
    wants = daily_wants(store["config"])

    def claim(entry):
        entry["days"] += 1
        entry["updated"] = now_iso()
        return entry["id"]

    kept = []
    for kind, want in wants.items():
        have = [i for i in alive if by_id[i]["type"] == kind]
        if len(have) > want:
            have = have[:want]
        elif len(have) < want:
            pool = by_priority([e for e in store["entries"] if not e["known"]
                                and e["type"] == kind and e["id"] not in alive])
            have += [claim(e) for e in pool[:want - len(have)]]
        kept += have

    # Only if a pool ran dry does the other type cover the shortfall -- a deck with three
    # structures left should still give a full day.
    total = sum(wants.values())
    if len(kept) < total:
        pool = by_priority([e for e in store["entries"]
                            if not e["known"] and e["id"] not in kept])
        kept += [claim(e) for e in pool[:total - len(kept)]]

    # Only re-interleave when the membership actually moved. Shuffling unconditionally
    # made `changed` true on every progress report, so the set's order churned and the
    # store was rewritten several times a minute for nothing.
    changed = sorted(kept) != sorted(store["daily"]["ids"])
    if changed:
        random.shuffle(kept)
    else:
        kept = store["daily"]["ids"]
    store["daily"]["ids"] = kept
    return changed


def daily_cards(store):
    by_id = {e["id"]: e for e in store["entries"]}
    return [by_id[i] for i in store["daily"]["ids"] if i in by_id]


# ------------------------------------------------------------------------------ querying

SEARCH_TOKEN = re.compile(
    r'(-?)(?:(front|back|ex|example|pos|any):)?(?:"([^"]*)"|(\S+))', re.IGNORECASE)

# `pos` is searchable but deliberately not part of `any`: a bare "v" would otherwise
# match every verb in the deck and drown the term the search was actually for.
FIELD_ALIASES = {"front": ("front",), "back": ("back",), "ex": ("example",),
                 "example": ("example",), "pos": ("pos",),
                 "any": ("front", "back", "example")}

SORT_KEYS = ("id", "front", "back", "pos", "days", "level", "type", "updated")


def fold(text):
    """Accent- and case-insensitive form, for both searching and sorting.

    Searching for "tiep can" should find "tiếp cận" -- typing Vietnamese diacritics to
    look something up is exactly the friction this tool exists to remove. Decomposing and
    dropping the combining marks also makes sorting put "ăn" next to "an" instead of after
    "z", which is where raw code points would leave it.
    """
    decomposed = unicodedata.normalize("NFD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def parse_search(query):
    """Splits a search box into AND-ed terms.

    Supports `front:afford` to scope a term to one field, `"tiếp cận"` to keep a phrase
    together, and a leading `-` to exclude. Everything is AND-ed: narrowing a search by
    adding a word is the behaviour people already expect from every other search box.
    """
    terms = []
    for negate, field, quoted, bare in SEARCH_TOKEN.findall(query or ""):
        needle = quoted if quoted else bare
        if not needle.strip():
            continue
        fields = FIELD_ALIASES.get((field or "any").lower(), FIELD_ALIASES["any"])
        terms.append((bool(negate), fields, fold(needle)))
    return terms


def matches_search(entry, terms):
    for negate, fields, needle in terms:
        hit = any(needle in fold(entry.get(f) or "") for f in fields)
        if hit == negate:
            return False
    return True


def csv_param(values):
    """Multi-value filters arrive as `type=word,structure` or repeated parameters."""
    out = set()
    for value in values or []:
        out |= {v.strip() for v in value.split(",") if v.strip()}
    return out


def status_of(entry):
    if entry["known"]:
        return "known"
    return "unseen" if entry["days"] == 0 else "learning"


def stats(entries):
    known = sum(1 for e in entries if e["known"])
    unseen = sum(1 for e in entries if not e["known"] and e["days"] == 0)
    return {
        "total": len(entries),
        "known": known,
        "remaining": len(entries) - known,
        "unseen": unseen,
        "words": sum(1 for e in entries if e["type"] == "word"),
        "structures": sum(1 for e in entries if e["type"] == "structure"),
    }


# -------------------------------------------------------------------------------- server


class Handler(BaseHTTPRequestHandler):
    server_version = "vocab-display"

    def handle_one_request(self):
        """Turn a dead database into a 503 rather than a stack trace and a dropped socket.

        Every write goes through write_store, which raises before anything is sent, so
        catching here is enough to answer properly. The board treats a non-200 as a miss
        and falls back to its compiled deck after three of them, which is exactly the
        behaviour wanted: a stale day is worse than an honest one.
        """
        try:
            super().handle_one_request()
        except StorageOffline as err:
            try:
                self.send_json({"error": "database unreachable", "detail": str(err),
                                "readonly": True}, 503)
            except Exception:
                pass

    def log_message(self, fmt, *args):  # quieter than the default per-request noise
        if "/api/batch" not in self.path:
            sys.stderr.write(f"{self.address_string()} {fmt % args}\n")

    # -- helpers

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, content_type):
        try:
            body = path.read_bytes()
        except OSError:
            return self.send_json({"error": "not found"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def token_ok(self, store):
        return self.headers.get("X-Vocab-Token") == store["token"]

    @property
    def query(self):
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    @property
    def route(self):
        return urllib.parse.urlparse(self.path).path

    # -- GET

    def do_GET(self):
        route = self.route
        if route in ("/", "/index.html"):
            return self.send_file(STATIC / "index.html", "text/html; charset=utf-8")
        if route == "/app.js":
            return self.send_file(STATIC / "app.js", "text/javascript; charset=utf-8")
        if route == "/style.css":
            return self.send_file(STATIC / "style.css", "text/css; charset=utf-8")

        with _lock:
            store = read_store()

            if route == "/api/stats":
                if ensure_daily(store):
                    write_store(store)
                return self.send_json({
                    "pos_full": POS_FULL,
                    "stats": stats(store["entries"]),
                    "token": store["token"],
                    "port": PORT,
                    "config": store["config"],
                    "daily": {"date": store["daily"]["date"],
                              "ids": store["daily"]["ids"],
                              "entries": daily_cards(store)},
                })

            if route == "/api/batch":
                if not self.token_ok(store):
                    return self.send_json({"error": "bad token"}, 401)
                # `n` is a ceiling the board applies to its own buffer, not a request for
                # a different set: the answer is today's day, whoever asks and however
                # often.
                limit = min(int(self.query.get("n", [40])[0]), 100)
                if ensure_daily(store):
                    write_store(store)
                cards = daily_cards(store)[:limit]
                return self.send_json({
                    "date": store["daily"]["date"],
                    "count": len(cards),
                    "remaining": stats(store["entries"])["remaining"],
                    "cards": [{"id": e["id"], "t": 1 if e["type"] == "structure" else 0,
                               "p": e.get("pos", ""),
                               "f": e["front"], "b": e["back"], "e": e["example"]}
                              for e in cards],
                })

            if route == "/api/entries":
                rows = store["entries"]
                terms = parse_search(self.query.get("q", [""])[0])
                if terms:
                    rows = [e for e in rows if matches_search(e, terms)]

                kinds = csv_param(self.query.get("type"))
                if kinds:
                    rows = [e for e in rows if e["type"] in kinds]
                levels = csv_param(self.query.get("level"))
                if levels:
                    rows = [e for e in rows if e["level"] in levels]
                # A word tagged "n,v" answers to both `pos=n` and `pos=v`, so an entry
                # matches when any of its tags is asked for.
                tags = csv_param(self.query.get("pos"))
                if tags:
                    rows = [e for e in rows
                            if tags & set(filter(None, e.get("pos", "").split(",")))]
                statuses = csv_param(self.query.get("status"))
                if statuses:
                    rows = [e for e in rows if status_of(e) in statuses]
                if self.query.get("daily", [""])[0] == "1":
                    today = set(store["daily"]["ids"])
                    rows = [e for e in rows if e["id"] in today]

                sort = self.query.get("sort", ["id"])[0]
                if sort not in SORT_KEYS:
                    sort = "id"
                descending = self.query.get("dir", ["asc"])[0] == "desc"
                # Text columns sort folded so Vietnamese lands alphabetically; the id is
                # always the tie-break so equal values keep a stable, repeatable order
                # across pages rather than shuffling between requests.
                if sort in ("front", "back", "level", "type", "pos"):
                    key = lambda e: (fold(e[sort]), e["id"])
                elif sort == "updated":
                    key = lambda e: (e["updated"], e["id"])
                else:
                    key = lambda e: (e[sort], e["id"])
                rows = sorted(rows, key=key, reverse=descending)

                # Paged server-side rather than in the browser: the deck is meant to grow,
                # and shipping every row to render fifty of them gets slower for no reason.
                per = max(1, min(500, int(self.query.get("per", [50])[0])))
                pages = max(1, (len(rows) + per - 1) // per)
                page = max(1, min(pages, int(self.query.get("page", [1])[0])))
                start = (page - 1) * per
                window = rows[start:start + per]
                today = set(store["daily"]["ids"])
                return self.send_json({
                    "entries": [dict(e, status=status_of(e), today=e["id"] in today)
                                for e in window],
                    "matched": len(rows),
                    "page": page,
                    "pages": pages,
                    "per": per,
                    "from": start + 1 if window else 0,
                    "to": start + len(window),
                    "sort": sort,
                    "dir": "desc" if descending else "asc",
                })

        self.send_json({"error": "not found"}, 404)

    # -- POST / PUT / DELETE

    def do_POST(self):
        route = self.route
        body = self.read_body()
        if body is None:
            return self.send_json({"error": "malformed json"}, 400)

        with _lock:
            store = read_store()

            if route == "/api/progress":
                if not self.token_ok(store):
                    return self.send_json({"error": "bad token"}, 401)
                # `known` is sticky: set, never assigned, so a board cannot un-know
                # something. It is the only thing the board reports now -- a repeated
                # report is therefore idempotent, which the old showing count never was.
                #
                # `days` is the number that matters and the board never touches it: the
                # host increments it exactly once per day per entry, when it builds the
                # set, and never takes the board's word for it.
                by_id = {e["id"]: e for e in store["entries"]}
                applied = 0
                for item in body.get("cards", []):
                    entry = by_id.get(item.get("id"))
                    if not entry:
                        continue
                    if item.get("known"):
                        entry["known"] = True
                    entry["updated"] = now_iso()
                    applied += 1
                reshape_daily(store)
                write_store(store)
                return self.send_json({"applied": applied,
                                       "remaining": stats(store["entries"])["remaining"]})

            if route == "/api/entries":
                entry = self.clean_entry(body, store["entries"])
                if isinstance(entry, str):
                    return self.send_json({"error": entry}, 400)
                store["entries"].append(entry)
                write_store(store)
                return self.send_json({"entry": entry}, 201)

            if route == "/api/import":
                added, skipped, errors = self.do_import(body.get("text", ""), store)
                if added:
                    write_store(store)
                return self.send_json({"added": added, "skipped": skipped,
                                       "errors": errors[:10],
                                       "stats": stats(store["entries"])})

            if route == "/api/config":
                config = store["config"]
                for field in ("daily_words", "daily_structures"):
                    if field in body:
                        config[field] = max(0, min(60, int(body[field])))
                # Resize today's set immediately rather than waiting for tomorrow --
                # changing the dose and seeing nothing change would read as a broken input.
                reshape_daily(store)
                write_store(store)
                return self.send_json({"config": config,
                                       "daily": {"date": store["daily"]["date"],
                                                 "entries": daily_cards(store)}})

            if route == "/api/rebuild-daily":
                store["daily"] = build_daily(store, local_date())
                write_store(store)
                return self.send_json({"daily": {"date": store["daily"]["date"],
                                                 "entries": daily_cards(store)}})

            if route == "/api/reset-token":
                store["token"] = secrets.token_hex(8)
                write_store(store)
                return self.send_json({"token": store["token"]})

        self.send_json({"error": "not found"}, 404)

    def do_PUT(self):
        match = re.fullmatch(r"/api/entries/(\d+)", self.route)
        if not match:
            return self.send_json({"error": "not found"}, 404)
        body = self.read_body()
        if body is None:
            return self.send_json({"error": "malformed json"}, 400)

        with _lock:
            store = read_store()
            entry = next((e for e in store["entries"] if e["id"] == int(match.group(1))), None)
            if not entry:
                return self.send_json({"error": "no such entry"}, 404)
            for field in ("front", "back", "example"):
                if field in body:
                    entry[field] = str(body[field]).strip()
            if body.get("type") in TYPES:
                entry["type"] = body["type"]
            if "pos" in body:
                entry["pos"] = clean_pos(body["pos"], entry["type"])
            elif body.get("type") == "structure":
                entry["pos"] = ""
            if body.get("level") in LEVELS:
                entry["level"] = body["level"]
            if "known" in body:
                entry["known"] = bool(body["known"])
            if "days" in body:
                entry["days"] = max(0, int(body["days"]))
            entry["updated"] = now_iso()
            # Marking something known mid-day pulls it out of today's set, so refill.
            reshape_daily(store)
            write_store(store)
            return self.send_json({"entry": entry})

    def do_DELETE(self):
        match = re.fullmatch(r"/api/entries/(\d+)", self.route)
        if not match:
            return self.send_json({"error": "not found"}, 404)
        with _lock:
            store = read_store()
            before = len(store["entries"])
            store["entries"] = [e for e in store["entries"] if e["id"] != int(match.group(1))]
            if len(store["entries"]) == before:
                return self.send_json({"error": "no such entry"}, 404)
            reshape_daily(store)
            write_store(store)
            return self.send_json({"deleted": int(match.group(1))})

    # -- validation and import

    def clean_entry(self, body, entries):
        front = str(body.get("front", "")).strip()
        back = str(body.get("back", "")).strip()
        if not front or not back:
            return "front and back are both required"
        if any(e["front"].lower() == front.lower() for e in entries):
            return f"{front!r} is already in the deck"
        kind = body.get("type") if body.get("type") in TYPES else "word"
        return {
            "id": next_id(entries),
            "type": kind,
            "level": body.get("level") if body.get("level") in LEVELS else "B1",
            "pos": clean_pos(body.get("pos", ""), kind),
            "front": front,
            "back": back,
            "example": str(body.get("example", "")).strip(),
            "days": 0,
            "known": False,
            "updated": now_iso(),
        }

    def do_import(self, text, store):
        """Bulk paste. Accepts `front <sep> back [<sep> example [<sep> pos]]`, tab or pipe.

        Deliberately forgiving about the separator and column count -- this is a paste box,
        and rejecting the whole batch over one bad line would be the wrong trade. Bad lines
        are reported and the rest goes in.
        """
        added, skipped, errors = 0, 0, []
        for number, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in re.split(r"\t|\s*\|\s*", line)]
            if len(parts) < 2 or not parts[0] or not parts[1]:
                errors.append(f"line {number}: need front and back")
                continue
            entry = self.clean_entry(
                {"front": parts[0], "back": parts[1],
                 "example": parts[2] if len(parts) > 2 else "",
                 "pos": parts[3] if len(parts) > 3 else "",
                 "type": "structure" if re.search(r"\+|\.\.\.", parts[0]) else "word"},
                store["entries"])
            if isinstance(entry, str):
                skipped += 1
                continue
            store["entries"].append(entry)
            added += 1
        return added, skipped, errors


if __name__ == "__main__":
    if not MONGO_URI:
        sys.exit("no MONGODB_URI: put it in .env or atlas-credentials.env")
    live = db_alive()
    with _lock:
        store = read_store()
    print(f"vocab-display host on http://localhost:{PORT}")
    if live:
        print(f"  store  mongodb {MONGO_DB}  ({len(store['entries'])} entries)")
        print(f"  mirror {STORE}  (every {MIRROR_INTERVAL_S}s when changed)")
        threading.Thread(target=mirror_loop, daemon=True).start()
        _mirror_dirty.set()          # one snapshot at startup, so the mirror is never stale
    else:
        print(f"  store  DATABASE UNREACHABLE -- read-only from the mirror {STORE}")
        print(f"         ({len(store['entries'])} entries; writes will answer 503)")
    print(f"  token  {store['token']}   <- paste into firmware config.h")
    print(f"  daily  {store['config']['daily_words']} words + "
          f"{store['config']['daily_structures']} structures")
    sys.stdout.flush()
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
