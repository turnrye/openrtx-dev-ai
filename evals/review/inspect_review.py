"""Inspect AI task: run /openrtx:review on each corpus case, sealed at its cutoff, and score it.

    uv run --with inspect-ai inspect eval evals/review/inspect_review.py --model none \
        -T limit=2 --max-samples 1

Task options (-T name=value): limit, ids (comma-separated case ids), plugin_dir (a
checkout of this plugin to test instead of the installed one), model (for the review
run), builds (let the review build and run tests; off by default), time_limit (seconds),
author (only that author's cases, or !name to leave them out).

Each sample gets its own folder under the openrtx cache (evals/review/runs): a local
origin with master as it stood at the cutoff and the PR's start head, a clone of it,
a copy of the research cache with everything after the cutoff removed, `gh` blocked,
and the run's stream-json transcript and findings.json.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time

from inspect_ai import Task, task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.scorer import SampleScore, Score, metric, scorer
from inspect_ai.solver import Generate, TaskState, solver
from inspect_ai.util import subprocess as async_subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

import score as sc  # noqa: E402
from openrtx import paths  # noqa: E402

CACHE = paths.cache_dir()
MIRROR = os.path.join(CACHE, "evals", "openrtx.git")
RUNS = os.path.join(CACHE, "evals", "review", "runs")
OPENRTX_BIN = os.path.join(os.path.dirname(os.path.dirname(HERE)), "bin", "openrtx")

PROMPT = """/openrtx:review branch origin/{base}

This is a non-interactive evaluation run: nobody can annotate, so print the full report \
instead of opening Plannotator. {builds}When you have finished, also write every finding to \
{findings} as a JSON list of objects with the keys path, line, end_line, severity (P0 to P3), \
title, body and suggestion (the replacement code, or null). Write [] if there are none."""

NO_BUILDS = "Don't run builds or tests; there is no toolchain here. "

# Tables in the GitHub cache and the column that dates each row.
DATED = {"comments": "created_at", "items": "created_at", "pr_events": "created_at",
         "pr_commits": "author_date", "versions": "observed_at", "search": "at"}


def git(*args, cwd=None, git_dir=None):
    """Run git and return stdout; raise on failure."""
    cmd = ["git"] + (["--git-dir", git_dir] if git_dir else []) + list(args)
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError("git %s: %s" % (" ".join(args), p.stderr.strip()))
    return p.stdout.strip()


def seal_cache(src, dst, cutoff, number):
    """Copy the research cache, dropping GitHub rows from the cutoff on and the PR's final state."""
    os.makedirs(dst)
    for name in os.listdir(src):
        if name.endswith(".sqlite"):
            shutil.copy2(os.path.join(src, name), os.path.join(dst, name))
    db = sqlite3.connect(os.path.join(dst, "github.sqlite"))
    have = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, col in DATED.items():
        if table in have:
            db.execute("DELETE FROM %s WHERE %s >= ?" % (table, col), (cutoff,))
    if "items" in have:
        db.execute("UPDATE items SET state='open', closed_at=NULL, merged_at=NULL WHERE closed_at >= ?",
                   (cutoff,))
    for table in ("pulls", "pr_files"):  # head, merge and file list as of the merge
        if table in have:
            db.execute("DELETE FROM %s WHERE repo='OpenRTX/OpenRTX' AND number=?" % table, (number,))
    db.commit()
    db.close()


def write_shims(bin_dir, cache):
    """`openrtx` pinned to the sealed cache with no refresh; `gh` refusing every call."""
    os.makedirs(bin_dir)
    with open(os.path.join(bin_dir, "openrtx"), "w") as f:
        f.write('#!/bin/bash\nexport OPENRTX_CACHE="%s"\ncase "$1" in\n'
                '  sync) echo "openrtx: sync is disabled here" >&2; exit 1;;\n'
                '  search|recent|show) exec "%s" "$@" --no-refresh;;\n'
                '  *) exec "%s" "$@";;\nesac\n' % (cache, OPENRTX_BIN, OPENRTX_BIN))
    with open(os.path.join(bin_dir, "gh"), "w") as f:
        f.write('#!/bin/bash\necho "gh: not available in this environment" >&2\nexit 1\n')
    for name in ("openrtx", "gh"):
        os.chmod(os.path.join(bin_dir, name), 0o755)


def prepare(case, root):
    """Build the sealed folder for one case and return (checkout, env)."""
    if os.path.exists(root):
        shutil.rmtree(root)
    os.makedirs(root)
    base = case["base_ref"]
    master = git("rev-list", "-1", "--first-parent", "--before=" + case["cutoff"], "master", git_dir=MIRROR)
    origin = os.path.join(root, "origin.git")
    git("init", "-q", "--bare", origin)
    refs = ["%s:refs/heads/master" % master, "%s:refs/heads/pr" % case["start"]["sha"]]
    if base != "master":
        refs.append("%s:refs/heads/%s" % (case["start"]["base"], base))
    git("push", "-q", origin, *refs, git_dir=MIRROR)
    checkout = os.path.join(root, "OpenRTX")
    git("clone", "-q", "-b", "pr", origin, checkout)
    seal_cache(CACHE, os.path.join(root, "cache"), case["cutoff"], case["number"])
    write_shims(os.path.join(root, "bin"), os.path.join(root, "cache"))
    env = dict(os.environ, PATH=os.path.join(root, "bin") + os.pathsep + os.environ["PATH"])
    return checkout, env


