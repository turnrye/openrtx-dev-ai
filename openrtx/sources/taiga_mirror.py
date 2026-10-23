"""Mirror the OpenRTX Taiga project into a local, versioned SQLite database.

Vendored from the openrtx-taiga repo as the `taiga` research source of
openrtx-dev-ai. It runs in local mode only here: `openrtx taiga …` always
passes --db with the cache file, and the Datasette remote isn't used.

    taiga_mirror.py sync [--full]   pull changes (incremental; full weekly or on request)
    taiga_mirror.py show REF        one item: fields, description, activity, links
    taiga_mirror.py search QUERY    full-text search (FTS5 query syntax)
    taiga_mirror.py log [...]       activity log, newest first
    taiga_mirror.py info            sync history and object counts
    taiga_mirror.py sql QUERY       read-only SQL, printed as rows or --json

With a remote configured (TAIGA_REMOTE, or `remote = URL` in
~/.config/openrtx-taiga/config) every command runs against a Datasette server
instead of a local file; `sync` then asks the server to sync.

Every API object is stored as the JSON the API returned (minus expiring media
tokens), keyed by the endpoint it came from. Each change seen between syncs
becomes a new row in `versions`, with per-field diffs in `field_changes`.
Views named after the endpoints expose the API fields as columns.
See README.md for the schema and example queries.

Stdlib only; Python 3.9+. Anonymous access is enough for public projects; set
TAIGA_TOKEN to a Taiga auth token to mirror what a logged-in member sees.
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
import datetime as dt
import hashlib
import http.client
import json
import os
import re
import sqlite3
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = os.environ.get("TAIGA_URL", "https://tasks.openrtx.org").rstrip("/")
PROJECT_SLUG = os.environ.get("TAIGA_PROJECT", "openrtx")
HERE = os.path.dirname(os.path.realpath(__file__))
DEFAULT_DB = os.environ.get("TAIGA_DB", os.path.join(HERE, "openrtx-taiga.sqlite"))
SCHEMA_VERSION = 1
FULL_SYNC_EVERY = dt.timedelta(days=7)

# Items with a detail endpoint and per-object history: kind -> history type.
ITEM_KINDS = {
    "epics": "epic",
    "userstories": "userstory",
    "tasks": "task",
    "issues": "issue",
    "wiki": "wiki",
}
# Short type names, also the URL segment in the Taiga web UI.
ITEM_TYPE = {
    "epics": "epic",
    "userstories": "us",
    "tasks": "task",
    "issues": "issue",
    "wiki": "wiki",
}

# Project-wide lists, one request each.
LIST_KINDS = [
    "milestones", "memberships", "users", "roles", "wiki-links",
    "epic-statuses", "userstory-statuses", "task-statuses", "issue-statuses",
    "issue-types", "priorities", "severities", "points", "swimlanes",
    "userstory-due-dates", "task-due-dates", "issue-due-dates",
    "epic-custom-attributes", "userstory-custom-attributes",
    "task-custom-attributes", "issue-custom-attributes",
    "epics/attachments", "userstories/attachments", "tasks/attachments",
    "issues/attachments", "wiki/attachments",
]
CUSTOM_ATTRIBUTES = {
    "epics": "epic-custom-attributes",
    "userstories": "userstory-custom-attributes",
    "tasks": "task-custom-attributes",
    "issues": "issue-custom-attributes",
}
# Per-item child collections that disappear with their parent. History is
# not listed: it stays as the record of what the item was.
CHILD_KINDS = ["epics/related_userstories"] + [
    k + "/custom-attributes-values" for k in CUSTOM_ATTRIBUTES
]

# Kept in the stored JSON but ignored when deciding whether an object changed:
# rolling counters and denormalised copies of other objects.
VOLATILE = {
    "projects": {
        "total_activity", "total_activity_last_week",
        "total_activity_last_month", "total_activity_last_year",
        "total_fans_last_week", "total_fans_last_month",
        "total_fans_last_year", "totals_updated_datetime",
    },
    "milestones": {"user_stories"},
    "userstories": {"tasks"},
    "epics": {"user_stories_counts"},
}
# Dropped entirely: navigation hints that change whenever a neighbour does.
DROP = {"neighbors"}
# Lists of permission names that Taiga returns in no particular order.
SORTED_LISTS = {"my_permissions", "anon_permissions", "public_permissions",
                "permissions"}

# Fields compared against the list endpoint to decide whether an item needs
# its detail and history refetched in an incremental sync.
CHANGE_MARKERS = ("version", "modified_date", "total_comments")


def log(*args):
    print(*args, file=sys.stderr, flush=True)


def iso_now():
    now = dt.datetime.now(dt.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (now.microsecond // 1000)


def dumps(value):
    return json.dumps(value, ensure_ascii=False)


# --------------------------------------------------------------------------
# API client
# --------------------------------------------------------------------------

class Taiga:
    RETRY_CODES = (429, 500, 502, 503, 504)

    def __init__(self, token=None):
        self.api = BASE_URL + "/api/v1"
        self.headers = {
            "Accept": "application/json",
            "Accept-Language": "en",
            "User-Agent": "openrtx-taiga-mirror/1",
        }
        if token:
            self.headers["Authorization"] = "Bearer " + token
        self.requests = 0
        self.bytes = 0
        self._lock = threading.Lock()

    def url(self, path, **params):
        query = urllib.parse.urlencode(params)
        return "%s/%s%s" % (self.api, path, "?" + query if query else "")

    def fetch(self, url, headers=None):
        hdrs = dict(self.headers)
        hdrs.update(headers or {})
        delay = 1.0
        for attempt in range(6):
            try:
                req = urllib.request.Request(url, headers=hdrs)
                with urllib.request.urlopen(req, timeout=120) as resp:
                    body = resp.read()
                    with self._lock:
                        self.requests += 1
                        self.bytes += len(body)
                    return body, resp.headers
            except urllib.error.HTTPError as e:
                if e.code not in self.RETRY_CODES or attempt == 5:
                    raise
            except (urllib.error.URLError, TimeoutError, ConnectionError,
                    http.client.HTTPException):
                if attempt == 5:
                    raise
            time.sleep(delay)
            delay *= 2
        raise AssertionError("unreachable")

    def get(self, path, **params):
        body, _ = self.fetch(self.url(path, **params))
        return json.loads(body)

    def get_all(self, path, **params):
        """GET a collection, unpaginated where the endpoint allows it."""
        body, hdrs = self.fetch(self.url(path, **params),
                                {"x-disable-pagination": "1"})
        data = json.loads(body)
        nxt = hdrs.get("x-pagination-next")
        while nxt:
            body, hdrs = self.fetch(nxt)
            data.extend(json.loads(body))
            nxt = hdrs.get("x-pagination-next")
        return data


def optional(fn, *args, **kwargs):
    """Run an API call, mapping 'not visible to us' to None."""
    try:
        return fn(*args, **kwargs)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            return None
        raise


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------

_TOKEN = re.compile(r"([?&])token=[^&#]*(&?)")


def _untoken(url):
    # Signed media URLs get a fresh token on every request.
    return _TOKEN.sub(lambda m: m.group(1) if m.group(2) else "", url)


def normalize(value):
    if isinstance(value, dict):
        return {k: (sorted(v) if k in SORTED_LISTS and isinstance(v, list)
                    and all(isinstance(x, str) for x in v) else normalize(v))
                for k, v in value.items() if k not in DROP}
    if isinstance(value, list):
        return [normalize(v) for v in value]
    if (isinstance(value, str) and value.startswith("http")
            and "token=" in value and "/media/" in value
            and not any(c.isspace() for c in value)):
        return _untoken(value)
    return value


def digest(kind, data):
    volatile = VOLATILE.get(kind)
    if volatile:
        data = {k: v for k, v in data.items() if k not in volatile}
    canon = json.dumps(data, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False)
    return hashlib.sha1(canon.encode()).hexdigest()


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS syncs (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    mode        TEXT NOT NULL,      -- initial | full | incremental
    status      TEXT NOT NULL,      -- running | ok | failed
    requests    INTEGER,
    bytes       INTEGER,
    summary     TEXT,               -- JSON {kind: {op: count}}
    error       TEXT
);
CREATE TABLE IF NOT EXISTS objects (
    kind          TEXT NOT NULL,    -- API endpoint, e.g. userstories, history
    id            TEXT NOT NULL,    -- API id (integers and uuids as text)
    parent        TEXT,             -- 'userstories:162' for per-item data
    data          TEXT NOT NULL,    -- API JSON, last seen
    hash          TEXT NOT NULL,
    rev           INTEGER NOT NULL, -- latest versions.rev
    first_seen_at TEXT NOT NULL,
    changed_at    TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    deleted_at    TEXT,             -- set when it vanished upstream
    PRIMARY KEY (kind, id)
);
CREATE INDEX IF NOT EXISTS objects_parent ON objects (kind, parent);
CREATE TABLE IF NOT EXISTS versions (
    kind        TEXT NOT NULL,
    id          TEXT NOT NULL,
    rev         INTEGER NOT NULL,
    sync_id     INTEGER NOT NULL REFERENCES syncs (id),
    observed_at TEXT NOT NULL,
    op          TEXT NOT NULL CHECK (op IN ('create', 'update', 'delete',
                                            'restore')),
    data        TEXT,               -- NULL for delete
    hash        TEXT,
    PRIMARY KEY (kind, id, rev)
);
CREATE INDEX IF NOT EXISTS versions_sync ON versions (sync_id);
CREATE TABLE IF NOT EXISTS field_changes (
    sync_id INTEGER NOT NULL REFERENCES syncs (id),
    kind    TEXT NOT NULL,
    id      TEXT NOT NULL,
    rev     INTEGER NOT NULL,
    field   TEXT NOT NULL,
    old     TEXT,                   -- JSON; NULL if the field was absent
    new     TEXT,
    PRIMARY KEY (kind, id, rev, field)
);
CREATE TABLE IF NOT EXISTS files (
    sha1       TEXT PRIMARY KEY,
    size       INTEGER NOT NULL,
    content    BLOB NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS refs (
    object     TEXT PRIMARY KEY,
    type       TEXT NOT NULL,
    ref        INTEGER,
    subject    TEXT,
    url        TEXT,
    deleted_at TEXT,
    known_from TEXT NOT NULL        -- api | timeline (deleted before mirroring)
);
CREATE INDEX IF NOT EXISTS refs_ref ON refs (ref);
CREATE TABLE IF NOT EXISTS mentions (
    src_object TEXT NOT NULL,
    src_type   TEXT,
    src_ref    INTEGER,
    via        TEXT NOT NULL,       -- description | comment
    history_id TEXT,
    at         TEXT,
    to_ref     INTEGER NOT NULL,
    to_type    TEXT,                -- NULL: no Taiga item has that ref
    context    TEXT
);
CREATE INDEX IF NOT EXISTS mentions_to ON mentions (to_ref);
CREATE TABLE IF NOT EXISTS links (
    src_object TEXT NOT NULL,
    src_type   TEXT,
    src_ref    INTEGER,
    via        TEXT NOT NULL,
    history_id TEXT,
    at         TEXT,
    url        TEXT NOT NULL,
    host       TEXT
);
CREATE INDEX IF NOT EXISTS links_url ON links (url);
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5 (
    subject, body,
    type UNINDEXED, ref UNINDEXED, source UNINDEXED, object UNINDEXED,
    at UNINDEXED,
    tokenize = 'porter unicode61'
);
CREATE TABLE IF NOT EXISTS schema_docs (
    name        TEXT PRIMARY KEY,
    description TEXT NOT NULL
);
"""

