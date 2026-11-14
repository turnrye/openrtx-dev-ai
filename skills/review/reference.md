# OpenRTX review reference

AGENTS.md and CONTRIBUTING.md on the base branch are the authority. This file says where their high-signal rules are, adds the correctness checks they leave implicit, and fixes the report's shape. Where it disagrees with the base's AGENTS.md, AGENTS.md wins — and the disagreement is worth telling the user, so this file gets fixed.

## Mechanical checks

Run from the checkout (or the PR's worktree). `MB` is the merge base.

| Check | Command | CI job |
|---|---|---|
| Formatting | `scripts/clang_format.sh --check` — checks every tracked C/C++ file not on its exclusion lists; a new file must be `git add`ed first | `Format sources` |
| Licence headers | `reuse lint`, or `uvx reuse lint` / `pipx run reuse lint` when it isn't installed | `Format sources` |
| String tables | `python3 scripts/check_strings.py` | `Format sources` |
| Linux build and unit tests | `meson setup build_review && meson compile -C build_review openrtx_linux && meson test -C build_review --print-errorlogs` | `Build and test` |
| Sanitizers | `meson setup build_review_asan -Dasan=true && meson test -C build_review_asan` — worth it for buffer and parser changes | — |
| A target the change touches | `meson setup --cross-file cross_cm4.txt build_review_cm4 && meson compile -C build_review_cm4 openrtx_<target>` (targets in AGENTS.md § Build and Test) | `Build and test` |

- CI uses Ubuntu 24.04's clang-format. Another version can disagree on a few constructs; if only your local run fails, say which version you ran.
- `clang_format.sh --check` failing on files the change doesn't touch is not a finding. Narrow it: `git diff --name-only $MB | grep -E '\.(c|cpp|h|hpp)$'` and check whether those are among the failures.
- A file the change converts to the current style must also be removed from the script's exclusion list (AGENTS.md § Working With the Formatter). A file *added* to the exclusion list is a finding.

**Commits** (CONTRIBUTING.md § Making Changes, AGENTS.md § Commits and PRs):

```sh
git log --reverse --format='%h %s' $MB..HEAD                       # module prefix, imperative, short
git log --format='%h %(trailers:key=Signed-off-by,valueonly)' $MB..HEAD   # each needs a sign-off
git log --format='%h %(trailers:key=Co-authored-by,valueonly)' $MB..HEAD
git log --merges --oneline $MB..HEAD                               # should be empty
git log --format='%h %s' $MB..HEAD | grep -E '^[0-9a-f]+ (fixup|squash|amend)!'
git show --stat <sha>                                              # one module per commit?
```

- Summary prefixed with the module (`ui:`, `m17:`, `core:`, `rtx:`, `drivers:`, `nvm:`, `build:`, …). Needing two prefixes suggests two commits.
- Reformatting an existing file is its own commit; nothing added in one commit and moved in the next.
- `fixup!` commits are expected *during* review and squashed before merge; on a PR that is otherwise ready, call them out as "to autosquash", not as an error.
- A missing `Signed-off-by` is a P1 for the PR: maintainers ask for it. An agent must never add one.
- Bisectability is a preference, not a gate (#498). To test it, in a worktree only: `git rebase -x 'meson compile -C build_review openrtx_linux' $MB`.

## Correctness on embedded targets

These are what a style-focused read misses. Each item is a question to answer for the hunks in front of you, not a list to paste into the report.

**Threads, interrupts and timing**
- Which thread runs this — UI, rtx, a driver worker, an ISR? Is shared data protected, and is the lock the right one? The rtx thread must not take the state mutex (AGENTS.md § Concurrency and Real-Time).
- Does anything called from the rtx FSM loop, sleep or block? It must do one thing and return (#427).
- ISRs: no allocation, no blocking calls, no `printf`; shared variables are `volatile` *only* when an ISR shares them (#500); read-modify-write of shared state is atomic or masked.
- Does the UI reconfigure the radio directly instead of through `sync_rtx` (#335)? Can `txDisable` be bypassed (#427)?
- A critical section that can be shorter, or one around code that runs once from one thread (init/terminate), is a finding (#396, #542).

**Memory**
- Large local arrays on a thread's stack — thread stacks are small; check the stack size where the thread is created.
- Static buffers cost CCM RAM on STM32F4 whether the feature runs or not; large or optional buffers belong on the heap, allocated when the mode starts (AGENTS.md § Embedded Constraints, #509).
- Per-frame or per-sample `malloc`/`free` in the signal path (#396); leaks on error paths; a second allocation where a flexible array member would do (#456).
- New fields in `state` or `rtxStatus` not gated by their `CONFIG_` (#396); struct field order that adds padding.

**Arithmetic and data**
- Integer width and sign: `uint8_t` counters against `ARRAY_SIZE` > 255, shifts of signed values, overflow in intermediate products (a 32-bit overflow sent the C62's top CTCSS tones off-frequency, OpenRTX/OpenRTX#552), division by a calibration value that can be zero.
- Floating point: most targets are Cortex-M4F (and the ESP32-S3), whose FPU is single precision only. A `double` literal or `math.h` double function silently pulls in software emulation; use `f` suffixes and `sinf`/`fabsf`. Maintainers prefer fixed point where inputs are integers (#524).
- Bounds: buffer lengths from the air, NVM or a host are untrusted and validated once at that boundary (AGENTS.md § Reuse Before Reimplementation, #509); internal callers are trusted, so a new defensive check deep inside is a finding too.
- Strings: `strncpy` termination, `snprintf` truncation, callsign lengths, `strlen` in a loop (#390).
- Byte order and bit order on the wire (M17 is big-endian); packed layouts kept inside the class that owns them (#390).

**Lifecycle and errors**
- Init and terminate called explicitly, once; no lazy init, no `initialized` guard that hides a double init (#498, #500).
- Functions that can fail return `0` or a negative `errno` (#478, #534); every caller checks it; error paths release what was acquired and don't leave a half-built object (#427).
- Each failure is reported where it happens (#542).

**Persistent data and on-air behaviour**
- `settings_t` grows only at the end; `stringsTable_t` grows only at the end and in every language file (AGENTS.md § Persistent Data). Either one wrong corrupts users' radios silently — always P0.
- Codeplug, NVM or calibration formats: does an old image still read? Defaults from the hardware config, not a constant (#335).
- RF: frequency limits, deviation, power, tone levels, TX timeout. Out-of-band or over-deviation transmission is P0. Hardware values carry their source (datasheet page, vendor function) (#542).
- Protocol changes against the spec (M17: [spec.m17project.org](https://spec.m17project.org)) and against the OSI layering in CONTRIBUTING.md.

**Tests**
- New logic in `openrtx/` that can run on Linux has a Catch2 test registered in `meson.build`; test executables are Catch2 binaries (#522).
- The PR's *Testing performed* says what was run, on which radios; "Not tested on hardware — help wanted" is an honest, acceptable state; silence is not.

## Rule map

Where to look in the base's AGENTS.md for each kind of hunk. The rules most often sent back are bold.

| The hunk touches | Read | Most-raised rules |
|---|---|---|
| any C | Naming, Types and Data Structures, Readability | **no `typedef struct`/`enum`**; **module prefix on public functions**; **no magic numbers, use `ARRAY_SIZE`**; `stdint` types, `size_t` for sizes; designated initializers; **trailing comma on the last enum/initializer element**; parenthesised compound conditions; dead and commented-out code removed |
| a header | Documentation | Doxygen comment on every public function, **in the header**; `///<` on members; enum declarations at the top of the file (#552) |
| C++ | C++ Class Conventions | member order public → private; private by default; **no `std::string`**, no heap containers, `std::array`; one class per file; no class declared in the middle of other code (#524); `static constexpr` constants in `UPPER_SNAKE_CASE`; root-relative includes; no anonymous namespaces |
| `platform/` drivers and targets | Hardware Targets and Drivers, Layering and Ownership | **chip registers only inside the chip's driver, by named field**; drivers named after the chip (`<peripheral><Chip>`, files `<peripheral>_<chip>.c`); instances in `hwconfig.h/.c`; pins in `pinmap.h` as `PORT, PIN`; no protocol policy in a driver; settings for a whole mode in `radio_setOpmode()` (#542) |
| `openrtx/src/rtx`, threads | Concurrency and Real-Time | see the correctness list above |
| `openrtx/src/protocols` | Layering and Ownership; CONTRIBUTING § Code Organization | OSI layers 1–3; demodulator knows only syncwords; reuse `fir.hpp`/`iir.hpp`/`Correlator.hpp` (#396) |
| UI | Layering and Ownership, String Table | UI never calls the radio API; UI-only state in the UI state; strings appended in every language |
| `#ifdef`, `CONFIG_` | Conditional Compilation | **don't wrap a whole module in `#ifdef CONFIG_*`** (a maintainer flagged an AI-written file for exactly this, #549); gate memory-costing fields; supply defaults instead of `#ifdef`s; `CONFIG_` names describe the capability (#442) |
| general helpers | Reuse Before Reimplementation | general code goes in `core/` (`utils.h`, `crc.h`, `datetime.h`) even with one caller (#552: `MIN`/`MAX`) |
| `meson.build`, tests | Testing | registration pattern; Catch2 only; assets by absolute path |
| new files | File Headers | SPDX header (the `reuse` job fails without it) |
| CHANGELOG, PR text, comments | Commits and PRs | short: one changelog line, a few plain lines of commit body; maintainers trim long text (#513) |

## Severity

| Tag | Meaning | Examples |
|---|---|---|
| **P0** | Blocks merge: harms users or radios, or CI fails | settings or string table reordered; TX out of band, over-deviation, `txDisable` bypassed; crash, hang or memory corruption on a reachable path; a target no longer builds; format/REUSE/strings job fails; code of unclear licence |
| **P1** | Must fix before merge | a real bug on a less common path; a rule maintainers send PRs back for (layering, register access, lazy init, typedefs, module wrapped in `#ifdef`); missing sign-off; testing section empty |
| **P2** | Should fix | smaller house-style rules, missing Doxygen, a helper that duplicates an existing one, commit split or message problems |
| **P3** | Nit | wording, naming preferences, optional simplifications |

Verdict, one of:

- **Ready for maintainer review** — no P0 or P1.
- **Needs changes** — any P0 or P1.
- **Split before review** — too large or mixing unrelated work to review usefully; give the proposed split (by module or feature, as in CONTRIBUTING.md § Splitting commits) instead of line findings.

## Human reviewer callouts

Facts a maintainer wants to see before merging, whatever the code quality. List only those that apply:

- **Persistent format:** `settings_t`, codeplug, NVM layout or calibration format changes.
- **RF behaviour:** frequencies, band limits, deviation, power, tones, timing on air.
- **On-air protocol:** wire format or spec interpretation changes.
- **Memory:** flash or RAM growth on a constrained target (compare `size` output if you built it).
- **New dependency or submodule bump:** `subprojects/`, `west.yml`, `requirements.txt`, Meson options.
- **Build or CI:** cross files, linker scripts, workflows.
- **Hardware coverage:** radios the change affects that nobody tested on.
- **AI-assisted:** commits carry `Co-authored-by` for an agent; per OpenRTX/OpenRTX#354 that is disclosed, not disqualifying.

## Report template

````markdown
# Review: <PR title or branch name>

<OpenRTX/OpenRTX#N link, or branch @ short SHA> against `<base>` @ <short SHA> · reviewed <date> · rules from AGENTS.md @ <base short SHA>

**Verdict: Needs changes** — <one or two sentences: what decides it>.

| P0 | P1 | P2 | P3 |
|---|---|---|---|
| 0 | 2 | 3 | 1 |

## Checks

| Check | Result |
|---|---|
| clang-format | ✅ pass |
| REUSE | ❌ `platform/drivers/foo/foo.c` has no SPDX header |
| Strings | ✅ pass |
| Linux build + tests | ✅ 41/41 |
| `openrtx_md3x0` | ⏭ not run: no ARM toolchain |
| Commits | ⚠️ 2 of 5 lack `Signed-off-by` |

## Findings

### [P1] Radio reconfigured from the UI thread — `openrtx/src/ui/default/ui.c:812`

`ui_foo()` calls `radio_setOpmode()` directly, racing the rtx thread that owns the radio. Set `sync_rtx` and let the rtx thread apply it. *AGENTS.md § Layering and Ownership (#335).*

### [P2] Magic number — `platform/drivers/foo/foo.c:120`

```suggestion
    for (size_t i = 0; i < ARRAY_SIZE(table); i++) {
```

*AGENTS.md § Reuse Before Reimplementation (#335, #429).*

## Questions for the author

- <things you suspect but could not verify>

## Already raised in the PR discussion

- <maintainer requests still open, with links; resolved ones are not repeated>

## Human reviewer callouts

- **RF behaviour:** …

## Not reviewed

- <files skipped (generated, vendored), checks not run, and why>
````

Rules for the report:

- Findings sorted by severity, then by file. Each title names the problem, not the fix; the location is a single line or a range of a few lines on the new side of the diff.
- One short paragraph per finding. Code snippets under three lines; ```` ```suggestion ```` only for literal replacement code, with the original indentation.
- Write file references as `path:line` so Plannotator and editors can follow them; link PRs and issues as the research skill does.
- No "great job" lines, no summary of what the diff does beyond one sentence — the author knows.
- If there are no findings, say so plainly and keep the checks table.

## Pending GitHub review

Only after the user has asked for it, for the findings they kept. A review created without an `event` stays **pending**: only the user sees it, and they submit it from the PR's *Files changed* tab.

```json
{
  "commit_id": "<headRefOid>",
  "body": "<optional summary, one or two lines>",
  "comments": [
    { "path": "platform/drivers/foo/foo.c", "line": 120, "side": "RIGHT",
      "body": "Use `ARRAY_SIZE(table)` instead of 16 here." },
    { "path": "openrtx/src/ui/default/ui.c", "start_line": 810, "line": 812, "side": "RIGHT",
      "body": "..." }
  ]
}
```

```sh
gh api repos/OpenRTX/OpenRTX/pulls/N/reviews --method POST --input review.json
```

- Show the user the JSON (or a readable rendering of it) first, and post only when they say so.
- Every `line` must be inside a diff hunk of that file at `commit_id`, or GitHub rejects the whole review (422); move a comment on unchanged code into `body`.
- A user can hold only one pending review per PR; if one exists the call fails — tell them to submit or discard it first.
- No `event` field, ever: `APPROVE`, `REQUEST_CHANGES` and `COMMENT` all publish at once.
- Give the user the PR link and tell them the review is waiting for them to submit.
