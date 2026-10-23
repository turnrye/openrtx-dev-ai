#!/usr/bin/env python3
"""Tests for `openrtx taiga` writes against a fake Taiga API (nothing reaches Taiga).

    python3 tests/test_taiga_write.py
"""

import contextlib
import importlib
import io
import base64
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENDPOINTS = {"epics": "epic", "userstories": "us", "tasks": "task", "issues": "issue"}
HISTORY = {"epic": "epic", "userstory": "us", "task": "task", "issue": "issue"}


class FakeTaiga:
    """Just enough of the Taiga API for taiga_write.py."""

    def __init__(self):
        self.items = {}           # (kind, id) -> dict
        self.history = {}         # (kind, id) -> [entries]
        self.related = []         # (epic id, story id)
        self.writes = []          # (method, path)
        self.next_ref = 900
        self.next_id = 100
        self.valid_tokens = {"good"}
        self.refresh_ok = True
        self.refreshes = 0
        self.add("us", "Existing story", "old text", ref=313)

    def add(self, kind, subject, description, ref=None):
        if ref is None:
            ref = self.next_ref
            self.next_ref += 1
        item = {"id": self.next_id, "ref": ref, "subject": subject, "version": 1,
                "description": description, "description_html": render(description),
                "project": 1}
        self.next_id += 1
        self.items[(kind, item["id"])] = item
        self.history[(kind, item["id"])] = []
        return item

    def by_ref(self, ref):
        for (kind, item_id), it in self.items.items():
            if it["ref"] == ref:
                return kind, item_id
        return None


def render(md):
    html = re.sub(r"^(.+)\n-{3,}$", r"<h2>\1</h2>", md, flags=re.M)
    html = re.sub(r"#(\d+)", r'<a class="reference" href="/x/\1">#\1</a>', html)
    return "<p>" + html + "</p>"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, data=None):
        body = b"" if data is None else json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authed(self):
        token = (self.headers.get("Authorization") or "").replace("Bearer ", "")
        return token in self.server.fake.valid_tokens

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n)) if n else {}

    def do_GET(self):
        fake = self.server.fake
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        parts = u.path.split("/")[3:]  # after /api/v1
        if parts == ["projects", "by_slug"]:
            return self.reply(200, {"id": 1})
        if parts == ["users", "me"]:
            return self.reply(200, {"id": 17, "username": "k0ret"}) if self.authed() \
                else self.reply(401, {"detail": "expired"})
        if parts == ["resolver"]:
            found = fake.by_ref(int(q["ref"]))
            return self.reply(200, {"project": 1, found[0]: found[1]} if found else {})
        if len(parts) == 1 and parts[0] in ENDPOINTS:
            kind = ENDPOINTS[parts[0]]
            return self.reply(200, [i for (k, _), i in fake.items.items() if k == kind])
        if len(parts) == 2 and parts[0] in ENDPOINTS:
            return self.reply(200, fake.items[(ENDPOINTS[parts[0]], int(parts[1]))])
        if len(parts) == 3 and parts[0] == "history":
            return self.reply(200, fake.history[(HISTORY[parts[1]], int(parts[2]))])
        self.reply(404, {"detail": "no route " + u.path})

    def do_POST(self):
        fake = self.server.fake
        parts = urlparse(self.path).path.split("/")[3:]
        data = self.body()
        if parts == ["auth", "refresh"]:
            if not fake.refresh_ok:
                return self.reply(401, {"detail": "refresh expired"})
            fake.refreshes += 1
            fake.valid_tokens.add("fresh")
            return self.reply(200, {"auth_token": "fresh", "refresh": "r2"})
        if parts == ["auth"]:
            if (data.get("username"), data.get("password")) != ("k0ret", "s3cret"):
                return self.reply(400, {"detail": "bad credentials"})
            fake.valid_tokens.add("logged-in")
            return self.reply(200, {"auth_token": "logged-in", "refresh": "r9",
                                    "username": "k0ret"})
        if not self.authed():
            return self.reply(401, {"detail": "expired"})
        fake.writes.append(("POST", "/".join(parts)))
        if len(parts) == 1 and parts[0] in ENDPOINTS:
            item = fake.add(ENDPOINTS[parts[0]], data["subject"], data["description"])
            item.update({k: v for k, v in data.items() if k in ("tags", "user_story")})
            return self.reply(201, item)
        if len(parts) == 3 and parts[0] == "epics" and parts[2] == "related_userstories":
            fake.related.append((data["epic"], data["user_story"]))
            return self.reply(201, data)
        self.reply(404, {"detail": "no route"})

    def do_PATCH(self):
        fake = self.server.fake
        parts = urlparse(self.path).path.split("/")[3:]
        if not self.authed():
            return self.reply(401, {"detail": "expired"})
        fake.writes.append(("PATCH", "/".join(parts)))
        data = self.body()
        kind, item_id = ENDPOINTS[parts[0]], int(parts[1])
        item = fake.items[(kind, item_id)]
        if data.pop("version") != item["version"]:
            return self.reply(400, {"version": "out of date"})
        comment = data.pop("comment", None)
        if comment:
            fake.history[(kind, item_id)].append(
                {"comment": comment, "comment_html": render(comment), "user": {"pk": 17}})
        item.update(data)
        if "description" in data:
            item["description_html"] = render(data["description"])
        item["version"] += 1
        self.reply(200, item)


