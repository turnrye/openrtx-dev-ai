"""Research sources and the contract each one implements.

Every source is a module in this package with:

    NAME          short name: its SQLite file (<cache>/<NAME>.sqlite), its
                  schema name in `openrtx sql`, and its prefix in refs
    PERIOD        datetime.timedelta: how old its cache may get before a read
                  refreshes it (overridable with `period.<NAME> = 2h` in config)
    DESCRIPTION   one line for `openrtx sources`

    sync(db_path, full=False) -> str
        Fetch changes into db_path, creating it if needed, and return a
        one-line summary. Record the outcome with record_sync(). Raise
        SourceError (or let a network error escape) on failure; the caller
        keeps serving the old cache.
    last_sync(db_path) -> datetime | None
        When the last successful sync finished (UTC), or None if never.
    search(db_path, query, limit) -> list[Hit]
        Full-text search; `query` is free text from the user (quote it with
        fts_query() for FTS5 if a raw MATCH fails).
    owns(ref) -> bool
        Whether a ref string belongs to this source (see REFS below).
    show(db_path, ref) -> str | None
        Readable text for one item: its fields, body and discussion, with
        links. None when the ref isn't found.
    recent(db_path, since, limit) -> list[Hit]      (optional)
        What changed since `since` (ISO UTC string), newest first, one Hit
        per item with a snippet saying what happened.

REFS are always source-qualified, so `#N` never means two things:

    taiga#841                         Taiga item 841 (also a tasks.openrtx.org URL)
    OpenRTX/OpenRTX#520               GitHub issue or PR (also a github.com URL)
    pad:officehours, pad:officehours@9689   the Office Hours pad, optionally at a revision

sync() may take extra keyword arguments with defaults (e.g. a test hook).
After a failed sync, reads keep using the cache and don't retry for
RETRY_AFTER_FAILURE, so a source that's down or rate-limited stays quiet.

Sources must be stdlib-only (Python 3.9+), work on macOS, Linux and Windows,
never write to the remote service, and log progress with log() (stderr), so
stdout stays the command's answer.
"""

from __future__ import annotations

import datetime as dt
import importlib
import os
import sqlite3
import sys

from .. import paths

# In the order `openrtx search` lists them
SOURCE_MODULES = ["taiga", "github", "pad"]
RETRY_AFTER_FAILURE = dt.timedelta(minutes=10)


class SourceError(Exception):
    pass


class Hit(dict):
    """One search result: source, ref, kind, title, when (ISO date), url, snippet."""

    def __init__(self, source, ref, title, kind="", when="", url="", snippet=""):
        super().__init__(source=source, ref=ref, title=title, kind=kind,
                         when=when or "", url=url or "", snippet=snippet or "")


def log(*args):
    print(*args, file=sys.stderr, flush=True)


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def iso(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text):
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


def fts_query(text):
    """Quote every word so FTS5 treats user text literally."""
    return " ".join('"%s"' % t.replace('"', '""') for t in text.split())


def connect(db_path):
    """Read-write connection with the shared bookkeeping table in place."""
    db = sqlite3.connect(db_path, timeout=60, isolation_level=None)
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("CREATE TABLE IF NOT EXISTS source_syncs ("
               "finished_at TEXT NOT NULL, ok INTEGER NOT NULL, summary TEXT)")
    return db


def connect_ro(db_path):
    """Read-only connection to an existing cache.

    Not `mode=ro`: a WAL database can't be opened that way once its -shm file
    is gone (after a clean close or a copy), so open it normally and make the
    connection query-only. Never creates a missing file.
    """
    if not os.path.exists(db_path):
        raise sqlite3.OperationalError("no cache at %s" % db_path)
    db = sqlite3.connect(db_path, timeout=60)
    db.execute("PRAGMA query_only = ON")
    db.row_factory = sqlite3.Row
    return db


def record_sync(db, ok, summary=""):
    db.execute("INSERT INTO source_syncs VALUES (?, ?, ?)",
               (iso(now_utc()), 1 if ok else 0, summary))


def last_ok_sync(db_path):
    """Default last_sync(): newest ok row in source_syncs."""
    try:
        db = connect_ro(db_path)
        row = db.execute("SELECT max(finished_at) FROM source_syncs WHERE ok = 1").fetchone()
    except sqlite3.Error:
        return None
    return parse_iso(row[0]) if row and row[0] else None


def load(name):
    return importlib.import_module("%s.%s" % (__name__, name))


def available():
    """The source modules that exist (a missing optional one is skipped)."""
    found = []
    for name in SOURCE_MODULES:
        try:
            found.append(load(name))
        except ImportError as e:
            if e.name != "%s.%s" % (__name__, name):
                raise
    return found


def period(source):
    override = paths.load_config().get("period.%s" % source.NAME)
    return paths.parse_age(override) if override else source.PERIOD


def age(source):
    """How old the source's cache is, or None if it was never synced."""
    last = source.last_sync(paths.db_path(source.NAME))
    return None if last is None else now_utc() - last


def is_stale(source, max_age=None):
    a = age(source)
    return a is None or a > (period(source) if max_age is None else max_age)


def _failure_marker(source):
    return paths.db_path(source.NAME) + ".failed"


def recent_failure(source):
    """When the last failed sync happened, if within RETRY_AFTER_FAILURE."""
    try:
        t = dt.datetime.fromtimestamp(os.path.getmtime(_failure_marker(source)),
                                      dt.timezone.utc)
    except OSError:
        return None
    return t if now_utc() - t < RETRY_AFTER_FAILURE else None


def refresh(source, max_age=None, full=False, quiet=False):
    """Sync if stale (or always with full=True). Returns (synced, error).

    An explicit request (full=True or a max_age) always tries; an automatic
    refresh on read waits RETRY_AFTER_FAILURE after a failure.
    """
    if not full and not is_stale(source, max_age):
        return False, None
    if not full and max_age is None and recent_failure(source):
        return False, None
    a = age(source)
    if not quiet:
        log("refreshing %s (%s)…" % (source.NAME, "first sync, this can take a few minutes"
                                     if a is None else "last synced %s ago" % paths.format_age(a)))
    path = paths.db_path(source.NAME)
    before = source.last_sync(path)
    try:
        summary = source.sync(path, full=full)
    except Exception as e:  # keep serving the old cache
        with open(_failure_marker(source), "w") as f:
            f.write("%s\n" % e)
        if not quiet:
            log("  %s: sync failed (%s); using the cached copy, retrying in %d min"
                % (source.NAME, e, RETRY_AFTER_FAILURE.total_seconds() // 60))
        return False, e
    after = source.last_sync(path)
    if after is None or (before is not None and after <= before):
        # It returned without completing (e.g. partial, rate-limited): back off like a failure
        with open(_failure_marker(source), "w") as f:
            f.write("incomplete: %s\n" % summary)
        if not quiet and summary:
            log("  %s: %s" % (source.NAME, summary))
        return False, None
    try:
        os.remove(_failure_marker(source))
    except OSError:
        pass
    try:  # fold the WAL back in, so each cache is one self-contained file
        sqlite3.connect(path).execute("PRAGMA wal_checkpoint(TRUNCATE)").close()
    except sqlite3.Error:
        pass
    if not quiet and summary:
        log("  %s: %s" % (source.NAME, summary))
    return True, None
