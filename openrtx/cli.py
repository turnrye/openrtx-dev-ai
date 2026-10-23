"""openrtx — research the OpenRTX project from a local cache, and write to its Taiga.

    openrtx sources                       what's cached, how old it is, refresh periods
    openrtx sync [SOURCE…] [--max-age AGE | --full]
                                          refresh sources older than their period
    openrtx search QUERY… [--source S]    full-text search across every source
    openrtx recent [--since 7d]           what changed lately, across every source
    openrtx show REF                      one item: taiga#841, OpenRTX/OpenRTX#520,
                                          pad:officehours[@REV], or a Taiga/GitHub URL
    openrtx sql "SELECT …" [--json]       read-only SQL; each source is a schema
                                          (taiga.items, github.items, pad.sections…)
    openrtx taiga …                       Taiga research (sync, show, search, log,
                                          info, sql) and writes (post, comment, edit,
                                          login, whoami, api); `openrtx taiga -h`

Reads refresh a source first when its cache is older than its period (Taiga
and GitHub 1 h, the pad 24 h), so repeated queries are fast and never hammer
the services. --no-refresh skips that. The cache lives in the platform cache
dir (see paths.py) and can be deleted at any time.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

from . import __version__, paths, sources
from .sources import log

TAIGA_WRITE_COMMANDS = {"post", "comment", "edit", "login", "whoami", "api"}
TAIGA_MIRROR_COMMANDS = {"sync", "show", "search", "log", "info", "sql", "rebuild"}


def pick(names):
    found = {s.NAME: s for s in sources.available()}
    if not names:
        return list(found.values())
    missing = [n for n in names if n not in found]
    if missing:
        sys.exit("unknown source %s; have %s" % (", ".join(missing), ", ".join(found)))
    return [found[n] for n in names]


def cmd_sources(args):
    for s in sources.available():
        path = paths.db_path(s.NAME)
        a = sources.age(s)
        state = "never synced" if a is None else "synced %s ago" % paths.format_age(a)
        if a is not None and sources.is_stale(s):
            state += " (stale)"
        size = "%.1f MB" % (os.path.getsize(path) / 1e6) if os.path.exists(path) else "-"
        print("%-7s every %-4s %-26s %8s  %s" % (
            s.NAME, paths.format_age(sources.period(s)), state, size, s.DESCRIPTION))
    print("\ncache: %s" % paths.cache_dir())


def cmd_sync(args):
    max_age = paths.parse_age(args.max_age) if args.max_age is not None else None
    failed = False
    for s in pick(args.source):
        synced, err = sources.refresh(s, max_age=max_age, full=args.full)
        if err is not None:
            failed = True
        elif not synced:
            log("%s: fresh (synced %s ago), skipping"
                % (s.NAME, paths.format_age(sources.age(s))))
    return 1 if failed else 0


def refresh_stale(selected, enabled):
    if enabled:
        for s in selected:
            sources.refresh(s)


def cmd_search(args):
    selected = pick(args.source)
    refresh_stale(selected, not args.no_refresh)
    query = " ".join(args.query)
    results = []
    for s in selected:
        path = paths.db_path(s.NAME)
        if s.last_sync(path) is None:
            log("%s: not cached yet; run `openrtx sync %s`" % (s.NAME, s.NAME))
            continue
        try:
            results.extend(s.search(path, query, args.limit))
        except sqlite3.Error as e:
            log("%s: search failed (%s)" % (s.NAME, e))
    print_hits(results, args.json)
    return 0


def since_iso(text):
    """7d / 24h / today / yesterday / an ISO date -> ISO UTC string."""
    now = sources.now_utc()
    if text in ("today", "yesterday"):
        day = now.date() - __import__("datetime").timedelta(days=text == "yesterday")
        return day.isoformat()
    try:
        return sources.iso(now - paths.parse_age(text))
    except ValueError:
        return text  # an ISO date or time, compared as text


def print_hits(results, as_json):
    if as_json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
        return
    if not results:
        print("nothing found")
        return
    current = None
    for h in results:
        if h["source"] != current:
            current = h["source"]
            print("== %s" % current)
        meta = " ".join(x for x in (h["kind"], h["when"]) if x)
        print("%s  %s%s" % (h["ref"], h["title"], "  (%s)" % meta if meta else ""))
        if h["snippet"]:
            print("    " + h["snippet"])
        if h["url"]:
            print("    " + h["url"])


def cmd_recent(args):
    selected = [s for s in pick(args.source) if hasattr(s, "recent")]
    refresh_stale(selected, not args.no_refresh)
    since = since_iso(args.since)
    results = []
    for s in selected:
        path = paths.db_path(s.NAME)
        if s.last_sync(path) is None:
            log("%s: not cached yet; run `openrtx sync %s`" % (s.NAME, s.NAME))
            continue
        results.extend(s.recent(path, since, args.limit))
    print_hits(results, args.json)
    return 0


def cmd_show(args):
    for s in sources.available():
        if s.owns(args.ref):
            refresh_stale([s], not args.no_refresh)
            text = s.show(paths.db_path(s.NAME), args.ref)
            if text is None:
                sys.exit("%s: nothing cached for %s" % (s.NAME, args.ref))
            sys.stdout.write(text if text.endswith("\n") else text + "\n")
            return 0
    sys.exit("can't tell which source %r belongs to; use taiga#N, OWNER/REPO#N, "
             "pad:NAME or a Taiga/GitHub URL" % args.ref)


def cmd_sql(args):
    db = sqlite3.connect(":memory:")
    attached = []
    for s in sources.available():
        path = paths.db_path(s.NAME)
        if os.path.exists(path):
            db.execute("ATTACH DATABASE ? AS %s" % s.NAME, (path,))
            attached.append(s.NAME)
    db.execute("PRAGMA query_only = ON")  # read-only across every attached cache
    if not attached:
        sys.exit("nothing cached yet; run `openrtx sync`")
    try:
        cur = db.execute(" ".join(args.query))
    except sqlite3.Error as e:
        sys.exit("query failed: %s (schemas: %s)" % (e, ", ".join(attached)))
    cols = [d[0] for d in cur.description or []]
    rows = cur.fetchall()
    if args.json:
        print(json.dumps([dict(zip(cols, r)) for r in rows], indent=2,
                         ensure_ascii=False, default=str))
    else:
        print(" | ".join(cols))
        for r in rows:
            print(" | ".join("" if v is None else str(v) for v in r))
    return 0


def cmd_taiga(argv):
    if argv and argv[0] in TAIGA_WRITE_COMMANDS:
        from . import taiga_write
        return taiga_write.main(argv)
    if argv and argv[0] in TAIGA_MIRROR_COMMANDS:
        from .sources import taiga
        return taiga.run_mirror_cli(paths.db_path("taiga"), argv) or 0
    print("usage: openrtx taiga {%s} …\n\n"
          "  research (the local cache): %s\n"
          "  writes (tasks.openrtx.org): %s\n\n"
          "`openrtx taiga post -h` etc. for each command's options."
          % (",".join(sorted(TAIGA_MIRROR_COMMANDS | TAIGA_WRITE_COMMANDS)),
             ", ".join(sorted(TAIGA_MIRROR_COMMANDS)),
             ", ".join(sorted(TAIGA_WRITE_COMMANDS))))
    return 0 if argv in ([], ["-h"], ["--help"]) else 2


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    # `openrtx taiga …` hands everything after `taiga` to the Taiga tools
    if argv[:1] == ["taiga"]:
        return cmd_taiga(argv[1:])
    p = argparse.ArgumentParser(prog="openrtx", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version="openrtx %s" % __version__)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sources", help="list sources and how fresh their caches are")
    s.set_defaults(fn=cmd_sources)
    s = sub.add_parser("sync", help="refresh stale sources")
    s.add_argument("source", nargs="*", help="source names (default: all)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--max-age", metavar="AGE",
                   help="refresh if older than AGE (e.g. 15m; 0 = always) "
                        "instead of each source's period")
    g.add_argument("--full", action="store_true", help="refetch everything")
    s.set_defaults(fn=cmd_sync)
    s = sub.add_parser("search", help="full-text search across sources")
    s.add_argument("query", nargs="+")
    s.add_argument("--source", action="append", help="limit to a source (repeatable)")
    s.add_argument("--limit", type=int, default=10, help="results per source")
    s.add_argument("--json", action="store_true")
    s.add_argument("--no-refresh", action="store_true")
    s.set_defaults(fn=cmd_search)
    s = sub.add_parser("recent", help="what changed lately, across sources")
    s.add_argument("--since", default="7d",
                   help="an age (7d, 24h), today, yesterday, or an ISO date (default 7d)")
    s.add_argument("--source", action="append", help="limit to a source (repeatable)")
    s.add_argument("--limit", type=int, default=30, help="items per source")
    s.add_argument("--json", action="store_true")
    s.add_argument("--no-refresh", action="store_true")
    s.set_defaults(fn=cmd_recent)
    s = sub.add_parser("show", help="one item by ref")
    s.add_argument("ref")
    s.add_argument("--no-refresh", action="store_true")
    s.set_defaults(fn=cmd_show)
    s = sub.add_parser("sql", help="read-only SQL across the cache")
    s.add_argument("query", nargs="+")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_sql)
    sub.add_parser("taiga", help="Taiga research and writes; `openrtx taiga -h`")
    args = p.parse_args(argv)
    return args.fn(args) or 0
