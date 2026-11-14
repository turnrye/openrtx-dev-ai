# Testing evidence

The PR template's *Testing performed* is required, and "a PR with this section empty will be sent back, because a reviewer cannot verify firmware behaviour by reading the diff". Maintainers ask "was the code tested on a real device?" (#523). The author's job is to produce the evidence and to be exact about what it covers: every item is **Passed**, **Failed** or **Not run** (with the reason), and only what ran in this session or has a link counts.

Collect it in this order: cheapest first, and each layer only where the change reaches.

## 1. What CI runs

Run the checks in `../review/reference.md` § Mechanical checks on the branch: formatting, REUSE, string tables, the Linux build and unit tests. CI also builds every target on every PR, so build locally each target whose code the change reaches (its `platform/targets/<T>`, the drivers it links, any `CONFIG_` it sets), with the commands in AGENTS.md § Build and Test. A failure here stops the stage: fix it before anything else.

## 2. Unit tests

New logic in `openrtx/` that can run on Linux needs a Catch2 test registered in `meson.build` (AGENTS.md § Testing). If the change adds or alters such logic without one, that's a gap to close now: propose the test cases, write them with the user's go-ahead, and check they fail without the change (revert the fix in a scratch worktree, run, restore) so they actually test it.

## 3. The emulator and the radios

- **Linux emulator** for UI, menus, settings and anything reachable without RF. Write down the steps and what you observed; the user runs it if you can't.
- **Radios.** Work out which of the template's targets the change affects. List each as tested (by whom, how), or not. Untested radios get the template's "Not tested on hardware — help wanted" box and the target named: CI builds a flashable binary for every target on every PR, so someone with the radio can help. That is an acceptable, honest state (the template says so); silence isn't.

## 4. OpenRTX Acceptance Testing (OAT)

