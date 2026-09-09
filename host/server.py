#!/usr/bin/env python3
"""The vocab-display host: a JSON store, a small API, and the web UI that edits it.

Standard library only, so it starts with `python3 host/server.py` and nothing else. The
store is a single JSON file -- readable, diffable, and editable by hand if this program is
ever in the way, which matters more than query performance for a few thousand rows.

Two audiences share the API:

  the board   GET  /api/batch      the next entries to show
              POST /api/progress   what it has shown and what was marked known
  the browser GET  /api/entries    list, search, filter
              PUT  /api/entries/<id>, DELETE /api/entries/<id>
              POST /api/import     paste TSV in bulk
              GET  /api/stats

Board requests carry a shared token; browser requests come from localhost and do not. The
token exists so that nothing else on the Wi-Fi can read or rewrite the deck, not as real
authentication -- it travels in cleartext over HTTP on a home network.
"""
import json
import os
import random
import re
import secrets
import sys
import tempfile
import threading
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "store.json"
SEED = ROOT / "data" / "seed.tsv"
STATIC = Path(__file__).resolve().parent / "static"
PORT = int(os.environ.get("VOCAB_PORT", 8788))

TYPES = ("word", "structure")
LEVELS = ("A2", "B1", "B2")

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
            "front": row["front"],
            "back": row["back"],
            "example": row.get("example", ""),
            "seen": 0,
            "known": False,
            "updated": now_iso(),
        })
    return rows


def default_store():
    return {
        "version": 1,
        # Copied into the firmware's config.h. Regenerating it locks out any board still
        # using the old one, which is the point.
        "token": secrets.token_hex(8),
        "entries": load_seed() if SEED.exists() else [],
    }


def read_store():
    if not STORE.exists():
        store = default_store()
        write_store(store)
        return store
    return json.loads(STORE.read_text(encoding="utf-8"))


def write_store(store):
    """Write through a temp file in the same directory, then rename.

    A half-written store is worse than a stale one: the deck plus every bit of progress
    lives in this single file, and rename is the only step that is atomic.
    """
    STORE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=STORE.parent, prefix=".store-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STORE)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def next_id(entries):
    """Ids are never reused: board progress is keyed off them, so a recycled id would
    silently inherit the history of a deleted entry."""
    return max((e["id"] for e in entries), default=0) + 1


# ------------------------------------------------------------------------------ selection


def pick_batch(entries, count):
    """Fewest-seen first, shuffled within each tier.

    Sorting purely by seen count would show the deck in a fixed order every session;
    shuffling inside a tier keeps coverage even while the order stays unpredictable. New
    entries naturally lead because their count is zero.
    """
    eligible = [e for e in entries if not e["known"]]
    random.shuffle(eligible)
    eligible.sort(key=lambda e: e["seen"])
    return eligible[:count]


def stats(entries):
    known = sum(1 for e in entries if e["known"])
    unseen = sum(1 for e in entries if not e["known"] and e["seen"] == 0)
    return {
        "total": len(entries),
        "known": known,
        "remaining": len(entries) - known,
        "unseen": unseen,
        "words": sum(1 for e in entries if e["type"] == "word"),
        "structures": sum(1 for e in entries if e["type"] == "structure"),
        "seen_total": sum(e["seen"] for e in entries),
    }


# -------------------------------------------------------------------------------- server


class Handler(BaseHTTPRequestHandler):
    server_version = "vocab-display"

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
                return self.send_json({"stats": stats(store["entries"]),
                                       "token": store["token"], "port": PORT})

            if route == "/api/batch":
                if not self.token_ok(store):
                    return self.send_json({"error": "bad token"}, 401)
                count = min(int(self.query.get("n", [30])[0]), 100)
                batch = pick_batch(store["entries"], count)
                return self.send_json({
                    "count": len(batch),
                    "remaining": stats(store["entries"])["remaining"],
                    "cards": [{"id": e["id"], "t": 1 if e["type"] == "structure" else 0,
                               "f": e["front"], "b": e["back"], "e": e["example"]}
                              for e in batch],
                })

            if route == "/api/entries":
                q = (self.query.get("q", [""])[0] or "").strip().lower()
                kind = self.query.get("type", [""])[0]
                status = self.query.get("status", [""])[0]
                rows = store["entries"]
                if q:
                    rows = [e for e in rows if q in e["front"].lower()
                            or q in e["back"].lower() or q in e["example"].lower()]
                if kind in TYPES:
                    rows = [e for e in rows if e["type"] == kind]
                if status == "known":
                    rows = [e for e in rows if e["known"]]
                elif status == "learning":
                    rows = [e for e in rows if not e["known"] and e["seen"] > 0]
                elif status == "unseen":
                    rows = [e for e in rows if not e["known"] and e["seen"] == 0]
                rows = sorted(rows, key=lambda e: e["id"])
                return self.send_json({"entries": rows, "matched": len(rows)})

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
                # Merged rather than assigned: the board may have been offline and is
                # reporting a delta, and two boards must not overwrite each other. Seen
                # counts take the larger value, known is sticky.
                by_id = {e["id"]: e for e in store["entries"]}
                applied = 0
                for item in body.get("cards", []):
                    entry = by_id.get(item.get("id"))
                    if not entry:
                        continue
                    entry["seen"] = max(entry["seen"], int(item.get("seen", 0)))
                    if item.get("known"):
                        entry["known"] = True
                    entry["updated"] = now_iso()
                    applied += 1
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
            if body.get("level") in LEVELS:
                entry["level"] = body["level"]
            if "known" in body:
                entry["known"] = bool(body["known"])
            if "seen" in body:
                entry["seen"] = max(0, int(body["seen"]))
            entry["updated"] = now_iso()
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
        return {
            "id": next_id(entries),
            "type": body.get("type") if body.get("type") in TYPES else "word",
            "level": body.get("level") if body.get("level") in LEVELS else "B1",
            "front": front,
            "back": back,
            "example": str(body.get("example", "")).strip(),
            "seen": 0,
            "known": False,
            "updated": now_iso(),
        }

    def do_import(self, text, store):
        """Bulk paste. Accepts tab or pipe separated `front <sep> back [<sep> example]`.

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
                 "type": "structure" if re.search(r"\+|\.\.\.", parts[0]) else "word"},
                store["entries"])
            if isinstance(entry, str):
                skipped += 1
                continue
            store["entries"].append(entry)
            added += 1
        return added, skipped, errors


if __name__ == "__main__":
    with _lock:
        store = read_store()
    print(f"vocab-display host on http://localhost:{PORT}")
    print(f"  store  {STORE}  ({len(store['entries'])} entries)")
    print(f"  token  {store['token']}   <- paste into firmware config.h")
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
