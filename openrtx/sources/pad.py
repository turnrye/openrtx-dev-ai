"""Community pads (Etherpad), such as the OpenRTX Office Hours notes.

Pads come from the config file, one per line: `pad.<name> = <pad URL>`, and
`pad.<name> = off` turns one off. The Office Hours notes are always included
unless turned off:

    pad.officehours = https://pad.opendoor.fr/p/ajerfmoijc9wf6

A sync makes one request per pad: the Markdown export, conditional on the
ETag from last time (304 when nothing changed). When a pad changed it fetches
the full history export (`.etherpad`) once, keeps it gzipped, and replays the
changesets locally for revision times, edit sessions and the text at any
revision. Only export endpoints are used: never the editor or its websocket,
and never a revision past the known head.

The current Markdown is split for search into one row per `##` meeting
section and one per top-level topic bullet in it, with the meeting date, the
attendees and the heading path; GitHub and Taiga references are pulled out
into `links`. Anyone with a pad's link can edit it, so a change that removes
more than half the text is not indexed: the last good copy stays searchable
and the pad is flagged until the text recovers.

Refs: `pad:<name>` (current text) and `pad:<name>@<rev>` (text at a revision).
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import __version__, paths
from . import (Hit, SourceError, connect, connect_ro, fts_query, iso, last_ok_sync,
               log, now_utc, record_sync)

NAME = "pad"
PERIOD = dt.timedelta(hours=24)
DESCRIPTION = "Community pads (the Office Hours meeting notes on pad.opendoor.fr)"

DEFAULT_PADS = {"officehours": "https://pad.opendoor.fr/p/ajerfmoijc9wf6"}
USER_AGENT = "openrtx-dev-ai/%s (+https://github.com/turnrye/openrtx-dev-ai)" % __version__
SESSION_GAP = dt.timedelta(minutes=30)
SHRINK_LIMIT = 0.5          # a change keeping less than this share of the text is suspect
MAX_RATE_WAIT = 90          # seconds we'll wait for a rate-limit window to reset

SCHEMA = """
CREATE TABLE IF NOT EXISTS pads (
    name TEXT PRIMARY KEY, url TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new',
    etag TEXT, head_rev INTEGER, head_at TEXT, last_checked TEXT, last_changed TEXT,
    markdown TEXT, good_markdown TEXT, indexed_rev INTEGER, flag TEXT,
    retry_after TEXT, history_gz BLOB);
CREATE TABLE IF NOT EXISTS sessions (
    pad TEXT NOT NULL, rev_start INTEGER NOT NULL, rev_end INTEGER NOT NULL,
    ts_start TEXT NOT NULL, ts_end TEXT NOT NULL, authors TEXT NOT NULL,
    text TEXT NOT NULL, PRIMARY KEY (pad, rev_start));
CREATE TABLE IF NOT EXISTS authors (
    pad TEXT NOT NULL, id TEXT NOT NULL, name TEXT, PRIMARY KEY (pad, id));