DOCS = {
    "objects": "Current state of every mirrored API object, one row per "
               "(kind, id). kind is the API endpoint; data is its JSON. "
               "Deleted upstream objects keep their last data and get "
               "deleted_at.",
    "versions": "Append-only: one row per observed create/update/delete/"
                "restore of an object, with the full JSON at that point.",
    "field_changes": "Top-level field diffs (JSON old/new) for each update "
                     "or restore in versions.",
    "syncs": "One row per sync run with mode, status and per-kind counts.",
    "files": "Attachment contents, keyed by sha1 (join <kind>_attachments "
             "on sha1).",
    "refs": "Every epic/story/task/issue by Taiga ref, with type, subject "
            "and web URL, including deleted ones: known_from='timeline' "
            "marks items deleted before the mirror saw them. Derived.",
    "items": "Readable view over live epics, stories, tasks and issues: "
             "status, people, milestone, parent story/epics, description.",
    "comments": "Every comment, with the item it belongs to.",
    "activity_log": "Unified activity feed. source=history: Taiga's own "
                     "per-item edit log with diffs and comments; "
                     "source=timeline: creations and events without "
                     "per-item history (milestones, memberships, epic links, "
                     "deleted items); source=mirror: changes only the mirror "
                     "saw (deletions, comment edits, settings). "
                     "ORDER BY at DESC.",
    "mentions": "#ref mentions in descriptions and comments. to_type NULL "
                "means no Taiga item has that ref (often a GitHub number).",
    "links": "URLs in descriptions and comments.",
    "search": "FTS5 index: subjects, descriptions, comments, milestones, "
              "wiki, and old descriptions (source='old-description'). "
              "Use: SELECT * FROM search WHERE search MATCH 'cmake'.",
    "<endpoint>": "Generated view per API endpoint ('-' and '/' become "
                  "'_'), one column per API field; live objects only. "
                  "Nested values are JSON text. _rev/_changed_at come from "
                  "the mirror.",
    "<endpoint>_versions": "Generated view per endpoint over versions: same "
                           "columns plus _op, _sync_id, _observed_at.",
}


def connect(path):
    db = sqlite3.connect(path, timeout=60, isolation_level=None)
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA foreign_keys = ON")
    return db


def init_schema(db):
    db.executescript(SCHEMA)
    db.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
    db.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", [
        ("base_url", BASE_URL),
        ("project_slug", PROJECT_SLUG),
        ("schema_version", str(SCHEMA_VERSION)),
    ])
    db.execute("DELETE FROM schema_docs")
    db.executemany("INSERT INTO schema_docs VALUES (?, ?)", DOCS.items())


# --------------------------------------------------------------------------
# Versioned store
# --------------------------------------------------------------------------

