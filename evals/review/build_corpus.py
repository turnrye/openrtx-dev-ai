#!/usr/bin/env python3
"""Build the review corpus: merged OpenRTX PRs with the maintainer's feedback, as start/feedback/end.

    python3 evals/review/build_corpus.py [--limit 100] [--reviewer silseva] [--refresh]

Each case pins three things:
  start     the PR head the maintainer first reviewed, and its fork point
  feedback  every comment the maintainer left: inline, review bodies, conversation
  end       the PR's final head and the merge commit

It walks merged PRs on OpenRTX/OpenRTX, newest first, and keeps those where the
maintainer left at least one substantive comment and the head moved after it. Raw API
responses are kept under the openrtx cache (evals/review/raw) so reruns are cheap;
--refresh refetches them. Commits are fetched into the cache's evals/openrtx.git, which
the commits eval shares; some exist only as old force-pushed heads.

The labels are silver: a maintainer comment marks something a reviewer should raise,
and `outcome` (inline comments only) records whether the lines it points at changed by
the end, which is evidence, not proof, that it was acted on.
"""

import argparse
import bisect
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from openrtx import paths  # noqa: E402

REPO = "OpenRTX/OpenRTX"
UPSTREAM = "https://github.com/%s.git" % REPO
TRIVIAL = re.compile(r"^\W*(thanks?( you)?|merged|lgtm|ok(ay)?|done|great|nice|perfect|good( job)?)\b[\W\w]{0,40}$",
                     re.I | re.S)
CONTEXT = 2  # lines either side of an inline comment that count as "its" lines


def gh(path, paginate=False):
    """Return the JSON of a GitHub API GET; paginated lists come back as one list."""
    cmd = ["gh", "api", "-H", "Accept: application/vnd.github+json", path]
    if paginate:
        cmd += ["--paginate", "--slurp"]
    for attempt in range(4):
        out = subprocess.run(cmd, capture_output=True, text=True)
        if not out.returncode:
            break
        time.sleep(5 * 2 ** attempt)
    else:
        raise RuntimeError("gh api %s: %s" % (path, out.stderr.strip()))
    data = json.loads(out.stdout)
    return [x for page in data for x in page] if paginate else data


def git(work, *args, check=True):
    """Run git on the bare mirror and return the completed process; fetches retry."""
    for attempt in range(4 if args[0] == "fetch" else 1):
        p = subprocess.run(["git", "--git-dir", work] + list(args), capture_output=True, text=True)
        if not p.returncode or "unable to access" not in p.stderr:
            break
        time.sleep(5 * 2 ** attempt)
    if check and p.returncode:
        raise RuntimeError("git %s: %s" % (" ".join(args), p.stderr.strip()))
    return p


def mirror(work):
    """Make sure the bare mirror exists and master is current."""
    if not os.path.isdir(work):
        print("cloning %s into %s …" % (REPO, work), file=sys.stderr)
        subprocess.run(["git", "clone", "--bare", "-q", UPSTREAM, work], check=True)
    git(work, "fetch", "-q", "origin", "+refs/heads/master:refs/heads/master")


def have(work, sha):
    """True when the mirror has the commit, fetching it by SHA if needed."""
    if not git(work, "cat-file", "-e", sha + "^{commit}", check=False).returncode:
        return True
    return not git(work, "fetch", "-q", "origin", sha, check=False).returncode


def raw_pr(raw_dir, number, refresh):
    """Fetch (or load) everything the corpus needs about one PR."""
    path = os.path.join(raw_dir, "pr-%d.json" % number)
    if os.path.exists(path) and not refresh:
        return json.load(open(path))
    base = "repos/%s" % REPO
    data = {
        "pull": gh("%s/pulls/%d" % (base, number)),
        "reviews": gh("%s/pulls/%d/reviews?per_page=100" % (base, number), True),
        "review_comments": gh("%s/pulls/%d/comments?per_page=100" % (base, number), True),
        "issue_comments": gh("%s/issues/%d/comments?per_page=100" % (base, number), True),
        "timeline": gh("%s/issues/%d/timeline?per_page=100" % (base, number), True),
    }
    paths.ensure(raw_dir)
    with open(path, "w") as f:
        json.dump(data, f)
    return data


def head_pushes(timeline):
    """(time, sha) for every known head update, oldest first.

    Force pushes carry their push time. Plain pushes only show up as `committed` events
    dated by the commit, which is earlier than or equal to the push.
    """
    out = []
    for e in timeline:
        if e.get("event") == "head_ref_force_pushed" and e.get("commit_id"):
            out.append((e["created_at"], e["commit_id"]))
        elif e.get("event") == "committed" and e.get("sha"):
            out.append(((e.get("committer") or {}).get("date") or "", e["sha"]))
    return sorted(x for x in out if x[0])


def head_at(pushes, when):
    """The PR head at a moment, from the push history; None before the first push."""
    i = bisect.bisect_right([t for t, _ in pushes], when)
    return pushes[i - 1][1] if i else None


