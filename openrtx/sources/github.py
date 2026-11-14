"""GitHub source: issues, PRs, comments and reviews across the OpenRTX org.

Caches every repository of the organisation (config `github.org`, default
OpenRTX; forks only with `github.include_forks = true`): issues and PRs,
issue comments, PR review comments and PR reviews. Syncs are incremental:
each repository remembers how far it got, so a sync that stops on the rate
limit resumes where it left off. Read-only towards GitHub.

Auth, in order: GITHUB_TOKEN, GH_TOKEN, `gh auth token`, else anonymous
(60 requests an hour, not enough for a first sync of the whole org).
GITHUB_API overrides the API base (tests).

Refs: OWNER/REPO#N, or a github.com issue or PR URL.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import __version__, paths
from . import (Hit, SourceError, connect, connect_ro, fts_query, iso, last_ok_sync,
               log, now_utc, parse_iso, record_sync)

NAME = "github"
PERIOD = dt.timedelta(hours=1)
DESCRIPTION = "GitHub issues, PRs, comments and reviews in every repo of the OpenRTX org"

OVERLAP = dt.timedelta(minutes=5)  # re-read a little before the last sync
USER_AGENT = "openrtx-dev-ai/%s (+https://github.com/turnrye/openrtx-dev-ai)" % __version__

REF_RE = re.compile(r"([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9._-]+)#(\d+)")
URL_RE = re.compile(
    r"https?://(?:www\.)?github\.com/([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9._-]+)/"
    r"(?:issues|pull)/(\d+)(?:[/?#].*)?")

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    full_name TEXT PRIMARY KEY, archived INTEGER, fork INTEGER, has_issues INTEGER,
    description TEXT, pushed_at TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS items (
    repo TEXT NOT NULL, number INTEGER NOT NULL, is_pr INTEGER NOT NULL,
    state TEXT, title TEXT, body TEXT, author TEXT, labels TEXT,
    created_at TEXT, updated_at TEXT, closed_at TEXT, merged_at TEXT, url TEXT,
    data TEXT, PRIMARY KEY (repo, number));
CREATE TABLE IF NOT EXISTS versions (
    repo TEXT NOT NULL, number INTEGER NOT NULL, updated_at TEXT NOT NULL,
    observed_at TEXT NOT NULL, data TEXT, PRIMARY KEY (repo, number, updated_at));
CREATE TABLE IF NOT EXISTS comments (
    kind TEXT NOT NULL, id INTEGER NOT NULL, repo TEXT NOT NULL, number INTEGER NOT NULL,
    author TEXT, body TEXT, state TEXT, path TEXT, line INTEGER,
    created_at TEXT, updated_at TEXT, url TEXT, data TEXT, PRIMARY KEY (kind, id));
CREATE INDEX IF NOT EXISTS comments_item ON comments (repo, number);
CREATE TABLE IF NOT EXISTS repo_state (
    repo TEXT PRIMARY KEY, synced_through TEXT);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
    repo UNINDEXED, number UNINDEXED, src UNINDEXED, at UNINDEXED, url UNINDEXED,
    title, body);
"""


class RateLimited(Exception):
    def __init__(self, reset):
        super().__init__("rate limited until %s" % iso(reset))
        self.reset = reset


# --------------------------------------------------------------------------
# Refs
# --------------------------------------------------------------------------

def parse_ref(ref):
    """OWNER/REPO#N or a github.com issue/PR URL -> ("OWNER/REPO", N), else None."""
    ref = (ref or "").strip()
    m = REF_RE.fullmatch(ref) or URL_RE.fullmatch(ref)
    if not m:
        return None
    return "%s/%s" % (m.group(1), m.group(2)), int(m.group(3))


def owns(ref):
    return parse_ref(ref) is not None


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def api_base():
    return os.environ.get("GITHUB_API", "https://api.github.com").rstrip("/")


