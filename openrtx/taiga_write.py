"""Create and edit OpenRTX Taiga items from Markdown drafts, keeping their formatting.

    openrtx taiga login [USERNAME]            log in (terminal, browser or env)
    openrtx taiga whoami                      check the saved login
    openrtx taiga post DRAFT.md [--post]      create the items and comments in a draft
    openrtx taiga comment REF FILE [--post]   comment on an item
    openrtx taiga edit REF [--subject S] [--description-file F] [--post]
    openrtx taiga api METHOD PATH [--data FILE] [--post]   any Taiga API call

Every command that writes is a dry run unless --post is given: it prints what
it would send and the checks it ran, and sends nothing.

Pasting Markdown into Taiga's editor loses its formatting; the API stores the
Markdown as written and renders it server-side, so headings, lists and links
survive. After posting, each item is read back and its rendered HTML checked.

A draft is Markdown with one header per item or comment:

    <!-- taiga: item toolkit
    type: epic
    subject: UI: Widget Toolkit
    tags: ui
    -->
    Description in Markdown. [look] becomes #<ref> of the item named "look".

    <!-- taiga: item look
    type: epic
    subject: UI: new fonts and a new look
    -->
    Follows [toolkit].

    <!-- taiga: comment #313 -->
    Comment text; [toolkit] and [look] resolve here too.

In Taiga text #N links to Taiga item N, never to GitHub -- even as the text
of a Markdown link: [#520](https://github.com/...) renders as an empty GitHub
link followed by a link to Taiga item 520. Write GitHub issues and PRs as
OWNER/REPO#N (e.g. OpenRTX/OpenRTX#520): it is turned into a link whose text
Taiga leaves alone, and [#N](GitHub URL) links are rewritten the same way. The
dry run lists every #N Taiga will link, with its target; one that matches no
Taiga item, or a non-GitHub link whose text starts with #N, stops the post.

Item fields: type (epic, us, task, issue), subject, and optionally tags
(comma-separated), epic (for a us: #REF or [name] to link it under) and story
(for a task: its parent us, #REF or [name]). Text before the first header is
ignored. Placeholders that point at items created later in the same draft are
filled in with a second update once every item exists.

Logging in never puts the password where an agent driving this tool can see
it. `login` asks in the terminal when it has one; without one (e.g. when Claude
Code runs it) it serves a one-time login page on 127.0.0.1 and opens it in the
browser; with TAIGA_USERNAME and TAIGA_PASSWORD set (for sessions with no local
browser) it uses those. Only the tokens are saved, in
taiga-auth.json in the openrtx config dir (TAIGA_AUTH overrides; see paths.py). The login token lasts a day and
is refreshed automatically; each refresh renews the 8-day refresh token, so a
new login is needed only on a new machine or after 8 days unused. A command
that needs a login it can't get exits with status 3.

TAIGA_API and TAIGA_PROJECT override the instance
(https://tasks.openrtx.org/api/v1) and project slug (openrtx). Stdlib only;
Python 3.9+; macOS, Linux and Windows.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import getpass
import html
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import paths

API = os.environ.get("TAIGA_API", "https://tasks.openrtx.org/api/v1").rstrip("/")
SITE = os.environ.get("TAIGA_SITE", API.rsplit("/api/", 1)[0])
PROJECT_SLUG = os.environ.get("TAIGA_PROJECT", "openrtx")
AUTH_PATH = os.path.expanduser(
    os.environ.get("TAIGA_AUTH") or os.path.join(paths.config_dir(), "taiga-auth.json"))
# Where the standalone taiga-write tool kept it; read once and moved
LEGACY_AUTH_PATH = os.path.expanduser("~/.config/openrtx-taiga/auth.json")
PROG = "openrtx taiga"

# Item type -> (API endpoint, history kind, URL segment)
KINDS = {
    "epic": ("epics", "epic", "epic"),
    "us": ("userstories", "userstory", "us"),
    "task": ("tasks", "task", "task"),
    "issue": ("issues", "issue", "issue"),
}
TYPE_ALIASES = {"story": "us", "userstory": "us", "user story": "us"}

HEADER_RE = re.compile(r"<!--\s*taiga:\s*(item|comment)\s+([^\s>]+)(.*?)-->", re.S)
PLACEHOLDER_RE = re.compile(r"\[([A-Za-z0-9_-]+)\](?!\()")
# OWNER/REPO#N outside a link; GitHub redirects /issues/N to /pull/N for PRs
GITHUB_REF_RE = re.compile(
    r"(?<![\[\w/.])([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9._-]+)#(\d+)\b(?!\]\()")
# [#N](https://github.com/OWNER/REPO/issues|pull/N...): Taiga would hijack the #N
GITHUB_HASH_LINK_RE = re.compile(
    r"\[#(\d+)\]\((https://github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9._-]+)/"
    r"(?:issues|pull)/(\d+)[^)\s]*)\)")
# Link text starting with #N (any other URL): Taiga turns the #N into its own link
HASH_LINK_TEXT_RE = re.compile(r"\[#(\d+)[^\]]*\]\((?!https://github\.com/)[^)]*\)")
# A #N that Taiga will turn into a link to its own item N. Taiga's rule: not
# preceded by a letter, digit or '-' (plus our own: not inside a URL or entity)
BARE_REF_RE = re.compile(r"(?<![\w/&#-])#(\d+)\b(?!\]\()")
MENTION_RE = re.compile(r"(?<![\w/`])@[A-Za-z0-9_.-]*[A-Za-z0-9_]")


EXIT_NEEDS_LOGIN = 3


class TaigaError(Exception):
    pass


class NeedsLogin(Exception):
    """No usable login: none saved, or the refresh token has expired."""


# --------------------------------------------------------------------------
# API access
# --------------------------------------------------------------------------

def load_auth():
    try:
        with open(AUTH_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        pass
    if not os.environ.get("TAIGA_AUTH") and os.path.exists(LEGACY_AUTH_PATH):
        with open(LEGACY_AUTH_PATH) as f:
            auth = json.load(f)
        save_auth(auth)  # move it to the openrtx config dir
        return auth
    raise NeedsLogin("no login saved in %s" % AUTH_PATH)


def save_auth(auth):
    os.makedirs(os.path.dirname(AUTH_PATH), exist_ok=True)
    fd = os.open(AUTH_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    if hasattr(os, "fchmod"):  # not on Windows, where the profile is private
        os.fchmod(fd, 0o600)  # also tighten a file that already existed
    with os.fdopen(fd, "w") as f:
        json.dump({k: auth[k] for k in ("auth_token", "refresh", "username")
                   if k in auth}, f)


def token_expiry(token):
    """Expiry (epoch seconds) from a JWT's payload, or None if unreadable."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))["exp"]
    except (AttributeError, IndexError, KeyError, ValueError):
        return None


