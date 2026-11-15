# Office Hours pad (`pad`)

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

