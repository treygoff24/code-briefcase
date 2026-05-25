# Fork Status and Attribution

Code Briefcase is a divergent fork of [parcadei/llm-tldr](https://github.com/parcadei/llm-tldr).

## Upstream

- Original project: https://github.com/parcadei/llm-tldr
- Fork repository: https://github.com/treygoff24/code-briefcase
- Upstream remote: `https://github.com/parcadei/llm-tldr`

The local upstream remote should remain fetch-only/read-only. Do not imply the original maintainer endorses this fork.

## License

This fork remains licensed under AGPL-3.0. Keep `LICENSE` and `NOTICE` intact when distributing modified versions.

The existing `NOTICE` file also preserves attribution for the earlier MIT-licensed `tldr-code` components that this project includes or derives from.

## Fork Direction

Code Briefcase is an agent-context runtime for coding agents:

- Claude Code, Codex, Factory Droid, and OpenCode hook integration
- installable hook runtime
- MCP hardening
- budgeted task context packs
- safer daemon/cache behavior
- attribution-preserving packaging and docs

Historical implementation planning lives in `docs/plans/` and may reference the upstream TLDR name because those files preserve development history.

## Publishing Rules

Before publishing this fork publicly or distributing packages:

1. Preserve upstream Git history where possible.
2. Keep `LICENSE` and `NOTICE`.
3. Do not imply the original maintainer endorses this fork.
4. Publish under the Code Briefcase name (`code-briefcase`) rather than `llm-tldr` or `tldr`.
5. Do not publish to PyPI under `llm-tldr`, `tldr`, or bare `briefcase`.
6. Document source availability for any network-accessible deployment, consistent with AGPL-3.0 obligations.

## Upstream Sync Practice

Use:

    git fetch upstream
    git log --oneline --left-right --graph upstream/main...main

Then selectively cherry-pick upstream changes when they are relevant to the fork.