def raw_request(method, path, body=None, token=None, headers=None):
    hdrs = {"Content-Type": "application/json"}
    if token:
        hdrs["Authorization"] = "Bearer " + token
    hdrs.update(headers or {})
    req = urllib.request.Request(
        API + path, method=method, headers=hdrs,
        data=None if body is None else json.dumps(body).encode())
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    return json.loads(data) if data else None


class Api:
    """Authenticated calls; refreshes the token once on a 401."""

    def __init__(self, anonymous=False):
        self.auth = None if anonymous else load_auth()
        if self.auth and self.auth.get("refresh"):
            exp = token_expiry(self.auth.get("auth_token"))
            if exp is not None and exp < time.time() + 60:
                self.refresh()  # renew ahead of time; also renews the refresh token

    def call(self, method, path, body=None, headers=None):
        token = self.auth and self.auth.get("auth_token")
        try:
            return raw_request(method, path, body, token, headers)
        except urllib.error.HTTPError as e:
            if e.code == 401 and self.auth and self.auth.get("refresh"):
                self.refresh()
                try:
                    return raw_request(method, path, body,
                                       self.auth["auth_token"], headers)
                except urllib.error.HTTPError as e2:
                    e = e2
            raise TaigaError("%s %s failed: HTTP %d %s"
                             % (method, path, e.code, e.read().decode()[:400]))

    def refresh(self):
        try:
            new = raw_request("POST", "/auth/refresh",
                              {"refresh": self.auth["refresh"]})
        except urllib.error.HTTPError:
            raise NeedsLogin("the saved login has expired (unused for over 8 days)")
        self.auth.update(new)
        save_auth(self.auth)

    def project_id(self):
        if not hasattr(self, "_project_id"):
            self._project_id = self.call(
                "GET", "/projects/by_slug?slug=%s" % PROJECT_SLUG)["id"]
        return self._project_id

    def resolve(self, ref):
        """#REF -> (type, id)."""
        data = self.call("GET", "/resolver?project=%s&ref=%d" % (PROJECT_SLUG, ref))
        for kind in KINDS:
            if kind in data:
                return kind, data[kind]
        raise TaigaError("#%d is not an epic, story, task or issue (%s)" % (ref, data))

    def get_item(self, kind, item_id):
        return self.call("GET", "/%s/%d" % (KINDS[kind][0], item_id))

    def list_subjects(self, kind):
        rows = self.call("GET", "/%s?project=%d" % (KINDS[kind][0], self.project_id()),
                         headers={"x-disable-pagination": "True"})
        return {r["subject"].strip().lower(): r["ref"] for r in rows}


