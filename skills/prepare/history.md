# Shaping the history

What the maintainers ask for, from CONTRIBUTING.md § Making Changes and AGENTS.md § Commits and PRs: one commit per module or logical addition, each building, prefixed with its module, in the imperative, short; reformatting in its own commit; nothing added in one commit and moved in another; review feedback as `fixup!` commits, squashed only once review settles. The `master` history is the reference for granularity and wording (`git log --format='%h %s' origin/master -30`).

Every recipe here rewrites history. Before each one, record the tip (`git update-ref refs/openrtx-prepare/<branch>/<stamp> HEAD`) and afterwards prove the result (see [Proving a rewrite](#proving-a-rewrite)). You can't run an interactive editor, so every rebase below is driven through `GIT_SEQUENCE_EDITOR`.

## Reading the branch

```sh
git log --reverse --format='%h %s' $BASE..HEAD
git log --reverse --stat --format='--- %h %s' $BASE..HEAD   # which modules each commit touches
git diff --stat $BASE...HEAD
git log --merges --oneline $BASE..HEAD                      # must be empty; rebase, never merge the base in
```

Problems to look for, each one a request maintainers have made:

| Problem | Example request |
|---|---|
| A commit spanning modules (`openrtx/src/ui` and `platform/drivers`) | "split it in two" (#531, #543) |
| Several commits that are one change, or "fix typo"/"Fix …" commits | "squash the two commits in a single one" (#564, #538) |
| clang-format reformatting mixed with behaviour | "separate the clang-format changes to a separate commit" (#442, #427) |
| Something added, then moved or renamed in a later commit | (#494) |
| A long or wordy message | "make the commit message a bit more concise" (#564, #513) |
| A commit that doesn't build | "make each commit bisectable" (#498) — a preference, not a gate, during bring-up |
| Vendored libraries or generated files committed | (#415) |

## Proposing the split

Group the changed files by module, using AGENTS.md's prefixes and the master history's:

| Path | Usual prefix |
|---|---|
| `openrtx/src/ui`, `openrtx/include/ui` | `ui:` |
| `openrtx/src/protocols/M17`, M17 OpMode | `m17:` (or `M17:`, as master writes it) |
| `openrtx/src/rtx` | `rtx:` |
| `openrtx/src/core`, `openrtx/include/core` | `core:` |
| `platform/drivers/...` | `drivers:` (master often adds the driver: `drivers: gpio: …`) |
| `platform/targets/<T>`, a target's bring-up | the target (`c62:`, `cs7000:`) |
| NVM, settings storage | `nvm:` / `nvmem:` |
| `tests/` | `test:` |
| `meson.build`, cross files, `west.yml` | `build:` |
| `scripts/` | `scripts:` |
| `.github/` | `ci:` |

Order the commits so each builds on what comes before: shared code (`core:`, drivers) before its users, the feature before its UI, tests with or right after what they test. For a new target, the maintainers' own split on #498 is the model: MCU glue, platform support, display and backlight, keyboard, GPS. Show the proposed list of commits with the files in each and wait for the user before rewriting.

## Recipes

**A custom rebase plan.** Write the todo yourself, then hand it to git:

```sh
cat > "$TMP/todo" <<'EOF'
pick 1a2b3c4 core: add the thing
edit 5d6e7f8 drivers: add the thing and use it in the UI
pick 9a8b7c6 test: cover the thing
EOF
GIT_SEQUENCE_EDITOR="cp $TMP/todo" git rebase -i $BASE
```

**Splitting a commit by path** (at an `edit` stop):

```sh
git reset HEAD~                                   # the commit's changes are back in the tree, unstaged
git add -- platform/drivers/thing/                # first half, by path
git commit -m "drivers: add the thing" -m "…"     # plus the Co-authored-by trailer if you wrote it
git add -- openrtx/src/ui/
git commit -m "ui: show the thing"
git status --short                                # nothing of the commit left over
git rebase --continue
```

Splitting hunks inside one file: write the full diff (`git diff > "$TMP/all.patch"`), cut it to the hunks for the first commit, `git apply --cached` that, commit, then add the rest. Check `git diff` is empty before continuing.

**Squashing.** Mark the later commits `fixup` (keep the first message) or `squash` in the todo. To reword at the same time, `fixup -C <sha>` takes that commit's message.

**Rewording only.** `git commit --fixup=reword:<sha>` creates an `amend!` commit, folded in by the next autosquash.

**Separating reformatting.** For a commit that both reformats an existing file and changes it, at an `edit` stop:

```sh
git reset HEAD~
cp path/to/file.c "$TMP/file.c.new"               # the commit's version
git checkout -- path/to/file.c                     # back to the original
clang-format -i path/to/file.c                     # format only; also drop it from clang_format.sh's exclusion list
git add -- path/to/file.c scripts/clang_format.sh
git commit -m "<module>: format <file>"
cp "$TMP/file.c.new" path/to/file.c && clang-format -i path/to/file.c
git add -- path/to/file.c <the commit's other files>
git commit -C ORIG_HEAD                              # the behaviour change, with the original message
git status --short                                   # nothing of the commit left over
```

The formatting commit goes first, so the behaviour commit's diff shows only behaviour. New files are formatted fully in the commit that adds them.

**Building every commit.**

```sh
git rebase -x 'meson compile -C build_prepare openrtx_linux' $BASE
```

Use the build of a target the branch touches as well (`meson compile -C build_prepare_cm4 openrtx_md3x0`); the C62 builds with west in its container, as its branch's README says. This doesn't change any commit, so it needs no backup, but report which commits fail.

**Conflicts.** Resolve only conflicts whose right answer is obvious from both sides (an import list, adjacent edits). Otherwise `git rebase --abort`, which returns the branch exactly as it was, and show the user the conflict. Never resolve by dropping either side's change.

## Fixups

During review, every change goes into a `fixup!` of the commit it corrects, so the reviewer sees what changed and the merged history shows no rework (#396, #429):

```sh
git log --format='%h %s' $BASE..HEAD -- path/to/file.c     # candidate targets
git blame -s $BASE..HEAD -- path/to/file.c                 # which commit wrote the line under review
git add -- path/to/file.c
git commit --fixup=<target sha>
```

`git absorb`, when installed, does the targeting itself (`git absorb --base $BASE`); check what it made. Don't autosquash during review: maintainers read the fixups (CONTRIBUTING.md § Responding to review).

### Fixups of base commits

On a long-running base such as `Retevis-c62`, a fix to a commit *already on the base* is a `fixup!` of that base commit, not a new commit (#537, #541). Its subject must be exactly `fixup! <base commit's subject>`: autosquash matches on it, and an autosquash in the PR's own range leaves it alone because its target is outside the range. The maintainer folds them in when squashing the base. A retitle of a base commit is an `amend!` commit (`git commit --fixup=amend:<base sha>`). Say in the PR description which base commits the fixups correct, and that they may not build one by one before squashing, if so (OpenRTX/OpenRTX#552 is an example).

## Finishing

**"Squash the fixups and I'll merge"** (#528, #527, #389):

```sh
GIT_SEQUENCE_EDITOR=true git rebase -i --autosquash $BASE
git log --format='%h %s' $BASE..HEAD | grep -E ' (fixup|amend|squash)!' # only base-commit fixups may remain
git diff <backup> HEAD                                                  # must be empty
```

Then build every commit, and push with the lease after approval.

**The base moved under the PR.** Maintainers rebase or autosquash long-running bases and force-push them (#492, #427; `Retevis-c62` is regularly squashed). Find the base tip the branch was built on — `git merge-base <backup> <old base ref>`, where the old base ref is in `git reflog <remote>/<base>` or was recorded in the backup — then:

```sh
git fetch <upstream> <base>
git rebase --onto <upstream>/<base> <old base tip> <branch>
git range-diff <old base tip>..<backup> <upstream>/<base>..HEAD
```

Fixups whose targets the maintainer already folded become empty and are dropped; check range-diff for any that weren't. Re-run the checks and say which evidence is now stale because the base changed under it.

## Proving a rewrite

```sh
git diff <backup> HEAD                         # empty for a pure reshape (split, squash, reword, reorder)
git range-diff $BASE <backup> HEAD             # commit by commit, what moved where
git log --format='%h %s%n%(trailers:key=Co-authored-by,valueonly)' $BASE..HEAD
```

A non-empty diff after a reshape means something was lost or duplicated: restore from the backup (`git reset --hard <backup>` — the one time `reset --hard` is right, and only on the branch you rewrote) and start again. Put the range-diff in the send plan so the user sees exactly what the force-push will replace.
