# Taiga API reference for `openrtx taiga`

The OpenRTX Taiga is `https://tasks.openrtx.org`, API base `https://tasks.openrtx.org/api/v1`, project id **1**, slug `openrtx`. Anonymous reads see everything in this public project. `openrtx taiga api` handles the login token for you.

## Items

| Kind | Endpoint | URL segment | History kind |
|---|---|---|---|
| epic | `/epics` | `/project/openrtx/epic/N` | `epic` |
| user story | `/userstories` | `/us/N` | `userstory` |
| task | `/tasks` | `/task/N` | `task` |
| issue | `/issues` | `/issue/N` | `issue` |

- **Refs vs ids:** `#N` is a project-wide ref; the API wants the object id. `GET /resolver?project=openrtx&ref=N` returns `{"project": 1, "us": 163}` (key = kind). `GET /<kind>/by_ref?ref=N&project=1` returns the object.
- **Lists:** `GET /<kind>?project=1`; add header `x-disable-pagination: True` for everything (the CLI doesn't, so use `openrtx sql` on the cache for bulk reads).

## Writing

- **Optimistic locking:** every PATCH needs the item's current `version`; a stale one returns HTTP 400. Read, then patch.
- **Create:** `POST /<kind>` with `project`, `subject`, `description` (+ `tags`, and `user_story` for a task).
- **Comment:** `PATCH /<kind>/<id>` with `{"comment": "...", "version": V}`; it appears in `GET /history/<history kind>/<id>`.
- **Story under an epic:** `POST /epics/<epic id>/related_userstories` with `{"epic": id, "user_story": id}`.
- **Status:** statuses are per project and kind: `GET /userstory-statuses?project=1` (also `task-statuses`, `issue-statuses`, `epic-statuses`); then `PATCH` with `{"status": <status id>, "version": V}`.
- **Assignee:** `GET /users?project=1` for ids; stories take `assigned_to` (and `assigned_users`), tasks/issues `assigned_to`.
- **Milestone (sprint):** `GET /milestones?project=1`; stories take `milestone`.
- **Tags:** `tags` is a list of names (colours optional as `[name, colour]` pairs).

Example (dry run first, then add `--post`):

```sh
openrtx taiga api GET "/userstory-statuses?project=1"
echo '{"status": 7, "version": 12}' | openrtx taiga api PATCH /userstories/163 --data -
```

## How Taiga renders text

- Descriptions and comments are Markdown, stored as sent and rendered on the server into `description_html` / `comment_html`. Setext headings (`Heading` over `-----`) become `<h2>`.
- **`#N` becomes a link to Taiga item N** when it isn't preceded by a letter, digit, `-` or `[` — and this runs on link *text* too. `[#520](https://github.com/…)` renders as an empty GitHub link followed by a link to Taiga item 520. Only refs that exist get linked. Write GitHub references as `OWNER/REPO#N`; `openrtx taiga` converts them to links whose text Taiga leaves alone.
- `@username` notifies that user.
- There is no server-side preview endpoint on this instance; check the `*_html` after posting (the CLI's read-back does).

## Login

- `POST /auth` with `{"type": "normal", "username", "password"}` returns `auth_token` (about 24 h) and `refresh` (about 8 days); `POST /auth/refresh` with `{"refresh"}` returns a new pair and restarts the 8-day window. The CLI refreshes ahead of expiry, so regular use never needs a new login.
- The token is saved in `taiga-auth.json` in the openrtx config dir (`~/.config/openrtx/`, or `%APPDATA%\openrtx` on Windows), mode 600.
