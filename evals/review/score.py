"""Scoring of review findings against a corpus case. Standard library only.

A finding is at the same *spot* as a maintainer comment when it is in the same file within
WINDOW lines. Whether it makes the same *point* is a judgement call, so a model makes it:
`judge_prompt` lists each comment with the findings at its spot, `parse_judgement` reads
the answer, and `assess` takes the result. Everything else here is deterministic.
Findings about the PR text rather than the code are counted apart.
"""

import json
import re
import subprocess

WINDOW = 5     # a finding within this many lines of a comment is at its spot
TOUCHED = 2    # a finding's lines count as changed if the diff comes this close
LEAK_MIN = 60  # a maintainer sentence this long, quoted verbatim, means the run saw the thread

# Findings about the PR or its commits, not the code.
META = re.compile(r"\b(pr|pull request)\b|description|testing performed|changelog|"
                  r"commit (message|body|bodies|subject|summar)|signed-off|sign-off|co-authored|template", re.I)

JUDGE = """You compare an automated code review with a maintainer's review of the same change.
For each maintainer comment below, decide whether one of the listed findings makes the same
point: it asks for the same change or names the same problem, even in other words. A finding
about a different problem in the same lines does not count.

{items}

Answer with JSON only, one entry per comment: [{{"comment": <number>, "finding": <letter or null>}}]"""


def norm_path(p):
    """Repo-relative path without ./ or a leading slash."""
    p = (p or "").strip()
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def targets(case):
    """Round-one inline comments made on the start head, the ones a review of start can catch.

    Replies in a thread ("Ow, sorry!", answers to the author) follow up on a discussion
    rather than raise something new, so they aren't targets.
    """
    start = case["start"]["sha"]
    return [f for f in case["feedback"]
            if f["kind"] == "inline" and not f["trivial"] and f["commit"] == start and f.get("line")
            and not f.get("prior") and not f.get("in_reply_to")]


def acted(f):
    """True when the comment's lines changed or the file went away by the end."""
    return f.get("outcome") in ("changed", "file_gone")


def is_meta(finding):
    """True for a finding about the PR text or commits rather than the code."""
    return not finding.get("path") or bool(META.search(finding.get("title") or ""))


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


def spot_pairs(case, code):
    """For each target, the code findings at its spot (indexes into `code`)."""
    return [(f, [i for i, x in enumerate(code) if hits(f, x)]) for f in targets(case)]


def judge_prompt(pairs, code):
    """The judge's prompt for the targets that have findings at their spot; None if there are none."""
    items = []
    for n, (f, idx) in enumerate(pairs, 1):
        if not idx:
            continue
        lines = ["Comment %d on %s:%s:\n%s\nFindings:" % (n, f["path"], f["line"], (f["body"] or "").strip())]
        for k, i in enumerate(idx):
            x = code[i]
            lines.append("  %s) %s:%s %s — %s" % ("abcdefghijklmnopqrstuvwxyz"[k], x.get("path"), x.get("line"),
                                                  x.get("title") or "", (x.get("body") or "")[:600]))
        items.append("\n".join(lines))
    return JUDGE.format(items="\n\n".join(items)) if items else None


def parse_judgement(text, pairs):
    """Map target number -> index into `code` of the finding judged to make its point."""
    m = re.search(r"\[.*\]", text or "", re.S)
    try:
        answers = json.loads(m.group(0)) if m else []
    except ValueError:
        return {}
    out = {}
    for a in answers if isinstance(answers, list) else []:
        try:
            n, letter = int(a.get("comment")), a.get("finding")
        except (AttributeError, TypeError, ValueError):
            continue
        if not letter or not 1 <= n <= len(pairs):
            continue
        k = "abcdefghijklmnopqrstuvwxyz".find(str(letter).strip().lower()[:1])
        idx = pairs[n - 1][1]
        if 0 <= k < len(idx):
            out[n] = idx[k]
    return out


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
    """True when the run quoted a maintainer comment it shouldn't have seen (not earlier context)."""
    t = squash(transcript)
    for f in case["feedback"]:
        if f.get("prior"):
            continue
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


def on_changed_lines(work, case, code):
    """For each code finding: True/False whether its lines changed by the end, None if it has no line."""
    cache, out = {}, []
    for x in code:
        path = norm_path(x.get("path"))
        try:
            line = int(x.get("line"))
        except (TypeError, ValueError):
            out.append(None)
            continue
        if path not in cache:
            cache[path] = changed_lines(work, case["start"]["sha"], case["end"]["sha"], path)
        out.append(any(a - TOUCHED <= line <= b + TOUCHED for a, b in cache[path]))
    return out


