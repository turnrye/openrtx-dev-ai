# Review corpus

A silver data set for evaluating `/openrtx:review`: merged OpenRTX PRs, each with
the code the maintainer first reviewed, what he said, and the code that was merged.

```sh
python3 evals/review/build_corpus.py            # rebuild corpus.json (100 cases)
python3 evals/review/build_corpus.py --refresh  # refetch from GitHub first
```

It needs `gh` signed in. Raw API data is cached under the openrtx cache
(`evals/review/raw`), and commits go into `evals/openrtx.git` there, shared with the
commits eval.

## What a case holds

| Field | Meaning |
|---|---|
| `start.sha`, `start.base` | The PR head at the maintainer's first substantive comment, and its fork point. Review that range. |
| `start.anchor` | `exact` when the comment is pinned to a commit (inline or review), `reconstructed` when it was a conversation comment and the head is worked out from the push history |
| `cutoff` | Time of that first comment. Anything a reviewer reads (PR thread, research cache) must be from before it. |
| `feedback[]` | Every comment the maintainer left before the merge, oldest first: `inline` (path, line, diff hunk, author's replies), `review` (a review body), `conversation` |
| `feedback[].commit` | The head the comment was made on. Later rounds point at later heads. |
| `feedback[].trivial` | Thanks, LGTM and the like; not something a review should raise |
| `feedback[].outcome` | Inline comments only: `changed` if the lines it points at (±2) differ at the end, `unchanged`, or `file_gone` |
| `end.sha`, `end.base`, `end.merge_commit` | The PR's final head, its fork point, and the merge commit |
| `skipped` | PRs looked at and why they were left out |

## How cases are chosen

Merged PRs on OpenRTX/OpenRTX, newest merge first, not opened by the reviewer, where
he left at least one non-trivial comment and the head moved after it. That a comment was
followed by a push, or that its lines changed, is evidence it was acted on, not proof.

## Caveats

- Most recent PRs are from one author (turnrye), whose PRs also shaped the review skill's
  rules. Report results with and without them.
- `start` is the first round only. A later round's comments were made on a later head;
  score them against their own `commit`, or count them as out of reach from `start`.
- A conversation comment's head comes from force pushes (exact time) and plain pushes
  (commit time, which can be earlier than the push).
- `outcome` compares the file at the comment's commit with the end, so a rebase can
  make an untouched line look changed.
