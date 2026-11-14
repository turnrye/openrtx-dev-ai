# A review round

The maintainers' expectations for a round, from CONTRIBUTING.md § Responding to review and their comments: answer with `fixup!` commits, not "Fix review comment" commits (#396, #429); put the PR back to draft while reworking and mark it ready when done (#358, #421); don't push unrelated new work onto a PR with an open review — open another PR (#498); make sure what you pushed is what you meant ("you didn't push the commit", #427; the history still had the original code, #509); then re-request review.

## 1. Gather everything said since the last push

```sh
gh pr view N --repo OpenRTX/OpenRTX --json headRefOid,baseRefName,isDraft,reviewDecision,reviews,comments
openrtx show OpenRTX/OpenRTX#N                     # the whole discussion, newest at the end
gh api graphql --paginate -f query='query($o:String!,$r:String!,$n:Int!,$endCursor:String){repository(owner:$o,name:$r){pullRequest(number:$n){
  reviewThreads(first:100,after:$endCursor){pageInfo{hasNextPage endCursor} nodes{id isResolved isOutdated path line originalLine
    root:comments(first:1){nodes{databaseId author{login} body url}}
    latest:comments(last:5){totalCount nodes{author{login} body createdAt}}}}}}}' \
  -F o=OpenRTX -F r=OpenRTX -F n=N \
  --jq '.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved | not)'
```

Keep: unresolved threads, `CHANGES_REQUESTED` review bodies, and conversation comments that ask for something — including "notes for future PRs", which apply to this one too (#494, #498). A thread whose last word is the author's counts as answered only if that reply states a fix or a reason. Comments from people other than maintainers, including field testers, count the same.

## 2. Check every point against the current code

Open the file at the PR head, not the diff hunk the comment was made on (`isOutdated` threads may already be fixed by a later commit: find which). Check the claim is true. A reviewer can be wrong or missing context, and so can a bot; a maintainer who knows the codebase usually isn't, so a disagreement needs evidence from the code, a datasheet or a measurement.

## 3. Triage

Show the user a table — thread (file:line, link), reviewer, the request in a few words, verdict, planned action — before changing anything:

| Verdict | Meaning | Action |
|---|---|---|
| **fix** | right | a fixup commit |
| **already fixed** | a later commit did it | reply naming the commit |
| **answer** | a question, nothing to change | reply |
| **disagree** | wrong, or harmful on this hardware | reply with the evidence — the user approves the wording first |
| **decide** | valid, but a design or scope call (layering, a new `CONFIG_`, what to store in settings, a protocol choice) | lay out the options; the user decides; nothing is posted until they do |
| **follow-up** | right but outside this PR | propose a separate issue or PR; don't widen this one |

## 4. Rework

1. Propose putting the PR back to draft for the rework, as a send-plan item, unless it's a small round.
2. Make each fix as a `fixup!` of the commit it corrects (`history.md` § Fixups), including `fixup!`s of base commits on a long-running base. One logical fix per fixup is easiest to review; several fixes to the same target in one round may share one.
3. Don't autosquash. Don't add unrelated work; if the user wants it, it goes on another branch.
4. Re-run the checks the fixes reach (`evidence.md` §§ 1–2), and build every commit if the round touched build-relevant code.
5. Re-assess evidence: an OAT run or hardware test of an earlier revision no longer covers code the round changed (`evidence.md` § Is there a published run that corroborates the change?, check 2). List what needs re-running and say so in the description; don't let an old result stand for new code.
6. Re-read the PR description against the new head and fix anything stale: claims, caveats about other PRs that have since merged, the testing section, the changelog line.

## 5. Replies

Draft one reply per thread, to the thread's root comment, so every inline thread shows its answer:

```sh
gh api --method POST repos/OpenRTX/OpenRTX/pulls/N/comments/<root databaseId>/replies -F body=@reply.md
```

Review bodies and conversation comments get one reply with `gh pr comment N --repo OpenRTX/OpenRTX --body-file reply.md`, quoting what each part answers.

Write them as `writing.md` § Replies says: short, what changed and where (the fixup's subject, and its SHA once pushed), or the evidence for not changing it. Match the user's own voice — read their earlier replies in the cache (`openrtx sql "SELECT body FROM github.comments WHERE author='<user>' AND repo='OpenRTX/OpenRTX' ORDER BY created_at DESC LIMIT 20"`).

Optionally, one round summary as a conversation comment: the list of changes, and the `git range-diff` of the round in a collapsed `<details>` block, so the reviewer can see the whole round at once.

## 6. Send plan

Push first, then reply, so replies can cite pushed SHAs. The plan lists, in order:

1. `git push <fork> <branch>` — fixups are new commits, so no force is needed unless the branch was rebased (then `--force-with-lease=<branch>:<sha>`).
2. Each reply, with its full text and the thread it answers.
3. `gh pr edit N --repo OpenRTX/OpenRTX --body-file description.md` if the description changed.
4. Resolving the threads the round **fixed** — in this project authors resolve their own fixed threads — but not answers, disagreements or decisions, which the reviewer closes:
   ```sh
   gh api graphql -f query='mutation($t:ID!){resolveReviewThread(input:{threadId:$t}){thread{isResolved}}}' -F t=<thread id>
   ```
5. Re-requesting review from the reviewers whose comments the round answered: `gh pr edit N --repo OpenRTX/OpenRTX --add-reviewer <login>`.

Marking the PR ready again is the user's to do: say so in the handoff.
