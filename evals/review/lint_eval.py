#!/usr/bin/env python3
"""Score the review skill's lint.py against the corpus: what the tool catches, for free.

    python3 evals/review/lint_eval.py --lint PATH/TO/skills/review/lint.py [--clang-tidy] [--split F] [--show]

Runs lint.py on each case's start (fork point..start head) and compares its findings
with the maintainer's round-one inline comments, using the review scorer's spot match
(same file, within 5 lines). A finding makes the *same point* when its rule matches what
the comment asks for (RULE_TOPICS); "Same as above" and "ditto" take the topic of the
comment before them. No model is involved, so this can run on every change to lint.py.
"""

import argparse
import collections
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

import score as sc  # noqa: E402
from openrtx import paths  # noqa: E402

MIRROR = os.path.join(paths.cache_dir(), "evals", "openrtx.git")

# What a maintainer comment says when it asks for what a rule checks.
RULE_TOPICS = {
    "trailing-comma": r"comma",
    "typedef": r"typedef",
    "array-size": r"ARRAY_SIZE|sizeof",
    "std-string": r"string",
    "static-constexpr": r"constexpr|static const",
    "module-prefix": r"prefix|\bstatic\b",
    "file-local": r"\bstatic\b",
    "layering": r"radio API|rtx thread|sync_rtx|mutex|ui_updateFSM|already called|display driver|re-?initiali",
    "unused": r"not used|unused|remove (them|it|this|these)|not needed",
    "doxygen": r"doxygen|document|comment",
    "static-local": r"static",
    "switch": r"switch",
    "define-location": r"macro|beginning of the|#define",
    "parentheses": r"parenthes",
    "std-array": r"std::array",
    "hw-literal": r"hardware|VHF|UHF|band|initiali[sz]ation value",
    "script-location": r"scripts",
    "changelog": r"short|cut",
    "redundant-guard": r"no need|not needed|free\(NULL\)|man free|no-op|NULL",
    "cpp-class": r"bottom of the class|above the|order|constructor|destructor|private|public|move (these|this|it)",
    "clang-tidy:readability-redundant-casting": r"cast",
    "clang-tidy:readability-make-member-function-const": r"\bconst\b",
    "append-only": r"bottom|end of the|append|voice prompt|move (this|it)",
    "clang-tidy:readability-simplify-boolean-expr": r"simplif|boils down|return (true|false)|== ?(true|false)|const variables",
    "clang-tidy:misc-use-internal-linkage": r"\bstatic\b",
    "clang-tidy:modernize-use-designated-initializers": r"designated|fully-qualified|member names",
    "clang-tidy:bugprone-switch-missing-default-case": r"switch|default",
    "clang-tidy:misc-redundant-expression": r"redundant|>> ?0|not needed|same",
    "clang-tidy:readability-redundant-control-flow": r"redundant|not needed",
    "clang-tidy:readability-redundant-declaration": r"redundant|already declared|duplicate",
}
DITTO = re.compile(r"^\W*(same (as above|here|for this)|ditto|also here|as above)\b", re.I)


def load_lint(path):
    """Import lint.py from the skill checkout under test."""
    spec = importlib.util.spec_from_file_location("lint", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def topics(case):
    """Each target's body for topic matching, with "Same as above" resolved to the comment before it."""
    out, prev = {}, ""
    for f in case["feedback"]:
        if f["kind"] != "inline":
            continue
        body = f["body"] or ""
        if DITTO.match(body) and prev:
            body = prev + "\n" + body
        out[f["id"]] = body
        prev = body
    return out


def same_point(f, x, bodies):
    """A lint finding at the comment's spot whose rule matches what the comment asks for."""
    pattern = RULE_TOPICS.get(x["rule"])
    return bool(pattern and sc.hits(f, x) and re.search(pattern, bodies.get(f["id"], ""), re.I))


def tree_at(sha):
    """A temporary checkout of a commit (files only), for clang-tidy."""
    d = tempfile.mkdtemp(prefix="lint-eval-")
    archive = subprocess.run(["git", "-C", MIRROR, "archive", sha], capture_output=True)
    subprocess.run(["tar", "-x", "-C", d], input=archive.stdout, check=True)
    return d


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--lint", required=True, help="path to skills/review/lint.py in the checkout under test")
    p.add_argument("--clang-tidy", action="store_true")
    p.add_argument("--split", help="a file under splits/ to limit the cases")
    p.add_argument("--corpus", default="corpus.json", help="corpus file under evals/review (e.g. corpus-heldout-b.json)")
    p.add_argument("--show", action="store_true", help="print every finding and target")
    p.add_argument("--json", help="also write per-case results here")
    a = p.parse_args()

    lint = load_lint(a.lint)
    cases = json.load(open(os.path.join(HERE, a.corpus)))["cases"]
    if a.split:
        path = a.split if os.path.isabs(a.split) else os.path.join(HERE, "splits", a.split)
        want = {l.split("#")[0].strip() for l in open(path)} - {""}
        cases = [c for c in cases if c["id"] in want]

    per_rule = collections.defaultdict(lambda: collections.Counter())
    caught, results = [], []
    totals = collections.Counter()
    for c in cases:
        tree = tree_at(c["start"]["sha"]) if a.clang_tidy else None
        try:
            found = lint.lint(MIRROR, c["start"]["base"], c["start"]["sha"], a.clang_tidy, tree)
        finally:
            if tree:
                shutil.rmtree(tree, ignore_errors=True)
        bodies = topics(c)
        tg = sc.targets(c)
        changed = sc.on_changed_lines(MIRROR, c, found)
        for x, ch in zip(found, changed):
            r = per_rule[x["rule"]]
            r["findings"] += 1
            r["on_changed"] += int(bool(ch))
            r["at_a_comment"] += int(any(sc.hits(f, x) for f in tg))
            r["same_point"] += int(any(same_point(f, x, bodies) for f in tg))
        hit = [f for f in tg if any(same_point(f, x, bodies) for x in found)]
        totals["targets"] += len(tg)
        totals["caught"] += len(hit)
        totals["findings"] += len(found)
        for f in hit:
            caught.append("%s %s:%s %s" % (c["id"], f["path"].split("/")[-1], f["line"],
                                          re.sub(r"\s+", " ", f["body"])[:90]))
        results.append({"id": c["id"], "findings": found, "caught": [f["id"] for f in hit]})
        if a.show and (found or hit):
            print("== %s: %d findings, caught %d of %d" % (c["id"], len(found), len(hit), len(tg)))
            for x in found:
                print("   %s:%s %s" % (x["path"].split("/")[-1], x["line"], x["rule"]))

    print("%d cases: %d findings; caught %d of %d round-one maintainer comments (%.1f%%)"
          % (len(cases), totals["findings"], totals["caught"], totals["targets"],
             100.0 * totals["caught"] / max(totals["targets"], 1)))
    print("\n%-52s %8s %10s %12s %11s" % ("rule", "findings", "same point", "at a comment", "on changed"))
    for rule, r in sorted(per_rule.items(), key=lambda kv: -kv[1]["findings"]):
        print("%-52s %8d %10d %12d %11d" % (rule, r["findings"], r["same_point"], r["at_a_comment"], r["on_changed"]))
    print("\ncaught:")
    for line in caught:
        print("  " + line)
    if a.json:
        json.dump(results, open(a.json, "w"), indent=1)


if __name__ == "__main__":
    main()
