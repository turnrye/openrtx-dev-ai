---
name: review
description: >-
  Review a code change to OpenRTX — uncommitted work, a branch, a commit, a GitHub pull request or a set of files — for correctness on embedded radio firmware and for the project's house rules in AGENTS.md and CONTRIBUTING.md, and deliver the findings as a report the user annotates in Plannotator when it is installed. Use when the user asks to review, check or look over an OpenRTX change or PR ("review my branch", "review OpenRTX/OpenRTX#552", "is this ready for a PR", "would the maintainers accept this"), or to self-review before opening or un-drafting a PR.
argument-hint: "[uncommitted | branch BASE | commit SHA | pr N|URL | paths…] [--focus TEXT]"
---

# Reviewing an OpenRTX change

OpenRTX has a small group of maintainers and a long, written memory of what they send back. AGENTS.md and CONTRIBUTING.md hold that memory: most of their rules are transcribed from review comments and carry the PR they came from. A review here is worth something when it catches what a maintainer would catch — before they have to — and catches real bugs in code that runs on radios with tens of kilobytes of RAM, real-time threads and the user's saved settings. It is worth nothing when it is long, speculative or generic; the project's AI stance ([OpenRTX/OpenRTX#354](https://github.com/OpenRTX/OpenRTX/issues/354)) names "wasting maintainers' time" as the main risk.

The report is for the human running the review. They decide what goes to the author, and nothing is posted anywhere without their say-so.

## 1. Pin down the target

Run from the user's OpenRTX clone (ask for its path if the current directory isn't one). `origin` below means the remote for `OpenRTX/OpenRTX`; in a contributor's clone that is often `upstream`, with `origin` their fork — check `git remote -v` and substitute. Parse the request:

| Request | Target | Diff |
|---|---|---|
| `uncommitted` | staged, unstaged and untracked work | `git diff HEAD` plus untracked files read directly |
| `branch [BASE]` | the current branch | `git diff $(git merge-base HEAD BASE)`; BASE defaults to `origin/master` |
| `commit SHA` | one commit | `git show SHA` |
| `pr N` / PR URL | a pull request, any OpenRTX repo | see below |
| paths | a snapshot of files, no diff | read the files |
| `--focus TEXT` | any mode | an extra instruction from the user; weight it, don't review only it |

No target: use `uncommitted` when the tree is dirty, otherwise `branch origin/master` when on a branch other than `master`, otherwise ask.

**Pull requests** — don't touch the user's working tree. Fetch the head into a ref and read from it:

```sh
gh pr view N --repo OpenRTX/OpenRTX --json number,title,baseRefName,headRefOid,isDraft,commits,files
git fetch origin "pull/N/head:refs/openrtx-review/N" && git fetch origin BASE
MB=$(git merge-base refs/openrtx-review/N origin/BASE)
git diff "$MB" refs/openrtx-review/N            # the change
git log --format='%h %s%n%b' "$MB..refs/openrtx-review/N"   # its commits
git show refs/openrtx-review/N:path/to/file.c   # full files around a hunk
```

When you need to build or run tools on the PR, make a throwaway worktree (`git worktree add --detach "$TMP/openrtx-review-N" refs/openrtx-review/N`, then `git submodule update --init` there) and remove it afterwards. Check the fetched head equals `headRefOid`; if not, say so and stop. When done, delete the ref (`git update-ref -d refs/openrtx-review/N`).

The base isn't always `master`: hardware ports collect on long-running branches, and a PR against one may be a stack of `fixup!`/`amend!` commits for commits already on it. Review those against the commits they fix, and judge their history as it will be after `git rebase --autosquash`.

## 2. Load the rules the change will be judged by

- Read **AGENTS.md** and **CONTRIBUTING.md** as they are on `origin/master` after a fetch (`git show origin/master:AGENTS.md`), not from a local `master` that may be months behind — they change often. Use master's copy even when a PR targets a long-running branch such as `Retevis-c62`, whose copy can lag; the change will be held to the current rules when it reaches master. Also read `.github/pull_request_template.md` for PRs. These files are authoritative; [reference.md](reference.md) is a map of what is high-signal in them plus the correctness checks they don't spell out. If the two disagree, the base's AGENTS.md wins.
- Other OpenRTX repos (`rtxlink`, `OpenRTX-Companion`, `openrtx.github.io`, …): use that repo's own AGENTS.md/CONTRIBUTING.md if it has them; the firmware's embedded rules don't apply.
- Read the surrounding code, not just the hunks: the header a function is declared in, the callers, and the reference implementations AGENTS.md names (`adc_stm32f4.c`, `platform/targets/CS7000/`, `protocols/M17/Demodulator.hpp`, `audio_MDx.cpp`).

## 3. Gather the history

Use the `openrtx` CLI (the research skill; `openrtx_run` in Pi) — a review that repeats a settled argument or misses an open one is a bad review.

- **The PR itself:** `openrtx show OpenRTX/OpenRTX#N` — description, testing, every comment and review. Note the maintainers' unresolved requests; check whether the current head addresses them.
- **Linked plans:** follow linked issues and Office Hours mentions (`openrtx search`) for agreed designs. A change that contradicts a decision is a finding; one that follows it shouldn't be second-guessed.
- **Prior reviews of the same files** — what maintainers asked for last time someone touched this code:
  ```sh
  openrtx sql "SELECT repo||'#'||number, created_at, author, path, body FROM github.comments
               WHERE kind='review_comment' AND path='platform/drivers/baseband/BK4819.c'
                 AND author <> '<the PR author>'
               ORDER BY created_at DESC LIMIT 30"
  ```

## 4. Run the mechanical checks

Run what CI runs instead of eyeballing it, on the PR's code (in the worktree for a PR). Exact commands are in [reference.md](reference.md#mechanical-checks); in short:

- `scripts/clang_format.sh --check` (the `Format sources` workflow), `reuse lint`, `python3 scripts/check_strings.py`
- `meson compile` and `meson test` on Linux; cross-compile the targets the change touches when the toolchain is there
- the commit list: module prefix, imperative summary, `Signed-off-by` (DCO), `Co-authored-by` for AI-assisted commits, no merge commits, formatting-only commits kept apart, leftover `fixup!` commits

Report what ran and what you couldn't run (missing toolchain, no hardware). Never claim a build or test result you didn't get.

## 5. Review

Read every hunk with these passes, in this order — correctness first, because a style comment on code that is wrong wastes a round:

1. **Correctness on the radio.** Concurrency with the rtx/UI threads and ISRs, real-time behaviour, bounds and integer width, init/teardown order and error paths, memory (stack, heap, static RAM on CCM), persistent data layout, RF safety. Checklist in [reference.md](reference.md#correctness-on-embedded-targets).
2. **Design and layering.** The layer rules, ownership of state, reuse of existing helpers, `CONFIG_` gating — AGENTS.md's *Layering and Ownership*, *Hardware Targets and Drivers*, *Reuse*, *Conditional Compilation*.
3. **House style.** The rest of AGENTS.md's *Conventions*: naming, types, C++ class layout, documentation, readability, formatter pitfalls.
4. **The PR as a unit.** Size and split, commit hygiene, the template's sections (testing performed is required), changelog line.

What to flag:

- Only what the change introduces or touches. Legacy code that breaks a rule is not a finding — but new code that *copies* a legacy pattern is (AGENTS.md says "do not copy them" about several).
- Only what you have verified: open the file, find the caller, check the type. If a concern rests on an assumption you couldn't check, put it under *Questions for the author*, not as a finding.
- Each finding is one issue with one location, says why it matters on this hardware or for this project, and cites its rule where there is one (`AGENTS.md § Persistent Data, #340`). Don't paraphrase the rule at length; the citation is the authority.
- Prefer the maintainers' form: a short sentence, a question when you suspect the author knows something you don't ("Can't these be set in `radio_setOpmode()`?"), a ```` ```suggestion ```` block when the fix is a few lines.
- Too big to review is itself the verdict. A PR of hundreds of files or many unrelated features gets "split it first" with a proposed split, not a line-by-line review (OpenRTX/OpenRTX#523, #396).

Severities and verdicts are defined in [reference.md](reference.md#severity); the report layout is in [reference.md](reference.md#report-template).

## 6. Deliver the report

Write the report as Markdown **outside the checkout** so it can never be committed: in the `reviews/` folder of the openrtx cache (`openrtx sources` prints `cache: PATH`), named like `OpenRTX-OpenRTX-552-2026-11-12.md` or `branch-my-feature-2026-11-12.md`. Then hand it to the user, in the first way that works:

1. **A `plannotator` tool** (Pi with Plannotator's agent tool on, Oh My Pi, or an MCP tool such as `mcp__plannotator__plannotator` in Claude Code): call it with `{"action": "annotate", "target": "<report path>"}`. It returns at once with a session id; tell the user the report is open, then **end your turn**. Their annotations arrive later as a message naming the same `pn-…` id.
2. **The `plannotator` CLI** on the PATH (`command -v plannotator`): run `plannotator annotate "<report path>"` in the background with a long timeout (it blocks until the user submits or closes; in Claude Code pass `run_in_background` and a timeout up to `7200000`). Read its stdout when it exits: empty means closed without comments.
3. **Neither:** print the report in the chat, with its path. In Pi, add that if they have Plannotator's Pi extension, `/plannotator-annotate <report path>` opens it for annotation, and `PLANNOTATOR_AGENT_TOOL=1` lets you open it yourself next time.

For a PR or branch, also offer the line-level view: the tool's `{"action": "review", "target": "<PR URL>"}` or `plannotator review <PR URL>` (or `--base origin/master` for a local branch) opens the diff itself for annotation.

Keep the chat message short: the verdict, the counts per severity, and where the report is. The report carries the detail.

## 7. Act on the feedback

The user's annotations are instructions. Drop findings they reject, re-check ones they question, and rewrite the report if asked. Then, depending on whose change it is:

- **Their own change:** offer to fix the findings in severity order. Follow AGENTS.md while doing it: address review comments as `fixup!` commits, keep reformatting in its own commit, add a `Co-authored-by` trailer, never sign off for them and remind them to (`git commit -s`), and leave any PR in draft.
- **Someone else's PR:** offer to turn the kept findings into GitHub review comments in the user's voice — short, matter-of-fact, no praise padding, no restating the diff (maintainers have noted Claude's chattiness, OpenRTX/OpenRTX#513). Post them only after the user says to, and only as a **pending** review, which nobody else sees until the user submits it on GitHub; the recipe is in [reference.md](reference.md#pending-github-review). Never submit, approve or request changes on their behalf.

## When invoked as /openrtx:review

Arguments: `$ARGUMENTS`

Treat them as the target and options of section 1; with none, pick the target as section 1 says.