class Store:
    def __init__(self, db, sync_id, now):
        self.db = db
        self.sync_id = sync_id
        self.now = now
        self.summary = collections.defaultdict(collections.Counter)

    def stored(self, kind, oid):
        row = self.db.execute(
            "SELECT data FROM objects WHERE kind = ? AND id = ?",
            (kind, oid)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, kind, oid, data, parent=None):
        oid = str(oid)
        text = dumps(data)
        h = digest(kind, data)
        row = self.db.execute(
            "SELECT data, hash, rev, deleted_at FROM objects "
            "WHERE kind = ? AND id = ?", (kind, oid)).fetchone()
        if row is None:
            self.db.execute(
                "INSERT INTO objects (kind, id, parent, data, hash, rev, "
                "first_seen_at, changed_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (kind, oid, parent, text, h, self.now, self.now, self.now))
            self._version(kind, oid, 1, "create", text, h)
            return
        old_text, old_hash, rev, deleted_at = row
        if deleted_at is None and old_hash == h:
            if old_text != text:  # only volatile fields moved
                self.db.execute(
                    "UPDATE objects SET data = ?, last_seen_at = ? "
                    "WHERE kind = ? AND id = ?", (text, self.now, kind, oid))
            else:
                self.db.execute(
                    "UPDATE objects SET last_seen_at = ? "
                    "WHERE kind = ? AND id = ?", (self.now, kind, oid))
            return
        op = "restore" if deleted_at else "update"
        rev += 1
        self.db.execute(
            "UPDATE objects SET parent = ?, data = ?, hash = ?, rev = ?, "
            "changed_at = ?, last_seen_at = ?, deleted_at = NULL "
            "WHERE kind = ? AND id = ?",
            (parent, text, h, rev, self.now, self.now, kind, oid))
        self._version(kind, oid, rev, op, text, h)
        self._field_changes(kind, oid, rev, json.loads(old_text), data)

    def delete_missing(self, kind, seen, parent=None, scoped=False):
        """Mark objects of kind (within parent if scoped) not in seen."""
        sql = "SELECT id, rev FROM objects WHERE kind = ? AND deleted_at IS NULL"
        args = [kind]
        if scoped:
            sql += " AND parent IS ?"
            args.append(parent)
        gone = [(oid, rev) for oid, rev in self.db.execute(sql, args)
                if oid not in seen]
        for oid, rev in gone:
            self.db.execute(
                "UPDATE objects SET rev = ?, changed_at = ?, deleted_at = ? "
                "WHERE kind = ? AND id = ?",
                (rev + 1, self.now, self.now, kind, oid))
            self._version(kind, oid, rev + 1, "delete", None, None)
        return [oid for oid, _ in gone]

    def _version(self, kind, oid, rev, op, text, h):
        self.db.execute(
            "INSERT INTO versions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, oid, rev, self.sync_id, self.now, op, text, h))
        self.summary[kind][op] += 1

    def _field_changes(self, kind, oid, rev, old, new):
        volatile = VOLATILE.get(kind, ())
        missing = object()
        for field in list(old) + [k for k in new if k not in old]:
            if field in volatile:
                continue
            a, b = old.get(field, missing), new.get(field, missing)
            if a != b:
                self.db.execute(
                    "INSERT INTO field_changes VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (self.sync_id, kind, oid, rev, field,
                     None if a is missing else dumps(a),
                     None if b is missing else dumps(b)))


# --------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------

def choose_mode(db, full):
    rows = db.execute(
        "SELECT mode, started_at FROM syncs WHERE status = 'ok' "
        "ORDER BY id DESC").fetchall()
    if not rows:
        return "initial"
    if full:
        return "full"
    last_full = next((r[1] for r in rows if r[0] != "incremental"), None)
    if last_full is None:
        return "full"
    age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(
        last_full.replace("Z", "+00:00"))
    return "full" if age > FULL_SYNC_EVERY else "incremental"


def needs_refresh(db, kind, item):
    row = db.execute(
        "SELECT data, deleted_at FROM objects WHERE kind = ? AND id = ?",
        (kind, str(item["id"]))).fetchone()
    if row is None or row[1] is not None:
        return True
    old = json.loads(row[0])
    if kind != "wiki" and "description" not in old:
        return True  # never got the detail representation
    return any(old.get(k) != item.get(k) for k in CHANGE_MARKERS)


def fetch_timeline(db, client, pid, mode):
    path = "timeline/project/%d" % pid
    if mode != "incremental":
        return client.get_all(path)
    known = db.execute(
        "SELECT max(CAST(id AS INTEGER)) FROM objects "
        "WHERE kind = 'timeline'").fetchone()[0] or 0
    out, page = [], 1
    while True:
        body, hdrs = client.fetch(client.url(path, page=page))
        entries = json.loads(body)
        out.extend(entries)
        if (not entries or min(e["id"] for e in entries) <= known
                or not hdrs.get("x-pagination-next")):
            return out
        page += 1


def run_sync(db, client, full=False, workers=8):
    init_schema(db)
    mode = choose_mode(db, full)
    started = iso_now()
    sync_id = db.execute(
        "INSERT INTO syncs (started_at, mode, status) VALUES (?, ?, 'running')",
        (started, mode)).lastrowid
    log("sync %d (%s) from %s" % (sync_id, mode, BASE_URL))
    try:
        fetched = fetch_all(db, client, mode, workers)
        db.execute("BEGIN IMMEDIATE")
        store = Store(db, sync_id, started)
        apply_all(store, fetched)
        rebuild_derived(db)
        db.execute(
            "UPDATE syncs SET finished_at = ?, status = 'ok', requests = ?, "
            "bytes = ?, summary = ? WHERE id = ?",
            (iso_now(), client.requests, client.bytes,
             dumps({k: dict(v) for k, v in sorted(store.summary.items())}),
             sync_id))
        db.execute("COMMIT")
    except BaseException as e:
        if db.in_transaction:
            db.execute("ROLLBACK")
        db.execute(
            "UPDATE syncs SET finished_at = ?, status = 'failed', requests = ?, "
            "error = ? WHERE id = ?",
            (iso_now(), client.requests, repr(e), sync_id))
        raise
    return sync_id, mode, store.summary


def fetch_all(db, client, mode, workers):
    t0 = time.time()
    project = client.get("projects/by_slug", slug=PROJECT_SLUG)
    pid = project["id"]
    with cf.ThreadPoolExecutor(workers) as pool:
        kinds = list(ITEM_KINDS) + LIST_KINDS
        lists = dict(zip(kinds, pool.map(
            lambda k: optional(client.get_all, k, project=pid), kinds)))
        log("  lists: %d endpoints in %.1fs" % (len(kinds), time.time() - t0))

        jobs = []
        for kind, htype in ITEM_KINDS.items():
            with_cav = bool(lists.get(CUSTOM_ATTRIBUTES.get(kind)))
            for item in lists.get(kind) or []:
                if mode != "incremental" or needs_refresh(db, kind, item):
                    oid = item["id"]
                    jobs.append(("detail", kind, oid, "%s/%d" % (kind, oid)))
                    jobs.append(("history", kind, oid,
                                 "history/%s/%d" % (htype, oid)))
                    if with_cav:
                        jobs.append(("cav", kind, oid,
                                     "%s/custom-attributes-values/%d"
                                     % (kind, oid)))
        for epic in lists.get("epics") or []:
            jobs.append(("related", "epics", epic["id"],
                         "epics/%d/related_userstories" % epic["id"]))

        def run(job):
            what, _, _, path = job
            if what in ("history", "related"):
                return optional(client.get_all, path)
            return optional(client.get, path)

        results = {}
        futures = {pool.submit(run, j): j for j in jobs}
        for n, fut in enumerate(cf.as_completed(futures), 1):
            results[futures[fut]] = fut.result()
            if n % 200 == 0:
                log("  items: %d/%d requests" % (n, len(jobs)))
        log("  items: %d requests in %.1fs" % (len(jobs), time.time() - t0))

        timeline = fetch_timeline(db, client, pid, mode)
        log("  timeline: %d entries" % len(timeline))

        have = {r[0] for r in db.execute("SELECT sha1 FROM files")}
        wanted = {}
        for kind in LIST_KINDS:
            if kind.endswith("/attachments"):
                for a in lists.get(kind) or []:
                    if a.get("sha1") and a["sha1"] not in have and a.get("url"):
                        wanted[a["sha1"]] = a["url"]
        files = {}
        for sha1, body in zip(wanted, pool.map(
                lambda u: client.fetch(u)[0], wanted.values())):
            actual = hashlib.sha1(body).hexdigest()
            if actual != sha1:
                log("  warning: attachment %s has sha1 %s" % (sha1, actual))
            files[actual] = body
        if files:
            log("  attachments: %d files" % len(files))

    return {"project": project, "lists": lists, "results": results,
            "timeline": timeline, "files": files, "full": mode != "incremental"}


def apply_all(store, fetched):
    db = store.db
    project = fetched["project"]
    store.put("projects", project["id"], normalize(project))

    details = {}
    for (what, kind, oid, _), value in fetched["results"].items():
        if value is None:
            continue
        if what == "detail":
            details[(kind, oid)] = value
        elif what == "history":
            parent = "%s:%d" % (kind, oid)
            seen = set()
            for entry in value:
                store.put("history", entry["id"], normalize(entry), parent)
                seen.add(entry["id"])
            store.delete_missing("history", seen, parent, scoped=True)
        elif what == "related":
            parent = "epics:%d" % oid
            seen = set()
            for rel in value:
                rid = "%d:%d" % (rel["epic"], rel["user_story"])
                store.put("epics/related_userstories", rid, normalize(rel),
                          parent)
                seen.add(rid)
            store.delete_missing("epics/related_userstories", seen, parent,
                                 scoped=True)
        elif what == "cav":
            store.put(kind + "/custom-attributes-values", oid,
                      normalize(value), "%s:%d" % (kind, oid))

    for kind, items in fetched["lists"].items():
        if items is None:
            continue  # endpoint not visible: leave the stored copy alone
        seen = set()
        for item in items:
            oid = str(item["id"])
            seen.add(oid)
            if kind in ITEM_KINDS:
                detail = details.get((kind, item["id"]))
                if detail is not None:
                    data = normalize(detail)
                else:
                    # Unchanged since the last detail fetch: refresh the list
                    # fields, keep the detail-only ones (description, ...).
                    data = store.stored(kind, oid) or {}
                    data.update(normalize(item))
            else:
                data = normalize(item)
            store.put(kind, oid, data)
        for oid in store.delete_missing(kind, seen):
            if kind in ITEM_KINDS:
                for child in CHILD_KINDS:
                    store.delete_missing(child, set(), "%s:%s" % (kind, oid),
                                         scoped=True)

    for entry in fetched["timeline"]:
        store.put("timeline", entry["id"], normalize(entry))

    for sha1, body in fetched["files"].items():
        db.execute("INSERT OR IGNORE INTO files VALUES (?, ?, ?, ?)",
                   (sha1, len(body), body, store.now))


# --------------------------------------------------------------------------
# Derived tables and views
# --------------------------------------------------------------------------

def view_name(kind):
    return kind.replace("-", "_").replace("/", "_")


def ident(name):
    return '"%s"' % name.replace('"', '""')


def sql_str(text):
    return "'%s'" % text.replace("'", "''")


def json_col(source, key):
    return "json_extract(%s, %s) AS %s" % (
        source, sql_str('$."%s"' % key.replace('"', '\\"')), ident(key))


def rebuild_derived(db):
    for (name,) in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'view'").fetchall():
        db.execute("DROP VIEW %s" % ident(name))

    kinds = [r[0] for r in db.execute(
        "SELECT DISTINCT kind FROM objects ORDER BY kind")]
    for kind in kinds:
        cols = {}
        has_parent = False
        for data, parent in db.execute(
                "SELECT data, parent FROM objects WHERE kind = ?", (kind,)):
            for key in json.loads(data):
                cols.setdefault(key, None)
            has_parent = has_parent or parent is not None
        fields = [json_col("data", k) for k in cols]
        extra = ", parent AS _parent" if has_parent else ""
        db.execute(
            "CREATE VIEW %s AS SELECT %s, rev AS _rev, first_seen_at AS "
            "_first_seen_at, changed_at AS _changed_at%s FROM objects "
            "WHERE kind = %s AND deleted_at IS NULL"
            % (ident(view_name(kind)), ", ".join(fields), extra,
               sql_str(kind)))
        id_col = json_col("data", "id") if "id" not in cols else None
        db.execute(
            "CREATE VIEW %s AS SELECT id AS _object_id, rev AS _rev, "
            "op AS _op, sync_id AS _sync_id, observed_at AS _observed_at, "
            "%s FROM versions WHERE kind = %s"
            % (ident(view_name(kind) + "_versions"),
               ", ".join(fields + ([id_col] if id_col else [])),
               sql_str(kind)))

    rebuild_refs(db)
    for sql in convenience_views():
        db.execute(sql)
    rebuild_text(db)


def rebuild_refs(db):
    db.execute("DELETE FROM refs")
    for kind, segment in ITEM_TYPE.items():
        if kind == "wiki":
            continue
        db.execute(
            "INSERT INTO refs SELECT kind || ':' || id, ?, "
            "json_extract(data, '$.ref'), json_extract(data, '$.subject'), "
            "? || json_extract(data, '$.ref'), deleted_at, 'api' "
            "FROM objects WHERE kind = ?",
            (segment, "%s/project/%s/%s/" % (BASE_URL, PROJECT_SLUG, segment),
             kind))
    db.execute(
        "INSERT INTO refs SELECT 'wiki:' || id, 'wiki', NULL, "
        "json_extract(data, '$.slug'), ? || json_extract(data, '$.slug'), "
        "deleted_at, 'api' FROM objects WHERE kind = 'wiki'",
        ("%s/project/%s/wiki/" % (BASE_URL, PROJECT_SLUG),))
    # Items deleted before the mirror existed survive only in the timeline.
    known = {r[0] for r in db.execute("SELECT object FROM refs")}
    latest = {}
    for (text,) in db.execute(
            "SELECT data FROM objects WHERE kind = 'timeline' "
            "ORDER BY json_extract(data, '$.created')"):
        entry = json.loads(text)
        event = entry["event_type"]
        for kind, key in (("tasks", "task"), ("issues", "issue"),
                          ("epics", "epic"), ("userstories", "userstory")):
            if event.startswith(kind + "."):
                info = entry["data"].get(key) or {}
                obj = "%s:%s" % (kind, info.get("id"))
                if info.get("ref") and obj not in known:
                    latest[obj] = (ITEM_TYPE[kind], info["ref"],
                                   info.get("subject"))
                break
    db.executemany(
        "INSERT INTO refs VALUES (?, ?, ?, ?, ?, NULL, 'timeline')",
        [(obj, typ, ref, subject, "%s/project/%s/%s/%d"
          % (BASE_URL, PROJECT_SLUG, typ, ref))
         for obj, (typ, ref, subject) in latest.items()])


def convenience_views():
    name_of = ("(SELECT json_extract(x.data, '$.name') FROM objects x "
               "WHERE x.kind = '%s' AND x.id = "
               "CAST(json_extract(o.data, '$.%s') AS TEXT))")
    yield """
CREATE VIEW items AS
SELECT r.type, r.ref, r.subject,
       json_extract(o.data, '$.status_extra_info.name') AS status,
       json_extract(o.data, '$.is_closed') AS is_closed,
       json_extract(o.data, '$.owner_extra_info.username') AS owner,
       coalesce((SELECT group_concat(json_extract(u.data, '$.username'), ', ')
                 FROM json_each(o.data, '$.assigned_users') a
                 JOIN objects u ON u.kind = 'users'
                               AND u.id = CAST(a.value AS TEXT)),
                json_extract(o.data, '$.assigned_to_extra_info.username'))
           AS assigned,
       coalesce(json_extract(o.data, '$.milestone_name'), %s) AS milestone,
       (SELECT group_concat(json_extract(t.value, '$[0]'), ', ')
        FROM json_each(o.data, '$.tags') t) AS tags,
       json_extract(o.data, '$.user_story_extra_info.ref') AS story_ref,
       (SELECT group_concat(json_extract(e.value, '$.ref'), ', ')
        FROM json_each(o.data, '$.epics') e) AS epic_refs,
       %s AS issue_type, %s AS severity, %s AS priority,
       json_extract(o.data, '$.is_blocked') AS is_blocked,
       json_extract(o.data, '$.created_date') AS created_date,
       json_extract(o.data, '$.modified_date') AS modified_date,
       coalesce(json_extract(o.data, '$.finished_date'),
                json_extract(o.data, '$.finish_date')) AS finished_date,
       json_extract(o.data, '$.total_comments') AS total_comments,
       r.url,
       json_extract(o.data, '$.description') AS description,
       o.kind, CAST(o.id AS INTEGER) AS id, r.object,
       o.rev AS _rev, o.changed_at AS _changed_at
FROM objects o JOIN refs r ON r.object = o.kind || ':' || o.id
WHERE o.kind IN ('epics', 'userstories', 'tasks', 'issues')
  AND o.deleted_at IS NULL""" % (
        name_of % ("milestones", "milestone"),
        name_of % ("issue-types", "type"),
        name_of % ("severities", "severity"),
        name_of % ("priorities", "priority"))

    yield """
CREATE VIEW comments AS
SELECT h.id,
       json_extract(h.data, '$.created_at') AS created_at,
       json_extract(h.data, '$.user.username') AS username,
       json_extract(h.data, '$.user.name') AS user_name,
       r.type, r.ref, r.subject,
       json_extract(h.data, '$.comment') AS comment,
       json_extract(h.data, '$.edit_comment_date') AS edited_at,
       json_extract(h.data, '$.delete_comment_date') AS deleted_at,
       h.parent AS object, h.rev AS _rev
FROM objects h LEFT JOIN refs r ON r.object = h.parent
WHERE h.kind = 'history' AND h.deleted_at IS NULL
  AND (json_extract(h.data, '$.comment') <> ''
       OR json_extract(h.data, '$.delete_comment_date') IS NOT NULL)"""

    item_types = ", ".join(sql_str(k) for k in ITEM_KINDS)
    yield """
CREATE VIEW activity_log AS
SELECT json_extract(h.data, '$.created_at') AS at,
       'history' AS source,
       json_extract(h.data, '$.user.username') AS actor,
       CASE json_extract(h.data, '$.type')
           WHEN 2 THEN 'create' WHEN 3 THEN 'delete'
           ELSE CASE WHEN json_extract(h.data, '$.comment') <> ''
                      AND (SELECT count(*) FROM json_each(h.data, '$.diff')) = 0
                     THEN 'comment' ELSE 'change' END
       END AS action,
       r.type AS item_type, r.ref, r.subject,
       coalesce((SELECT group_concat(key, ', ')
                 FROM json_each(h.data, '$.diff')),
                (SELECT group_concat(key, ', ')
                 FROM json_each(h.data, '$.values_diff'))) AS fields,
       nullif(json_extract(h.data, '$.comment'), '') AS comment,
       json_extract(h.data, '$.values_diff') AS detail,
       h.parent AS object
FROM objects h LEFT JOIN refs r ON r.object = h.parent
WHERE h.kind = 'history' AND h.deleted_at IS NULL
UNION ALL
SELECT json_extract(t.data, '$.created'),
       'timeline',
       json_extract(t.data, '$.data.user.username'),
       CASE WHEN ev LIKE 'epics.relateduserstory.create' THEN 'add-story'
            WHEN ev LIKE 'epics.relateduserstory.change' THEN 'reorder-story'
            WHEN ev LIKE '%.create' THEN 'create'
            WHEN ev LIKE '%.change' THEN 'change'
            WHEN ev LIKE '%.delete' THEN 'delete' ELSE ev END,
       CASE WHEN ev LIKE 'userstories.%' THEN 'us'
            WHEN ev LIKE 'tasks.%' THEN 'task'
            WHEN ev LIKE 'issues.%' THEN 'issue'
            WHEN ev LIKE 'epics.%' THEN 'epic'
            WHEN ev LIKE 'wiki.%' THEN 'wiki'
            WHEN ev LIKE 'milestones.%' THEN 'milestone'
            WHEN ev LIKE 'projects.membership.%' THEN 'membership'
            WHEN ev LIKE 'projects.%' THEN 'project' ELSE ev END,
       coalesce(json_extract(t.data, '$.data.task.ref'),
                json_extract(t.data, '$.data.issue.ref'),
                json_extract(t.data, '$.data.epic.ref'),
                json_extract(t.data, '$.data.userstory.ref')),
       coalesce(json_extract(t.data, '$.data.task.subject'),
                json_extract(t.data, '$.data.issue.subject'),
                json_extract(t.data, '$.data.epic.subject'),
                json_extract(t.data, '$.data.userstory.subject'),
                json_extract(t.data, '$.data.milestone.name'),
                json_extract(t.data, '$.data.role.name'),
                json_extract(t.data, '$.data.project.name')),
       CASE WHEN ev LIKE 'epics.relateduserstory.%'
            THEN '#' || json_extract(t.data, '$.data.userstory.ref') || ' '
                 || json_extract(t.data, '$.data.userstory.subject')
            ELSE (SELECT group_concat(key, ', ')
                  FROM json_each(t.data, '$.data.values_diff')) END,
       nullif(json_extract(t.data, '$.data.comment'), ''),
       json_extract(t.data, '$.data.values_diff'),
       obj
FROM (SELECT data, json_extract(data, '$.event_type') AS ev,
             CASE WHEN json_extract(data, '$.event_type') LIKE 'epics.relateduserstory.%'
                  THEN NULL
                  WHEN json_extract(data, '$.data.task.id') IS NOT NULL
                  THEN 'tasks:' || json_extract(data, '$.data.task.id')
                  WHEN json_extract(data, '$.data.issue.id') IS NOT NULL
                  THEN 'issues:' || json_extract(data, '$.data.issue.id')
                  WHEN json_extract(data, '$.data.epic.id') IS NOT NULL
                  THEN 'epics:' || json_extract(data, '$.data.epic.id')
                  WHEN json_extract(data, '$.data.userstory.id') IS NOT NULL
                  THEN 'userstories:' || json_extract(data, '$.data.userstory.id')
             END AS obj
      FROM objects WHERE kind = 'timeline') t
WHERE ev NOT IN ('userstories.userstory.change', 'tasks.task.change',
                 'issues.issue.change', 'epics.epic.change',
                 'wiki.wikipage.change')
   OR NOT EXISTS (SELECT 1 FROM objects x
                  WHERE x.kind = 'history' AND x.parent = t.obj)
UNION ALL
SELECT observed_at,
       'mirror',
       NULL,
       CASE WHEN kind = 'history' AND op = 'update'
                 AND json_extract(d, '$.delete_comment_date') IS NOT NULL
            THEN 'comment-delete'
            WHEN kind = 'history' AND op = 'update' THEN 'comment-edit'
            WHEN kind = 'history' THEN 'comment-' || op
            WHEN kind = 'epics/related_userstories' AND op = 'delete'
            THEN 'remove-story'
            ELSE op END,
       coalesce(r.type, CASE kind WHEN 'milestones' THEN 'milestone'
                                  WHEN 'memberships' THEN 'membership'
                                  ELSE kind END),
       r.ref,
       coalesce(r.subject, json_extract(d, '$.subject'),
                json_extract(d, '$.name'), json_extract(d, '$.username'),
                json_extract(d, '$.full_name'), id),
       CASE WHEN kind = 'epics/related_userstories'
            THEN (SELECT '#' || s.ref || ' ' || s.subject FROM refs s
                  WHERE s.object = 'userstories:'
                                   || json_extract(d, '$.user_story'))
            ELSE (SELECT group_concat(f.field, ', ') FROM field_changes f
                  WHERE f.kind = m.kind AND f.id = m.id AND f.rev = m.rev)
       END,
       CASE WHEN kind = 'history'
            THEN nullif(json_extract(d, '$.comment'), '') END,
       (SELECT json_group_object(f.field, json_array(json(f.old), json(f.new)))
        FROM field_changes f
        WHERE f.kind = m.kind AND f.id = m.id AND f.rev = m.rev),
       coalesce(parent, kind || ':' || id)
FROM (SELECT v.kind, v.id, v.rev, v.op, v.observed_at, o.parent,
             coalesce(v.data, o.data) AS d
      FROM versions v
      JOIN syncs s ON s.id = v.sync_id
      JOIN objects o ON o.kind = v.kind AND o.id = v.id
      WHERE s.mode <> 'initial'
        -- creations and edits that Taiga's history or timeline already logs
        AND NOT (v.op = 'create' AND v.kind IN (
                     ITEM_KINDS, 'history', 'timeline', 'milestones',
                     'memberships', 'epics/related_userstories'))
        AND NOT (v.op = 'update' AND v.kind IN (
                     ITEM_KINDS, 'timeline', 'milestones', 'projects',
                     'epics/related_userstories'))) m
LEFT JOIN refs r ON r.object = coalesce(m.parent, m.kind || ':' || m.id)""".replace(
        "ITEM_KINDS", item_types)


REF_RE = re.compile(r"(?<![\w/&#])#(\d{1,6})\b")
URL_RE = re.compile(r"https?://[^\s<>()\[\]\"'`]+")


def rebuild_text(db):
    db.execute("DELETE FROM search")
    db.execute("DELETE FROM mentions")
    db.execute("DELETE FROM links")
    ref_types = {r: t for r, t in db.execute(
        "SELECT ref, type FROM refs WHERE ref IS NOT NULL "
        "ORDER BY deleted_at IS NULL")}

    sources = []  # (object, type, ref, via, history_id, at, subject, text)
    for row in db.execute(
            "SELECT object, type, ref, subject, description, modified_date "
            "FROM items"):
        sources.append(row[:3] + ("description", None, row[5], row[3],
                                  row[4] or ""))
    for row in db.execute(
            "SELECT object, type, ref, id, created_at, subject, comment "
            "FROM comments WHERE comment <> ''"):
        sources.append(row[:3] + ("comment",) + row[3:])

    for obj, typ, ref, via, hid, at, subject, text in sources:
        db.execute("INSERT INTO search VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (subject, text, typ, ref, via, obj, at))
        for m in REF_RE.finditer(text):
            to = int(m.group(1))
            lo, hi = max(0, m.start() - 40), min(len(text), m.end() + 40)
            ctx = " ".join(text[lo:hi].split())
            db.execute("INSERT INTO mentions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (obj, typ, ref, via, hid, at, to, ref_types.get(to),
                        ctx))
        for m in URL_RE.finditer(text):
            url = m.group(0).rstrip(".,;:!?*_")
            db.execute("INSERT INTO links VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (obj, typ, ref, via, hid, at, url,
                        urllib.parse.urlsplit(url).hostname))

    db.execute(
        "INSERT INTO search SELECT json_extract(data, '$.name'), '', "
        "'milestone', NULL, 'milestone', 'milestones:' || id, "
        "json_extract(data, '$.created_date') FROM objects "
        "WHERE kind = 'milestones' AND deleted_at IS NULL")
    db.execute(
        "INSERT INTO search SELECT json_extract(data, '$.slug'), "
        "json_extract(data, '$.content'), 'wiki', NULL, 'wiki', "
        "'wiki:' || id, json_extract(data, '$.modified_date') FROM objects "
        "WHERE kind = 'wiki' AND deleted_at IS NULL")
    # Text that has since been edited out of a description.
    db.execute(
        "INSERT INTO search SELECT r.subject, "
        "json_extract(h.data, '$.diff.description[0]'), r.type, r.ref, "
        "'old-description', h.parent, json_extract(h.data, '$.created_at') "
        "FROM objects h JOIN refs r ON r.object = h.parent "
        "WHERE h.kind = 'history' "
        "AND json_extract(h.data, '$.diff.description[0]') <> ''")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def open_existing(path):
    if not os.path.exists(path):
        sys.exit("%s does not exist; run `%s sync` first"
                 % (path, os.path.basename(sys.argv[0])))
    return connect(path)