def rate_floor():
    return int(os.environ.get("GITHUB_RATE_FLOOR", "50"))


def find_token():
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    gh = shutil.which("gh")
    if gh:
        try:
            out = subprocess.run([gh, "auth", "token"], capture_output=True, text=True,
                                 timeout=15)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return None


class Client:
    """GET-only GitHub REST client that stops before the rate limit runs out."""

    def __init__(self, token):
        self.token = token
        self.requests = 0
        self.remaining = None
        self.reset = now_utc() + dt.timedelta(hours=1)

    def get(self, path, params=None):
        """One page: (json, next_url). path may be a full URL (from Link)."""
        url = path if path.startswith("http") else api_base() + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        headers = {"Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": USER_AGENT}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if self.remaining is not None and self.remaining < rate_floor():
            raise RateLimited(self.reset)
        for attempt in range(3):
            self.requests += 1
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                            timeout=60) as resp:
                    self._track(resp.headers)
                    data = json.loads(resp.read() or b"null")
                    return data, self._next(resp.headers.get("Link"))
            except urllib.error.HTTPError as e:
                self._track(e.headers)
                if e.code in (403, 429) and (self.remaining == 0 or e.headers.get("Retry-After")):
                    wait = e.headers.get("Retry-After")
                    raise RateLimited(now_utc() + dt.timedelta(seconds=int(wait))
                                      if wait else self.reset)
                if e.code in (404, 410):
                    return None, None  # gone, private, or issues disabled
                if e.code == 401:
                    raise SourceError("GitHub rejected the token (HTTP 401)")
                if e.code >= 500 and attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise SourceError("GET %s: HTTP %d" % (url.split("?")[0], e.code))
            except urllib.error.URLError as e:
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise SourceError("can't reach %s: %s" % (api_base(), e.reason))
        raise SourceError("GET %s failed" % url)

    def pages(self, path, params=None):
        """Every item of a paginated list endpoint."""
        params = dict(params or {}, per_page=100)
        data, nxt = self.get(path, params)
        while True:
            for row in data or []:
                yield row
            if not nxt:
                return
            data, nxt = self.get(nxt)

    def _track(self, headers):
        if headers is None:
            return
        rem, reset = headers.get("X-RateLimit-Remaining"), headers.get("X-RateLimit-Reset")
        if rem is not None:
            self.remaining = int(rem)
        if reset is not None:
            self.reset = dt.datetime.fromtimestamp(int(reset), dt.timezone.utc)

    @staticmethod
    def _next(link):
        for part in (link or "").split(","):
            m = re.match(r'\s*<([^>]+)>;\s*rel="next"', part)
            if m:
                return m.group(1)
        return None


# --------------------------------------------------------------------------
# Storing
# --------------------------------------------------------------------------

def _init(db):
    db.executescript(SCHEMA)


def _meta(db, key, value=None):
    if value is None:
        row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None
    db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))


def _login(user):
    return (user or {}).get("login") or ""


def _store_item(db, repo, d, observed):
    """An issue or PR as returned by /issues or /pulls. Returns 'new', 'updated' or ''."""
    is_pr = 1 if ("pull_request" in d or "merged_at" in d) else 0
    merged = d.get("merged_at") or (d.get("pull_request") or {}).get("merged_at")
    row = (repo, d["number"], is_pr, d.get("state"), d.get("title") or "", d.get("body") or "",
           _login(d.get("user")), ", ".join(l.get("name", "") for l in d.get("labels") or []),
           d.get("created_at"), d.get("updated_at"), d.get("closed_at"), merged,
           d.get("html_url"), json.dumps(d, sort_keys=True))
    old = db.execute("SELECT updated_at FROM items WHERE repo = ? AND number = ?",
                     (repo, d["number"])).fetchone()
    if old and old[0] == d.get("updated_at"):
        return ""
    db.execute("INSERT OR REPLACE INTO items VALUES (%s)" % ",".join("?" * 14), row)
    db.execute("INSERT OR IGNORE INTO versions VALUES (?, ?, ?, ?, ?)",
               (repo, d["number"], d.get("updated_at") or observed, observed, row[-1]))
    return "updated" if old else "new"


