"""Heuristic scoring of review findings against a corpus case. Standard library only."""

import re
import subprocess

WINDOW = 5    # a finding within this many lines of a comment catches it
TOUCHED = 2   # a finding's lines count as changed if the diff comes this close
LEAK_MIN = 60  # a maintainer sentence this long, quoted verbatim, means the run saw the thread


def norm_path(p):
    """Repo-relative path without ./ or a leading slash."""
    p = (p or "").strip()
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def targets(case):
    """Round-one inline comments made on the start head, the ones a review of start can catch."""
    start = case["start"]["sha"]
    return [f for f in case["feedback"]
            if f["kind"] == "inline" and not f["trivial"] and f["commit"] == start and f.get("line")]


def acted(f):
    """True when the comment's lines changed or the file went away by the end."""
    return f.get("outcome") in ("changed", "file_gone")


def hits(f, finding, window=WINDOW):
    """True when a finding lands on the same file within `window` lines of a comment."""
    if norm_path(finding.get("path")) != norm_path(f["path"]):
        return False
    try:
        lo = int(finding.get("line"))
    except (TypeError, ValueError):
        return False
    hi = int(finding.get("end_line") or lo)
    c_lo = (f.get("start_line") or f["line"]) - window
    c_hi = f["line"] + window
    return lo <= c_hi and hi >= c_lo


def same_file(f, finding):
    """True when a finding is anywhere in the comment's file."""
    return norm_path(finding.get("path")) == norm_path(f["path"])


def changed_lines(work, start, end, path):
    """Old-side line ranges of start..end that changed in a file, as (first, last) pairs."""
    p = subprocess.run(["git", "--git-dir", work, "diff", "-U0", start, end, "--", path],
                       capture_output=True, text=True)
    out = []
    for m in re.finditer(r"^@@ -(\d+)(?:,(\d+))? ", p.stdout, re.M):
        first, count = int(m.group(1)), int(m.group(2) or 1)
        out.append((first, first + max(count, 1) - 1))
    return out


def squash(text):
    """Lower-case text with runs of whitespace collapsed, for verbatim matching."""
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def leaked(case, transcript):
    """True when the run quoted one of the maintainer's comments verbatim."""
    t = squash(transcript)
    for f in case["feedback"]:
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", f["body"] or ""):
            s = squash(sentence)
            if len(s) >= LEAK_MIN and s in t:
                return True
    return False


def suggestion_landed(work, case, finding):
    """True when a finding's suggested code is in the end version of its file."""
    code = squash(finding.get("suggestion"))
    if len(code) < 10:
        return False
    p = subprocess.run(["git", "--git-dir", work, "show", "%s:%s" % (case["end"]["sha"], norm_path(finding.get("path")))],
                       capture_output=True, text=True)
    return not p.returncode and code in squash(p.stdout)


def score_case(work, case, findings, transcript=""):
    """Counts for one case; metrics sum them across cases so recall is micro-averaged."""
    tg = targets(case)
    act = [f for f in tg if acted(f)]
    cache = {}
    on_changed = 0
    for x in findings:
        path = norm_path(x.get("path"))
        try:
            line = int(x.get("line"))
        except (TypeError, ValueError):
            continue
        if path not in cache:
            cache[path] = changed_lines(work, case["start"]["sha"], case["end"]["sha"], path)
        if any(a - TOUCHED <= line <= b + TOUCHED for a, b in cache[path]):
            on_changed += 1
    return {
        "targets": len(tg),
        "target_hits": sum(any(hits(f, x) for x in findings) for f in tg),
        "acted": len(act),
        "acted_hits": sum(any(hits(f, x) for x in findings) for f in act),
        "acted_file_hits": sum(any(same_file(f, x) for x in findings) for f in act),
        "findings": len(findings),
        "findings_located": sum(1 for x in findings if x.get("path") and x.get("line")),
        "findings_on_changed": on_changed,
        "serious": sum(1 for x in findings if str(x.get("severity", "")).upper() in ("P0", "P1", "P2")),
        "flagged_case": int(any(str(x.get("severity", "")).upper() in ("P0", "P1", "P2") for x in findings)),
        "suggestions_landed": sum(1 for x in findings if suggestion_landed(work, case, x)),
        "leaked": int(leaked(case, transcript)),
    }
