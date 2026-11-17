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

### Reading the results

Each case's score is a set of rates between 0 and 1, `None` where a rate doesn't apply
(a case with no comments to catch has no catch rate; a leaked run has none at all).
The run's metrics are the mean of each rate over the cases it applies to, with a
standard error; `catch_rate` is the headline. In the viewer, a case's **Explanation**
lists every maintainer comment with what the review did about it, and every code
finding with whether its lines changed before the merge; raw counts are in its metadata.

The "targets" are the maintainer's round-one inline comments (made on the head the
review sees) whose lines changed by the merge.

| Metric | Meaning |
|---|---|
| `catch_rate` | Share of targets where a finding made **the same point**: in the same file within 5 lines, and judged by a small model (Haiku, through the `claude` CLI, so no API key) to ask for the same change or name the same problem. Only same-spot pairs go to the judge; its answer is in the score's metadata. The headline. |
| `catch_rate_spot` | Looser: a finding within 5 lines, whatever it says |
| `catch_rate_file` | Loosest: a finding anywhere in the file |
| `precision` | Share of code findings on lines that changed before the merge (±2), a stand-in for precision |
| `flagged` | Share of cases with at least one P0–P2 finding; every case had feedback that changed the code |
| `code_findings`, `meta_findings` | Findings per case about the code, and about the PR text or commits (not scored) |
| `leaked` | Share of runs that quoted a maintainer comment they shouldn't have seen; those cases are left out of the rates |
| `*_stderr`, `catch_rate_cases` | Standard error of a mean, and how many cases it is over |

Compare two skill versions on the same cases and epochs. A difference smaller than about
twice the standard error is noise.

## Scoring the deterministic checks (no model)

`lint_eval.py` runs the review skill's `lint.py` (house rules a tool can check) over every
case's start and scores it with the same spot match, no model involved. It takes about a
minute for the whole corpus, so it can run on every change to `lint.py`:

```sh
python3 evals/review/lint_eval.py --lint <plugin checkout>/skills/review/lint.py [--clang-tidy] [--show]
```

It prints, per rule, how many findings it raised, how many make the same point as a
maintainer comment (its rule matches what the comment asks for, `RULE_TOPICS`), how many
land at a comment at all, and how many are on lines that changed before the merge.

## What a case holds

| Field | Meaning |
|---|---|
| `start.sha`, `start.base` | The PR head at the maintainer's first substantive comment, and its fork point. Review that range. |
| `start.anchor` | `exact` when the comment is pinned to a commit (inline or review), `reconstructed` when it was a conversation comment and the head is worked out from the push history |
| `cutoff` | Time of that first comment. Anything a reviewer reads (PR thread, research cache) must be from before it. |
| `feedback[].prior` | A comment from before the cutoff, on a version of the PR that no longer exists. A reviewer of `start` would have seen it, so it is context: neither a target nor a leak. |
| `start.base`, `end.base` | Fork point from whichever known base tip leaves the PR smallest: the base before and after each force push (GraphQL), the PR's base, and master at the cutoff. Handles base branches rewritten after the merge. |
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
