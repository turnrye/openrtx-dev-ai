---
name: research
description: >-
  Research the OpenRTX project from a local cache spanning the OpenRTX Taiga (tasks.openrtx.org), GitHub issues and pull requests across every repo in the OpenRTX organisation, and the community Office Hours meeting notes. Use for any question about OpenRTX plans, decisions, status or prior discussion ("has this been discussed", "what did the maintainers say about", "what's the status of", "who's working on", "what was decided at office hours"), before writing or reviewing a story, issue or PR, and whenever a ref such as taiga#841, OpenRTX/OpenRTX#520 or pad:officehours comes up.
argument-hint: "[REF | question]"
allowed-tools: Bash(openrtx sources*) Bash(openrtx sync*) Bash(openrtx recent*) Bash(openrtx search *) Bash(openrtx show *) Bash(openrtx sql *) Bash(openrtx taiga search *) Bash(openrtx taiga show *) Bash(openrtx taiga log*) Bash(openrtx taiga info*) Bash(openrtx taiga sql *)
---

# Researching OpenRTX

The `openrtx` command (on the PATH while this plugin is enabled; otherwise run `"${CLAUDE_PLUGIN_ROOT}/bin/openrtx"`) answers from a local cache of every OpenRTX source. Each source refreshes itself when its copy is older than its period — Taiga and GitHub after an hour, the Office Hours pad after a day — so query as often as you like. The very first query on a machine syncs each source in full, which takes a few minutes; tell the user that's what's happening.

## Ask

| Need | Command |
|---|---|
| A topic, across everything | `openrtx search littlefs power loss` (FTS5: `"exact phrase"`, `a OR b`, `prefix*`; `--source taiga` to narrow; `--json`) |
| One item with its discussion | `openrtx show taiga#841`, `openrtx show OpenRTX/OpenRTX#520`, `openrtx show pad:officehours` (`@REV` for an older revision), or a Taiga/GitHub URL |
| What changed lately, everywhere | `openrtx recent --since 7d` (also `24h`, `today`, `yesterday`, an ISO date; `--source`) |
| Taiga's detailed activity | `openrtx taiga log --since yesterday` (`--ref 841`, `--actor Redman`) |
| Anything else | `openrtx sql "SELECT …"`; each source is a schema: `taiga.items`, `github.items`, `pad.sections` |
| How fresh the cache is | `openrtx sources`; force a refresh with `openrtx sync SOURCE --max-age 0` |

Tables, recipes and data caveats per source are in [reference.md](reference.md); read it before writing SQL.

## Sources and refs

Refs always name their source, so a bare `#N` never means two things:

- **Taiga** — `taiga#841`: epics, stories, tasks and issues, with comments and full history. Many Taiga issues are imported GitHub issues.
- **GitHub** — `OWNER/REPO#N`: issues, PRs, comments, review comments and reviews in every OpenRTX repo — the firmware `OpenRTX/OpenRTX`, but also `rtxlink`, `rtxflash`, `OpenRTX-Companion`, `vpcgen`, `miosix-kernel`, `sa8x8-fw`, `openrtx.github.io` (website and docs), `OpenRTX-external-docs` and the Zephyr mirrors for the Retevis C62. Most design discussion and review happens here.
- **Office Hours** — `pad:officehours`: notes of the community calls (date, attendees, topics, decisions), one entry per meeting and topic.
- **Code history** — use git in the user's OpenRTX clone directly (`git log -S`, `git log --grep`, `git blame`); cite commits by short SHA with a GitHub link.

## Research well

- Search several phrasings before concluding something was never discussed, then open the top hits with `show`.
- Follow cross-references: Taiga items cite PRs, PRs cite Taiga refs, meeting notes cite both. A decision often lives in a PR review or an Office Hours topic rather than in Taiga.
- Say how old the cache is when freshness matters.

## Reporting

- Cite everything as links with source-qualified text: `[taiga#840](https://tasks.openrtx.org/project/openrtx/us/840)`, `[OpenRTX/OpenRTX#520](https://github.com/OpenRTX/OpenRTX/pull/520)`, "Office Hours, 2026-07-23" with its pad link. Never a bare `#N`.
- Quote comments with their author and date.
- When findings feed a story, issue or review, link the item rather than restating it.

## When invoked as /openrtx:research

Arguments: `$ARGUMENTS`

- **None:** run `openrtx recent --since 7d` and summarise what changed, grouped by topic rather than by source.
- **A ref or URL:** `openrtx show` it and summarise its status, open threads and recent activity.
- **Anything else:** treat it as a research question and answer with links.