DRAFT = """Notes for me; ignored.

<!-- taiga: item toolkit
type: epic
subject: UI: Widget Toolkit
tags: ui
-->
Intro. Later work is in [look].

Appetite
--------

Text with a [real link](https://example.org) and #313.

<!-- taiga: item look
type: epic
subject: UI: new look
-->
Follows [toolkit].

<!-- taiga: item tests
type: us
subject: UI golden-image tests
epic: [toolkit]
-->
Story text.

<!-- taiga: item runner
type: task
subject: Port the runner
story: [tests]
-->
Task text, part of [tests].

<!-- taiga: comment #313 -->
See [toolkit] and [look]. Mail noreply@example.org is fine.

<!-- taiga: comment [look] -->
Self note.
"""


class ServerCase(unittest.TestCase):
    """A fake Taiga, a temporary auth file and the module under test."""

    def setUp(self):
        self.fake = FakeTaiga()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.fake = self.fake
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.auth = os.path.join(self.tmp.name, "auth.json")
        with open(self.auth, "w") as f:
            json.dump({"auth_token": "good", "refresh": "r1"}, f)
        os.environ["TAIGA_API"] = "http://127.0.0.1:%d/api/v1" % self.server.server_port
        os.environ["TAIGA_AUTH"] = self.auth
        os.environ["OPENRTX_CACHE"] = os.path.join(self.tmp.name, "cache")
        os.environ["OPENRTX_CONFIG"] = os.path.join(self.tmp.name, "config")
        sys.path.insert(0, ROOT)
        import openrtx.taiga_write as taiga_write
        self.tw = importlib.reload(taiga_write)
        self.draft = os.path.join(self.tmp.name, "draft.md")
        self.write_draft(DRAFT)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def write_draft(self, text):
        with open(self.draft, "w") as f:
            f.write(text)

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.tw.main(list(argv))
        return code, out.getvalue()

    def item(self, subject):
        return next(i for i in self.fake.items.values() if i["subject"] == subject)


