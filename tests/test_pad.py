#!/usr/bin/env python3
"""Tests for the pad source against a fake Etherpad (nothing reaches the network).

    python3 tests/test_pad.py
"""

import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openrtx import paths  # noqa: E402
from openrtx.sources import SourceError, pad  # noqa: E402

MARKDOWN_V1 = """
# Office Hours for OpenRTX

The goal of these sessions is to promote community, discuss problems and key decisions.

## October 2026

Held on 2026-10-19 in [https://meet.jit.si/openrtx](https://meet.jit.si/openrtx)

In attendance were DM4RCO, IU2KWO, DM3MAT, ON4MOD, K0RET

### Discussion topics:

   * Brief project pulse update
       * Persistence: almost ready to be mainlined, then USB on the CS7000 Plus with PR #439.
       * Demodulator work tracked on [https://tasks.openrtx.org/project/openrtx/epic/862](https://tasks.openrtx.org/project/openrtx/epic/862)
   * Mailing list
       * three public lists: users, developers and ptscd


## July 2026

Held on 2026-07-23T17:00Z in [https://meet.jit.si/openrtx](https://meet.jit.si/openrtx)

Attendees: Silvano IU2KWO, Hannes DM3MAT, Ryan K0RET

### Discussion topics:

   * News from ham convention
       * FT65 platform support in progress?
   * Open PRs
       * Ailunce HD2 port, see OpenRTX/OpenRTX#487


## Template

Held on [datetime] in [collaboration tool or space]

### Discussion topics:

   * Topic one
"""

T0 = 1760000000000  # ms
STATES_V1 = [  # (text, timestamp ms, author): revisions 0..3
    ("Office Hours\n\n", T0, "a.alice"),
    ("Office Hours\n\nOctober: persistence\n", T0 + 60_000, "a.alice"),
    ("Office Hours\n\nOctober: persistence, USB 🎉\n", T0 + 120_000, "a.bob"),
    ("Office Hours\n\nOctober: persistence, USB 🎉\nJuly: HD2\n", T0 + 3 * 3600_000, "a.alice"),
]


def b36(n):
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = digits[r] + out
        if n == 0:
            return out


def u16(s):
    return len(s.encode("utf-16-le")) // 2


def changeset(old, new):
    """An Etherpad changeset turning old into new (keep prefix, delete, insert)."""
    p = 0
    while p < min(len(old), len(new)) and old[p] == new[p]:
        p += 1
    s = 0
    while s < min(len(old), len(new)) - p and old[-1 - s] == new[-1 - s]:
        s += 1
    deleted, inserted = old[p:len(old) - s], new[p:len(new) - s]
    grow = u16(new) - u16(old)
    cs = "Z:%s%s%s" % (b36(u16(old)), ">" if grow >= 0 else "<", b36(abs(grow)))
    if p:
        cs += "|%s=%s" % (b36(old[:p].count("\n")), b36(u16(old[:p]))) if "\n" in old[:p] else "=" + b36(u16(old[:p]))
    if deleted:
        cs += "-" + b36(u16(deleted))
    if inserted:
        cs += "*0+" + b36(u16(inserted))
    return cs + "$" + inserted


def history_json(states, snapshot_at=None):
    data = {}
    text = "\n"
    for rev, (new, ts, author) in enumerate(states):
        meta = {"author": author, "timestamp": ts}
        if rev == snapshot_at:
            meta["atext"] = {"text": new, "attribs": ""}
        data["pad:testpad:revs:%d" % rev] = {"changeset": changeset(text, new), "meta": meta}
        text = new
    data["pad:testpad"] = {"atext": {"text": text, "attribs": ""}, "pool": {}, "head": len(states) - 1}
    data["globalAuthor:a.alice"] = {"name": "Alice"}
    data["globalAuthor:a.bob"] = {"name": None}
    return json.dumps(data).encode()