[OAT](https://github.com/turnrye/openrtx-acceptance-testing) measures what a radio puts on air and plays, on a bench of SDRs: Robot Framework suites, with published runs at [turnrye.github.io/OpenRTX-Acceptance-Testing](https://turnrye.github.io/OpenRTX-Acceptance-Testing/). It turns "works on my radio" into numbers a reviewer can check.

### When it's needed

When the change can alter RF or audio behaviour: radio and baseband drivers (`radio_*`, `BK4819`, `AT1846S`, `HR_C6000`…), audio drivers, codecs and the DSP path, mic gain, M17 or APRS modulation and demodulation, OpModes, calibration, power, bandwidth, CTCSS, TX timing. Not for UI-only, NVM, documentation or build changes that don't touch those paths. If unsure, list what in the diff could change the radio's output and decide from that.

### Finding the OAT repository

Use the clone named by `oat.repo` in the openrtx config file (`~/.config/openrtx/config` on Linux; `%APPDATA%\openrtx\config` on Windows), else ask the user once whether they have one, else clone it read-only into the openrtx cache:

```sh
git clone --filter=blob:none https://github.com/turnrye/openrtx-acceptance-testing "<cache>/oat"
git -C "$OAT" fetch -q origin          # always read origin/main: a local main can be far behind
```

Read everything below from `origin/main` (`git show origin/main:<path>`, `git ls-tree origin/main <dir>`), never from a working tree that may be stale.

### What OAT can test

```sh
git -C "$OAT" ls-tree --name-only origin/main radios/       # radios with a radio file (ignore example, sim, *.local.*)
git -C "$OAT" ls-tree -r --name-only origin/main acceptance/ | grep '\.robot$'
git -C "$OAT" show origin/main:acceptance/fm/bandwidth.robot \
  | awk '/^\*\*\* Test Cases/{t=1;next} /^\*\*\*/{t=0} t&&/^[A-Za-z]/{print} t&&/\[Tags\]/{print "    " $0}'
```

The README's *The suites* table says what each suite covers; read it from `origin/main`. As of November 2026:

| Change reaches | Suite | Notes |
|---|---|---|
| FM TX deviation, bandwidth, CTCSS, power steps; FM after M17 | `fm/bandwidth.robot` | `--include tx` / `rx`; RX tests are partly `manual` |
| Mic and speaker path, pre-emphasis, SINAD, audio after idle | `fm/audio.robot` | `needs:audio` (an audio interface); SINAD sensitivity `needs:abs-level` |
| M17 modulator, deviation, symbol timing, preamble, Codec2 input | `m17/tx.robot` | the only suite that can't run on the simulated bench |
| M17 demodulator, decoding, RX audio | `m17/rx.robot` | mostly `needs:audio` |
| APRS receive, inbox | `aprs/rx.robot`; performance from the radio's TNC: `aprs/performance.robot` | `needs:tnc` |
| Every evidence session | `selftest-bench.robot` first | sets the bench's repeatability, the tolerance for comparing runs |

Radio files map to OpenRTX targets: `c62`, `c2`, `c2-gmrs` → the Retevis C62/C2 port; `cs7000p` → CS7000-PLUS; `mduv3x0` → MD-UV3x0. A target without a radio file has no OAT coverage.

### Is there a published run that corroborates the change?

List the published runs with their provenance:

```sh
cd "$OAT" && for f in $(git ls-tree -r --name-only origin/main results | grep measurements.json); do
  git show "origin/main:$f" | python3 -c "import json,sys; m=json.load(sys.stdin); print(sys.argv[1], '|', m.get('suite'), '|', m.get('firmware'), '|', m.get('hardware'), '|', str(m.get('dsp'))[:8], '|', m.get('suite_commit'), '|', m.get('started','')[:10])" "${f%/measurements.json}"
done
```

A run's folder is `results/<firmware>/<radio file>/<start>-<station>-<suite>/`; `<firmware>` is the commit after `-g` in a `git describe` label (`v0.4.5-88-g30c7570a` → `30c7570a`), or the label itself. Its log is `https://turnrye.github.io/OpenRTX-Acceptance-Testing/<folder>/log.html`. Pass and fail counts per test are in its `output.xml` (`git show origin/main:<folder>/output.xml | grep -o '<stat pass="[0-9]*" fail="[0-9]*"[^>]*>All Tests'`), and every measurement with its limit in `measurements.json` (`records`).

A run **corroborates** the change only if all of these hold. Check each and say which failed:

1. **Its firmware is this change.** The commit is the PR head, one of its commits, or an earlier pushed head of the PR:
   ```sh
   gh api graphql -f query='query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n){
     commits(last:100){nodes{commit{oid}}}
     timelineItems(itemTypes:[HEAD_REF_FORCE_PUSHED_EVENT],first:100){nodes{... on HeadRefForcePushedEvent{beforeCommit{oid} afterCommit{oid}}}}}}}' \
     -F o=OpenRTX -F r=OpenRTX -F n=N
   ```
   or, before a PR exists, a commit on the branch or in its reflog. If the commit can't be found anywhere — not in the clone (`git cat-file -e <sha>`), not on GitHub (`gh api repos/<fork>/commits/<sha>`) — nobody can tell what the run tested: say so, ask the user where that build came from, and don't count it.
2. **Nothing that matters changed since.** `git diff <run sha> HEAD -- <paths on the tested path>` is empty, or the user can show it's behaviour-neutral (same register writes, a pure rename). Otherwise the run is evidence for the older revision only; say which tests it no longer covers.
3. **The same radio and DSP image.** The radio file matches the target; for radios with a separately flashed DSP (`dsp_image: true`, the C62/C2), the run's `dsp` matches the image the PR ships or expects.
4. **The results support the claim.** The relevant tests passed, or each failure is explained — typically by a stock-firmware run on the same unit failing the same way (OpenRTX/OpenRTX#552's 12.5 kHz CTCSS level). Read limits against the run's `suite_commit`. With `calibration.method: assumed`, only relative claims hold (before vs after, not absolute dBm).
5. **It's published.** Every OAT link in the PR description resolves (`curl -s -o /dev/null -w '%{http_code}' <url>` gives 200). A run that exists only on the tester's machine is not evidence yet: it needs `scripts/export-run.sh` and a results PR on the OAT repo, which is a send-plan item.

Cite each corroborating run in *Testing performed* with its log link, the firmware commit, the unit, and the numbers that support the claim (a small table, as in OpenRTX/OpenRTX#552), plus a stock or before run for comparison where the claim is relative.

### No corroborating run: what to run

List the suites and tags the change needs, each with the command, for the user to run on their bench (you can't drive the radio). Start the session with `selftest-bench.robot`. For a claim relative to current behaviour, run the base build ("before") and the PR build ("after") in the same session on the same unit, and stock firmware too when the claim is "as stock does".

```sh
cd "$OAT"
FW=$(git -C <OpenRTX clone> describe --tags --always <commit flashed>)
uv run robot -A bench/<bench>.local.args -v "FIRMWARE:$FW" -v "HARDWARE:<model> <serial>" \
    [-v DSP:<image sha256 prefix>] -d runs/<name> [--include tx] [--exclude needs:audio] \
    acceptance/<suite>.robot
uv run python -m oat.compare runs/<before> runs/<after>          # the table for the PR
scripts/export-run.sh runs/<after>                                 # to publish it
```

Before the run, push the exact commit being flashed somewhere that will keep it — the PR branch, or a tag on the fork (`git push <fork> <sha>:refs/tags/oat/<sha>`, a send-plan item) — so check 1 can pass later; runs of commits that were never pushed can't be matched to anything. Add `-A bench/sdr-only.args` on a bench without an audio interface, so the run records that its audio tests were left out on purpose. The README on `origin/main` is authoritative for options; check it before handing over the commands.

### Test gaps to close first

Compare what the PR claims and changes, sentence by sentence, with the test names of the suites above. A gap is behaviour the change alters that no test measures, a target without a radio file, or a test the user's bench can't run (`needs:abs-level` with an assumed calibration, `needs:audio` without an interface). For each, say:

- what is uncovered and why it matters for this change;
- where a test would go (an existing suite or a new one) and what it would measure, with its limit's source (the M17 spec, TIA-603, stock firmware on the same unit);
- that it is an OAT change, landed there first in its own PR, before the evidence run — or, if the user decides to defer it, that *Testing performed* names the gap.

Offer to draft the OAT test; don't write it unasked.

## Writing it up

*Testing performed* lists each item with its status and link; the template's *Tested on* boxes match exactly the radios that were tested; anything not run says why and what would run it. The send plan repeats this as its readiness table.
