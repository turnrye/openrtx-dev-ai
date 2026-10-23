# openrtx-dev-ai

A Claude Code plugin for people working on [OpenRTX](https://github.com/OpenRTX/OpenRTX). It gives Claude (and you) one command, `openrtx`, that answers questions from a local cache of everything the project knows — the [Taiga](https://tasks.openrtx.org/project/openrtx), GitHub issues and pull requests across the whole OpenRTX organisation, and the community Office Hours notes — and writes to the Taiga without mangling formatting.

## What you get

- **`/openrtx:research`** — Claude researches plans, decisions, status and prior discussion across every source, and cites each item with an unambiguous link (`taiga#841`, `OpenRTX/OpenRTX#520`, an Office Hours meeting).
- **`/openrtx:taiga-write`** — Claude turns a Markdown draft into Taiga epics, stories, tasks or comments, dry-running first and checking what Taiga rendered afterwards.
- **`openrtx`** — the command both skills use, which you can run yourself:

```sh
openrtx recent --since 7d              # what changed lately, everywhere
openrtx search littlefs power loss     # full-text search across all sources
openrtx show OpenRTX/OpenRTX#445       # one item with its whole discussion
openrtx sql "SELECT … FROM github.items"
openrtx sources                        # what's cached and how fresh it is
openrtx taiga post draft.md            # dry run; add --post to create the items
```

## Install

You need Claude Code and Python 3.9 or newer (macOS, Linux or Windows). Nothing else is installed.

- **As a plugin:** `/plugin marketplace add turnrye/openrtx-dev-ai`, then `/plugin install openrtx@openrtx-dev-ai`.
- **From a clone:** clone this repo and either start Claude Code with `claude --plugin-dir /path/to/openrtx-dev-ai`, or link it into your personal skills folder so it loads in every session: `ln -s /path/to/openrtx-dev-ai ~/.claude/skills/openrtx`.

While the plugin is enabled, `openrtx` is on the PATH of the shell Claude uses. To use it in your own terminal, link `bin/openrtx` into a directory on your PATH (Windows: `bin\openrtx.cmd`).

**GitHub access:** the GitHub source uses your `gh` login (or `GITHUB_TOKEN`); without one, GitHub's anonymous limit is too low for the first sync. **Taiga writes** need a Taiga account: `openrtx taiga login` asks for the password in your terminal, or serves a one-time login page on 127.0.0.1 when Claude runs it, so the password never passes through Claude.

## How it works

Each source keeps its own SQLite file in your platform's cache folder (`~/Library/Caches/openrtx`, `~/.cache/openrtx`, `%LOCALAPPDATA%\openrtx\cache`). A read refreshes a source only when its copy is older than its period, so agents can query as often as they like:

| Source | What | Refreshed |
|---|---|---|
| `taiga` | every epic, story, task and issue with comments and full history | hourly |
| `github` | issues, PRs, comments, review comments and reviews in every OpenRTX repo | hourly |
| `pad` | the Office Hours notes (Etherpad), with every revision | daily |

The first sync of each source takes a few minutes; after that a refresh takes seconds. `openrtx sync` refreshes on demand, and the cache can be deleted at any time. Settings (refresh periods, extra pads, the GitHub org) go in `~/.config/openrtx/config` (`%APPDATA%\openrtx\config` on Windows); see `openrtx/paths.py`.

## Development

```sh
python3 -m unittest discover tests     # fake Taiga, GitHub and Etherpad servers; no network
claude plugin validate .
```

Sources follow the contract in `openrtx/sources/__init__.py`. The Taiga source vendors the mirror from the former `openrtx-taiga` project.

## License

GPL-3.0-or-later, the same as OpenRTX. See [LICENSE](LICENSE).
