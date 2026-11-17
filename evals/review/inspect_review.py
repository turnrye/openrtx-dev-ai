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
from inspect_ai.log import HeadlineMetric
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


def over_cases(key, stat="mean"):
    """A metric over the cases where a per-case value applies (Inspect's mean() counts None as 0).

    stat is "mean", "stderr" (of that mean) or "cases" (how many cases it is over).
    """

    @metric(name=key if stat == "mean" else "%s_%s" % (key, stat))
    def _metric():
        def compute(scores: list[SampleScore]) -> float:
            v = [float(x) for s in scores if (x := (s.score.value or {}).get(key)) is not None]
            if stat == "cases":
                return float(len(v))
            if not v or (stat == "stderr" and len(v) < 2):
                return float("nan")
            mu = sum(v) / len(v)
            return mu if stat == "mean" else (sum((x - mu) ** 2 for x in v) / (len(v) - 1) / len(v)) ** 0.5
        return compute

    return _metric()


async def judge(case, code, model, cwd):
    """Ask a model which findings make the same point as the maintainer's comments.

    Only comments with findings at their spot are asked about. Runs through the claude CLI
    so it needs no API key. Returns (judged, raw answer).
    """
    pairs = sc.spot_pairs(case, code)
    prompt = sc.judge_prompt(pairs, code)
    if not prompt:
        return {}, None
    res = await async_subprocess(["claude", "-p", prompt, "--model", model, "--output-format", "json",
                                  "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--tools", ""],
                                 cwd=cwd, timeout=300)
    try:
        answer = json.loads(res.stdout or "{}").get("result", "")
    except ValueError:
        answer = res.stdout or res.stderr
    return sc.parse_judgement(answer, pairs), answer


@scorer(metrics=[over_cases("catch_rate"), over_cases("catch_rate", "stderr"), over_cases("catch_rate", "cases"),
                 over_cases("catch_rate_spot"), over_cases("catch_rate_file"),
                 over_cases("precision"), over_cases("precision", "stderr"),
                 over_cases("flagged"), over_cases("code_findings"), over_cases("meta_findings"),
                 over_cases("leaked")])
def review_vs_maintainer(judge_model="claude-haiku-5-5"):
    """Compare a run's findings with the maintainer's comments and with what changed before merge.

    The value holds per-case rates (0..1, None where they don't apply); the run's metrics
    are their means across cases. The explanation lists each maintainer comment with what
    the review did about it. Raw counts and the judge's answer are in the metadata.
    """

    async def do_score(state: TaskState, target) -> Score:
        case, found = state.metadata["case"], state.metadata.get("findings")
        code = [x for x in (found or []) if not sc.is_meta(x)]
        judged, answer = await judge(case, code, judge_model, state.metadata.get("root") or HERE)
        counts, rows, flagged_code = sc.assess(MIRROR, case, found or [], state.metadata.get("transcript", ""),
                                               judged)
        meta = [x for x in (found or []) if sc.is_meta(x)]
        explanation = sc.explain(case, counts, rows, flagged_code, meta)
        if found is None:
            explanation = "**The run wrote no findings file**; it is scored as finding nothing.\n\n" + explanation
        caught = counts["acted_point_hits"]
        return Score(value=sc.rates(counts),
                     answer="caught %d of %d acted-on comments; %d code findings" % (caught, counts["acted"],
                                                                                     counts["code_findings"]),
                     explanation=explanation,
                     metadata=dict(counts, cost=state.metadata.get("cost"), seconds=state.metadata.get("seconds"),
                                   run_dir=state.metadata.get("root"), no_findings_file=found is None,
                                   judge_answer=answer))

    return do_score


@task
def review_corpus(limit: int = 0, ids: str | list[str] = "", plugin_dir: str = "", model: str = "claude-opus-5-5",
                  builds: bool = False, time_limit: int = 1800, author: str = ""):
    """The review corpus as an Inspect task."""
    cases = json.load(open(os.path.join(HERE, "corpus.json")))["cases"]
    if ids:
        # the inspect CLI turns `-T ids=a,b` into a list; the Python API passes the string
        want = set(ids.split(",") if isinstance(ids, str) else ids)
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
                scorer=review_vs_maintainer(), time_limit=time_limit + 300,
                headline_metric=HeadlineMetric(metric="catch_rate"))