# --------------------------------------------------------------------------
# Query backends: the local file, or Datasette serving it
# --------------------------------------------------------------------------

CONFIG_PATH = os.path.expanduser(
    os.environ.get("TAIGA_CONFIG", "~/.config/openrtx-taiga/config"))


def load_config():
    """Read `key = value` lines from CONFIG_PATH."""
    conf = {}
    try:
        with open(CONFIG_PATH) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    conf[key.strip()] = value.strip()
    except FileNotFoundError:
        pass
    return conf


def sync_token():
    return os.environ.get("TAIGA_SYNC_TOKEN") or load_config().get("sync_token")


class QueryError(Exception):
    pass


class LocalBackend:
    def __init__(self, path):
        if not os.path.exists(path):
            sys.exit("%s does not exist; run `%s sync` first, or configure "
                     "a remote in %s" % (path, os.path.basename(sys.argv[0]),
                                         CONFIG_PATH))
        self.path = path
        # openrtx-dev-ai: query-only rather than mode=ro, which can't open a
        # WAL database whose -shm file is gone
        self.db = sqlite3.connect(path, timeout=60)
        self.db.execute("PRAGMA query_only = ON")
        self.db.row_factory = lambda cur, row: {
            d[0]: v for d, v in zip(cur.description, row)}

    def describe(self):
        return "%s (%.1f MB)" % (self.path, os.path.getsize(self.path) / 1e6)

    def all(self, sql, params=()):
        try:
            return self.db.execute(sql, params).fetchall()
        except sqlite3.Error as e:
            raise QueryError(str(e))


