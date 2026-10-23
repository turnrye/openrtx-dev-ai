#!/usr/bin/env python3
"""Tests for the cache core and the `openrtx` CLI, with a dummy source (no network).

    python3 tests/test_core.py
"""

import contextlib
import datetime as dt
import io
import os
import sqlite3
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from openrtx import cli, paths, sources  # noqa: E402


def dummy_source(name="dummy", fail=False):
    """A source that records its syncs and stores one searchable row."""
    m = types.SimpleNamespace(NAME=name, PERIOD=dt.timedelta(hours=1),
                              DESCRIPTION="a dummy source", calls=[])

    def sync(db_path, full=False):
        m.calls.append(full)
        if fail:
            raise sources.SourceError("down")
        db = sources.connect(db_path)
        db.execute("CREATE TABLE IF NOT EXISTS notes (ref TEXT, text TEXT)")
        db.execute("DELETE FROM notes")
        db.execute("INSERT INTO notes VALUES ('dummy:1', 'golden image harness')")
        sources.record_sync(db, True, "1 note")
        return "1 note"

    def search(db_path, query, limit):
        rows = sources.connect_ro(db_path).execute(
            "SELECT ref, text FROM notes WHERE text LIKE ?", ("%" + query + "%",)).fetchall()
        return [sources.Hit(name, r["ref"], r["text"], kind="note") for r in rows]

    def recent(db_path, since, limit):
        return [sources.Hit(name, "dummy:1", "golden image harness", kind="note",
                            snippet="changed since %s" % since[:10])]

    m.sync = sync
    m.recent = recent
    m.last_sync = sources.last_ok_sync
    m.search = search
    m.owns = lambda ref: ref.startswith("dummy:")
    m.show = lambda db_path, ref: "note %s" % ref if ref == "dummy:1" else None
    return m


class CoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["OPENRTX_CACHE"] = os.path.join(self.tmp.name, "cache")
        os.environ["OPENRTX_CONFIG"] = os.path.join(self.tmp.name, "config")
        self.src = dummy_source()
        self._available = sources.available
        sources.available = lambda: [self.src]

    def tearDown(self):
        sources.available = self._available
        self.tmp.cleanup()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_refresh_only_when_stale(self):
        self.assertEqual(sources.refresh(self.src, quiet=True), (True, None))
        self.assertEqual(sources.refresh(self.src, quiet=True), (False, None))
        self.assertEqual(len(self.src.calls), 1)
        # An explicit max-age of 0 always syncs
        sources.refresh(self.src, max_age=dt.timedelta(0), quiet=True)
        self.assertEqual(len(self.src.calls), 2)

    def test_period_override_from_config(self):
        os.makedirs(os.environ["OPENRTX_CONFIG"])
        with open(os.path.join(os.environ["OPENRTX_CONFIG"], "config"), "w") as f:
            f.write("period.dummy = 2d\n")
        self.assertEqual(sources.period(self.src), dt.timedelta(days=2))

    def test_failure_backs_off_on_read_but_not_on_request(self):
        bad = dummy_source(fail=True)
        synced, err = sources.refresh(bad, quiet=True)
        self.assertFalse(synced)
        self.assertIsNotNone(err)
        sources.refresh(bad, quiet=True)  # automatic: waits
        self.assertEqual(len(bad.calls), 1)
        sources.refresh(bad, max_age=dt.timedelta(0), quiet=True)  # explicit: tries
        self.assertEqual(len(bad.calls), 2)

    def test_incomplete_sync_backs_off(self):
        lazy = dummy_source()
        lazy.sync = lambda db_path, full=False: lazy.calls.append(full) or "partial, rate-limited"
        self.assertEqual(sources.refresh(lazy, quiet=True), (False, None))
        sources.refresh(lazy, quiet=True)
        self.assertEqual(len(lazy.calls), 1)

    def test_cache_file_copied_alone_still_reads(self):
        sources.refresh(self.src, quiet=True)
        lone = os.path.join(self.tmp.name, "lone.sqlite")
        with open(paths.db_path("dummy"), "rb") as src, open(lone, "wb") as dst:
            dst.write(src.read())
        self.assertIsNotNone(sources.last_ok_sync(lone))
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "missing.sqlite"))
                         or sources.last_ok_sync(os.path.join(self.tmp.name, "missing.sqlite")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "missing.sqlite")))

    def test_sync_leaves_no_wal(self):
        sources.refresh(self.src, quiet=True)
        self.assertFalse(os.path.exists(paths.db_path("dummy") + "-wal")
                         and os.path.getsize(paths.db_path("dummy") + "-wal") > 0)

    def test_search_refreshes_then_searches(self):
        code, out, err = self.run_cli("search", "golden")
        self.assertEqual(code, 0)
        self.assertIn("refreshing dummy (first sync", err)
        self.assertIn("dummy:1  golden image harness  (note)", out)
        code, out, err = self.run_cli("search", "golden")
        self.assertNotIn("refreshing", err)
        self.assertEqual(len(self.src.calls), 1)

    def test_search_no_refresh_on_empty_cache(self):
        code, out, err = self.run_cli("search", "golden", "--no-refresh")
        self.assertIn("not cached yet", err)
        self.assertEqual(self.src.calls, [])

    def test_recent_refreshes_and_lists(self):
        code, out, err = self.run_cli("recent", "--since", "7d")
        self.assertEqual(code, 0, err)
        self.assertIn("dummy:1  golden image harness  (note)", out)
        self.assertIn("changed since", out)
        self.assertEqual(len(self.src.calls), 1)

    def test_since_accepts_ages_and_dates(self):
        self.assertTrue(cli.since_iso("7d").endswith("Z"))
        self.assertEqual(cli.since_iso("2026-10-01"), "2026-10-01")
        self.assertEqual(len(cli.since_iso("today")), 10)

    def test_show_dispatches_by_ref(self):
        code, out, err = self.run_cli("show", "dummy:1")
        self.assertEqual((code, out), (0, "note dummy:1\n"))
        code, out, err = self.run_cli("show", "#520")
        self.assertIn("can't tell which source", str(code) + err)

    def test_sql_attaches_each_source_as_a_schema(self):
        sources.refresh(self.src, quiet=True)
        code, out, err = self.run_cli("sql", "SELECT ref, text FROM dummy.notes")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines(), ["ref | text", "dummy:1 | golden image harness"])
        # read-only
        code, out, err = self.run_cli("sql", "DELETE FROM dummy.notes")
        self.assertIn("query failed", str(code))

    def test_sources_lists_age(self):
        sources.refresh(self.src, quiet=True)
        code, out, err = self.run_cli("sources")
        self.assertIn("dummy", out)
        self.assertIn("synced 0s ago", out)

    def test_taiga_help(self):
        code, out, err = self.run_cli("taiga")
        self.assertEqual(code, 0)
        self.assertIn("writes (tasks.openrtx.org)", out)


class PathsTest(unittest.TestCase):
    def test_parse_and_format_age(self):
        self.assertEqual(paths.parse_age("15m"), dt.timedelta(minutes=15))
        self.assertEqual(paths.parse_age("90"), dt.timedelta(seconds=90))
        self.assertEqual(paths.format_age(dt.timedelta(hours=26)), "1d")
        with self.assertRaises(ValueError):
            paths.parse_age("soon")


if __name__ == "__main__":
    unittest.main()
