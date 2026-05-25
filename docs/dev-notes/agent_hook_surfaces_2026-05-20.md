# Agent Hook Surfaces Research Appendix

Date: 2026-05-20
Repo: `/Users/treygoff/Code/code-briefcase`

## Local versions observed

- Codex CLI: `codex-cli 0.131.0`
- Droid / Factory CLI: `droid 0.129.0`
- Cursor Agent CLI: `cursor-agent 2026.05.16-0338208`
- Cursor app CLI: `3.5.11`, build `4830a52b57b7283f2d1ae93f8121d2b10cfb8420`, `arm64`
- OpenCode: `1.14.29`
- `factory` binary: not present locally; Code Briefcase `factory` is only an alias for Droid hook configuration.

## Sources checked

- Codex hooks docs: https://developers.openai.com/codex/hooks
- Codex config reference: https://developers.openai.com/codex/config-reference
- Codex repository docs pointer: https://raw.githubusercontent.com/openai/codex/main/docs/config.md
- Context7 Codex docs lookup: `/websites/developers_openai_codex`
- Factory hooks reference: https://docs.factory.ai/reference/hooks-reference
- Factory settings docs: https://docs.factory.ai/factory-cli/configuration/settings
- Cursor CLI using docs: https://docs.cursor.com/en/cli/using
- Cursor CLI configuration docs: https://docs.cursor.com/en/cli/reference/configuration
- Cursor rules docs: https://docs.cursor.com/en/context
- Cursor marketplace hook evidence: https://cursor.com/marketplace/hooks/pretooluse
- OpenCode config docs: https://opencode.ai/docs/config/
- OpenCode plugins docs: https://opencode.ai/docs/plugins/

## Codex CLI 0.131.0 surface

Stable enough for implementation.

Events documented for release behavior:

- `SessionStart`
- `PreToolUse`
- `PermissionRequest`
- `PostToolUse`
- `UserPromptSubmit`
- `Stop`

Config locations:

- `~/.codex/hooks.json`
- `~/.codex/config.toml`
- `<repo>/.codex/hooks.json`
- `<repo>/.codex/config.toml`

Important constraints:

- Hooks can also live inline under `[hooks]` in TOML, but Code Briefcase's current installer writes JSON and should keep doing that for now.
- Matching hooks from multiple files all run; layers do not replace lower-precedence hooks.
- Command hooks for the same event are launched concurrently.
- Non-managed hooks must be reviewed/trusted through `/hooks`.
- Plugin hooks are opt-in behind `[features].plugin_hooks = true`.
- Project-local hooks load only when the project `.codex/` layer is trusted.
- `PreToolUse`, `PermissionRequest`, `PostToolUse`, `UserPromptSubmit`, and `Stop` run at turn scope.
- `PreToolUse` and `PostToolUse` currently intercept Bash, `apply_patch` file edits, and MCP tool calls, but not every shell/tool path.

Codex matcher behavior:

- `SessionStart`: source, values `startup`, `resume`, `clear`.
- `PreToolUse`, `PermissionRequest`, `PostToolUse`: tool name / aliases. Current values include `Bash`, `apply_patch`, MCP names, plus `Edit`/`Write` aliases for `apply_patch`.
- `UserPromptSubmit`, `Stop`: matcher ignored.

## Droid / Factory CLI 0.129.0 surface

Stable enough for implementation, but use Droid tool names rather than Claude/Codex names.

Config locations:

- `~/.factory/settings.json`
- `.factory/settings.json`
- `.factory/settings.local.json`
- enterprise managed policy settings

Events:

- `PreToolUse`
- `PostToolUse`
- `Notification`
- `UserPromptSubmit`
- `Stop`
- `SubagentStop`
- `PreCompact`
- `SessionStart`
- `SessionEnd`

Common tool matchers:

- `Task`
- `Execute`
- `Glob`
- `Grep`
- `Read`
- `Edit`
- `Create`
- `FetchUrl`
- `WebSearch`
- docs examples also use `ApplyPatch`

Important constraints:

- Use absolute hook command paths. For project scripts, `$FACTORY_PROJECT_DIR` is available when Droid spawns the hook.
- `PreCompact` matchers: `manual`, `auto`.
- `SessionStart` matchers: `startup`, `resume`, `clear`, `compact`.
- Exit code `0` with stdout is user-visible in transcript mode except `UserPromptSubmit` and `SessionStart`, where stdout is added to context.
- Exit code `2` blocks/feeds feedback depending on event.
- JSON output supports common fields on all hook types; event-specific decision fields vary.

## Cursor CLI and app surface

Not stable enough for default hook installation.

Confirmed stable docs:

- Cursor CLI supports rules from `.cursor/rules`, root `AGENTS.md`, and root `CLAUDE.md`.
- Cursor CLI supports MCP using `mcp.json` configured for the IDE.
- Cursor CLI config paths are `~/.cursor/cli-config.json` globally and `<project>/.cursor/cli.json` locally.
- Cursor Marketplace exposes plugins containing hooks, skills, agents, commands, and MCP entries.

Unstable/unconfirmed for Code Briefcase:

- Official Cursor docs checked do not provide a complete hook reference comparable to Codex or Factory.
- Marketplace/community evidence indicates hooks exist, but Code Briefcase must not default-install Cursor hooks until a local runtime smoke proves the exact hook path, payload, and output schema for the installed version.

## OpenCode 1.14.29 surface

Stable enough for a generated JS adapter, not for JSON hook config.

Config/plugin locations:

- `~/.config/opencode/opencode.json`
- project `opencode.json`
- global plugin files under `~/.config/opencode/plugins/`
- project plugin files under `.opencode/plugins/`

Documented plugin events:

- Command: `command.executed`
- File: `file.edited`, `file.watcher.updated`
- Installation: `installation.updated`
- LSP: `lsp.client.diagnostics`, `lsp.updated`
- Message: `message.part.removed`, `message.part.updated`, `message.removed`, `message.updated`
- Permission: `permission.asked`, `permission.replied`
- Server: `server.connected`
- Session: `session.created`, `session.compacted`, `session.deleted`, `session.diff`, `session.error`, `session.idle`, `session.status`, `session.updated`
- Todo: `todo.updated`
- Shell: `shell.env`
- Tool: `tool.execute.before`, `tool.execute.after`
- TUI: `tui.prompt.append`, `tui.command.execute`, `tui.toast.show`
- Experimental compaction: `experimental.session.compacting`

Important constraints:

- OpenCode plugins are JS/TS functions, not shell command hook declarations.
- The plugin receives `{ project, client, $, directory, worktree }` and returns an object with event callbacks.
- Local plugin files are auto-loaded at startup.
- No direct documented `UserPromptSubmit` equivalent was found in the plugin event list.