_PLACEHOLDER = re.compile(r"'(?:[^']|'')*'|\?")


def bind(sql, params):
    """Turn ? placeholders into Datasette's :named ones.

    Numbers are inlined: Datasette passes every parameter as text, and the
    json_extract() columns of the views have no affinity to convert it back.
    """
    params = list(params)
    named = {}

    def sub(m):
        if m.group(0) != "?":
            return m.group(0)  # a string literal
        value = params.pop(0)
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            return str(int(value))
        if isinstance(value, (int, float)):
            return repr(value)
        key = "p%d" % len(named)
        named[key] = str(value)
        return ":" + key

    sql = _PLACEHOLDER.sub(sub, sql)
    if params:
        raise ValueError("more parameters than placeholders")
    return sql, named


class RemoteBackend:
    def __init__(self, url, database="openrtx-taiga", token=None):
        self.url = url.rstrip("/")
        self.database = database
        self.token = token

    def describe(self):
        return "%s/%s" % (self.url, self.database)

    def request(self, path, method="GET", query=None, auth=False):
        url = self.url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"Accept": "application/json",
                   "User-Agent": "openrtx-taiga-client/1"}
        if auth:
            if not self.token:
                sys.exit("triggering a sync needs the sync token: set "
                         "sync_token in %s or TAIGA_SYNC_TOKEN" % CONFIG_PATH)
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(
            url, method=method, headers=headers,
            data=b"" if method == "POST" else None)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            body = e.read()
            try:
                return e.code, json.loads(body)
            except ValueError:
                return e.code, {"error": body.decode(errors="replace")[:300]}
        except urllib.error.URLError as e:
            sys.exit("cannot reach %s: %s" % (self.url, e.reason))

    def all(self, sql, params=()):
        sql, named = bind(sql, params)
        status, body = self.request(
            "/%s.json" % self.database,
            query=dict(named, sql=sql, _shape="objects"))
        if status != 200 or not isinstance(body, dict) or "rows" not in body:
            error = body.get("error") if isinstance(body, dict) else None
            raise QueryError(error or "HTTP %d" % status)
        if body.get("truncated"):
            log("warning: the server truncated this result at its row "
                "limit; narrow the query or page with LIMIT/OFFSET")
        return body["rows"]