def _store_comment(db, kind, repo, number, d):
    row = (kind, d["id"], repo, number, _login(d.get("user")), d.get("body") or "",
           d.get("state"), d.get("path"), d.get("line") or d.get("original_line"),
           d.get("created_at") or d.get("submitted_at"),
           d.get("updated_at") or d.get("submitted_at"), d.get("html_url"),
           json.dumps(d, sort_keys=True))
    old = db.execute("SELECT updated_at FROM comments WHERE kind = ? AND id = ?",
                     (kind, d["id"])).fetchone()
    if old and old[0] == row[10]:
        return ""
    db.execute("INSERT OR REPLACE INTO comments VALUES (%s)" % ",".join("?" * 13), row)
    return "updated" if old else "new"


def _number_from(url, part):
    m = re.search(r"/%s/(\d+)$" % part, url or "")
    return int(m.group(1)) if m else None


def _rebuild_search(db):
    db.execute("DELETE FROM search")
    db.execute("INSERT INTO search SELECT repo, number, CASE is_pr WHEN 1 THEN 'pr' "
               "ELSE 'issue' END, updated_at, url, title, body FROM items")
    db.execute("INSERT INTO search SELECT c.repo, c.number, c.kind, c.created_at, c.url, "
               "i.title, c.body FROM comments c JOIN items i USING (repo, number) "
               "WHERE c.body <> ''")


# --------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------

def _org():
    return paths.load_config().get("github.org", "OpenRTX")


def _include_forks():
    return paths.load_config().get("github.include_forks", "").lower() in ("1", "true", "yes")


def _sync_repo(db, client, repo, has_issues, since, counts):
    """Fetch one repository's changes since `since` (None = everything)."""
    observed = iso(now_utc())
    params = {"since": iso(since)} if since else {}
    prs = set()
    if has_issues:
        listing = client.pages("/repos/%s/issues" % repo,
                               dict(params, state="all", sort="updated", direction="asc"))
    else:  # no issues endpoint: walk PRs newest first and stop at `since`
        listing = client.pages("/repos/%s/pulls" % repo,
                               {"state": "all", "sort": "updated", "direction": "desc"})
    for d in listing:
        if not has_issues and since and parse_iso(d["updated_at"]) < since:
            break  # the rest are older; stops fetching further pages
        result = _store_item(db, repo, d, observed)
        if result:
            counts["items_" + result] += 1
        if "pull_request" in d or "merged_at" in d:
            prs.add(d["number"])
    for d in client.pages("/repos/%s/issues/comments" % repo,
                          dict(params, sort="updated", direction="asc")):
        number = _number_from(d.get("issue_url"), "issues")
        if number is not None and _store_comment(db, "comment", repo, number, d):
            counts["comments"] += 1
    for d in client.pages("/repos/%s/pulls/comments" % repo,
                          dict(params, sort="updated", direction="asc")):
        number = _number_from(d.get("pull_request_url"), "pulls")
        if number is not None and _store_comment(db, "review_comment", repo, number, d):
            counts["comments"] += 1
    for number in sorted(prs):
        for d in client.pages("/repos/%s/pulls/%d/reviews" % (repo, number)):
            if (d.get("body") or d.get("state") not in (None, "COMMENTED")) and \
                    _store_comment(db, "review", repo, number, d):
                counts["comments"] += 1