@solver
def sealed_review(plugin_dir=None, model="claude-opus-5-5", builds=False, time_limit=1800):
    """Run /openrtx:review headless on the case's start head and keep its findings."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        case = state.metadata["case"]
        root = os.path.join(RUNS, state.metadata["run"], "%s-e%d" % (case["id"], state.epoch))
        checkout, env = prepare(case, root)
        findings = os.path.join(root, "findings.json")
        prompt = PROMPT.format(base=case["base_ref"], findings=findings,
                               builds="" if builds else NO_BUILDS)
        cmd = ["claude", "-p", prompt, "--model", model,
               "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
               "--permission-mode", "acceptEdits", "--add-dir", root,
               "--allowedTools", "Bash", "Read", "Grep", "Glob", "Write", "Edit", "Skill", "Agent",
               "--disallowedTools", "WebFetch", "WebSearch",
               "--output-format", "stream-json", "--verbose"]
        if plugin_dir:
            cmd[1:1] = ["--plugin-dir", os.path.abspath(os.path.expanduser(plugin_dir))]
        started = time.time()
        res = await async_subprocess(cmd, cwd=checkout, env=env, timeout=time_limit)
        with open(os.path.join(root, "transcript.jsonl"), "w") as f:
            f.write(res.stdout or "")
        report, cost = "", None
        for line in (res.stdout or "").splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "result":
                report, cost = ev.get("result") or "", ev.get("total_cost_usd")
        try:
            found = json.load(open(findings))
        except (OSError, ValueError):
            found = None
        state.metadata.update(findings=found, report=report, cost=cost, root=root,
                              seconds=round(time.time() - started), transcript=res.stdout or "")
        state.output.completion = report
        return state

    return solve


def ratio(name, num, den):
    """A metric: sum of `num` over sum of `den` across samples (micro average)."""

    @metric(name=name)
    def _ratio():
        def compute(scores: list[SampleScore]) -> float:
            n = sum(s.score.value.get(num, 0) for s in scores)
            d = sum(s.score.value.get(den, 0) for s in scores)
            return n / d if d else 0.0
        return compute

    return _ratio()


def total(key):
    """A metric: the sum of one count across samples."""

    @metric(name="total_" + key)
    def _total():
        def compute(scores: list[SampleScore]) -> float:
            return float(sum(s.score.value.get(key, 0) for s in scores))
        return compute

    return _total()


@scorer(metrics=[ratio("recall_acted", "acted_hits", "acted"),
                 ratio("recall_round1", "target_hits", "targets"),
                 ratio("recall_acted_same_file", "acted_file_hits", "acted"),
                 ratio("precision_on_changed", "findings_on_changed", "findings_located"),
                 ratio("cases_flagged", "flagged_case", "samples"),
                 ratio("suggestions_landed", "suggestions_landed", "findings"),
                 total("findings"), total("leaked"), total("no_findings_file")])
def review_heuristics():
    """Score a run's findings against the maintainer's comments and the end state."""

    async def do_score(state: TaskState, target) -> Score:
        case, found = state.metadata["case"], state.metadata.get("findings")
        value = sc.score_case(MIRROR, case, found or [], state.metadata.get("transcript", ""))
        value.update(samples=1, no_findings_file=int(found is None))
        return Score(value=value, answer="%d findings" % len(found or []),
                     metadata={"cost": state.metadata.get("cost"), "seconds": state.metadata.get("seconds"),
                               "run_dir": state.metadata.get("root")})

    return do_score


@task
def review_corpus(limit: int = 0, ids: str = "", plugin_dir: str = "", model: str = "claude-opus-5-5",
                  builds: bool = False, time_limit: int = 1800, author: str = ""):
    """The review corpus as an Inspect task."""
    cases = json.load(open(os.path.join(HERE, "corpus.json")))["cases"]
    if ids:
        want = set(ids.split(","))
        cases = [c for c in cases if c["id"] in want]
    if author.startswith("!"):
        cases = [c for c in cases if c["author"] != author[1:]]
    elif author:
        cases = [c for c in cases if c["author"] == author]
    if limit:
        cases = cases[:limit]
    run = time.strftime("%Y%m%d-%H%M%S")
    samples = [Sample(input=c["title"], id=c["id"], metadata={"case": c, "run": run}) for c in cases]
    return Task(dataset=MemoryDataset(samples),
                solver=sealed_review(plugin_dir or None, model, builds, time_limit),
                scorer=review_heuristics(), time_limit=time_limit + 300)