def feedback_items(raw, reviewer):
    """The reviewer's comments on the PR, oldest first, each anchored to a commit."""
    pr = raw["pull"]
    pushes = head_pushes(raw["timeline"])
    author = pr["user"]["login"]
    replies = {}
    for c in raw["review_comments"]:
        if c.get("in_reply_to_id") and c["user"]["login"] != reviewer:
            replies.setdefault(c["in_reply_to_id"], []).append(
                {"author": c["user"]["login"], "at": c["created_at"], "body": c["body"]})
    items = []
    for c in raw["review_comments"]:
        if c["user"]["login"] != reviewer:
            continue
        items.append({
            "kind": "inline", "id": c["id"], "at": c["created_at"], "body": c["body"],
            "commit": c["original_commit_id"], "anchor": "exact",
            "path": c["path"], "line": c.get("original_line"),
            "start_line": c.get("original_start_line"), "side": c.get("side"),
            "diff_hunk": c.get("diff_hunk"), "in_reply_to": c.get("in_reply_to_id"),
            "review_id": c.get("pull_request_review_id"),
            "replies": replies.get(c["id"], []),
        })
    for r in raw["reviews"]:
        if r["user"] and r["user"]["login"] == reviewer and (r.get("body") or "").strip():
            items.append({"kind": "review", "id": r["id"], "at": r["submitted_at"], "body": r["body"],
                          "state": r["state"], "commit": r["commit_id"], "anchor": "exact"})
    for c in raw["issue_comments"]:
        if c["user"]["login"] != reviewer:
            continue
        sha = head_at(pushes, c["created_at"])
        items.append({"kind": "conversation", "id": c["id"], "at": c["created_at"], "body": c["body"],
                      "commit": sha, "anchor": "reconstructed" if sha else None})
    merged = pr.get("merged_at") or "9999"
    last_push = pushes[-1][0] if pushes else ""
    for it in items:
        it["trivial"] = bool(TRIVIAL.match((it["body"] or "").strip()))
        it["after_merge"] = it["at"] > merged
        it["before_last_push"] = it["at"] < last_push
    items.sort(key=lambda it: it["at"])
    return [it for it in items if not it["after_merge"]], author


def outcome(work, item, end):
    """For an inline comment: did the lines it points at change between its commit and the end?"""
    if item["kind"] != "inline" or not item.get("line") or item.get("side") == "LEFT":
        return None
    if git(work, "cat-file", "-e", "%s:%s" % (end, item["path"]), check=False).returncode:
        return "file_gone"
    lo = (item.get("start_line") or item["line"]) - CONTEXT
    hi = item["line"] + CONTEXT
    diff = git(work, "diff", "-U0", item["commit"], end, "--", item["path"], check=False).stdout
    for m in re.finditer(r"^@@ -(\d+)(?:,(\d+))? ", diff, re.M):
        start, count = int(m.group(1)), int(m.group(2) or 1)
        if start <= hi and start + max(count, 1) - 1 >= lo:
            return "changed"
    return "unchanged"


def build_case(work, raw, reviewer):
    """One corpus case, or (None, reason) when the PR doesn't qualify."""
    pr = raw["pull"]
    items, author = feedback_items(raw, reviewer)
    real = [it for it in items if not it["trivial"] and it["commit"]]
    if not real:
        return None, "no substantive feedback"
    if not any(it["before_last_push"] for it in real):
        return None, "head never moved after feedback"
    first = real[0]
    end, merge = pr["head"]["sha"], pr.get("merge_commit_sha")
    if first["commit"] == end:
        return None, "start equals end"
    shas = {first["commit"], end, merge} | {it["commit"] for it in items if it["commit"]}
    missing = [s for s in shas if s and not have(work, s)]
    if first["commit"] in missing or end in missing or merge in missing:
        return None, "commits not fetchable: %s" % ", ".join(s[:9] for s in missing)
    for it in items:
        it["available"] = bool(it["commit"]) and it["commit"] not in missing
        it["outcome"] = outcome(work, it, end) if it["available"] else None

    def fork(sha):
        p = git(work, "merge-base", sha, merge, check=False)
        return p.stdout.strip() or None

    return {
        "id": "OpenRTX-%d" % pr["number"],
        "number": pr["number"],
        "url": pr["html_url"],
        "title": pr["title"],
        "author": author,
        "base_ref": pr["base"]["ref"],
        "created_at": pr["created_at"],
        "merged_at": pr["merged_at"],
        "merged_by": (pr.get("merged_by") or {}).get("login"),
        "cutoff": first["at"],
        "start": {"sha": first["commit"], "base": fork(first["commit"]), "anchor": first["anchor"]},
        "end": {"sha": end, "base": fork(end), "merge_commit": merge},
        "feedback": items,
    }, None


def candidates(reviewer):
    """Merged PRs not opened by the reviewer, newest merge first."""
    prs = gh("repos/%s/pulls?state=closed&per_page=100" % REPO, True)
    prs = [p for p in prs if p.get("merged_at") and p["user"]["login"] != reviewer]
    return sorted(prs, key=lambda p: p["merged_at"], reverse=True)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--reviewer", default="silseva")
    p.add_argument("--refresh", action="store_true", help="refetch the raw API data")
    p.add_argument("--work", default=os.path.join(paths.cache_dir(), "evals", "openrtx.git"))
    p.add_argument("--out", default=os.path.join(HERE, "corpus.json"))
    a = p.parse_args()

    raw_dir = os.path.join(paths.cache_dir(), "evals", "review", "raw")
    mirror(a.work)
    cases, skipped = [], {}
    for pr in candidates(a.reviewer):
        if len(cases) >= a.limit:
            break
        raw = raw_pr(raw_dir, pr["number"], a.refresh)
        case, why = build_case(a.work, raw, a.reviewer)
        if case:
            cases.append(case)
            print("#%d ok: %d feedback" % (pr["number"], len(case["feedback"])), file=sys.stderr)
        else:
            skipped[pr["number"]] = why
    with open(a.out, "w") as f:
        json.dump({"repo": REPO, "reviewer": a.reviewer, "cases": cases, "skipped": skipped},
                  f, indent=1, sort_keys=True)
        f.write("\n")
    print("%d cases, %d skipped -> %s" % (len(cases), len(skipped), a.out), file=sys.stderr)


if __name__ == "__main__":
    main()