def backend(args):
    if args.remote:
        return RemoteBackend(args.remote, database=os.environ.get(
            "TAIGA_REMOTE_DB") or load_config().get("database", "openrtx-taiga"))
    return LocalBackend(args.db)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def parse_age(text):
    m = re.fullmatch(r"(\d+)([smhd]?)", text.strip())
    if not m:
        raise argparse.ArgumentTypeError("expected e.g. 90s, 15m, 2h, 1d")
    unit = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    return dt.timedelta(seconds=int(m.group(1)) * unit)


def parse_time(text):
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


def age_of(timestamp):
    return dt.datetime.now(dt.timezone.utc) - parse_time(timestamp)


def since_cutoff(text):
    """ISO date/time as-is; 'today', 'yesterday' or an age like 24h/3d."""
    now = dt.datetime.now(dt.timezone.utc)
    if text in ("today", "yesterday"):
        day = now.date() - dt.timedelta(days=text == "yesterday")
        return day.isoformat()
    if re.fullmatch(r"\d+[smhd]", text):
        return (now - parse_age(text)).strftime("%Y-%m-%dT%H:%M:%S")
    return text


def last_sync_age(db):
    row = db.execute("SELECT max(finished_at) FROM syncs "
                     "WHERE status = 'ok'").fetchone()
    return age_of(row[0]) if row[0] else None


