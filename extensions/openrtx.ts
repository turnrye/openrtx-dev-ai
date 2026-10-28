/**
 * OpenRTX Pi Extension
 *
 * Registers an `openrtx_run` tool that wraps the openrtx CLI, and adds
 * PATH context in `before_agent_start` so the model knows how to use it.
 *
 * Works both when cloned locally and when installed via `pi install git:...`.
 */

import { execFileSync } from "node:child_process";
import path from "node:path";
import { Type } from "@earendil-works/pi-ai";
import type { ExtensionAPI, AgentToolResult } from "@earendil-works/pi-coding-agent";

/** Resolve the openrtx CLI relative to this extension file. */
function resolveCliPath(): string {
	const dir = __dirname; // extensions/openrtx.ts → dir is the extensions/ folder
	return path.join(dir, "..", "bin", "openrtx");
}

const cliPath = resolveCliPath();

/** Check whether the CLI binary exists (graceful degradation). */
function cliAvailable(): boolean {
	try {
		require("fs").accessSync(cliPath, require("fs").constants.X_OK);
		return true;
	} catch {
		return false;
	}
}

const openrtxTool = {
	name: "openrtx_run",
	label: "OpenRTX CLI",
	description:
		"Run the openrtx CLI to query the local OpenRTX project cache (Taiga, GitHub issues/PRs, Office Hours notes). " +
		"Examples: 'openrtx sources', 'openrtx search littlefs power loss', 'openrtx recent --since 7d', " +
		"'openrtx show taiga#841'. See the research skill for full command reference.",
	promptSnippet: "Run openrtx CLI commands to query the OpenRTX project cache.",
	promptGuidelines: [
		"Use `openrtx_run` to query the local OpenRTX cache; it wraps the `openrtx` CLI.",
		"Read the research skill (`skills/research/SKILL.md`) for full command reference and best practices.",
	],
	parameters: Type.Object({
		command: Type.String({ description: "The openrtx command to run (without the 'openrtx' prefix, e.g. 'sources', 'search littlefs power loss')" }),
	}),

	async execute(_toolCallId, params, _ctx) {
		const fullCommand = `"${cliPath}" ${params.command}`;

		if (!cliAvailable()) {
			return {
				content: [{ type: "text", text: `openrtx CLI not found at ${cliPath}. Install it by running \`pi install git:github.com/turnrye/openrtx-dev-ai\` or clone the repo and symlink \`bin/openrtx\` to your PATH.` }],
				details: { error: "cli_not_found" },
			};
		}

		try {
			const output = execFileSync(cliPath, params.command.split(/\s+/), {
				cwd: ctx.cwd,
				encoding: "utf-8",
				timeout: 60_000,
			});

			return {
				content: [{ type: "text", text: output.trim() || "(no output)" }],
				details: { command: params.command, exitCode: 0 },
			};
		} catch (err: unknown) {
			const message = err instanceof Error ? err.message : String(err);
			return {
				content: [{ type: "text", text: `openrtx failed: ${message}` }],
				details: { command: params.command, error: message },
			};
		}
	},
};

export default function (pi: ExtensionAPI) {
	// Register the openrtx_run tool
	pi.registerTool(openrtxTool);

	// Add PATH context so the model knows about openrtx
	pi.on("before_agent_start", (event) => {
		if (event.messageType !== "user") return;

		const cliHint = cliAvailable()
			? `The \`openrtx\` CLI is available at \`${cliPath}\`. You can also use the \`openrtx_run\` tool to run openrtx commands.`
			: `The \`openrtx\` CLI is not installed. Install it via \`pi install git:github.com/turnrye/openrtx-dev-ai\` or clone the repo and symlink \`bin/openrtx\` to your PATH.`;

		event.systemPromptOptions.guidelines = [
			...(event.systemPromptOptions.guidelines ?? []),
			cliHint,
		];
	});

	// Register a /openrtx slash command for quick access
	pi.registerCommand("openrtx", {
		description: "Run an openrtx CLI command and show the result",
		handler: async (args, ctx) => {
			if (!args.trim()) {
				ctx.ui.notify("Usage: /openrtx <command>", "info");
				return;
			}

			if (!cliAvailable()) {
				ctx.ui.notify(`openrtx CLI not found at ${cliPath}`, "error");
				return;
			}

			try {
				const output = execFileSync(cliPath, args.trim().split(/\s+/), {
					cwd: ctx.cwd,
					encoding: "utf-8",
					timeout: 60_000,
				});

				ctx.ui.notify(output.trim() || "(no output)", "info");
			} catch (err: unknown) {
				const message = err instanceof Error ? err.message : String(err);
				ctx.ui.notify(`openrtx failed: ${message}`, "error");
			}
		},
	});
}