class TaigaWriteTest(ServerCase):

    def test_dry_run_sends_nothing(self):
        code, out = self.run_cli("post", self.draft)
        self.assertEqual(code, 0, out)
        self.assertIn("Dry run", out)
        self.assertEqual(self.fake.writes, [])

    def test_post_resolves_placeholders_links_and_comments(self):
        code, out = self.run_cli("post", self.draft, "--post")
        self.assertEqual(code, 0, out)
        toolkit, look = self.item("UI: Widget Toolkit"), self.item("UI: new look")
        tests, runner = self.item("UI golden-image tests"), self.item("Port the runner")
        self.assertIn("Later work is in #%d." % look["ref"], toolkit["description"])
        self.assertIn("[real link](https://example.org)", toolkit["description"])
        self.assertIn("Follows #%d." % toolkit["ref"], look["description"])
        self.assertIn("part of #%d." % tests["ref"], runner["description"])
        self.assertEqual(toolkit["tags"], ["ui"])
        self.assertEqual(runner["user_story"], tests["id"])
        self.assertEqual(self.fake.related, [(toolkit["id"], tests["id"])])
        existing = self.fake.by_ref(313)
        self.assertEqual(self.fake.history[existing][0]["comment"],
                         "See #%d and #%d. Mail noreply@example.org is fine."
                         % (toolkit["ref"], look["ref"]))
        self.assertEqual(self.fake.history[("epic", look["id"])][0]["comment"], "Self note.")
        # Only the item whose placeholder pointed forward needed a second write
        patches = [p for m, p in self.fake.writes if m == "PATCH"
                   and p == "epics/%d" % toolkit["id"]]
        self.assertEqual(len(patches), 1)
        self.assertNotIn("NOT FOUND", out)
        with open(self.draft + ".posted.json") as f:
            self.assertEqual(json.load(f)["items"]["look"]["ref"], look["ref"])

    def test_github_refs_become_links_and_bare_refs_are_listed(self):
        self.write_draft(DRAFT.replace("Intro.", "Intro, see OpenRTX/OpenRTX#520."))
        code, out = self.run_cli("post", self.draft)
        self.assertEqual(code, 0, out)
        self.assertIn("#313 -> Taiga us 'Existing story'", out)
        self.assertNotIn("#520", out)  # expanded into a link, so not a bare ref
        code, out = self.run_cli("post", self.draft, "--post")
        self.assertEqual(code, 0, out)
        self.assertIn("[OpenRTX/OpenRTX#520](https://github.com/OpenRTX/OpenRTX/issues/520)",
                      self.item("UI: Widget Toolkit")["description"])

    def test_refuses_bare_ref_with_no_taiga_item(self):
        self.write_draft(DRAFT.replace("Self note.", "Supersedes #445."))
        code, out = self.run_cli("post", self.draft, "--post")
        self.assertEqual(code, 1)
        self.assertIn("#445 matches no Taiga item", out)
        self.assertIn("OpenRTX/OpenRTX#445", out)
        self.assertEqual(self.fake.writes, [])

    def test_hash_link_text_to_github_is_rewritten(self):
        self.write_draft(DRAFT.replace(
            "Intro.", "Intro, [#520](https://github.com/OpenRTX/OpenRTX/pull/520)."))
        code, out = self.run_cli("post", self.draft, "--post")
        self.assertEqual(code, 0, out)
        self.assertIn("[OpenRTX/OpenRTX#520](https://github.com/OpenRTX/OpenRTX/pull/520)",
                      self.item("UI: Widget Toolkit")["description"])

    def test_refuses_hash_link_text_to_other_sites(self):
        self.write_draft(DRAFT.replace("Self note.", "See [#313 notes](https://example.org)."))
        code, out = self.run_cli("post", self.draft, "--post")
        self.assertEqual(code, 1)
        self.assertIn("starts with #313", out)
        self.assertEqual(self.fake.writes, [])

    def test_refuses_duplicates(self):
        self.fake.add("epic", "UI: Widget Toolkit", "already there")
        code, out = self.run_cli("post", self.draft, "--post")
        self.assertEqual(code, 1)
        self.assertIn("already exists", out)
        self.assertEqual(self.fake.writes, [])

    def test_refuses_mentions(self):
        self.write_draft(DRAFT.replace("Self note.", "Thanks @fizzo."))
        code, out = self.run_cli("post", self.draft, "--post")
        self.assertEqual(code, 1)
        self.assertIn("@fizzo", out)
        self.assertEqual(self.fake.writes, [])

    def test_checks_draft_structure(self):
        self.write_draft(DRAFT.replace("story: [tests]", "story: [nope]")
                         .replace("type: us", "type: storyy"))
        code, out = self.run_cli("post", self.draft)
        self.assertEqual(code, 1)
        self.assertIn("[nope] is not an item", out)
        self.assertIn("type must be one of", out)

    def test_task_story_must_come_first(self):
        self.write_draft("<!-- taiga: item t\ntype: task\nsubject: A task\nstory: [s]\n-->\nT.\n\n"
                         "<!-- taiga: item s\ntype: us\nsubject: A story\n-->\nS.\n")
        code, out = self.run_cli("post", self.draft)
        self.assertEqual(code, 1)
        self.assertIn("must come earlier", out)

    def test_refreshes_expired_token(self):
        self.fake.valid_tokens = set()
        code, out = self.run_cli("whoami")
        self.assertEqual(code, 0, out)
        with open(self.auth) as f:
            self.assertEqual(json.load(f)["auth_token"], "fresh")
        if os.name == "posix":
            self.assertEqual(os.stat(self.auth).st_mode & 0o777, 0o600)

    def test_comment_and_edit_commands(self):
        note = os.path.join(self.tmp.name, "note.md")
        with open(note, "w") as f:
            f.write("A comment.\n")
        code, out = self.run_cli("comment", "#313", note)
        self.assertEqual((code, self.fake.writes), (0, []))
        code, out = self.run_cli("comment", "#313", note, "--post")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.fake.history[self.fake.by_ref(313)][-1]["comment"], "A comment.")
        desc = os.path.join(self.tmp.name, "desc.md")
        with open(desc, "w") as f:
            f.write("New text\n--------\n")
        code, out = self.run_cli("edit", "313", "--description-file", desc, "--post")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.fake.items[self.fake.by_ref(313)]["description"], "New text\n--------")
        self.assertIn("h2 1", out)