def print_summary(summary):
    for kind, ops in sorted(summary.items()):
        log("  %-32s %s" % (kind, ", ".join(
            "%s %d" % kv for kv in sorted(ops.items()))))
    if not summary:
        log("  no changes")


def cmd_sync(args):
    if args.remote:
        return remote_sync(args)
    db = connect(args.db)
    init_schema(db)
    if args.max_age is not None and not args.full:
        age = last_sync_age(db)
        if age is not None and age < args.max_age:
            log("fresh: last sync %ds ago, skipping" % age.total_seconds())
            return
    client = Taiga(os.environ.get("TAIGA_TOKEN"))
    t0 = time.time()
    sync_id, mode, summary = run_sync(db, client, args.full, args.workers)
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # keep the file self-contained
    log("sync %d (%s) ok: %d requests, %.1f MB, %.0fs"
        % (sync_id, mode, client.requests, client.bytes / 1e6,
           time.time() - t0))
    print_summary(summary)


def report_sync(s):
    secs = ""
    if s.get("finished_at"):
        secs = ", %.0fs" % (parse_time(s["finished_at"])
                            - parse_time(s["started_at"])).total_seconds()
    log("sync %d (%s) %s: %s requests%s"
        % (s["id"], s["mode"], s["status"], s.get("requests"), secs))
    if s.get("error"):
        log("  error: %s" % s["error"])
    elif s["status"] == "ok":
        print_summary(s.get("summary") or {})


def remote_sync(args):
    b = RemoteBackend(args.remote, token=sync_token())
    status, state = b.request("/-/sync")
    if status != 200:
        sys.exit("cannot read sync status: HTTP %d %s" % (status, state))
    last_ok = next((s for s in state["syncs"] if s["status"] == "ok"), None)
    if (args.max_age is not None and not args.full and not state["running"]
            and last_ok and age_of(last_ok["finished_at"]) < args.max_age):
        log("fresh: last sync %ds ago, skipping"
            % age_of(last_ok["finished_at"]).total_seconds())
        return
    if not state["running"]:
        status, body = b.request("/-/sync", "POST", auth=True,
                                 query={"full": "1"} if args.full else None)
        if status == 429:
            log("a sync finished %ds ago; not starting another"
                % body.get("age", 0))
            return report_sync(state["syncs"][0])
        if status not in (202, 409):
            sys.exit("sync request failed: HTTP %d %s"
                     % (status, body.get("error") if body else ""))
    if args.no_wait:
        log("sync started on %s" % b.url)
        return
    deadline = time.time() + 900
    while True:
        time.sleep(2)
        status, state = b.request("/-/sync")
        if status == 200 and not state["running"]:
            break
        if time.time() > deadline:
            sys.exit("still running after 15 minutes; check %s/-/sync" % b.url)
    report_sync(state["syncs"][0])


def wrap(text, indent="    "):
    out = []
    for line in (text or "").splitlines() or [""]:
        out.extend(textwrap.wrap(line, 100, initial_indent=indent,
                                 subsequent_indent=indent) or [indent])
    return "\n".join(out)


def first(rows):
    return rows[0] if rows else None


def cmd_show(args):
    b = backend(args)
    ref = int(args.ref.lstrip("#"))
    r = first(b.all("SELECT * FROM refs WHERE ref = ? ORDER BY known_from, "
                    "deleted_at IS NOT NULL LIMIT 1", (ref,)))
    if r is None:
        sys.exit("no item with ref #%d" % ref)
    item = first(b.all("SELECT * FROM items WHERE object = ?", (r["object"],)))
    print("#%d [%s] %s" % (ref, r["type"], r["subject"]))
    print(r["url"])
    if item is None and r["known_from"] == "timeline":
        print("DELETED before the mirror started; only the timeline "
              "remembers it")
    elif item is None:
        print("DELETED upstream at %s; last known data in objects/versions"
              % r["deleted_at"])
    else:
        facts = [("status", item["status"]), ("assigned", item["assigned"]),
                 ("owner", item["owner"]), ("milestone", item["milestone"]),
                 ("tags", item["tags"]), ("epics", item["epic_refs"]),
                 ("story", item["story_ref"]), ("type", item["issue_type"]),
                 ("severity", item["severity"]),
                 ("priority", item["priority"]),
                 ("created", item["created_date"]),
                 ("modified", item["modified_date"]),
                 ("finished", item["finished_date"])]
        print("  ".join("%s: %s" % f for f in facts if f[1] not in (None, "")))
        children = b.all(
            "SELECT type, ref, status, subject FROM items WHERE story_ref = ? "
            "OR (? = 'epic' AND object IN (SELECT 'userstories:' || "
            "json_extract(data, '$.user_story') FROM objects WHERE kind = "
            "'epics/related_userstories' AND deleted_at IS NULL AND "
            "parent = ?)) ORDER BY ref",
            (ref if r["type"] == "us" else -1, r["type"], r["object"]))
        for c in children:
            print("  -> #%d [%s] %s (%s)" % (c["ref"], c["type"], c["subject"],
                                             c["status"]))
        print("\n--- description")
        print(wrap(item["description"]))
    print("\n--- activity")
    for a in b.all("SELECT * FROM activity_log WHERE object = ? ORDER BY at",
                   (r["object"],)):
        line = "%s %-8s %-10s %s" % (a["at"][:16].replace("T", " "),
                                     a["source"], a["actor"] or "-",
                                     a["action"])
        if a["fields"]:
            line += ": " + a["fields"]
        print(line)
        if a["comment"]:
            print(wrap(a["comment"], "      | "))
        if args.diffs and a["detail"] and a["detail"] != "{}":
            print(wrap(json.dumps(json.loads(a["detail"]), indent=1,
                                  ensure_ascii=False), "      ~ "))
    rows = b.all(
        "SELECT DISTINCT src_type, src_ref, via FROM mentions WHERE to_ref = ? "
        "AND src_object <> ? ORDER BY src_ref", (ref, r["object"]))
    if rows:
        print("\n--- mentioned by")
        print("  " + ", ".join("#%s (%s)" % (m["src_ref"], m["via"])
                               for m in rows))
    rows = b.all(
        "SELECT DISTINCT to_ref, to_type FROM mentions WHERE src_object = ? "
        "ORDER BY to_ref", (r["object"],))
    if rows:
        print("\n--- mentions")
        print("  " + ", ".join("#%d%s" % (m["to_ref"], "" if m["to_type"]
                                          else " (not a Taiga ref)")
                               for m in rows))
    rows = b.all("SELECT DISTINCT url FROM links WHERE src_object = ?",
                 (r["object"],))
    if rows:
        print("\n--- links")
        for l in rows:
            print("  " + l["url"])


