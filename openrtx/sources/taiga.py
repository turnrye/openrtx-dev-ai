"""The OpenRTX Taiga (tasks.openrtx.org) as a research source.

The heavy lifting is the vendored mirror in taiga_mirror.py: a versioned copy
of every epic, story, task and issue with comments, history and a full-text
index. This module adapts it to the source contract in __init__.py.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import os
import re
import sqlite3

from . import Hit, connect_ro, fts_query, parse_iso
from . import taiga_mirror as mirror

NAME = "taiga"
PERIOD = dt.timedelta(hours=1)
DESCRIPTION = "OpenRTX Taiga (tasks.openrtx.org): epics, stories, tasks, issues, comments and history"

REF_RE = re.compile(r"^taiga#(\d+)$", re.I)
URL_RE = re.compile(r"^https?://tasks\.openrtx\.org/project/[^/]+/(?:epic|us|task|issue)/(\d+)",
                    re.I)


def ref_number(ref):
    m = REF_RE.match(ref.strip()) or URL_RE.match(ref.strip())
    return int(m.group(1)) if m else None


def owns(ref):
    return ref_number(ref) is not None


def sync(db_path, full=False):
    db = mirror.connect(db_path)
    mirror.init_schema(db)
    client = mirror.Taiga(os.environ.get("TAIGA_TOKEN"))
    _, mode, summary = mirror.run_sync(db, client, full)
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    changes = sum(sum(ops.values()) for ops in summary.values())
    return "%s sync, %d change%s, %d requests" % (
        mode, changes, "" if changes == 1 else "s", client.requests)


def last_sync(db_path):
    if not os.path.exists(db_path):
        return None
    try:
        row = connect_ro(db_path).execute(
            "SELECT max(finished_at) FROM syncs WHERE status = 'ok'").fetchone()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    t = parse_iso(row[0])
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def search(db_path, query, limit):
    if not os.path.exists(db_path):
        return []
    db = connect_ro(db_path)
    sql = ("SELECT s.type, s.ref, s.source, s.at, s.subject, r.url, "
           "snippet(search, -1, '[', ']', '…', 16) AS snippet "
           "FROM search s LEFT JOIN refs r ON r.object = s.object "
           "WHERE search MATCH ? AND s.source <> 'old-description' "
           "ORDER BY rank LIMIT ?")
    try:
        rows = db.execute(sql, (query, limit)).fetchall()
    except sqlite3.OperationalError:
        rows = db.execute(sql, (fts_query(query), limit)).fetchall()
    return [Hit("taiga", "taiga#%s" % r["ref"] if r["ref"] is not None else "taiga",
                r["subject"] or "", kind=r["type"], when=(r["at"] or "")[:10],
                url=r["url"] or "",
                snippet=" ".join((r["snippet"] or "").split()))
            for r in rows]


def recent(db_path, since, limit):
    if not os.path.exists(db_path):
        return []
    db = connect_ro(db_path)
    rows = db.execute(
        "SELECT i.type, i.ref, i.subject, i.status, i.modified_date, i.url, "
        "(SELECT group_concat(DISTINCT a.actor) FROM activity_log a "
        " WHERE a.ref = i.ref AND a.at >= ?) AS actors, "
        "(SELECT count(*) FROM activity_log a WHERE a.ref = i.ref AND a.at >= ?) AS events "
        "FROM items i WHERE i.modified_date >= ? "
        "ORDER BY i.modified_date DESC LIMIT ?", (since, since, since, limit)).fetchall()
    hits = []
    for r in rows:
        what = "status %s" % r["status"] if r["status"] else ""
        if r["events"]:
            what += "; %d change%s by %s" % (r["events"], "" if r["events"] == 1 else "s",
                                             (r["actors"] or "").replace(",", ", "))
        hits.append(Hit(NAME, "taiga#%s" % r["ref"], r["subject"] or "", kind=r["type"],
                        when=(r["modified_date"] or "")[:10], url=r["url"] or "",
                        snippet=what.strip("; ")))
    return hits


def show(db_path, ref):
    n = ref_number(ref)
    if n is None or not os.path.exists(db_path):
        return None
    args = argparse.Namespace(remote=None, db=db_path, ref=str(n), diffs=False)
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            mirror.cmd_show(args)
    except SystemExit:  # the mirror exits when the ref isn't known
        return None
    return out.getvalue()


def run_mirror_cli(db_path, argv):
    """`openrtx taiga sync|show|search|log|info|sql|rebuild …` on the cache file."""
    return mirror.main(["--db", db_path] + list(argv))