def jwt(exp):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return "%s.%s.sig" % (enc({"alg": "none"}), enc({"exp": exp}))


class ApiCommandTest(ServerCase):
    def test_get_runs_without_login(self):
        os.remove(self.auth)
        code, out = self.run_cli("api", "GET", "/projects/by_slug?slug=openrtx")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out), {"id": 1})

    def test_write_is_a_dry_run_until_post(self):
        body = os.path.join(self.tmp.name, "body.json")
        with open(body, "w") as f:
            json.dump({"project": 1, "subject": "New epic",
                       "description": "See OpenRTX/OpenRTX#520 and #313."}, f)
        code, out = self.run_cli("api", "POST", "/epics", "--data", body)
        self.assertEqual(code, 0, out)
        self.assertIn("Dry run", out)
        self.assertIn("[OpenRTX/OpenRTX#520](https://github.com/OpenRTX/OpenRTX/issues/520)", out)
        self.assertEqual(self.fake.writes, [])
        code, out = self.run_cli("api", "POST", "/epics", "--data", body, "--post")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.fake.writes, [("POST", "epics")])
        self.assertIn("issues/520)", self.item("New epic")["description"])

    def test_write_refuses_mentions_and_unknown_refs(self):
        body = os.path.join(self.tmp.name, "body.json")
        with open(body, "w") as f:
            json.dump({"comment": "Thanks @fizzo, see #445", "version": 1}, f)
        code, out = self.run_cli("api", "PATCH", "/userstories/100", "--data", body, "--post")
        self.assertEqual(code, 1)
        self.assertIn("@fizzo", out)
        self.assertIn("#445 matches no Taiga item", out)
        self.assertEqual(self.fake.writes, [])

    def test_legacy_login_is_moved(self):
        legacy = os.path.join(self.tmp.name, "legacy.json")
        with open(legacy, "w") as f:
            json.dump({"auth_token": "good", "refresh": "r1"}, f)
        del os.environ["TAIGA_AUTH"]
        try:
            tw = importlib.reload(self.tw)
            tw.LEGACY_AUTH_PATH = legacy
            self.assertEqual(tw.load_auth()["auth_token"], "good")
            self.assertTrue(os.path.exists(tw.AUTH_PATH))
            self.assertTrue(tw.AUTH_PATH.startswith(os.environ["OPENRTX_CONFIG"]))
        finally:
            os.environ["TAIGA_AUTH"] = self.auth