def fts_query(text):
    return " ".join('"%s"' % t.replace('"', '""') for t in text.split())


def cmd_search(args):
    b = backend(args)
    where = "" if args.all else "AND source <> 'old-description'"
    sql = ("SELECT type, ref, source, at, subject, snippet(search, -1, '[', "
           "']', '…', 16) AS snippet FROM search WHERE search MATCH ? %s "
           "ORDER BY rank LIMIT ?" % where)
    query = " ".join(args.query)
    try:
        rows = b.all(sql, (query, args.limit))
    except QueryError:
        rows = b.all(sql, (fts_query(query), args.limit))
    for r in rows:
        print("#%s [%s] %s  (%s %s)" % (
            r["ref"] if r["ref"] is not None else "-", r["type"],
            r["subject"], r["source"], (r["at"] or "")[:10]))
        print("    " + " ".join((r["snippet"] or "").split()))


def cmd_log(args):
    b = backend(args)
    cond, params = [], []
    if args.since:
        cond.append("at >= ?")
        params.append(since_cutoff(args.since))
    if args.ref:
        cond.append("ref = ?")
        params.append(int(args.ref.lstrip("#")))
    if args.actor:
        cond.append("actor = ?")
        params.append(args.actor)
    if args.source:
        cond.append("source = ?")
        params.append(args.source)
    sql = ("SELECT at, source, actor, action, item_type, ref, subject, fields, "
           "comment FROM activity_log %s ORDER BY at DESC LIMIT ?"
           % ("WHERE " + " AND ".join(cond) if cond else ""))
    for r in b.all(sql, params + [args.limit]):
        line = "%s %-8s %-10s %-13s %s #%s %s" % (
            r["at"][:16].replace("T", " "), r["source"], r["actor"] or "-",
            r["action"], r["item_type"],
            r["ref"] if r["ref"] is not None else "-", r["subject"] or "")
        if r["fields"]:
            line += "  [%s]" % r["fields"]
        print(line)
        if r["comment"]:
            print("      | " + " ".join(r["comment"].split())[:200])


def cmd_info(args):
    b = backend(args)
    print("database: %s" % b.describe())
    print("\nrecent syncs:")
    for s in b.all("SELECT id, started_at, mode, status, requests, summary, "
                   "error FROM syncs ORDER BY id DESC LIMIT 5"):
        summary = json.loads(s["summary"]) if s["summary"] else {}
        changes = sum(sum(v.values()) for v in summary.values())
        print("  #%d %s %-11s %-7s %s requests, %d changes%s"
              % (s["id"], s["started_at"][:19], s["mode"], s["status"],
                 s["requests"], changes,
                 "  " + s["error"] if s["error"] else ""))
    print("\nobjects (live / deleted / versions):")
    for r in b.all(
            "SELECT o.kind, sum(o.deleted_at IS NULL) AS live, "
            "sum(o.deleted_at IS NOT NULL) AS deleted, "
            "(SELECT count(*) FROM versions v WHERE v.kind = o.kind) "
            "AS versions FROM objects o GROUP BY o.kind ORDER BY o.kind"):
        print("  %-36s %6d %4d %7d" % (r["kind"], r["live"], r["deleted"],
                                       r["versions"]))


def cell(value):
    if value is None:
        return ""
    return str(value).replace("\n", "\\n")


def cmd_sql(args):
    b = backend(args)
    try:
        rows = b.all(" ".join(args.query))
    except QueryError as e:
        sys.exit("SQL error: %s" % e)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    elif not rows:
        print("(no rows)")
    else:
        cols = list(rows[0])
        print(" | ".join(cols))
        for r in rows:
            print(" | ".join(cell(r[c]) for c in cols))


def cmd_rebuild(args):
    db = open_existing(args.db)
    init_schema(db)
    db.execute("BEGIN IMMEDIATE")
    rebuild_derived(db)
    db.execute("COMMIT")


def main(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", help="local SQLite file (default: %s)" % DEFAULT_DB)
    p.add_argument("--remote", metavar="URL",
                   help="query a Datasette server instead (default: "
                        "TAIGA_REMOTE or `remote` in %s)" % CONFIG_PATH)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync", help="pull changes from Taiga")
    s.add_argument("--full", action="store_true",
                   help="refetch every item's detail and history")
    s.add_argument("--max-age", type=parse_age, metavar="AGE",
                   help="skip if the last good sync is younger than AGE "
                        "(e.g. 15m)")
    s.add_argument("--no-wait", action="store_true",
                   help="remote only: return once the sync has started")
    s.add_argument("--workers", type=int, default=8)
    s.set_defaults(fn=cmd_sync)
    s = sub.add_parser("show", help="show one item by ref")
    s.add_argument("ref")
    s.add_argument("--diffs", action="store_true",
                   help="include field values from each change")
    s.set_defaults(fn=cmd_show)
    s = sub.add_parser("search", help="full-text search")
    s.add_argument("query", nargs="+")
    s.add_argument("--all", action="store_true",
                   help="include old description revisions")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_search)
    s = sub.add_parser("log", help="activity log, newest first")
    s.add_argument("--since",
                   help="ISO date or time, today, yesterday, or an age such "
                        "as 24h or 3d")
    s.add_argument("--ref")
    s.add_argument("--actor", help="Taiga username")
    s.add_argument("--source", choices=["history", "timeline", "mirror"])
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(fn=cmd_log)
    s = sub.add_parser("info", help="sync history and object counts")
    s.set_defaults(fn=cmd_info)
    s = sub.add_parser("sql", help="run a read-only SQL query")
    s.add_argument("query", nargs="+")
    s.add_argument("--json", action="store_true", help="print rows as JSON")
    s.set_defaults(fn=cmd_sql)
    s = sub.add_parser("rebuild", help="regenerate views and derived tables")
    s.set_defaults(fn=cmd_rebuild)
    args = p.parse_args(argv)
    # An explicit or environment-provided local file wins; otherwise use the
    # configured remote, if any.
    # openrtx-dev-ai: local mode only (the Datasette remote is retired)
    if args.db is None:
        args.db = DEFAULT_DB
    if args.remote and args.cmd == "rebuild":
        sys.exit("rebuild works on a local file only")
    try:
        args.fn(args)
    except QueryError as e:
        sys.exit("query failed: %s" % e)


if __name__ == "__main__":
    main()
