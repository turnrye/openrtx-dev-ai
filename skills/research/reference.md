# Research reference: what's in the cache

`openrtx sql "…"` attaches each source's database as a schema of the same name. Every source also has `schema_docs` describing its tables, e.g. `openrtx sql "SELECT * FROM taiga.schema_docs"`. Search syntax everywhere is SQLite FTS5: `"exact phrase"`, `a OR b`, `NEAR(a b, 10)`, `prefix*`.

## Taiga (`taiga`)

- **`items`**: live epics, stories, tasks and issues. Columns include `type` (epic, us, task, issue), `ref`, `subject`, `status`, `is_closed`, `owner`, `assigned`, `milestone`, `tags`, `story_ref` (a task's parent story), `epic_refs` (a story's epics), `issue_type`, `severity`, `priority`, `created_date`, `modified_date`, `finished_date`, `total_comments`, `url`, `description`, `object` (e.g. `userstories:163`).
- **`refs`**: every ref, including deleted ones (`deleted_at`; `known_from = 'timeline'` = deleted before mirroring).
- **`comments`**: `created_at`, `username`, `type`, `ref`, `subject`, `comment`, `edited_at`, `object`.
- **`activity_log`**: `at`, `source`, `actor`, `action`, `item_type`, `ref`, `subject`, `fields`, `comment`, `detail` (JSON), `object`. Always `ORDER BY at`.
- **`mentions`** (`src_ref` → `to_ref` with `context`), **`links`** (URLs cited in descriptions and comments), **`search`** (FTS5).
- **API-shaped views:** `userstories`, `tasks`, `issues`, `epics`, `milestones`, `history`, `timeline`, `users`, …; nested values are JSON (`json_extract(status_extra_info, '$.name')`). Each has a `<view>_versions` twin for point-in-time questions.

```sql
-- A milestone's stories and their tasks
SELECT s.ref, s.subject, s.status, t.ref AS task, t.subject AS task_subject, t.status AS task_status
FROM taiga.items s LEFT JOIN taiga.items t ON t.story_ref = s.ref
WHERE s.type = 'us' AND s.milestone LIKE 'v0.6.0%' ORDER BY s.ref, t.ref

-- Everything that cites taiga#840, in context
SELECT src_type, src_ref, via, at, context FROM taiga.mentions WHERE to_ref = 840 ORDER BY at

-- Taiga items tied to a GitHub PR
SELECT src_type, src_ref, via, at FROM taiga.links WHERE url LIKE '%/OpenRTX/OpenRTX/pull/517%'

-- Discussion by a person on a topic
SELECT c.created_at, c.ref, c.subject, c.comment FROM taiga.comments c
WHERE c.username = 'Redman'
  AND c.object IN (SELECT object FROM taiga.search WHERE search MATCH 'cmake')
ORDER BY c.created_at
```

**Reading Taiga correctly**

- **People** (Taiga username → person, GitHub handle): `Redman` → Silvano Seva (`silseva`), lead maintainer; `nizzo` → Niccolò Izzo (`n1zzo`); `fizzo` → Federico; `on4mod` → Morgan Diepart (`mdiepart`); `k0ret` → Ryan Turner (`turnrye`). `taiga.users` has the rest.
- **The `github-…` actor** is Taiga's GitHub importer: most issues are imported GitHub issues and its comments read "[name] says in [GitHub#N](url)". The real author and the GitHub number are in that text.
- **`#N` inside Taiga text can be a GitHub number**, because the importer and people write bare numbers. Taiga refs are dense, so a number nearly always "resolves" to something; check `mentions.context` or the surrounding text.
- **Creations** come from `timeline` (Taiga's history API omits them). **Deletions** are known only from when mirroring started; earlier ones appear via `refs.known_from = 'timeline'`.
- **Comment edits** show up only after the weekly full sweep (`openrtx taiga sync --full` forces one).
- **Counters can disagree with the data** (e.g. `total_comments`); trust the rows.

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

-- Which PRs and Taiga items each meeting discussed
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
