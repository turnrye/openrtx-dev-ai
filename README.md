# openrtx-dev-ai

A Claude Code plugin and Pi extension for people working on [OpenRTX](https://github.com/OpenRTX/OpenRTX). It gives Claude (and you) one command, `openrtx`, that answers questions from a local cache of everything the project knows — GitHub issues and pull requests across the whole OpenRTX organisation, and the community Office Hours notes.

## What you get

- **`/openrtx:research`** — Claude researches plans, decisions, status and prior discussion across every source, and cites each item with an unambiguous link (`OpenRTX/OpenRTX#520`, an Office Hours meeting).
- **`/openrtx:review`** — Claude reviews a change (uncommitted work, a branch, a commit or a pull request) against the project's AGENTS.md and CONTRIBUTING.md and for correctness on the radios, runs the CI checks, reads the PR's discussion and earlier reviews of the same files, and opens the report in [Plannotator](https://github.com/backnotprop/plannotator) for you to annotate when it's installed. Nothing is posted to GitHub unless you ask, and then only as a pending review you submit yourself.
- **`/openrtx:prepare`** — the author's side: Claude gets your branch ready for a PR and through each review round. It checks for competing work, self-reviews, splits the commits the way maintainers ask, runs the CI checks, gathers testing evidence — including [OpenRTX Acceptance Testing](https://github.com/turnrye/openrtx-acceptance-testing) bench runs that back the change, or the suites still to run and the test gaps to close — writes the description from the PR template, and turns review comments into fixup commits and replies. It rewrites history only with a backup ref, and pushes, posts or re-requests review only after you approve the exact plan (in Plannotator when installed); PRs stay in draft and sign-off stays yours.
- **`openrtx`** — the command both skills use, which you can run yourself:

```sh
openrtx recent --since 7d              # what changed lately, everywhere
openrtx search littlefs power loss     # full-text search across all sources
openrtx show OpenRTX/OpenRTX#445       # one item with its whole discussion
openrtx sql "SELECT … FROM github.items"
openrtx sources                        # what's cached and how fresh it is
```

## Install

You need Python 3.9 or newer (macOS, Linux or Windows). Nothing else is installed.

### Claude Code

- **As a plugin:** `/plugin marketplace add turnrye/openrtx-dev-ai`, then `/plugin install openrtx@openrtx-dev-ai`.
- **From a clone:** clone this repo and either start Claude Code with `claude --plugin-dir /path/to/openrtx-dev-ai`, or link it into your personal skills folder so it loads in every session: `ln -s /path/to/openrtx-dev-ai ~/.claude/skills/openrtx`.

While the plugin is enabled, `openrtx` is on the PATH of the shell Claude uses. To use it in your own terminal, link `bin/openrtx` into a directory on your PATH (Windows: `bin\openrtx.cmd`).

### Pi

- **Via package:** run `pi install git:github.com/turnrye/openrtx-dev-ai` — skills and the `openrtx_run` tool load automatically.
- **From a clone:** start Pi from within the repo directory. Skills are auto-discovered via `.agents/skills/`, and the `openrtx_run` tool is registered by the extension.

To use `openrtx` in your own terminal, link `bin/openrtx` into a directory on your PATH (Windows: `bin\openrtx.cmd`).

**Reviews and PR preparation** run from your OpenRTX clone and use `git`, `gh`, and the project's own checks (`clang-format`, `reuse`, `meson`) when they're installed. To get the report in Plannotator rather than in the chat, install it: `pi install npm:@plannotator/pi-extension` for Pi, with its agent tool turned on (`PLANNOTATOR_AGENT_TOOL=1` or `{ "agentTool": true }` in `~/.plannotator/config.json`), or the `plannotator` CLI and plugin for Claude Code. If you have a clone of the acceptance-testing repo, name it in the config file (`oat.repo = ~/path/to/openrtx-acceptance-testing`); otherwise `prepare` reads the published results from a copy in the cache.

**GitHub access:** the GitHub source uses your `gh` login (or `GITHUB_TOKEN`); without one, GitHub's anonymous limit is too low for the first sync.

## How it works

Each source keeps its own SQLite file in your platform's cache folder (`~/Library/Caches/openrtx`, `~/.cache/openrtx`, `%LOCALAPPDATA%\openrtx\cache`). A read refreshes a source only when its copy is older than its period, so agents can query as often as they like:

| Source | What | Refreshed |
|---|---|---|
| `github` | issues, PRs, comments, review comments and reviews in every OpenRTX repo, plus each PR's branches, commits (kept after force-pushes), changed files and timeline events | hourly |
| `pad` | the Office Hours notes (Etherpad), with every revision | daily |

The first sync of each source takes a few minutes; after that a refresh takes seconds. `openrtx sync` refreshes on demand, and the cache can be deleted at any time. Settings (refresh periods, extra pads, the GitHub org) go in `~/.config/openrtx/config` (`%APPDATA%\openrtx\config` on Windows); see `openrtx/paths.py`.

## Development

```sh
python3 -m unittest discover tests     # fake GitHub and Etherpad servers; no network
claude plugin validate .
```

Sources follow the contract in `openrtx/sources/__init__.py`.

## License

GPL-3.0-or-later, the same as OpenRTX. See [LICENSE](LICENSE).
