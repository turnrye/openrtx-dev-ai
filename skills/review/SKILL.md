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
- Compare with the siblings: other files of the same target family or driver kind, found by grepping for what the hunk uses (`git grep -n PLATFORM_CS7000P platform/`, `git grep -ln stm32adc_init`). Note how they handle the same thing. When the change picks a different idiom, it needs a reason; "the same file does it elsewhere" counts only if the two cases really are the same.

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
- **Prior reviews of the same construct**, in any file: maintainers repeat style requests across the codebase, so a rule they apply often may have been stated on a file this change doesn't touch. Search their review comments for the hunk's construct (`#ifdef PLATFORM`, `static constexpr`, `malloc`, the API being called):
  ```sh
  openrtx sql "SELECT repo||'#'||number, created_at, path, body FROM github.comments
               WHERE kind='review_comment' AND author IN ('silseva','marcoSchr')
                 AND body LIKE '%#ifdef PLATFORM%'
               ORDER BY created_at DESC LIMIT 30"
  ```

## 4. Run the mechanical checks

Run what CI runs instead of eyeballing it, on the PR's code (in the worktree for a PR). Exact commands are in [reference.md](reference.md#mechanical-checks); in short:

- **`lint.py`, the house rules a tool can check**, from this skill's directory, on the change (`MB` is the merge base; add `--pr-body FILE` with the PR description saved to a file when reviewing a PR, and `--clang-tidy` when LLVM's `clang-tidy` is installed and the change is checked out, for redundant casts and non-`const` getters):
  ```sh
  python3 <this skill's directory>/lint.py --repo . "$MB" HEAD --pr --json [--clang-tidy]
  ```
  It reports, on the lines the change adds: entries inserted mid-list in settings, string or voice-prompt tables; enums clang-format packed for lack of a trailing comma; `typedef`s; `sizeof`/`sizeof` instead of `ARRAY_SIZE`; `std::string`; module prefixes; functions and variables that should be `static`; layering (radio API outside the rtx thread, the rtx thread taking the state mutex, `ui_updateFSM` or `display_init` called from the UI); unused new struct members and macros; missing Doxygen; `static` locals; `#define`s between functions; unparenthesised comparisons; `if`/`else if` chains that want a `switch`; NULL checks before `free`/`delete`; protocol constants defined outside `protocols/<name>/`; C++ clamps written as `if`/`else` (`std::min`/`std::max`); code for C++ standards newer than C++14; runs of related integer constants that want an `enum`; C++ class layout (member order: public methods, private methods, private constants and enums, private variables; explicit constructor and destructor on new classes; no public data; access specifiers at column 0); raw arrays in C++ classes; hard-coded frequencies in core code; scripts outside `scripts/`. And on the change as a whole: commit prefixes, merges, fixups to autosquash, review responses that should be `fixup!`s, reformatting mixed with changes, saved-settings and codeplug layout, new sources missing from the build, `CONFIG_` macros nothing defines, the PR template's testing and changelog sections.
- `scripts/clang_format.sh --check` (the `Format sources` workflow), `reuse lint`, `python3 scripts/check_strings.py`
- `meson compile` and `meson test` on Linux; cross-compile the targets the change touches when the toolchain is there

`lint.py`'s findings join the report's ranked list like any other, at their lines and severities (its commit findings grouped as one); drop one only when you can show it is wrong for this change (say why in *Not reviewed*), and don't re-derive what it already checks. Report what ran and what you couldn't run (missing toolchain, no hardware). Never claim a build or test result you didn't get.

## 5. Review

Read every hunk with these passes, in this order — correctness first, because a style comment on code that is wrong wastes a round:

1. **Correctness on the radio.** Concurrency with the rtx/UI threads and ISRs, real-time behaviour, bounds and integer width, init/teardown order and error paths, memory (stack, heap, static RAM on CCM), persistent data layout, RF safety. Checklist in [reference.md](reference.md#correctness-on-embedded-targets).
2. **Design and layering.** The layer rules, ownership of state, reuse of existing helpers, `CONFIG_` gating — AGENTS.md's *Layering and Ownership*, *Hardware Targets and Drivers*, *Reuse*, *Conditional Compilation*. Two questions for every hunk:
   - **Does it belong to this PR?** Take the PR's purpose from its title, description and commit subjects, and check each hunk serves it. A hunk for another feature, a drive-by refactor, or a line removed that the change doesn't need gets "this belongs in a separate PR" (or "restore this") at its line. When one hunk is out of scope, follow the same change into the other files it touches (the header for a source change, the declaration for a definition, the caller for a callee) and flag those hunks too. Maintainers keep PRs to one logical change and ask every time.
   - **Is it in the right place?** For each new function, constant group and type, decide where it belongs and say so when that isn't where it is. Does it use the state of the module or class it was added to? If not, it is general: find the `core/` header whose topic it matches (a coordinate or unit conversion → `gps.h` or `utils.h`) or, for a protocol's fields, flags and constants, the protocol's frame class in `protocols/<name>/`, as `enum`s or members. If it does, check it against [where code lives](reference.md#where-code-lives): a chip's definitions in its driver, UI-only state and indicators in the UI, a new driver or tool in its own file in its own directory. Flagging the same lines for another reason (unused, untested) doesn't replace saying where they belong.
3. **Simpler form.** For each changed function, ask what a maintainer asks first:
   - Can it be shorter? A condition that boils down to one expression, a `bool` that can be returned directly, a helper that a designated initializer replaces, a variable whose scope can shrink into the loop that uses it, a step (`>> 0`, a cast, a copy) that does nothing.
   - In C++, does the standard library already do it? Saturation and limits with `std::min`/`std::max`; sums and dot products with `std::accumulate`/`std::inner_product` instead of hand-written or hand-unrolled loops (keep an unrolled loop only for a measured need); `std::copy`/`std::fill` instead of copy loops. When the operands' types differ, name the type: `int32_t` is `long` on the ARM toolchain and `INT16_MAX` is `int`, so `std::min(out, INT16_MAX)` fails to build on the radios while compiling on Linux; write `std::min<int32_t>(out, INT16_MAX)`. Don't pass arguments equal to their defaults (the `std::plus`/`std::multiplies` of `std::inner_product`, a template argument its default already gives). The tree is C++14: never suggest `std::clamp`, `if constexpr`, `std::optional`, `std::string_view`, deduction guides or anything newer, and flag code added for a newer standard (`#if __cplusplus >= 201703L`): newer revisions are handled when the project moves to them.
   - Is each new thing needed? Go through every new function, field, parameter, cast, include and file, and every behaviour change, and ask: does what the PR sets out to do need it, and does the module already have something that does it (search the module and its siblings for an existing function or API before accepting a new one)? When the answer is no, ask "Is this (still) needed?" or "Isn't `existingFunction()` enough?" at its line.
   - Is there a simpler design? Decide this for each new or changed function, type and interface, and when the answer is yes, propose the alternative as a short question at its line, the way maintainers do ("Perhaps we can have a `CONFIG_…` instead of the enum?"). This is the comment maintainers write most, and a bug hunt at the same lines doesn't replace it. Typical answers: pass the one value a function needs instead of the whole object; a table indexed (or cycled modulo its size) instead of a chain of branches; a build-time `CONFIG_` instead of a runtime enum for something fixed per target; related fields grouped and sized to what they hold; an existing helper or API instead of new code, and where an existing class almost fits (`Fir` for a fixed-point filter), extending that class's interface (a template parameter for the sample, coefficient and accumulator types, a method that splits a step) rather than free functions beside it; constants that want to be an `enum`; a hand-packed layout that wants a struct; a helper that serves one C++ class and wants to be its private member.
   - Do new names follow their neighbours? For each new public function, list the module's (or class's) existing function names and compare: the same verbs and word order (`compareX` next to `compareY`, `init`/`update` → `reinit`, not a new verb or one that promises more than it does), the module prefix, the pattern of existing names of the same kind (`CONFIG_<FEATURE>` for hardware features), short rather than verbose.

   These are small, and maintainers raise them on almost every PR. Report them, with a ```` ```suggestion ```` block when the fix is a line or two.
4. **House rules the checker can't see.** `lint.py` covers the mechanical rules; walk the [rule map](reference.md#rule-map) for the rest, rule by rule: designated initializers for structs of function pointers and configs, a `switch` with `default: return -ENOTSUP` where a mode is dispatched through a two-way `if`, magic numbers where a named field or `ARRAY_SIZE` belongs, register access outside the chip's driver, `CONFIG_` naming. Also what text matching can't tell apart: a new member that shares its name with a used member of another struct, and code copied from elsewhere in the file (it looks old to the checker but is new to the change). Each violation is its own finding, at its line.
5. **The PR as a unit.** What needs judgement: whether to split it and how, and whether the description and testing match what the change does. The checker reports commit hygiene and the template's empty sections; the code comes first.

What to flag:

- Only what the change introduces or touches. Legacy code that breaks a rule is not a finding — but new code that *copies* a legacy pattern is (AGENTS.md says "do not copy them" about several).
- Only what you have verified: open the file, find the caller, check the type. If a concern rests on an assumption you couldn't check, put it under *Questions for the author*, not as a finding.
- Each finding is one issue with one location, says why it matters on this hardware or for this project, and cites its rule where there is one (`AGENTS.md § Persistent Data, #340`). Don't paraphrase the rule at length; the citation is the authority.
- Prefer the maintainers' form: a short sentence, a question when you suspect the author knows something you don't ("Can't these be set in `radio_setOpmode()`?"), a ```` ```suggestion ```` block when the fix is a few lines.
- Anchor each code finding to the line it is about, so the author can find it, and keep findings about the PR text, commits and testing after the code findings.
- Keep the list to what a maintainer would write: **at most ten findings**, ranked by severity and then by how much a maintainer would care, with the design questions from pass 3 kept among them rather than pushed out by speculative correctness concerns. Put `lint.py`'s commit findings (prefixes, fixups, merges) together as one *Commits* finding. If more is worth knowing, list it in one line each under *Also noticed*, without the detail.
- Leave out what maintainers don't raise and what costs them time to read: typos and wording in comments, commit-message wording, style nits in test code, CI action versions and workflow trivia, a file that "isn't formatted" (the format check reports that), and defensive checks for cases the callers can't produce. A real bug still goes in, wherever it is.
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

- **Their own change:** offer to fix the findings in severity order. Before calling a finding fixed, check the fix against what the finding asked, not just that the code moved: a finding to extend `fir.hpp` isn't fixed by free functions placed beside the `Fir` class. Follow AGENTS.md while doing it: address review comments as `fixup!` commits, keep reformatting in its own commit, add a `Co-authored-by` trailer, never sign off for them and remind them to (`git commit -s`), and leave any PR in draft.
- **Someone else's PR:** offer to turn the kept findings into GitHub review comments in the user's voice — short, matter-of-fact, no praise padding, no restating the diff (maintainers have noted Claude's chattiness, OpenRTX/OpenRTX#513). Post them only after the user says to, and only as a **pending** review, which nobody else sees until the user submits it on GitHub; the recipe is in [reference.md](reference.md#pending-github-review). Never submit, approve or request changes on their behalf.

## When invoked as /openrtx:review

Arguments: `$ARGUMENTS`

Treat them as the target and options of section 1; with none, pick the target as section 1 says.
