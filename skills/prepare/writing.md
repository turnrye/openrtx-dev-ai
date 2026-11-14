# Writing for the maintainers

Everything the author writes is read by a few busy maintainers. They trim long text (#513: "Let's cut it short"; "Claude tends to be a bit chatty"), ask for concise commit messages (#564), and prefer one line where one line will do. Write plainly, say what changed and why, cite rather than restate, and stop.

## Commit messages

```text
dsp: dcBlockFilter: avoid UB left shift of negative sample

<a few plain lines: why, and anything a bisecting reader needs>

Co-authored-by: <agent> <noreply@…>
```

- Summary: `<module>: ` prefix (`history.md` § Proposing the split), imperative, under about 72 characters. The example is the one a maintainer suggested on #564.
- Body only when the summary can't carry it, and then a few lines (#513). No bullet lists restating the diff.
- The agent's `Co-authored-by` trailer on commits it wrote. No `Signed-off-by`: the user adds it (`git rebase --signoff <base>`).

## The PR description

Fill the base's `.github/pull_request_template.md` (read it from `<upstream>/<base>`; it changes), keeping its headings, deleting only sections that genuinely don't apply, and removing its HTML comments from the result.

- **Summary:** one or two sentences — what and why. Detail goes in a short *Change* list only if the PR has several parts.
- **Related issue:** `Fixes #N` for GitHub issues it closes; Taiga items as links (`[taiga#889](https://tasks.openrtx.org/project/openrtx/epic/889)`); the PRs it follows or depends on as `OpenRTX/OpenRTX#N`.
- **Design notes and alternatives:** for large changes, the discussion that agreed the approach; the self-review findings you chose not to act on, with the reason; and anything a reviewer will wonder about (why not X).
- **Testing performed:** from `evidence.md`, item by item with status and links; numbers in a small table where there are numbers. *Tested on* boxes ticked for exactly the radios tested; "help wanted" ticked and the target named for the rest.
- **Changelog entry:** one line, `module: what changed for users`, or `none` for internal changes. "C62: fix CTCSS tones above 208 Hz", not a paragraph.
- **Checklist:** tick only what's true.
- For a PR of fixups against a long-running base, say which base commits they correct and whether they build one by one (OpenRTX/OpenRTX#552 does both).

The `Co-authored-by` trailers are the AI disclosure AGENTS.md asks for. Add a line about AI assistance in the description only if the user wants one; the project's policy is still being settled in OpenRTX/OpenRTX#354.

## Replies

- Answer the comment, not the whole PR: "Done in `fixup! c62: add radio HAL` (`1c7e3cd`): the M17 settings moved into `radio_setOpmode()`." Or, for a disagreement: the fact that decides it ("`REG_43` is rewritten by `enableRx()`, so it can't move: datasheet p. 38"), and an offer ("happy to add a comment saying so").
- A question back is fine when you need the reviewer's intent.
- One "thanks" is normal in this project; praise and apologies aren't needed. No restating the reviewer's point, no summaries of what you're about to do.
- Write in the user's voice and keep it something they would sign: they post it.