class FakeEtherpad:
    def __init__(self):
        self.set(MARKDOWN_V1, STATES_V1)
        self.requests = []          # (path, If-None-Match)
        self.queue = []             # forced responses: (status, headers, body)

    def set(self, markdown, states, snapshot_at=2):
        self.markdown = markdown.encode()
        self.etag = 'W/"%s"' % hashlib.sha1(self.markdown).hexdigest()[:20]
        self.history = history_json(states, snapshot_at)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        fake = self.server.fake
        fake.requests.append((self.path, self.headers.get("If-None-Match")))
        if self.path == "/login":
            return self.reply(200, b"<html><form>password <input type=password></form></html>",
                              {"Content-Type": "text/html"})
        if fake.queue:
            status, headers, body = fake.queue.pop(0)
            return self.reply(status, body, headers)
        if self.path == "/p/testpad/export/markdown":
            if self.headers.get("If-None-Match") == fake.etag:
                return self.reply(304, b"", {"ETag": fake.etag})
            return self.reply(200, fake.markdown, {"ETag": fake.etag,
                                                   "Content-Type": "plain/text; charset=utf-8"})
        if self.path == "/p/testpad/export/etherpad":
            return self.reply(200, fake.history, {"Content-Type": "application/json"})
        self.reply(404, b"Cannot GET " + self.path.encode())

    def reply(self, status, body, headers=None):
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class PadTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeEtherpad()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.fake = self.fake
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {k: os.environ.get(k) for k in ("OPENRTX_CACHE", "OPENRTX_CONFIG")}
        os.environ["OPENRTX_CACHE"] = os.path.join(self.tmp.name, "cache")
        os.environ["OPENRTX_CONFIG"] = os.path.join(self.tmp.name, "config")
        self.url = "http://127.0.0.1:%d/p/testpad" % self.server.server_port
        os.makedirs(os.environ["OPENRTX_CONFIG"])
        with open(os.path.join(os.environ["OPENRTX_CONFIG"], "config"), "w") as f:
            f.write("pad.officehours = off\npad.test = %s\n" % self.url)
        self.db = paths.db_path("pad")
        self.sleeps = []

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def sync(self, **kw):
        return pad.sync(self.db, sleep=self.sleeps.append, **kw)

    def rows(self, sql, *args):
        db = pad.connect_ro(self.db)
        return [dict(r) for r in db.execute(sql, args).fetchall()]

    def paths_requested(self):
        return [p for p, _ in self.fake.requests]

    # ---------------------------------------------------------------

    def test_first_sync_stores_history_sessions_sections_links(self):
        summary = self.sync()
        self.assertIn("test: rev 3, 2 sessions", summary)
        self.assertEqual(self.paths_requested(),
                         ["/p/testpad/export/markdown", "/p/testpad/export/etherpad"])
        p = self.rows("SELECT * FROM pads")[0]
        self.assertEqual((p["status"], p["head_rev"], p["indexed_rev"]), ("ok", 3, 3))
        sessions = self.rows("SELECT rev_start, rev_end, authors, text FROM sessions ORDER BY rev_start")
        self.assertEqual([(s["rev_start"], s["rev_end"]) for s in sessions], [(0, 2), (3, 3)])
        self.assertEqual(json.loads(sessions[0]["authors"]), ["a.alice", "a.bob"])
        self.assertEqual(sessions[0]["text"], STATES_V1[2][0])
        meetings = self.rows("SELECT heading, meeting_date, attendees FROM sections "
                             "WHERE kind = 'meeting' ORDER BY seq")
        self.assertEqual([(m["heading"], m["meeting_date"]) for m in meetings],
                         [("October 2026", "2026-10-19"), ("July 2026", "2026-07-23")])
        self.assertEqual(meetings[1]["attendees"], "Silvano IU2KWO, Hannes DM3MAT, Ryan K0RET")
        topics = [t["topic"] for t in self.rows("SELECT topic FROM sections WHERE kind = 'topic' ORDER BY seq")]
        self.assertEqual(topics, ["Brief project pulse update", "Mailing list",
                                  "News from ham convention", "Open PRs"])
        self.assertNotIn("Topic one", topics)  # the Template section is skipped
        links = {(l["kind"], l["ref"]) for l in self.rows("SELECT kind, ref FROM links")}
        self.assertEqual(links, {("github", "OpenRTX/OpenRTX#439"), ("taiga", "taiga#862"),
                                 ("github", "OpenRTX/OpenRTX#487")})
        self.assertIsNotNone(pad.last_sync(self.db))
        self.assertEqual(self.rows("SELECT name FROM authors WHERE id = 'a.alice'")[0]["name"], "Alice")

    def test_unchanged_pad_costs_one_conditional_request(self):
        self.sync()
        summary = self.sync()
        self.assertIn("unchanged (rev 3)", summary)
        self.assertEqual(self.fake.requests[-1], ("/p/testpad/export/markdown", self.fake.etag))
        self.assertEqual(self.paths_requested().count("/p/testpad/export/etherpad"), 1)

    def test_change_is_detected_and_reindexed(self):
        self.sync()
        states = STATES_V1 + [(STATES_V1[-1][0] + "Wiki\n", T0 + 4 * 3600_000, "a.bob")]
        self.fake.set(MARKDOWN_V1.replace("Mailing list", "Wiki setup"), states)
        summary = self.sync()
        self.assertIn("rev 4, 3 sessions", summary)
        hits = pad.search(self.db, "Wiki setup")
        self.assertEqual({(h["ref"], h["kind"]) for h in hits},
                         {("pad:test", "meeting"), ("pad:test", "topic")})
        self.assertEqual(pad.search(self.db, "Mailing"), [])

    def test_search_hits(self):
        self.sync()
        hits = pad.search(self.db, "persistence")
        topic = [h for h in hits if h["kind"] == "topic"][0]
        self.assertEqual(topic["source"], "pad")
        self.assertEqual(topic["ref"], "pad:test")
        self.assertEqual(topic["title"], "Office Hours — 2026-10-19 — Brief project pulse update")
        self.assertEqual(topic["when"], "2026-10-19")
        self.assertEqual(topic["url"], self.url + "#rev/3")
        self.assertIn("[persistence]", topic["snippet"].lower())
        # FTS syntax errors fall back to a literal search
        self.assertTrue(pad.search(self.db, "PR #439"))

    def test_show_head_revision_and_refs(self):
        self.sync()
        head = pad.show(self.db, "pad:test")
        self.assertIn("url: %s" % self.url, head)
        self.assertIn("head: rev 3", head)
        self.assertIn("## October 2026", head)
        self.assertIn(STATES_V1[1][0].strip(), pad.show(self.db, "pad:test@1"))
        self.assertIn("USB 🎉", pad.show(self.db, "pad:test@3"))  # replayed through the rev 2 snapshot
        self.assertIn("has no revision 99 (head is 3)", pad.show(self.db, "pad:test@99"))
        self.assertIsNone(pad.show(self.db, "pad:nope"))
        self.assertIsNone(pad.show(self.db, "taiga#1"))
        self.assertTrue(pad.owns("pad:test@4"))
        self.assertTrue(pad.owns(self.url + "#rev/2"))
        self.assertEqual(pad.parse_ref(self.url + "#rev/2"), ("test", 2))
        self.assertFalse(pad.owns("OpenRTX/OpenRTX#1"))

    def test_large_deletion_keeps_last_good_copy(self):
        self.sync()
        states = STATES_V1 + [("x\n", T0 + 5 * 3600_000, "a.vandal")]
        self.fake.set("# Office Hours for OpenRTX\n\ngone\n", states)
        summary = self.sync()
        self.assertIn("FLAGGED", summary)
        p = self.rows("SELECT indexed_rev, head_rev, flag FROM pads")[0]
        self.assertEqual((p["indexed_rev"], p["head_rev"]), (3, 4))
        self.assertTrue(pad.search(self.db, "persistence"))
        self.assertIn("FLAG:", pad.show(self.db, "pad:test"))
        self.assertIn("## October 2026", pad.show(self.db, "pad:test"))

    def test_404_marks_gone_and_keeps_cache(self):
        self.sync()
        self.fake.queue.append((404, {}, b"Cannot GET"))
        self.assertIn("gone (404)", self.sync())
        self.assertEqual(self.rows("SELECT status FROM pads")[0]["status"], "gone")
        self.assertTrue(pad.search(self.db, "persistence"))

    def test_403_and_login_redirect_mark_private(self):
        self.sync()
        self.fake.queue.append((403, {}, b"forbidden"))
        self.assertIn("private", self.sync(full=True))
        self.fake.queue.append((302, {"Location": "/login"}, b""))
        self.assertIn("private", self.sync(full=True))
        self.assertEqual(self.rows("SELECT status FROM pads")[0]["status"], "private")

    def test_short_rate_limit_waits_once_then_retries(self):
        reset = str(int(time.time()) + 2)
        self.fake.queue.append((429, {"x-ratelimit-reset": reset}, b"slow down"))
        self.assertIn("rev 3", self.sync())
        self.assertEqual(len(self.sleeps), 1)
        self.assertLessEqual(self.sleeps[0], 3)

    def test_long_rate_limit_backs_off_without_requests(self):
        reset = str(int(time.time()) + 3600)
        self.fake.queue.append((429, {"x-ratelimit-reset": reset}, b"slow down"))
        with self.assertRaises(SourceError):
            self.sync()
        self.assertEqual(self.sleeps, [])
        count = len(self.fake.requests)
        with self.assertRaisesRegex(SourceError, "rate limited until"):
            self.sync()
        self.assertEqual(len(self.fake.requests), count)  # no request while backing off
        self.assertIsNone(pad.last_sync(self.db))

    def test_replay_handles_utf16_and_snapshots(self):
        h = pad.History(json.loads(history_json(STATES_V1, snapshot_at=2)))
        self.assertEqual([h.text_at(r) for r in range(4)], [s[0] for s in STATES_V1])
        h = pad.History(json.loads(history_json(STATES_V1, snapshot_at=None)))
        self.assertEqual(h.text_at(3), STATES_V1[3][0])
        with self.assertRaises(SourceError):
            pad.apply_changeset(pad.utf16("abc"), "Z:9>1=1*0+1$x")

    def test_default_pad_is_configured_unless_turned_off(self):
        with open(os.path.join(os.environ["OPENRTX_CONFIG"], "config"), "w") as f:
            f.write("")
        self.assertEqual(pad.configured_pads(), {"officehours": pad.DEFAULT_PADS["officehours"]})
        with open(os.path.join(os.environ["OPENRTX_CONFIG"], "config"), "w") as f:
            f.write("pad.extra = https://example.org/p/x/timeslider#3\n")
        self.assertEqual(pad.configured_pads()["extra"], "https://example.org/p/x")


if __name__ == "__main__":
    unittest.main()