def expand_github(text):
    """Write GitHub references so Taiga leaves them alone.

    OWNER/REPO#N becomes a link, and [#N](GitHub URL) gets OWNER/REPO#N as its
    text: Taiga links any #N not preceded by a letter or digit, link text
    included, so only the OWNER/REPO prefix keeps it from becoming a Taiga ref.
    """
    text = GITHUB_HASH_LINK_RE.sub(
        lambda m: "[%s/%s#%s](%s)" % (m.group(3), m.group(4), m.group(1), m.group(2)), text)
    return GITHUB_REF_RE.sub(
        lambda m: "[%s/%s#%s](https://github.com/%s/%s/issues/%s)"
        % (m.group(1), m.group(2), m.group(3), m.group(1), m.group(2), m.group(3)), text)


def bare_refs(text):
    return sorted({int(n) for n in BARE_REF_RE.findall(text)})


def lookup(api, ref):
    """#REF -> (type, subject), or None when Taiga has no such item."""
    try:
        kind, item_id = api.resolve(ref)
    except TaigaError:
        return None
    return kind, api.get_item(kind, item_id)["subject"]


def check_bare_refs(api, label, text, problems, out=print):
    """Show what each bare #N links to; a #N with no Taiga item is a problem."""
    for m in HASH_LINK_TEXT_RE.finditer(text):
        problems.append("%s: link text %r starts with #%s, which Taiga turns into a "
                        "link to its own item #%s; reword the link text"
                        % (label, m.group(0).split("](")[0] + "]", m.group(1), m.group(1)))
    for ref in bare_refs(text.replace("[#", "[ #")):
        found = lookup(api, ref)
        if found:
            out("      #%d -> Taiga %s %r" % (ref, found[0], found[1]))
        else:
            out("      #%d -> no Taiga item" % ref)
            problems.append("%s: #%d matches no Taiga item; if it's a GitHub issue or PR, "
                            "write it as OWNER/REPO#%d (e.g. OpenRTX/OpenRTX#%d)"
                            % (label, ref, ref, ref))


def url(kind, ref):
    return "%s/project/%s/%s/%d" % (SITE, PROJECT_SLUG, KINDS[kind][2], ref)


def html_summary(html):
    summary = ("h2 %d, h3 %d, numbered lists %d, bullet lists %d, links %d"
               % (html.count("<h2"), html.count("<h3"), html.count("<ol"),
                  html.count("<ul"), html.count("<a ")))
    empty = len(re.findall(r"<a href=\"[^\"]+\"[^>]*></a>", html))
    if empty:
        # What Taiga leaves when it turns a link's #N text into a ref of its own
        summary += "; WARNING: %d empty link(s), probably #N link text taken over by Taiga" % empty
    return summary


# --------------------------------------------------------------------------
# Drafts
# --------------------------------------------------------------------------

class Item:
    def __init__(self, name, fields, body):
        self.name = name
        kind = fields.get("type", "").strip().lower()
        self.kind = TYPE_ALIASES.get(kind, kind)
        self.subject = fields.get("subject", "").strip()
        self.tags = [t.strip() for t in fields.get("tags", "").split(",") if t.strip()]
        self.epic = fields.get("epic", "").strip() or None
        self.story = fields.get("story", "").strip() or None
        self.body = body
        self.ref = self.id = self.version = None


class Comment:
    def __init__(self, target, body):
        self.target = target
        self.body = body


def parse_fields(text):
    fields = {}
    for line in text.strip().splitlines():
        if line.strip():
            if ":" not in line:
                raise TaigaError("bad header line %r (expected `key: value`)" % line)
            key, value = line.split(":", 1)
            fields[key.strip().lower()] = value.strip()
    return fields


