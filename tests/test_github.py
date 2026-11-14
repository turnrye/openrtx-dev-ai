#!/usr/bin/env python3
"""Tests for the GitHub source against a fake GitHub API (nothing reaches GitHub).

    python3 tests/test_github.py
"""

import copy
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openrtx import paths  # noqa: E402
from openrtx.sources import github  # noqa: E402

OLD = "2026-01-0%dT00:00:00Z"


def user(login):
    return {"login": login}


def issue(n, title, body="", updated=OLD % 1, pr=False, state="open", labels=()):
    d = {"number": n, "title": title, "body": body, "state": state, "user": user("alice"),
         "labels": [{"name": l} for l in labels], "created_at": OLD % 1,
         "updated_at": updated, "closed_at": None,
         "html_url": "https://github.com/OpenRTX/OpenRTX/%s/%d" % ("pull" if pr else "issues", n)}
    if pr:
        d["pull_request"] = {"merged_at": None}
    return d


class FakeGitHub:
    def __init__(self):
        self.repos = [
            {"full_name": "OpenRTX/OpenRTX", "has_issues": True, "fork": False, "archived": False},
            {"full_name": "OpenRTX/rtxlink", "has_issues": True, "fork": False, "archived": True},
            {"full_name": "OpenRTX/forked", "has_issues": True, "fork": True, "archived": False},
            {"full_name": "OpenRTX/mirror", "has_issues": False, "fork": False, "archived": False},
        ]
        self.issues = {
            "OpenRTX/OpenRTX": [
                issue(1, "Golden image tests", "Screenshots of the UI", OLD % 2, pr=True,
                      labels=["ui"]),
                issue(2, "Crash on boot", "It crashes", OLD % 3),
                issue(3, "clang-format drift", "Two configs", OLD % 4),
                issue(4, "Add keyhold", "Hold keys", OLD % 5, pr=True),
                issue(5, "Five", "body five", OLD % 6),
            ],
            "OpenRTX/rtxlink": [issue(1, "rtxlink bug", "Serial issue", OLD % 2)],
            "OpenRTX/forked": [issue(1, "fork issue", "should be skipped", OLD % 2)],
        }
        self.pulls = {"OpenRTX/mirror": [
            dict(issue(7, "Mirror PR", "mirrored", OLD % 3), merged_at=OLD % 4),
        ]}
        self.comments = {"OpenRTX/OpenRTX": [
            {"id": 11, "issue_url": "https://x/repos/OpenRTX/OpenRTX/issues/1",
             "user": user("bob"), "body": "Please use PNG goldens", "created_at": OLD % 3,
             "updated_at": OLD % 3, "html_url": "https://github.com/c/11"},
            {"id": 12, "issue_url": "https://x/repos/OpenRTX/OpenRTX/issues/1",
             "user": user("carol"), "body": "Agreed, smaller repo", "created_at": OLD % 4,
             "updated_at": OLD % 4, "html_url": "https://github.com/c/12"},
        ]}
        self.review_comments = {"OpenRTX/OpenRTX": [
            {"id": 21, "pull_request_url": "https://x/repos/OpenRTX/OpenRTX/pulls/1",
             "user": user("silseva"), "body": "Move this into the runner", "path": "scripts/run_e2e.py",
             "line": 12, "created_at": OLD % 5, "updated_at": OLD % 5,
             "html_url": "https://github.com/rc/21"},
        ]}
        self.reviews = {("OpenRTX/OpenRTX", 1): [
            {"id": 31, "user": user("silseva"), "body": "Split the goldens out", "state":
             "CHANGES_REQUESTED", "submitted_at": OLD % 6, "html_url": "https://github.com/r/31"},
            {"id": 32, "user": user("bob"), "body": "", "state": "COMMENTED",
             "submitted_at": OLD % 7, "html_url": "https://github.com/r/32"},
        ]}
        self.page_size = 2
        self.remaining = 5000
        self.reset_ts = int(time.time()) + 3600
        self.requests = []
        self.auth = set()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, data, link=None):
        fake = self.server.fake
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-RateLimit-Remaining", str(max(fake.remaining, 0)))
        self.send_header("X-RateLimit-Reset", str(fake.reset_ts))
        if link:
            self.send_header("Link", link)
        self.end_headers()
        self.wfile.write(body)

    def page(self, rows, u, q):
        n = int(q.get("page", "1"))
        size = self.server.fake.page_size
        chunk = rows[(n - 1) * size:n * size]
        link = None
        if n * size < len(rows):
            q2 = dict(q, page=str(n + 1))
            link = '<http://127.0.0.1:%d%s?%s>; rel="next"' % (
                self.server.server_port, u.path, urlencode(q2))
        self.reply(200, chunk, link)

    def do_GET(self):
        fake = self.server.fake
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        fake.requests.append((u.path, q))
        fake.auth.add(self.headers.get("Authorization"))
        if fake.remaining <= 0:
            return self.reply(403, {"message": "API rate limit exceeded"})
        fake.remaining -= 1
        since = q.get("since")
        recent = lambda rows: [r for r in rows if not since or
                               (r.get("updated_at") or "") >= since]
        parts = u.path.strip("/").split("/")
        if parts[:1] == ["orgs"]:
            return self.page(fake.repos, u, q)
        repo = "/".join(parts[1:3])
        rest = parts[3:]
        if rest == ["issues"]:
            if repo == "OpenRTX/mirror":
                return self.reply(410, {"message": "Issues are disabled"})
            rows = sorted(recent(fake.issues.get(repo, [])), key=lambda r: r["updated_at"])
            return self.page(rows, u, q)
        if rest == ["issues", "comments"]:
            return self.page(recent(fake.comments.get(repo, [])), u, q)
        if rest == ["pulls", "comments"]:
            return self.page(recent(fake.review_comments.get(repo, [])), u, q)
        if rest == ["pulls"]:
            rows = sorted(fake.pulls.get(repo, []), key=lambda r: r["updated_at"], reverse=True)
            return self.page(rows, u, q)
        if len(rest) == 3 and rest[0] == "pulls" and rest[2] == "reviews":
            return self.page(fake.reviews.get((repo, int(rest[1])), []), u, q)
        self.reply(404, {"message": "Not Found"})


class GitHubSourceTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGitHub()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.fake = self.fake
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.env = dict(os.environ)
        os.environ.update(
            OPENRTX_CACHE=os.path.join(self.tmp.name, "cache"),
            OPENRTX_CONFIG=os.path.join(self.tmp.name, "config"),
            GITHUB_API="http://127.0.0.1:%d" % self.server.server_port,
            GITHUB_TOKEN="test-token", GITHUB_RATE_FLOOR="1")
        os.environ.pop("GH_TOKEN", None)
        self.db = paths.db_path("github")
        self.stderr, sys.stderr = sys.stderr, io.StringIO()  # progress lines

    def tearDown(self):
        sys.stderr = self.stderr
        self.server.shutdown()
        self.server.server_close()
        os.environ.clear()
        os.environ.update(self.env)
        self.tmp.cleanup()

    def rows(self, sql, *args):
        return github.connect_ro(self.db).execute(sql, args).fetchall()

    def test_first_sync_stores_items_comments_and_reviews(self):
        summary = github.sync(self.db)
        self.assertIn("issues/PRs", summary)
        items = {(r["repo"], r["number"]): r for r in self.rows("SELECT * FROM items")}
        self.assertEqual(len(items), 5 + 1 + 1)  # OpenRTX, rtxlink, mirror; fork skipped
        self.assertNotIn(("OpenRTX/forked", 1), items)
        self.assertEqual(items[("OpenRTX/OpenRTX", 1)]["is_pr"], 1)
        self.assertEqual(items[("OpenRTX/OpenRTX", 2)]["is_pr"], 0)
        self.assertEqual(items[("OpenRTX/OpenRTX", 1)]["labels"], "ui")
        self.assertEqual(items[("OpenRTX/mirror", 7)]["merged_at"], OLD % 4)
        kinds = sorted(r["kind"] for r in self.rows("SELECT kind FROM comments"))
        # the empty COMMENTED review is skipped; the change request is kept
        self.assertEqual(kinds, ["comment", "comment", "review", "review_comment"])
        self.assertEqual(self.fake.auth, {"Bearer test-token"})
        self.assertIsNotNone(github.last_sync(self.db))

    def test_pagination_follows_link_headers(self):
        github.sync(self.db)
        pages = [q.get("page") for p, q in self.fake.requests if p == "/repos/OpenRTX/OpenRTX/issues"]
        self.assertEqual(pages, [None, "2", "3"])  # 5 items, 2 per page
        self.assertTrue(all(q.get("per_page") == "100" for p, q in self.fake.requests
                            if q.get("page") is None))

    def test_incremental_sync_uses_since(self):
        github.sync(self.db)
        self.fake.requests.clear()
        now = github.iso(github.now_utc() + github.dt.timedelta(seconds=5))
        self.fake.comments["OpenRTX/OpenRTX"].append(
            {"id": 13, "issue_url": "https://x/repos/OpenRTX/OpenRTX/issues/2",
             "user": user("dave"), "body": "Fixed by the new boot order", "created_at": now,
             "updated_at": now, "html_url": "https://github.com/c/13"})
        changed = copy.deepcopy(self.fake.issues["OpenRTX/OpenRTX"][1])
        changed.update(updated_at=now, state="closed")
        self.fake.issues["OpenRTX/OpenRTX"][1] = changed
        summary = github.sync(self.db)
        self.assertIn("0 new and 1 updated", summary)
        issue_reqs = [q for p, q in self.fake.requests if p == "/repos/OpenRTX/OpenRTX/issues"]
        self.assertTrue(issue_reqs and "since" in issue_reqs[0])
        self.assertEqual(self.rows("SELECT state FROM items WHERE number = 2 AND "
                                   "repo = 'OpenRTX/OpenRTX'")[0]["state"], "closed")
        versions = self.rows("SELECT count(*) AS n FROM versions WHERE number = 2 AND "
                             "repo = 'OpenRTX/OpenRTX'")[0]["n"]
        self.assertEqual(versions, 2)
        self.assertIn("boot order", github.show(self.db, "OpenRTX/OpenRTX#2"))

    def test_full_sync_ignores_since(self):
        github.sync(self.db)
        self.fake.requests.clear()
        github.sync(self.db, full=True)
        self.assertFalse(any("since" in q for p, q in self.fake.requests))

    def test_rate_limit_stops_then_resumes(self):
        self.fake.remaining = 4
        self.fake.reset_ts = int(time.time()) - 1  # already reset by the next sync
        summary = github.sync(self.db)
        self.assertIn("partial, rate-limited", summary)
        self.assertIsNone(github.last_sync(self.db))  # not a successful sync
        self.fake.remaining = 5000
        summary = github.sync(self.db)
        self.assertNotIn("partial", summary)
        self.assertEqual(len(self.rows("SELECT * FROM items")), 7)
        self.assertIsNotNone(github.last_sync(self.db))

    def test_rate_limit_waits_until_reset(self):
        self.fake.remaining = 3
        github.sync(self.db)
        self.fake.remaining = 5000
        self.fake.requests.clear()
        summary = github.sync(self.db)
        self.assertIn("resuming after", summary)
        self.assertEqual(self.fake.requests, [])

    def test_search_returns_one_hit_per_item(self):
        github.sync(self.db)
        hits = github.search(self.db, "goldens", 10)
        self.assertEqual([h["ref"] for h in hits], ["OpenRTX/OpenRTX#1"])
        self.assertEqual(hits[0]["source"], "github")
        self.assertTrue(hits[0]["kind"].startswith("pr"))
        self.assertIn("[goldens]", hits[0]["snippet"].lower())
        self.assertEqual(github.search(self.db, "clang-format", 5)[0]["ref"], "OpenRTX/OpenRTX#3")
        self.assertEqual(github.search(self.db, "nothing-matches-this", 5), [])

    def test_search_before_any_sync_is_empty(self):
        self.assertEqual(github.search(self.db, "x", 5), [])
        self.assertIsNone(github.show(self.db, "OpenRTX/OpenRTX#1"))

    def test_show_lists_discussion_in_time_order(self):
        github.sync(self.db)
        text = github.show(self.db, "https://github.com/OpenRTX/OpenRTX/pull/1")
        self.assertTrue(text.startswith("OpenRTX/OpenRTX#1  Golden image tests"))
        self.assertIn("PR, open, by alice", text)
        self.assertIn("labels: ui", text)
        order = [text.index(s) for s in ("Please use PNG goldens", "Agreed, smaller repo",
                                         "Move this into the runner", "Split the goldens out")]
        self.assertEqual(order, sorted(order))
        self.assertIn("review comment by silseva, 2026-01-05, scripts/run_e2e.py:12", text)
        self.assertIn("(changes requested)", text)
        self.assertIsNone(github.show(self.db, "OpenRTX/OpenRTX#99"))

    def test_refs(self):
        self.assertEqual(github.parse_ref("OpenRTX/OpenRTX#520"), ("OpenRTX/OpenRTX", 520))
        self.assertEqual(github.parse_ref("https://github.com/OpenRTX/rtxlink/issues/12"),
                         ("OpenRTX/rtxlink", 12))
        self.assertEqual(github.parse_ref("https://github.com/OpenRTX/OpenRTX/pull/509"
                                          "#pullrequestreview-5"), ("OpenRTX/OpenRTX", 509))
        for other in ("taiga#841", "#520", "pad:officehours", "OpenRTX/OpenRTX",
                      "https://tasks.openrtx.org/project/openrtx/us/841"):
            self.assertFalse(github.owns(other), other)
        self.assertTrue(github.owns("OpenRTX/OpenRTX#520"))


if __name__ == "__main__":
    unittest.main()