def sync(db_path, full=False):
    db = connect(db_path)
    _init(db)
    resume = _meta(db, "rate_limited_until")
    if resume and not full and parse_iso(resume) > now_utc():
        return "rate-limited, resuming after %s" % resume
    token = find_token()
    if not token:
        log("  github: no token (GITHUB_TOKEN, GH_TOKEN or `gh auth login`); anonymous "
            "access allows 60 requests an hour, too few for a first sync of the org")
    client = Client(token)
    counts = {"items_new": 0, "items_updated": 0, "comments": 0}
    started = now_utc()
    org = _org()
    try:
        repos = list(client.pages("/orgs/%s/repos" % org, {"type": "all"}))
        if not repos:
            raise SourceError("no repositories found for org %r" % org)
        db.execute("BEGIN")
        for r in repos:
            db.execute("INSERT OR REPLACE INTO repos VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (r["full_name"], int(r.get("archived", False)), int(r.get("fork", False)),
                        int(r.get("has_issues", True)), r.get("description"),
                        r.get("pushed_at"), json.dumps(r, sort_keys=True)))
        db.execute("COMMIT")
        done = 0
        for r in repos:
            if r.get("fork") and not _include_forks():
                continue
            name = r["full_name"]
            state = db.execute("SELECT synced_through FROM repo_state WHERE repo = ?",
                               (name,)).fetchone()
            since = None if full or not state or not state[0] else parse_iso(state[0]) - OVERLAP
            before = dict(counts)
            repo_started = now_utc()
            db.execute("BEGIN")
            try:
                _sync_repo(db, client, name, r.get("has_issues", True), since, counts)
            except BaseException:
                db.execute("COMMIT")  # keep what was fetched; repo_state is not advanced
                raise
            db.execute("INSERT OR REPLACE INTO repo_state VALUES (?, ?)",
                       (name, iso(repo_started)))
            db.execute("COMMIT")
            done += 1
            if counts != before and (full or since is None):
                log("  github: %s (%d items, %d comments so far)"
                    % (name, counts["items_new"] + counts["items_updated"], counts["comments"]))
    except RateLimited as e:
        db.execute("BEGIN")
        _rebuild_search(db)
        _meta(db, "rate_limited_until", iso(e.reset))
        summary = ("partial, rate-limited, resume at %s: %d new and %d updated issues/PRs, "
                   "%d comments, %d requests" % (iso(e.reset), counts["items_new"],
                                                 counts["items_updated"], counts["comments"],
                                                 client.requests))
        record_sync(db, False, summary)
        db.execute("COMMIT")
        return summary
    except Exception as e:
        record_sync(db, False, str(e))
        raise
    db.execute("BEGIN")
    _rebuild_search(db)
    db.execute("DELETE FROM meta WHERE key = 'rate_limited_until'")
    summary = ("%d repos: %d new and %d updated issues/PRs, %d comments, %d requests, %.0fs"
               % (done, counts["items_new"], counts["items_updated"], counts["comments"],
                  client.requests, (now_utc() - started).total_seconds()))
    record_sync(db, True, summary)
    db.execute("COMMIT")
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return summary


def last_sync(db_path):
    return last_ok_sync(db_path)


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

def _open(db_path):
    if not os.path.exists(db_path):
        return None
    return connect_ro(db_path)


def search(db_path, query, limit=20):
    db = _open(db_path)
    if db is None:
        return []
    sql = ("SELECT repo, number, src, at, url, title, "
           "snippet(search, 6, '[', ']', '…', 14) AS snip "
           "FROM search WHERE search MATCH ? ORDER BY rank LIMIT ?")
    try:
        rows = db.execute(sql, (query, limit * 5)).fetchall()
    except Exception:
        rows = db.execute(sql, (fts_query(query), limit * 5)).fetchall()
    hits, seen = [], set()
    for r in rows:
        key = (r["repo"], r["number"])
        if key in seen:
            continue
        seen.add(key)
        item = db.execute("SELECT is_pr, url, updated_at FROM items WHERE repo = ? AND "
                          "number = ?", key).fetchone()
        kind = "pr" if item and item["is_pr"] else "issue"
        if r["src"] not in ("pr", "issue"):
            kind += " " + r["src"].replace("_", " ")
        hits.append(Hit(NAME, "%s#%d" % key, r["title"], kind=kind,
                        when=(r["at"] or "")[:10], url=item["url"] if item else r["url"],
                        snippet=" ".join((r["snip"] or "").split())))
        if len(hits) >= limit:
            break
    return hits