def parse_draft(text):
    matches = list(HEADER_RE.finditer(text))
    if not matches:
        raise TaigaError("no `<!-- taiga: item NAME ... -->` or "
                         "`<!-- taiga: comment TARGET -->` headers found")
    items, comments = [], []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = expand_github(text[m.end():end].strip())
        if m.group(1) == "item":
            items.append(Item(m.group(2), parse_fields(m.group(3)), body))
        else:
            if m.group(3).strip():
                raise TaigaError("comment header takes only a target: %r" % m.group(0))
            comments.append(Comment(m.group(2), body))
    return items, comments


def check_draft(items, comments, allow_mentions=False):
    """Return a list of problems; empty means the draft can be posted."""
    problems = []
    names = {}
    for it in items:
        if it.name in names:
            problems.append("item name [%s] is used twice" % it.name)
        names[it.name] = it
        if it.kind not in KINDS:
            problems.append("[%s]: type must be one of %s, not %r"
                            % (it.name, ", ".join(KINDS), it.kind))
        if not it.subject:
            problems.append("[%s]: no subject" % it.name)
        if not it.body:
            problems.append("[%s]: empty description" % it.name)
        if it.epic and it.kind != "us":
            problems.append("[%s]: `epic:` only applies to a us" % it.name)
        if it.story and it.kind != "task":
            problems.append("[%s]: `story:` only applies to a task" % it.name)
    order = [it.name for it in items]
    for it in items:
        for field, want in (("epic", "epic"), ("story", "us")):
            target = getattr(it, field)
            if not target:
                continue
            m = re.fullmatch(r"\[([A-Za-z0-9_-]+)\]", target)
            if m:
                other = names.get(m.group(1))
                if not other:
                    problems.append("[%s]: %s %s is not an item in this draft"
                                    % (it.name, field, target))
                elif other.kind != want:
                    problems.append("[%s]: %s %s is a %s, not a %s"
                                    % (it.name, field, target, other.kind, want))
                elif field == "story" and order.index(other.name) > order.index(it.name):
                    problems.append("[%s]: its story %s must come earlier in the draft"
                                    % (it.name, target))
            elif not re.fullmatch(r"#\d+", target):
                problems.append("[%s]: %s must be #REF or [name], not %r"
                                % (it.name, field, target))
    for c in comments:
        m = re.fullmatch(r"\[([A-Za-z0-9_-]+)\]", c.target)
        if m and m.group(1) not in names:
            problems.append("comment target %s is not an item in this draft" % c.target)
        elif not m and not re.fullmatch(r"#\d+", c.target):
            problems.append("comment target must be #REF or [name], not %r" % c.target)
        if not c.body:
            problems.append("comment on %s is empty" % c.target)
    if not allow_mentions:
        for label, text in ([("[%s]" % it.name, it.subject + "\n" + it.body) for it in items]
                            + [("comment on %s" % c.target, c.body) for c in comments]):
            found = sorted(set(MENTION_RE.findall(text)))
            if found:
                problems.append("%s mentions %s (Taiga notifies them); remove, or "
                                "pass --allow-mentions" % (label, ", ".join(found)))
    return problems


def fill(text, items_by_name):
    """Replace [name] with #ref for every item that has a ref."""
    def sub(m):
        it = items_by_name.get(m.group(1))
        return "#%d" % it.ref if it is not None and it.ref else m.group(0)
    return PLACEHOLDER_RE.sub(sub, text)


def unresolved(text, items_by_name):
    return [n for n in PLACEHOLDER_RE.findall(text) if n in items_by_name]


# --------------------------------------------------------------------------
# Logging in
# --------------------------------------------------------------------------

def password_login(username, password):
    """Exchange a username and password for tokens and save them."""
    try:
        auth = raw_request("POST", "/auth", {"type": "normal", "username": username,
                                             "password": password})
    except urllib.error.HTTPError as e:
        if e.code in (400, 401):
            raise TaigaError("Taiga didn't accept that username and password")
        raise TaigaError("login failed: HTTP %d" % e.code)
    auth["username"] = auth.get("username") or username
    save_auth(auth)
    return auth["username"]


