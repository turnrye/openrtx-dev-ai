#!/usr/bin/env python3
"""Score lint.py's PR-level checks (commits, PR text, persistence, build) against the corpus.

    python3 evals/review/pr_eval.py --lint PATH/TO/skills/review/lint.py [--show]

These checks have no line to match, so they are scored per maintainer comment: a comment
of any kind (inline, review, conversation) that raises a rule's topic (RULE_TOPICS) is
*caught* when the rule fires on the head the comment was made on. The PR body is the final
one from the raw API data, so the PR-text rules see later edits too.

  recall     share of on-topic comments whose rule fired on their head
  confirmed  share of findings on the start head in a case where the maintainer raised that
             topic at some point (a lower bound on precision: much gets fixed silently)
"""

import argparse
import collections
import importlib.util
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from openrtx import paths  # noqa: E402

MIRROR = os.path.join(paths.cache_dir(), "evals", "openrtx.git")
RAW = os.path.join(paths.cache_dir(), "evals", "review", "raw")

# What a maintainer comment says when it asks for what a PR-level rule checks.
RULE_TOPICS = {
    "commit-fixup": r"fixup|squash|fold .* into|turn commit|into (a single|one) commit|rewrite .*history",
    "commit-autosquash": r"autosquash|squash",
    "commit-merge": r"merge commit|rebase",
    "commit-signoff": r"sign(ed)?[- ]?off|\bDCO\b",
    "commit-prefix": r"commit (message|title|subject)|prefix",
    "commit-mixed-format": r"clang-format|formatting|whitespace|indent",
    "pr-testing": r"\btest(ed|ing)\b|tested on",
    "pr-changelog": r"changelog",
    "persistence": r"existing settings|saved settings|settings_t|persistent|codeplug|break(s)? .*settings|migrat",
    "build-unlisted": r"meson|cmake|not (compiled|built)",
    "config-undefined": r"CONFIG_\w+",
}


def load_lint(path):
    spec = importlib.util.spec_from_file_location("lint", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def is_ancestor(a, b):
    return subprocess.run(["git", "-C", MIRROR, "merge-base", "--is-ancestor", a, b]).returncode == 0


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--lint", required=True)
    p.add_argument("--show", action="store_true")
    a = p.parse_args()
    lint = load_lint(a.lint)
    cases = json.load(open(os.path.join(HERE, "corpus.json")))["cases"]

    cache = {}

    def run(case, head, body):
        base = case["start"]["base"] if is_ancestor(case["start"]["base"], head) else case["end"]["base"]
        if (base, head) not in cache:
            cache[(base, head)] = lint.pr_checks(MIRROR, base, head, body)
        return cache[(base, head)]

    relevant, caught = collections.Counter(), collections.Counter()
    findings, confirmed = collections.Counter(), collections.Counter()
    hits = []
    for c in cases:
        raw_path = os.path.join(RAW, "pr-%d.json" % c["number"])
        body = json.load(open(raw_path))["pull"].get("body") if os.path.exists(raw_path) else None
        raised = set()
        for f in c["feedback"]:
            if f["trivial"]:
                continue
            text = re.sub(r"```.*?```", " ", f["body"] or "", flags=re.S)
            topics = {r for r, pat in RULE_TOPICS.items() if re.search(pat, text, re.I)}
            raised |= topics
            if not topics or not f.get("commit"):
                continue
            fired = {x["rule"] for x in run(c, f["commit"], body)}
            for r in topics:
                relevant[r] += 1
                if r in fired:
                    caught[r] += 1
                    hits.append("%s %s [%s] %s" % (r, c["id"], f["kind"], re.sub(r"\s+", " ", text)[:100]))
        start = run(c, c["start"]["sha"], body)
        for x in start:
            findings[x["rule"]] += 1
            confirmed[x["rule"]] += int(x["rule"] in raised)
        if a.show and start:
            print("== %s" % c["id"])
            for x in start:
                print("   %-20s %s" % (x["rule"], x["title"]))

    print("\n%-20s %9s %8s %8s %10s" % ("rule", "comments", "caught", "findings", "confirmed"))
    for r in RULE_TOPICS:
        print("%-20s %9d %8d %8d %10d" % (r, relevant[r], caught[r], findings[r], confirmed[r]))
    print("\ncaught:")
    for h in hits:
        print("  " + h)


if __name__ == "__main__":
    main()
