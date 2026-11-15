# GitHub (`github`)

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