class LoginTest(ServerCase):
    """Login and token renewal."""

    def saved(self):
        with open(self.auth) as f:
            return json.load(f)

    def run_err(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, out = self.run_cli(*argv)
        return code, out, err.getvalue()

    def test_no_login_exits_3(self):
        os.remove(self.auth)
        code, _, err = self.run_err("whoami")
        self.assertEqual(code, 3)
        self.assertIn("Login needed", err)

    def test_expired_refresh_exits_3(self):
        self.fake.valid_tokens = set()
        self.fake.refresh_ok = False
        code, _, err = self.run_err("whoami")
        self.assertEqual(code, 3)
        self.assertIn("expired", err)

    def test_dry_run_works_without_login(self):
        os.remove(self.auth)
        code, out = self.run_cli("post", self.draft)
        self.assertEqual(code, 0, out)

    def test_refreshes_before_expiry(self):
        expired = jwt(int(time.time()) - 10)
        self.fake.valid_tokens.add(expired)  # still accepted: renewal is proactive
        with open(self.auth, "w") as f:
            json.dump({"auth_token": expired, "refresh": "r1"}, f)
        code, out = self.run_cli("whoami")
        self.assertEqual((code, self.fake.refreshes), (0, 1), out)
        self.assertEqual(self.saved()["auth_token"], "fresh")

    def test_unexpired_token_is_not_refreshed(self):
        live = jwt(int(time.time()) + 3600)
        self.fake.valid_tokens.add(live)
        with open(self.auth, "w") as f:
            json.dump({"auth_token": live, "refresh": "r1"}, f)
        self.assertEqual(self.run_cli("whoami")[0], 0)
        self.assertEqual(self.fake.refreshes, 0)

    def test_env_login(self):
        os.remove(self.auth)
        os.environ.update(TAIGA_USERNAME="k0ret", TAIGA_PASSWORD="s3cret")
        try:
            code, out = self.run_cli("login")
        finally:
            del os.environ["TAIGA_USERNAME"], os.environ["TAIGA_PASSWORD"]
        self.assertEqual(code, 0, out)
        self.assertNotIn("s3cret", out)
        self.assertEqual(self.saved(), {"auth_token": "logged-in", "refresh": "r9",
                                        "username": "k0ret"})

    def test_browser_login(self):
        os.remove(self.auth)
        ready, result = threading.Event(), {}

        def on_ready(url):
            result["url"] = url
            ready.set()

        def run():
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                result["user"] = self.tw.browser_login(
                    "k0ret", timeout=20, open_browser=False, on_ready=on_ready)
            result["out"] = out.getvalue()

        t = threading.Thread(target=run)
        t.start()
        self.assertTrue(ready.wait(10))
        url = result["url"]
        page = urllib.request.urlopen(url).read().decode()
        self.assertIn('value="k0ret"', page)
        self.assertIn('type="password"', page)

        def post(password, target=url, host=None):
            req = urllib.request.Request(
                target, data=urllib.parse.urlencode(
                    {"username": "k0ret", "password": password}).encode(),
                headers={"Host": host} if host else {})
            try:
                return urllib.request.urlopen(req).read().decode()
            except urllib.error.HTTPError as e:
                return "HTTP %d" % e.code

        # Wrong path, or a Host other than loopback (DNS rebinding): refused
        base = url.rsplit("/", 1)[0]
        self.assertEqual(post("s3cret", base + "/guess"), "HTTP 404")
        self.assertEqual(post("s3cret", host="evil.example:80"), "HTTP 404")
        # Wrong password: the form again, with an error, and no password echoed
        page = post("wrong")
        self.assertIn("accept that username and password", page)
        self.assertNotIn('value="wrong"', page)
        self.assertFalse(os.path.exists(self.auth))
        # Right password: done, token saved, server stops
        self.assertIn("Logged in as k0ret", post("s3cret"))
        t.join(10)
        self.assertEqual(result["user"], "k0ret")
        self.assertEqual(self.saved()["auth_token"], "logged-in")
        self.assertNotIn("s3cret", result["out"])
        with self.assertRaises(OSError):
            urllib.request.urlopen(url, timeout=2)

    def test_browser_login_times_out(self):
        os.remove(self.auth)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(self.tw.TaigaError):
            self.tw.browser_login("k0ret", timeout=2, open_browser=False)
        self.assertFalse(os.path.exists(self.auth))


if __name__ == "__main__":
    unittest.main()
