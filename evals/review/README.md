# Review corpus

A silver data set for evaluating `/openrtx:review`: merged OpenRTX PRs, each with
the code the maintainer first reviewed, what he said, and the code that was merged.

```sh
python3 evals/review/build_corpus.py            # rebuild corpus.json (up to --limit 100; 64 qualify today)
python3 evals/review/build_corpus.py --refresh  # refetch from GitHub first
```

It needs `gh` signed in. Raw API data is cached under the openrtx cache
(`evals/review/raw`), and commits go into `evals/openrtx.git` there, shared with the
commits eval.

## Running the eval (Inspect AI)

`inspect_review.py` is an [Inspect AI](https://inspect.aisi.org.uk/) task. Each sample
runs `/openrtx:review` headless on a case's start head, sealed at its cutoff, and scores
the findings. No install needed beyond `uv`; `--model none` because the task drives
Claude Code itself.

```sh
# two cases, to check the setup (about $1 each)
uv run --with inspect-ai inspect eval evals/review/inspect_review.py --model none \
    -T limit=2 --log-dir ~/Library/Caches/openrtx/evals/review/logs

# the whole corpus, 3 at a time, twice per case, testing a plugin checkout
uv run --with inspect-ai inspect eval evals/review/inspect_review.py --model none \
    -T plugin_dir=. --max-samples 3 --epochs 2 --log-dir ~/Library/Caches/openrtx/evals/review/logs

# browse runs: per-case scores, cost, and links to each run's folder
uv run --with inspect-ai inspect view --log-dir ~/Library/Caches/openrtx/evals/review/logs
```

Task options (`-T name=value`): `ids` (comma-separated case ids), `limit`, `author`
(`turnrye`, or `!turnrye` to leave those out), `plugin_dir` (default: the installed
plugin), `model`, `builds=true` (let the review build; off by default), `time_limit`.

Each sample's folder under `~/Library/Caches/openrtx/evals/review/runs/<run>/<case>-e<epoch>`
holds the sealed checkout, the cut-down research cache, `transcript.jsonl` and
`findings.json`. A crashed run resumes with `inspect eval-retry <log>`.

### Metrics

Counts are summed over cases, so the ratios are micro-averaged.

| Metric | Meaning |
|---|---|
| `recall_acted` | Share of the maintainer's round-one inline comments whose lines changed by the end that a finding lands on (same file, ±5 lines). The headline. |
| `recall_round1` | The same over all round-one inline comments |
| `recall_acted_same_file` | Looser: a finding anywhere in the comment's file |
| `precision_on_changed` | Share of located findings whose lines changed between start and end (±2), a stand-in for precision |
| `cases_flagged` | Share of cases with at least one P0–P2 finding; every case had feedback that changed the code |
| `suggestions_landed` | Share of findings whose suggested code is in the merged file |
| `total_findings`, `total_leaked`, `total_no_findings_file` | Volume; runs that quoted the maintainer verbatim (the seal failed); runs that wrote no findings file |

Compare two skill versions on the same cases and epochs; with this many cases a few
points of difference is within the noise.

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
