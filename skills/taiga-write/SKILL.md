---
name: taiga-write
description: >-
  Create or edit items on the OpenRTX Taiga (tasks.openrtx.org) — epics, user stories, tasks, issues — or post comments, from a Markdown draft, keeping the formatting that pasting into Taiga's editor loses; also any other Taiga API call. Use when the user asks to post, create, file, publish, update, close or comment on something in Taiga, or to turn a Taiga draft into real items. For reading or researching, use the research skill instead.
argument-hint: "[DRAFT.md | REF]"
---

# Writing to the OpenRTX Taiga

Pasting Markdown into Taiga's rich-text editor loses the formatting. The API doesn't: Taiga stores the Markdown as sent and renders it on the server, so headings, lists and links come out like the existing epics. `openrtx taiga` posts drafts through the API, reads every item back to check what Taiga rendered, and refreshes the local research cache so the change shows up there at once.

Writes are public and notify watchers, so:

- **Always dry-run first** (the default), show the user the plan it prints, and add `--post` only after they say to post. A "looks good" on the text is not a go-ahead to post.
- **No @mentions and no names of people** unless the user asks for them. The tool refuses `@name` unless given `--allow-mentions`.
- **GitHub issues and PRs: always `OWNER/REPO#N`.** In Taiga text `#N` links to *Taiga* item N, never GitHub — including link text: `[#520](https://github.com/…)` renders as an empty link followed by a link to Taiga item 520. Write `OpenRTX/OpenRTX#520` or `OpenRTX/rtxlink#12`; the tool turns those into links and rewrites `[#N](GitHub URL)` the same way. Check the dry run's list of every `#N` Taiga will link and where it points.

## Logging in

The password must never pass through Claude: don't ask for it in chat, don't put it on a command line, and never read or print `TAIGA_PASSWORD`.

- **Check first:** `openrtx taiga whoami`. Exit status **3** means a login is needed (none saved, or unused for over 8 days); every write command exits 3 in the same case. Otherwise the login renews itself.
- **To log in,** run `openrtx taiga login` in the background (it waits), read its first output line and give the user the link: a one-time page on their own machine (127.0.0.1) where they type the password. It tries to open the browser too. Wait for "Logged in as …", then carry on.
- **If that link can't reach the user** (a cloud session, remote container or headless box): ask them to run `openrtx taiga login` in a terminal on that machine, or to set `TAIGA_USERNAME` and `TAIGA_PASSWORD` as session secrets and then run `openrtx taiga login --env`. Over SSH the tool prints the port-forward to use.

## Drafts

One Markdown file holds any number of items and comments, each under an HTML-comment header (invisible in a preview). Text before the first header is notes for the user and isn't posted.

```markdown
<!-- taiga: item toolkit
type: epic
subject: UI: Widget Toolkit
tags: ui
-->
Description in Markdown. Fonts come later, in [look]. Supersedes OpenRTX/OpenRTX#445.

Appetite
--------

...

<!-- taiga: item look
type: epic
subject: UI: new fonts and a new look
-->
Starts after [toolkit]'s cutover.

<!-- taiga: item tests
type: us
subject: UI golden-image tests
epic: [toolkit]
-->
Story description.

<!-- taiga: comment taiga#313 -->
Comment text; [toolkit] and [look] resolve here too.
```

- **Item fields:** `type` (`epic`, `us`, `task`, `issue`), `subject`; optional `tags`, `epic` (a story is linked under it) and `story` (a task's parent, earlier in the draft). Both take `#REF` or `[name]`.
- **`[name]`** becomes the new item's `#ref`, filled in after everything exists. Markdown links `[text](url)` are left alone.
- **Don't** separate items with `---`: under a paragraph that makes the paragraph a heading.
- Write the way the project's epics read (outcome first, then Appetite / Solution sketch / Decided / Rabbit holes / No-gos / Related), and link prior art instead of restating it.

## Commands

| Command | Does |
|---|---|
| `openrtx taiga post DRAFT.md [--post]` | Creates the items, fills placeholders, links stories, posts comments, reads back; refs saved to `DRAFT.md.posted.json` |
| `openrtx taiga comment REF FILE [--post]` | Comments on one item |
| `openrtx taiga edit REF [--subject S] [--description-file F] [--post]` | Changes a subject or description |
| `openrtx taiga api METHOD PATH [--data FILE] [--post]` | Anything else (status, assignee, tags, milestones…): reads run at once, writes are a dry run until `--post`, with the same text checks |
| `openrtx taiga whoami` / `login` | See Logging in |

`post` refuses before sending anything when a subject already exists for that type (`--allow-duplicate`), a placeholder or parent isn't in the draft, a task's story comes after it, text is empty, or there's an `@mention` or a `#N` that matches no Taiga item. The API, its fields and recipes for common changes are in [reference.md](reference.md).

## Reporting

Give each new or changed item as a link, what the read-back showed (headings, lists, links, any warnings), and anything refused. If a write fails part-way, say which items already exist (they're printed as they're created); re-running `post` refuses those, so post the rest from a trimmed draft.