LOGIN_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Taiga login</title>
<style>body{font-family:system-ui,sans-serif;max-width:24rem;margin:3rem auto;padding:0 1rem}
label,input,button{display:block;width:100%%;box-sizing:border-box;margin:.4rem 0}
input,button{padding:.5rem;font-size:1rem}.err{color:#b00020}</style></head>
<body><h1>Log in to %(site)s</h1>
<p>For <code>taiga-write</code>. The password goes only to Taiga; just the login
token is saved, in <code>%(path)s</code>.</p>%(error)s
<form method="post"><label>Username<input name="username" value="%(user)s"
autocomplete="username" required></label>
<label>Password<input name="password" type="password" autocomplete="current-password"
required autofocus></label><button>Log in</button></form></body></html>"""

DONE_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Logged in</title></head>
<body style="font-family:system-ui,sans-serif;max-width:24rem;margin:3rem auto">
<h1>Logged in as %s</h1><p>You can close this tab.</p></body></html>"""


def browser_login(username="", port=0, timeout=300, open_browser=True, on_ready=None):
    """Serve a one-time login page on 127.0.0.1 and wait for it to be used.

    The page lives at a random path, answers only requests addressed to the
    loopback host, and stops after one successful login or the timeout.
    """
    nonce = secrets.token_urlsafe(24)
    state = {"user": None}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # never log requests (the POST body holds the password)

        def allowed(self):
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            return self.path == "/" + nonce and host in ("127.0.0.1", "localhost")

        def page(self, code, body):
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def form(self, user, error=""):
            return LOGIN_PAGE % {"site": html.escape(SITE), "path": html.escape(AUTH_PATH),
                                 "user": html.escape(user),
                                 "error": error and '<p class="err">%s</p>' % html.escape(error)}

        def do_GET(self):
            if not self.allowed():
                return self.page(404, "Not found")
            self.page(200, self.form(username))

        def do_POST(self):
            if not self.allowed():
                return self.page(404, "Not found")
            length = int(self.headers.get("Content-Length") or 0)
            fields = urllib.parse.parse_qs(self.rfile.read(length).decode())
            user = (fields.get("username") or [""])[0].strip()
            password = (fields.get("password") or [""])[0]
            try:
                state["user"] = password_login(user, password)
            except TaigaError as e:
                return self.page(200, self.form(user, str(e)))
            self.page(200, DONE_PAGE % html.escape(state["user"]))

    server = HTTPServer(("127.0.0.1", port), Handler)
    server.timeout = 1
    url = "http://127.0.0.1:%d/%s" % (server.server_port, nonce)
    try:
        print("Log in to Taiga in your browser: %s" % url, flush=True)
        print("(waiting up to %d s; the page works once and only on this machine)"
              % timeout, flush=True)
        if os.environ.get("SSH_CONNECTION"):
            print("This is an SSH session: forward the port first, e.g. "
                  "`ssh -L %d:127.0.0.1:%d <this host>`, then open the link."
                  % (server.server_port, server.server_port), flush=True)
        if on_ready:
            on_ready(url)
        if open_browser:
            webbrowser.open(url)
        deadline = time.time() + timeout
        while state["user"] is None and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if state["user"] is None:
        raise TaigaError("no login within %d s; run `login` again when ready" % timeout)
    return state["user"]


def saved_username():
    try:
        with open(AUTH_PATH) as f:
            return json.load(f).get("username", "")
    except (FileNotFoundError, ValueError):
        return ""


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def refresh_cache():
    """Pull a write into the local Taiga cache so research sees it at once.

    Only when the cache already exists: a first sync takes minutes and
    belongs to `openrtx sync`, not to the end of a post.
    """
    try:
        from .sources import refresh
        from .sources import taiga as source
        if source.last_sync(paths.db_path(source.NAME)) is not None:
            refresh(source, max_age=dt.timedelta(0), quiet=True)
    except Exception:  # the write succeeded; a stale cache isn't an error
        pass


def cmd_login(args):
    username = args.username or os.environ.get("TAIGA_USERNAME") or saved_username()
    mode = args.mode
    if not mode:
        if os.environ.get("TAIGA_USERNAME") and os.environ.get("TAIGA_PASSWORD"):
            mode = "env"
        elif sys.stdin.isatty():
            mode = "terminal"
        else:
            mode = "browser"
    if mode == "env":
        if not (os.environ.get("TAIGA_USERNAME") and os.environ.get("TAIGA_PASSWORD")):
            raise TaigaError("--env needs TAIGA_USERNAME and TAIGA_PASSWORD set")
        user = password_login(os.environ["TAIGA_USERNAME"], os.environ["TAIGA_PASSWORD"])
    elif mode == "terminal":
        if not sys.stdin.isatty():
            raise TaigaError("--terminal needs an interactive terminal")
        username = username or input("Taiga username: ").strip()
        user = password_login(username,
                              getpass.getpass("Taiga password for %s: " % username))
    else:
        user = browser_login(username, args.port, args.timeout, not args.no_open)
    print("Logged in as %s; token saved in %s" % (user, AUTH_PATH))


def cmd_whoami(args):
    me = Api().call("GET", "/users/me")
    print("%s (id %d), token in %s" % (me["username"], me["id"], AUTH_PATH))


def cmd_post(args):
    with open(args.draft) as f:
        items, comments = parse_draft(f.read())
    problems = check_draft(items, comments, args.allow_mentions)
    by_name = {it.name: it for it in items}
    # A dry run only reads public data, so it works without a usable login
    try:
        api = Api()
    except NeedsLogin:
        if args.post:
            raise
        api = Api(anonymous=True)

    if not problems and not args.allow_duplicate:
        existing = {}
        for it in items:
            if it.kind not in existing:
                existing[it.kind] = api.list_subjects(it.kind)
            ref = existing[it.kind].get(it.subject.lower())
            if ref:
                problems.append("[%s]: %s %s named %r already exists as #%d "
                                "(pass --allow-duplicate to post anyway)"
                                % (it.name, "an" if it.kind in ("epic", "issue") else "a",
                                   it.kind, it.subject, ref))

    print("Draft %s: %d item(s), %d comment(s)" % (args.draft, len(items), len(comments)))
    for it in items:
        extra = [("tags " + ", ".join(it.tags)) if it.tags else "",
                 ("under epic " + it.epic) if it.epic else "",
                 ("under story " + it.story) if it.story else ""]
        print("  [%s] %s %r, %d chars%s" % (it.name, it.kind, it.subject, len(it.body),
              "".join("; " + e for e in extra if e)))
        refs = sorted(set(unresolved(it.body, by_name)))
        if refs:
            print("      placeholders: %s" % ", ".join("[%s]" % r for r in refs))
        check_bare_refs(api, "[%s]" % it.name, it.body, problems)
    for c in comments:
        print("  comment on %s, %d chars" % (c.target, len(c.body)))
        check_bare_refs(api, "comment on %s" % c.target, c.body, problems)
    if problems:
        print("\nNot posting:")
        for p in problems:
            print("  - " + p)
        return 1
    if not args.post:
        print("\nChecks passed. Dry run: nothing sent; add --post to send.")
        return 0

    pid = api.project_id()
    # 1. Create items in draft order, filling placeholders for items that exist
    for it in items:
        it.sent = fill(it.body, by_name)
        body = {"project": pid, "subject": it.subject, "description": it.sent}
        if it.tags:
            body["tags"] = it.tags
        if it.kind == "task" and it.story:
            body["user_story"] = target_id(api, it.story, by_name, "us")
        created = api.call("POST", "/" + KINDS[it.kind][0], body)
        it.ref, it.id, it.version = created["ref"], created["id"], created["version"]
        print("created #%d %s  %s" % (it.ref, it.subject, url(it.kind, it.ref)))
    # 2. Fill placeholders that pointed at items created later
    for it in items:
        final = fill(it.body, by_name)
        if final != it.sent:
            current = api.get_item(it.kind, it.id)
            updated = api.call("PATCH", "/%s/%d" % (KINDS[it.kind][0], it.id),
                               {"description": final, "version": current["version"]})
            it.sent, it.version = final, updated["version"]
            print("filled placeholders in #%d" % it.ref)
    # 3. Link stories under their epics
    for it in items:
        if it.kind == "us" and it.epic:
            epic_id = target_id(api, it.epic, by_name, "epic")
            api.call("POST", "/epics/%d/related_userstories" % epic_id,
                     {"epic": epic_id, "user_story": it.id})
            print("linked #%d under %s" % (it.ref, fill(it.epic, by_name)))
    # 4. Comments
    posted_comments = []
    for c in comments:
        kind, item_id = target(api, c.target, by_name)
        text = fill(c.body, by_name)
        current = api.get_item(kind, item_id)
        api.call("PATCH", "/%s/%d" % (KINDS[kind][0], item_id),
                 {"comment": text, "version": current["version"]})
        posted_comments.append((kind, item_id, current["ref"], text))
        print("commented on #%d" % current["ref"])
    # 5. Read back
    ok = readback(api, items, by_name, posted_comments)
    refresh_cache()
    record = {"draft": os.path.abspath(args.draft),
              "items": {it.name: {"type": it.kind, "ref": it.ref, "url": url(it.kind, it.ref)}
                        for it in items},
              "comments": [{"ref": ref} for _, _, ref, _ in posted_comments]}
    with open(args.draft + ".posted.json", "w") as f:
        json.dump(record, f, indent=2)
    print("Refs recorded in %s.posted.json" % args.draft)
    return 0 if ok else 1


def target(api, spec, by_name):
    m = re.fullmatch(r"\[([A-Za-z0-9_-]+)\]", spec)
    if m:
        it = by_name[m.group(1)]
        return it.kind, it.id
    return api.resolve(int(spec.lstrip("#")))


def target_id(api, spec, by_name, want):
    kind, item_id = target(api, spec, by_name)
    if kind != want:
        raise TaigaError("%s is a %s, not a %s" % (spec, kind, want))
    return item_id


def readback(api, items, by_name, comments):
    ok = True
    print("\nRead back:")
    for it in items:
        cur = api.get_item(it.kind, it.id)
        left = unresolved(cur["description"], by_name)
        status = "OK" if not left else "placeholders left: " + ", ".join(left)
        ok = ok and not left
        summary = html_summary(cur.get("description_html") or "")
        ok = ok and "WARNING" not in summary
        print("  #%d %s: %s; %s" % (it.ref, cur["subject"], summary, status))
    me = api.call("GET", "/users/me")["id"]
    for kind, item_id, ref, text in comments:
        hist = api.call("GET", "/history/%s/%d" % (KINDS[kind][1], item_id))
        mine = [h for h in hist if (h.get("comment") or "").strip() == text.strip()
                and h.get("user", {}).get("pk") == me]
        if mine:
            print("  comment on #%d: %s" % (ref, html_summary(mine[0].get("comment_html") or "")))
        else:
            ok = False
            print("  comment on #%d: NOT FOUND in its history" % ref)
    return ok


def cmd_comment(args):
    with open(args.file) as f:
        text = expand_github(f.read().strip())
    api = Api()
    kind, item_id = api.resolve(args.ref)
    found = sorted(set(MENTION_RE.findall(text)))
    if found and not args.allow_mentions:
        print("Not posting: the comment mentions %s (Taiga notifies them); "
              "remove, or pass --allow-mentions" % ", ".join(found))
        return 1
    current = api.get_item(kind, item_id)
    print("Comment on #%d %s (%s), %d chars" % (args.ref, current["subject"], kind, len(text)))
    problems = []
    check_bare_refs(api, "the comment", text, problems)
    if problems:
        print("Not posting:\n" + "\n".join("  - " + p for p in problems))
        return 1
    if not args.post:
        print("Dry run: nothing sent; add --post to send.")
        return 0
    api.call("PATCH", "/%s/%d" % (KINDS[kind][0], item_id),
             {"comment": text, "version": current["version"]})
    ok = readback(api, [], {}, [(kind, item_id, args.ref, text)])
    refresh_cache()
    return 0 if ok else 1


def cmd_edit(args):
    if not args.subject and not args.description_file:
        sys.exit("Nothing to change: give --subject and/or --description-file")
    api = Api()
    kind, item_id = api.resolve(args.ref)
    current = api.get_item(kind, item_id)
    change = {}
    if args.subject and args.subject != current["subject"]:
        change["subject"] = args.subject
    if args.description_file:
        with open(args.description_file) as f:
            desc = expand_github(f.read().strip())
        if desc != (current.get("description") or "").strip():
            change["description"] = desc
    print("Edit #%d %s (%s): %s" % (args.ref, current["subject"], kind,
          ", ".join(change) or "no changes"))
    if "description" in change:
        found = sorted(set(MENTION_RE.findall(change["description"])))
        if found and not args.allow_mentions:
            print("Not posting: the description mentions %s; remove, or pass "
                  "--allow-mentions" % ", ".join(found))
            return 1
        print("  description: %d -> %d chars"
              % (len(current.get("description") or ""), len(change["description"])))
        problems = []
        check_bare_refs(api, "the description", change["description"], problems)
        if problems:
            print("Not posting:\n" + "\n".join("  - " + p for p in problems))
            return 1
    if not change or not args.post:
        if change:
            print("Dry run: nothing sent; add --post to send.")
        return 0
    change["version"] = current["version"]
    api.call("PATCH", "/%s/%d" % (KINDS[kind][0], item_id), change)
    cur = api.get_item(kind, item_id)
    print("Read back: #%d %s: %s" % (cur["ref"], cur["subject"],
                                      html_summary(cur.get("description_html") or "")))
    refresh_cache()
    return 0


TEXT_FIELDS = ("description", "comment", "content", "blocked_note")


def cmd_api(args):
    method = args.method.upper()
    path = args.path if args.path.startswith("/") else "/" + args.path
    body = None
    if args.data:
        text = sys.stdin.read() if args.data == "-" else open(args.data).read()
        try:
            body = json.loads(text)
        except ValueError as e:
            raise TaigaError("--data isn't JSON: %s" % e)
    writes = method not in ("GET", "HEAD", "OPTIONS")
    try:
        api = Api()
    except NeedsLogin:
        if writes:
            raise
        api = Api(anonymous=True)  # reads of a public project work without a login
    if not writes:
        print(json.dumps(api.call(method, path, body), indent=2, ensure_ascii=False))
        return 0
    # The same checks as the other write commands, on fields Taiga renders
    problems = []
    if isinstance(body, dict):
        for key in TEXT_FIELDS:
            if isinstance(body.get(key), str):
                body[key] = expand_github(body[key])
                found = sorted(set(MENTION_RE.findall(body[key])))
                if found and not args.allow_mentions:
                    problems.append("%s mentions %s (Taiga notifies them); remove, or "
                                    "pass --allow-mentions" % (key, ", ".join(found)))
                check_bare_refs(api, key, body[key], problems)
    print("%s %s%s" % (method, API, path))
    if body is not None:
        print(json.dumps(body, indent=2, ensure_ascii=False))
    if problems:
        print("Not sending:\n" + "\n".join("  - " + p for p in problems))
        return 1
    if not args.post:
        print("Dry run: nothing sent; add --post to send.")
        return 0
    result = api.call(method, path, body)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    refresh_cache()
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        prog=PROG, description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("login", help="log in and save a token")
    s.add_argument("username", nargs="?")
    m = s.add_mutually_exclusive_group()
    m.add_argument("--terminal", dest="mode", action="store_const", const="terminal",
                   help="prompt in this terminal (default when there is one)")
    m.add_argument("--browser", dest="mode", action="store_const", const="browser",
                   help="one-time login page on 127.0.0.1 (default without a terminal)")
    m.add_argument("--env", dest="mode", action="store_const", const="env",
                   help="use TAIGA_USERNAME and TAIGA_PASSWORD")
    s.add_argument("--port", type=int, default=0, help="port for --browser (default: any)")
    s.add_argument("--timeout", type=int, default=300, help="seconds to wait for --browser")
    s.add_argument("--no-open", action="store_true", help="don't open the browser")
    sub.add_parser("whoami", help="check the saved token")
    s = sub.add_parser("post", help="create the items and comments in a draft")
    s.add_argument("draft")
    s.add_argument("--post", action="store_true", help="actually send (default: dry run)")
    s.add_argument("--allow-mentions", action="store_true")
    s.add_argument("--allow-duplicate", action="store_true")
    s = sub.add_parser("comment", help="comment on an item")
    s.add_argument("ref", type=lambda v: int(v.lstrip("#")))
    s.add_argument("file")
    s.add_argument("--post", action="store_true")
    s.add_argument("--allow-mentions", action="store_true")
    s = sub.add_parser("edit", help="change an item's subject or description")
    s.add_argument("ref", type=lambda v: int(v.lstrip("#")))
    s.add_argument("--subject")
    s.add_argument("--description-file")
    s.add_argument("--post", action="store_true")
    s.add_argument("--allow-mentions", action="store_true")
    s = sub.add_parser("api", help="any Taiga API call (reads run; writes dry-run until --post)")
    s.add_argument("method", help="GET, POST, PATCH, PUT or DELETE")
    s.add_argument("path", help="API path, e.g. /epics?project=1 or /userstories/163")
    s.add_argument("--data", help="JSON request body: a file, or - for stdin")
    s.add_argument("--post", action="store_true", help="actually send a write")
    s.add_argument("--allow-mentions", action="store_true")
    args = p.parse_args(argv)
    try:
        return {"login": cmd_login, "whoami": cmd_whoami, "post": cmd_post,
                "comment": cmd_comment, "edit": cmd_edit, "api": cmd_api}[args.cmd](args) or 0
    except NeedsLogin as e:
        print("Login needed: %s. Run `%s login`." % (e, PROG),
              file=sys.stderr)
        return EXIT_NEEDS_LOGIN
    except TaigaError as e:
        sys.exit(str(e))