CREATE TABLE IF NOT EXISTS links (
    pad TEXT NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL, url TEXT,
    meeting_date TEXT, heading TEXT, context TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS sections USING fts5(
    heading, attendees, body, pad UNINDEXED, kind UNINDEXED,
    meeting_date UNINDEXED, topic UNINDEXED, seq UNINDEXED);
"""

DOCS = """pads: one row per configured pad (url, status ok/gone/private, etag,
head_rev and head_at of the newest revision, good_markdown = the copy that is
indexed, indexed_rev, flag when a change was not indexed).
sessions: edit sessions (30 min idle gap) with revision range, times, author
ids and the full text at the end of the session.
authors: Etherpad author ids and their self-chosen names (unverified).
links: GitHub (`OpenRTX/OpenRTX#N`; bare #N in pads means a GitHub PR/issue)
and Taiga (`taiga#N`) references found in the text.
sections (FTS5): kind meeting/topic/intro rows with heading, attendees, body,
meeting_date and topic."""


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def configured_pads():
    """{name: base URL} from config, with the default pads unless turned off."""
    pads = dict(DEFAULT_PADS)
    for key, value in paths.load_config().items():
        m = re.fullmatch(r"pad\.([A-Za-z0-9_-]+)", key)
        if m:
            pads[m.group(1)] = value
    return {name: normalize_url(url) for name, url in pads.items()
            if url.strip().lower() not in ("off", "none", "")}


def normalize_url(url):
    url = url.strip().split("#", 1)[0].rstrip("/")
    for suffix in ("/timeslider", "/export/markdown", "/export/txt"):
        if url.endswith(suffix):
            url = url[:-len(suffix)]
    return url


# --------------------------------------------------------------------------
# Etherpad changesets
# --------------------------------------------------------------------------

_CS_HEADER = re.compile(r"Z:([0-9a-z]+)([><])([0-9a-z]+)")
_CS_OP = re.compile(r"((?:\*[0-9a-z]+)*)(?:\|([0-9a-z]+))?([-+=])([0-9a-z]+)")


def apply_changeset(text, changeset):
    """Apply one changeset to `text` (UTF-16-LE bytes, as Etherpad counts
    lengths in UTF-16 code units) and return the new bytes."""
    m = _CS_HEADER.match(changeset)
    if not m:
        raise SourceError("not an Etherpad changeset: %r" % changeset[:40])
    if int(m.group(1), 36) * 2 != len(text):
        raise SourceError("changeset expects %d chars, text has %d"
                          % (int(m.group(1), 36), len(text) // 2))
    ops, _, bank = changeset[m.end():].partition("$")
    bank = bank.encode("utf-16-le", "surrogatepass")
    out, pos, bpos = [], 0, 0
    for _attribs, _lines, op, count in _CS_OP.findall(ops):
        n = int(count, 36) * 2
        if op == "=":
            out.append(text[pos:pos + n])
            pos += n
        elif op == "-":
            pos += n
        else:
            out.append(bank[bpos:bpos + n])
            bpos += n
    out.append(text[pos:])
    return b"".join(out)


def utf16(text):
    return text.encode("utf-16-le", "surrogatepass")


def from_utf16(data):
    return data.decode("utf-16-le", "surrogatepass")


class History:
    """A parsed `.etherpad` export: revisions, snapshots and authors."""

    def __init__(self, data):
        pad_keys = [k for k in data if k.startswith("pad:") and k.count(":") == 1]
        if len(pad_keys) != 1:
            raise SourceError("expected one pad in the .etherpad export, found %d"
                              % len(pad_keys))
        self.key = pad_keys[0]
        self.head = data[self.key]["head"]
        self.revs = []
        for r in range(self.head + 1):
            rev = data.get("%s:revs:%d" % (self.key, r))
            if rev is None:
                raise SourceError("revision %d missing from the history export" % r)
            self.revs.append(rev)
        self.authors = {k.split(":", 1)[1]: (v or {}).get("name")
                        for k, v in data.items() if k.startswith("globalAuthor:")}

    def meta(self, rev):
        return self.revs[rev].get("meta") or {}

    def time(self, rev):
        ms = self.meta(rev).get("timestamp")
        return None if ms is None else dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc)

    def text_at(self, rev):
        """Text at `rev`, replaying from the nearest snapshot at or below it."""
        if not 0 <= rev <= self.head:
            raise SourceError("no revision %d (head is %d)" % (rev, self.head))
        text, base = utf16("\n"), -1   # every pad starts as a single newline
        for r in range(rev, -1, -1):
            atext = self.meta(r).get("atext")
            if atext:
                text, base = utf16(atext["text"]), r
                break
        for r in range(base + 1, rev + 1):
            text = apply_changeset(text, self.revs[r]["changeset"])
        return from_utf16(text)

    def sessions(self):
        """[(rev_start, rev_end, t_start, t_end, author ids, text at rev_end)]"""
        out, text, cur = [], utf16("\n"), None
        for r in range(self.head + 1):
            text = apply_changeset(text, self.revs[r]["changeset"])
            t = self.time(r) or (cur and cur[3]) or dt.datetime.fromtimestamp(0, dt.timezone.utc)
            author = self.meta(r).get("author") or ""
            if cur is None or t - cur[3] > SESSION_GAP:
                if cur is not None:
                    out.append(cur)
                cur = [r, r, t, t, set(), None]
            cur[1], cur[3] = r, t
            if author:
                cur[4].add(author)
            cur[5] = text
        if cur is not None:
            out.append(cur)
        return [(s, e, ts, te, sorted(a), from_utf16(tx)) for s, e, ts, te, a, tx in out]


# --------------------------------------------------------------------------
# Splitting the Markdown for search
# --------------------------------------------------------------------------

_HELD_ON = re.compile(r"Held on\s+(\d{4}-\d{2}-\d{2})")
_MONTH_YEAR = re.compile(r"^(January|February|March|April|May|June|July|August|"
                         r"September|October|November|December)\s+(\d{4})$", re.I)
_ATTENDEES = re.compile(r"^\s*(?:In attendance were|Attendees|Attendance|Present)\s*:?\s*(.+)$",
                        re.I | re.M)
_BULLET = re.compile(r"^(\s*)[*+-]\s+(.*)$")
_GITHUB_URL = re.compile(r"https?://github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9._-]+)/"
                         r"(?:issues|pull)/(\d+)")
_TAIGA_URL = re.compile(r"https?://tasks\.openrtx\.org/project/openrtx/"
                        r"(?:epic|us|task|issue)/(\d+)")
_OWNER_REPO_REF = re.compile(r"(?<![\w/])([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9._-]+)#(\d+)\b")
_BARE_REF = re.compile(r"(?<![\w/&#])#(\d+)\b")


def pad_title(markdown, name):
    for line in markdown.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            return re.sub(r"\s+for OpenRTX$", "", title) or name
    return name


def meeting_date(heading, body):
    m = _HELD_ON.search(body)
    if m:
        return m.group(1)
    m = _MONTH_YEAR.match(heading.strip())
    if m:
        month = dt.datetime.strptime(m.group(1)[:3].title(), "%b").month
        return "%s-%02d" % (m.group(2), month)
    return ""


def split_sections(markdown):
    """Rows {kind, heading, topic, attendees, body, meeting_date} for search."""
    rows, title = [], ""
    parts = re.split(r"(?m)^##\s+(?!#)(.*)$", markdown)
    intro = parts[0]
    for line in intro.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
    if intro.strip():
        rows.append(dict(kind="intro", heading=title, topic="", attendees="",
                         body=intro.strip(), meeting_date=""))
    for heading, body in zip(parts[1::2], parts[2::2]):
        heading = heading.strip()
        if heading.lower() == "template":
            continue
        date = meeting_date(heading, body)
        m = _ATTENDEES.search(body)
        attendees = m.group(1).strip() if m else ""
        rows.append(dict(kind="meeting", heading=heading, topic="", attendees=attendees,
                         body=body.strip(), meeting_date=date))
        for topic, text, sub in split_topics(body):
            rows.append(dict(kind="topic", heading="%s › %s%s" % (heading, sub + " › " if sub else "", topic),
                             topic=topic, attendees=attendees, body=text, meeting_date=date))
    return rows


def split_topics(body):
    """[(topic, text, subheading)]: one per top-level bullet in a section."""
    lines = body.splitlines()
    indents = [len(m.group(1).expandtabs(4)) for m in map(_BULLET.match, lines) if m]
    if not indents:
        return []
    top = min(indents)
    topics, cur, sub = [], None, ""
    for line in lines:
        if line.startswith("#"):
            if cur:
                topics.append(cur)
                cur = None
            sub = line.lstrip("#").strip().rstrip(":")
            continue
        m = _BULLET.match(line)
        if m and len(m.group(1).expandtabs(4)) == top:
            if cur:
                topics.append(cur)
            cur = [clean_inline(m.group(2)), [line], sub]
        elif cur is not None:
            cur[1].append(line)
    if cur:
        topics.append(cur)
    return [(t, "\n".join(ls).strip(), s) for t, ls, s in topics if t]


def clean_inline(text):
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return re.sub(r"[*_`]", "", text).strip()


def extract_links(rows):
    """[(kind, ref, url, meeting_date, heading, context)] from meeting rows."""
    found = []
    for row in rows:
        if row["kind"] not in ("meeting", "intro"):
            continue
        for line in row["body"].splitlines():
            seen = set()
            for m in _GITHUB_URL.finditer(line):
                ref = "%s/%s#%s" % m.groups()
                seen.add(ref)
                found.append(("github", ref, m.group(0), row["meeting_date"], row["heading"], line.strip()))
            for m in _OWNER_REPO_REF.finditer(line):
                ref = "%s/%s#%s" % m.groups()
                if ref not in seen:
                    seen.add(ref)
                    found.append(("github", ref, "https://github.com/%s/%s/issues/%s" % m.groups(),
                                  row["meeting_date"], row["heading"], line.strip()))
            for m in _TAIGA_URL.finditer(line):
                found.append(("taiga", "taiga#%s" % m.group(1), m.group(0),
                              row["meeting_date"], row["heading"], line.strip()))
            plain = _TAIGA_URL.sub("", _GITHUB_URL.sub("", line))
            for m in _BARE_REF.finditer(plain):
                ref = "OpenRTX/OpenRTX#%s" % m.group(1)
                if ref not in seen:
                    seen.add(ref)
                    found.append(("github", ref, "https://github.com/OpenRTX/OpenRTX/issues/%s" % m.group(1),
                                  row["meeting_date"], row["heading"], line.strip()))
    return found


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

class Response:
    def __init__(self, status, body=b"", headers=None, url=""):
        self.status, self.body, self.headers, self.url = status, body, headers or {}, url


def fetch(url, etag=None):
    headers = {"User-Agent": USER_AGENT}
    if etag:
        headers["If-None-Match"] = etag
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return Response(resp.status, resp.read(), dict(resp.headers.items()), resp.geturl())
    except urllib.error.HTTPError as e:
        return Response(e.code, e.read() or b"", dict(e.headers.items()) if e.headers else {}, url)


def fetch_polite(url, etag=None, sleep=time.sleep):
    """GET, waiting once for the rate-limit window if the server asks us to."""
    resp = fetch(url, etag)
    if resp.status == 429:
        wait = rate_limit_wait(resp)
        if wait is not None and wait <= MAX_RATE_WAIT:
            log("  rate limited; waiting %ds" % wait)
            sleep(wait)
            resp = fetch(url, etag)
    return resp


def rate_limit_wait(resp):
    h = {k.lower(): v for k, v in resp.headers.items()}
    try:
        if "retry-after" in h:
            return max(0, int(h["retry-after"]))
        if "x-ratelimit-reset" in h:
            return max(0, int(float(h["x-ratelimit-reset"]) - time.time()) + 1)
    except ValueError:
        pass
    return None


def looks_like_login(resp, url):
    if resp.url and urllib.parse.urlparse(resp.url).path != urllib.parse.urlparse(url).path:
        return True
    ctype = {k.lower(): v for k, v in resp.headers.items()}.get("content-type", "")
    return "text/html" in ctype and b"<form" in resp.body[:20000].lower() and b"password" in resp.body.lower()


# --------------------------------------------------------------------------
# Source contract
# --------------------------------------------------------------------------

def open_db(db_path):
    db = connect(db_path)
    db.executescript(SCHEMA)
    db.execute("CREATE TABLE IF NOT EXISTS schema_docs (name TEXT PRIMARY KEY, doc TEXT)")
    db.execute("INSERT OR REPLACE INTO schema_docs VALUES ('pad', ?)", (DOCS,))
    return db


def sync(db_path, full=False, sleep=time.sleep):
    db = open_db(db_path)
    pads = configured_pads()
    notes, failures = [], []
    for name, url in sorted(pads.items()):
        try:
            notes.append(sync_pad(db, name, url, full, sleep))
        except SourceError as e:
            failures.append("%s: %s" % (name, e))
    summary = "; ".join(notes + failures) or "no pads configured"
    record_sync(db, not failures, summary)
    if failures:
        raise SourceError(summary)
    return summary


def sync_pad(db, name, url, full, sleep):
    row = db.execute("SELECT url, etag, head_rev, good_markdown, retry_after FROM pads "
                     "WHERE name = ?", (name,)).fetchone()
    if row is None or row[0] != url:  # new pad, or its URL changed in config: start over
        db.execute("INSERT OR REPLACE INTO pads (name, url, status) VALUES (?, ?, 'new')", (name, url))
        db.execute("DELETE FROM sections WHERE pad = ?", (name,))
        row = (url, None, None, None, None)
    _url, etag, head_rev, good_markdown, retry_after = row
    now = now_utc()
    if retry_after and now < dt.datetime.fromisoformat(retry_after.replace("Z", "+00:00")):
        raise SourceError("rate limited until %s" % retry_after)

    resp = fetch_polite(url + "/export/markdown", None if full else etag, sleep)
    db.execute("UPDATE pads SET last_checked = ?, retry_after = NULL WHERE name = ?", (iso(now), name))
    if resp.status == 304:
        db.execute("UPDATE pads SET status = 'ok' WHERE name = ?", (name,))
        return "%s: unchanged (rev %s)" % (name, head_rev)
    if resp.status == 404:
        db.execute("UPDATE pads SET status = 'gone' WHERE name = ?", (name,))
        return "%s: gone (404), serving the cached copy" % name
    if resp.status in (401, 403) or (resp.status == 200 and looks_like_login(resp, url + "/export/markdown")):
        db.execute("UPDATE pads SET status = 'private' WHERE name = ?", (name,))
        return "%s: private (%s), serving the cached copy" % (name, resp.status)
    if resp.status == 429:
        wait = rate_limit_wait(resp) or 90
        until = iso(now + dt.timedelta(seconds=wait))
        db.execute("UPDATE pads SET retry_after = ? WHERE name = ?", (until, name))
        raise SourceError("rate limited until %s" % until)
    if resp.status != 200:
        raise SourceError("GET %s/export/markdown: HTTP %d" % (url, resp.status))

    markdown = resp.body.decode("utf-8", "replace")
    new_etag = {k.lower(): v for k, v in resp.headers.items()}.get("etag")

    hist_resp = fetch_polite(url + "/export/etherpad", None, sleep)
    if hist_resp.status != 200:
        raise SourceError("GET %s/export/etherpad: HTTP %d" % (url, hist_resp.status))
    try:
        history = History(json.loads(hist_resp.body.decode("utf-8")))
        sessions = history.sessions()
    except (ValueError, KeyError) as e:
        raise SourceError("unreadable history export: %s" % e)

    head_at = history.time(history.head)
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("UPDATE pads SET status = 'ok', etag = ?, head_rev = ?, head_at = ?, "
                   "last_changed = ?, markdown = ?, history_gz = ? WHERE name = ?",
                   (new_etag, history.head, head_at and iso(head_at), iso(now), markdown,
                    gzip.compress(hist_resp.body), name))
        db.execute("DELETE FROM sessions WHERE pad = ?", (name,))
        db.executemany("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
                       [(name, s, e, iso(ts), iso(te), json.dumps(a), tx)
                        for s, e, ts, te, a, tx in sessions])
        db.execute("DELETE FROM authors WHERE pad = ?", (name,))
        db.executemany("INSERT INTO authors VALUES (?, ?, ?)",
                       [(name, aid, aname) for aid, aname in sorted(history.authors.items())])
        shrunk = good_markdown and len(markdown.strip()) < SHRINK_LIMIT * len(good_markdown.strip())
        if shrunk:
            flag = ("rev %d keeps only %d%% of the text; still indexing the last good copy"
                    % (history.head, 100 * len(markdown.strip()) // max(1, len(good_markdown.strip()))))
            db.execute("UPDATE pads SET flag = ? WHERE name = ?", (flag, name))
        else:
            db.execute("UPDATE pads SET good_markdown = ?, indexed_rev = ?, flag = NULL "
                       "WHERE name = ?", (markdown, history.head, name))
            index(db, name, markdown)
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    note = "%s: rev %d, %d sessions" % (name, history.head, len(sessions))
    if shrunk:
        note += "; FLAGGED: " + flag
    return note


def index(db, name, markdown):
    rows = split_sections(markdown)
    db.execute("DELETE FROM sections WHERE pad = ?", (name,))
    db.executemany("INSERT INTO sections (heading, attendees, body, pad, kind, meeting_date, "
                   "topic, seq) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   [(r["heading"], r["attendees"], r["body"], name, r["kind"],
                     r["meeting_date"], r["topic"], i) for i, r in enumerate(rows)])
    db.execute("DELETE FROM links WHERE pad = ?", (name,))
    db.executemany("INSERT INTO links VALUES (?, ?, ?, ?, ?, ?, ?)",
                   [(name,) + link for link in extract_links(rows)])


def last_sync(db_path):
    return last_ok_sync(db_path)


def search(db_path, query, limit=20):
    try:
        db = connect_ro(db_path)
        db.execute("SELECT 1 FROM sections LIMIT 1")
    except Exception:
        return []
    sql = ("SELECT s.pad, s.kind, s.heading, s.topic, s.meeting_date, p.url, p.indexed_rev, "
           "p.good_markdown, snippet(sections, 2, '[', ']', '…', 16) AS snip "
           "FROM sections s JOIN pads p ON p.name = s.pad "
           "WHERE sections MATCH ? ORDER BY rank LIMIT ?")
    try:
        rows = db.execute(sql, (query, limit)).fetchall()
    except Exception:
        rows = db.execute(sql, (fts_query(query), limit)).fetchall()
    hits = []
    for r in rows:
        title = pad_title(r["good_markdown"] or "", r["pad"])
        parts = [title] + [p for p in (r["meeting_date"], r["topic"] or (r["heading"] if r["kind"] != "intro" else "")) if p]
        hits.append(Hit(NAME, "pad:%s" % r["pad"], " — ".join(parts), kind=r["kind"],
                        when=r["meeting_date"],
                        url="%s#rev/%s" % (r["url"], r["indexed_rev"]) if r["indexed_rev"] is not None else r["url"],
                        snippet=" ".join((r["snip"] or "").split())))
    return hits


def recent(db_path, since, limit=20):
    """Edit sessions that ended since `since`, newest first."""
    try:
        db = connect_ro(db_path)
        rows = db.execute(
            "SELECT s.pad, s.rev_start, s.rev_end, s.ts_start, s.ts_end, s.authors, "
            "p.url, p.good_markdown FROM sessions s JOIN pads p ON p.name = s.pad "
            "WHERE s.ts_end >= ? ORDER BY s.ts_end DESC LIMIT ?", (since, limit)).fetchall()
    except Exception:
        return []
    hits = []
    for r in rows:
        authors = len([a for a in (r["authors"] or "").replace(",", " ").split() if a])
        hits.append(Hit(NAME, "pad:%s@%d" % (r["pad"], r["rev_end"]),
                        "%s edited" % pad_title(r["good_markdown"] or "", r["pad"]),
                        kind="edit", when=(r["ts_end"] or "")[:10],
                        url="%s#rev/%d" % (r["url"], r["rev_end"]),
                        snippet="revisions %d–%d, %s to %s, %d author id%s" % (
                            r["rev_start"], r["rev_end"], (r["ts_start"] or "")[:16],
                            (r["ts_end"] or "")[11:16], authors, "" if authors == 1 else "s")))
    return hits


_REF = re.compile(r"pad:([A-Za-z0-9_-]+)(?:@(\d+))?")


def parse_ref(ref):
    """(name, rev or None) for `pad:name[@rev]` or a configured pad URL."""
    ref = ref.strip()
    m = _REF.fullmatch(ref)
    if m:
        return m.group(1), int(m.group(2)) if m.group(2) else None
    if ref.startswith("http"):
        base, _, frag = ref.partition("#")
        base = normalize_url(base)
        for name, url in configured_pads().items():
            if base == url:
                rm = re.fullmatch(r"(?:rev/)?(\d+)", frag)
                return name, int(rm.group(1)) if rm else None
    return None


def owns(ref):
    return parse_ref(ref) is not None


def show(db_path, ref):
    parsed = parse_ref(ref)
    if not parsed:
        return None
    name, rev = parsed
    try:
        db = connect_ro(db_path)
        row = db.execute("SELECT * FROM pads WHERE name = ?", (name,)).fetchone()
    except Exception:
        return None
    if row is None or row["good_markdown"] is None:
        return None
    header = ["%s (pad:%s)" % (pad_title(row["good_markdown"], name), name),
              "url: %s" % row["url"],
              "head: rev %s, last edit %s; status %s" % (row["head_rev"], row["head_at"], row["status"])]
    if row["flag"]:
        header.append("FLAG: %s" % row["flag"])
    if rev is None:
        header.append("showing the indexed copy (rev %s): %s#rev/%s"
                      % (row["indexed_rev"], row["url"], row["indexed_rev"]))
        return "\n".join(header) + "\n\n" + row["good_markdown"].strip() + "\n"
    if row["history_gz"] is None:
        return "\n".join(header) + "\n\nno history stored; run `openrtx sync pad` first\n"
    history = History(json.loads(gzip.decompress(row["history_gz"]).decode("utf-8")))
    if not 0 <= rev <= history.head:
        return "\n".join(header) + "\n\npad:%s has no revision %d (head is %d)\n" % (name, rev, history.head)
    when = history.time(rev)
    header.append("showing rev %d (%s), plain text without Markdown: %s#rev/%d"
                  % (rev, when and iso(when), row["url"], rev))
    return "\n".join(header) + "\n\n" + history.text_at(rev).strip() + "\n"