def assess(work, case, findings, transcript="", judged=None):
    """Counts, per-comment verdicts and per-finding flags for one case.

    `judged` maps target number (1-based, in `targets` order) to the index of the code
    finding the judge said makes its point.
    """
    judged = judged or {}
    code = [x for x in findings if not is_meta(x)]
    rows = []
    for n, (f, idx) in enumerate(spot_pairs(case, code), 1):
        if n in judged:
            rows.append((f, "same point", code[judged[n]]))
        elif idx:
            rows.append((f, "same spot only", code[idx[0]]))
        else:
            near = next((x for x in code if same_file(f, x)), None)
            rows.append((f, "same file only", near) if near else (f, "missed", None))
    changed = on_changed_lines(work, case, code)
    serious = [x for x in findings if str(x.get("severity", "")).upper() in ("P0", "P1", "P2")]

    def count(rows, *verdicts):
        return sum(1 for _, v, _ in rows if v in verdicts)

    act = [r for r in rows if acted(r[0])]
    counts = {
        "targets": len(rows),
        "target_spot_hits": count(rows, "same point", "same spot only"),
        "target_point_hits": count(rows, "same point"),
        "acted": len(act),
        "acted_spot_hits": count(act, "same point", "same spot only"),
        "acted_point_hits": count(act, "same point"),
        "acted_file_hits": count(act, "same point", "same spot only", "same file only"),
        "findings": len(findings),
        "code_findings": len(code),
        "meta_findings": len(findings) - len(code),
        "findings_located": sum(1 for c in changed if c is not None),
        "findings_on_changed": sum(1 for c in changed if c),
        "flagged_case": int(bool(serious)),
        "suggestions_landed": sum(1 for x in code if suggestion_landed(work, case, x)),
        "leaked": int(leaked(case, transcript)),
    }
    return counts, rows, list(zip(code, changed))


def score_case(work, case, findings, transcript="", judged=None):
    """The counts for one case."""
    return assess(work, case, findings, transcript, judged)[0]


def rates(c):
    """Per-case rates in 0..1, None where a rate doesn't apply (no comments to catch, no findings)."""
    def rate(n, d):
        # a leaked run saw the answers, so its rates say nothing
        return round(n / d, 3) if d and not c["leaked"] else None
    return {
        "catch_rate": rate(c["acted_point_hits"], c["acted"]),
        "catch_rate_spot": rate(c["acted_spot_hits"], c["acted"]),
        "catch_rate_file": rate(c["acted_file_hits"], c["acted"]),
        "precision": rate(c["findings_on_changed"], c["findings_located"]),
        "flagged": c["flagged_case"],
        "code_findings": c["code_findings"],
        "meta_findings": c["meta_findings"],
        "leaked": c["leaked"],
    }


def _cell(text, n=110):
    """One line of text, shortened, safe inside a Markdown table."""
    t = re.sub(r"\s+", " ", text or "").replace("|", "\\|").strip()
    return t if len(t) <= n else t[:n - 1] + "…"


def explain(case, counts, rows, flagged_code, meta):
    """Markdown for the viewer: each maintainer comment against the review, then each finding."""
    out = ["**%s** — %s ([PR](%s))" % (case["id"], _cell(case["title"], 80), case["url"]), ""]
    if counts["leaked"]:
        out += ["**Leak:** the run quoted a maintainer comment it shouldn't have seen; don't count this case.", ""]
    if rows:
        out += ["| Maintainer comment | Acted on | Review | Closest finding |", "|---|---|---|---|"]
        for f, v, x in rows:
            where = "`%s:%s`" % (f["path"].split("/")[-1], f["line"])
            near = "`%s:%s` %s" % ((x.get("path") or "").split("/")[-1], x.get("line"), _cell(x.get("title"), 70)) if x else ""
            out.append("| %s %s | %s | **%s** | %s |" % (where, _cell(f["body"]), "yes" if acted(f) else "no", v, near))
    else:
        out.append("_No round-one inline comments to catch in this case; it only counts towards precision._")
    out += ["", "| Code finding | Severity | Lines changed by merge |", "|---|---|---|"]
    for x, c in flagged_code:
        out.append("| `%s:%s` %s | %s | %s |" % ((x.get("path") or "").split("/")[-1], x.get("line"),
                                                _cell(x.get("title"), 90), x.get("severity"),
                                                "—" if c is None else "yes" if c else "no"))
    if meta:
        out += ["", "PR-text findings (not scored): " + "; ".join(_cell(x.get("title"), 60) for x in meta)]
    return "\n".join(out)
