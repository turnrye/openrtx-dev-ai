# Research reference: what's in the cache

`openrtx sql "…"` attaches each source's database as a schema of the same name. Every source also has `schema_docs` describing its tables, e.g. `openrtx sql "SELECT * FROM github.schema_docs"`. Search syntax everywhere is SQLite FTS5: `"exact phrase"`, `a OR b`, `NEAR(a b, 10)`, `prefix*`.

## Office Hours pad (`pad`)

The community call notes (Etherpad at pad.opendoor.fr), refreshed daily. Each `##` section is a meeting (newest first) with a "Held on <date>" line, attendees (mostly callsigns) and topics.

- **`sections`** (FTS5): one row per meeting, per top-level topic and the intro: `heading`, `attendees`, `body`, `pad`, `kind` (meeting/topic/intro), `meeting_date`, `topic`.
- **`pads`**: `url`, `status` (ok/gone/private), `head_rev`, `head_at`, `good_markdown` (the indexed copy), `indexed_rev`, `flag` (a change that wasn't indexed, e.g. a suspicious mass deletion).
- **`sessions`**: edit sessions (30 min idle gap) with revision range, times, author ids and the full text at the end of the session.
- **`links`**: GitHub (`OpenRTX/OpenRTX#N`; a bare `#N` in the pad means a GitHub PR or issue) and Taiga (`taiga#N`) references found in the notes, with `meeting_date` and `heading`.
- **`authors`**: Etherpad author ids and self-chosen names (unverified; prefer the attendee lines).

```sql
-- Decisions about a topic, meeting by meeting
SELECT meeting_date, topic, snippet(sections, 2, '[', ']', '…', 20)
FROM pad.sections WHERE sections MATCH 'mailing list' ORDER BY meeting_date DESC

-- Which PRs each meeting discussed
SELECT meeting_date, kind, ref FROM pad.links ORDER BY meeting_date DESC
```

Cite a meeting by date with the pad link (`…#rev/N`); `openrtx show pad:officehours@N` rebuilds any revision offline.

## GitHub (`github`)

Every repo in the OpenRTX organisation (forks skipped), refreshed hourly with the user's `gh` login (`GITHUB_TOKEN`/`GH_TOKEN` also work; anonymous access is too rate-limited for a first sync).

- **`items`**: issues and PRs: `repo` (`OpenRTX/OpenRTX`), `number`, `is_pr`, `state`, `title`, `body`, `author`, `labels` (JSON), `created_at`, `updated_at`, `closed_at`, `merged_at`, `url`, `data` (the raw API object).
- **`comments`**: everything said on an item: `kind` (`comment`, `review_comment` with `path` and `line`, `review` with `state` such as APPROVED or CHANGES_REQUESTED), `repo`, `number`, `author`, `body`, `created_at`, `url`.
- **`versions`**: earlier copies of an item, one per `updated_at` seen.
- **`repos`**: `full_name`, `archived`, `has_issues`, `description`, `pushed_at`.
- **`search`** (FTS5): titles, bodies and every comment and review, with `repo`, `number`, `src`, `at`, `url`.

```sql
-- What a maintainer asked for in reviews of PRs about a topic
SELECT c.repo || '#' || c.number AS ref, c.created_at, c.kind, c.path, c.body
FROM github.comments c
WHERE c.author = 'silseva' AND c.kind IN ('review', 'review_comment')
  AND (c.repo, c.number) IN (SELECT repo, number FROM github.search WHERE search MATCH 'clang-format')
ORDER BY c.created_at

-- Open PRs by author, newest activity first
SELECT repo || '#' || number, author, title, updated_at FROM github.items
WHERE is_pr = 1 AND state = 'open' ORDER BY updated_at DESC

-- PRs mentioned in Office Hours notes, with their state today
SELECT l.meeting_date, l.ref, i.state, i.merged_at, i.title
FROM pad.links l JOIN github.items i ON l.ref = i.repo || '#' || i.number
ORDER BY l.meeting_date DESC
```

- Review discussion is where most design decisions live; search comments, not just titles.
- A PR's `state` is `closed` when merged; check `merged_at`.