def _date(text):
    return (text or "")[:10]


def recent(db_path, since, limit=30):
    """Issues and PRs updated since `since`, with what happened to them."""
    db = _open(db_path)
    if db is None:
        return []
    rows = db.execute(
        "SELECT i.repo, i.number, i.is_pr, i.state, i.title, i.created_at, i.updated_at, "
        "i.closed_at, i.merged_at, i.url, "
        "(SELECT count(*) FROM comments c WHERE c.repo = i.repo AND c.number = i.number "
        " AND c.created_at >= ?) AS n, "
        "(SELECT group_concat(DISTINCT c.author) FROM comments c WHERE c.repo = i.repo "
        " AND c.number = i.number AND c.created_at >= ?) AS who "
        "FROM items i WHERE i.updated_at >= ? ORDER BY i.updated_at DESC LIMIT ?",
        (since, since, since, limit)).fetchall()
    hits = []
    for r in rows:
        events = []
        if (r["created_at"] or "") >= since:
            events.append("opened")
        if r["merged_at"] and r["merged_at"] >= since:
            events.append("merged")
        elif r["closed_at"] and r["closed_at"] >= since:
            events.append("closed")
        if r["n"]:
            events.append("%d new comment%s/review%s by %s" % (
                r["n"], "" if r["n"] == 1 else "s", "" if r["n"] == 1 else "s",
                (r["who"] or "").replace(",", ", ")))
        state = "merged" if r["merged_at"] else r["state"]
        hits.append(Hit(NAME, "%s#%d" % (r["repo"], r["number"]), r["title"] or "",
                        kind="pr" if r["is_pr"] else "issue", when=_date(r["updated_at"]),
                        url=r["url"] or "",
                        snippet="; ".join([state] + events) if state else "; ".join(events)))
    return hits


def show(db_path, ref):
    parsed = parse_ref(ref)
    db = _open(db_path)
    if not parsed or db is None:
        return None
    repo, number = parsed
    it = db.execute("SELECT * FROM items WHERE repo = ? COLLATE NOCASE AND number = ?",
                    (repo, number)).fetchone()
    if it is None:
        return None
    kind = "PR" if it["is_pr"] else "Issue"
    state = "merged" if it["merged_at"] else it["state"]
    out = ["%s#%d  %s" % (it["repo"], it["number"], it["title"]),
           "%s, %s, by %s; opened %s, updated %s%s"
           % (kind, state, it["author"], _date(it["created_at"]), _date(it["updated_at"]),
              ", closed %s" % _date(it["closed_at"]) if it["closed_at"] else ""),
           ]
    if it["labels"]:
        out.append("labels: %s" % it["labels"])
    out += [it["url"] or "", "", (it["body"] or "(no description)").strip(), ""]
    rows = db.execute("SELECT * FROM comments WHERE repo = ? AND number = ? "
                      "ORDER BY created_at, id", (it["repo"], number)).fetchall()
    labels = {"comment": "comment", "review_comment": "review comment", "review": "review"}
    for c in rows:
        head = "--- %s by %s, %s" % (labels.get(c["kind"], c["kind"]), c["author"],
                                    _date(c["created_at"]))
        if c["kind"] == "review_comment" and c["path"]:
            head += ", %s%s" % (c["path"], ":%d" % c["line"] if c["line"] else "")
        if c["kind"] == "review" and c["state"]:
            head += " (%s)" % c["state"].lower().replace("_", " ")
        out += [head, (c["body"] or "").strip(), ""]
    return "\n".join(out).rstrip() + "\n"
