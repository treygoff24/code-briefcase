# Fork Status and Attribution

This repository is a divergent fork of [parcadei/llm-tldr](https://github.com/parcadei/llm-tldr).

## Upstream

- Original project: https://github.com/parcadei/llm-tldr
- Local fork remote: https://github.com/treygoff24/llm-tldr
- Upstream remote: `https://github.com/parcadei/llm-tldr`

The local `upstream` remote is intentionally configured with push disabled to avoid accidental pushes to the original project.

## License

This fork remains licensed under AGPL-3.0. Keep `LICENSE` and `NOTICE` intact when distributing modified versions.

The existing `NOTICE` file also preserves attribution for the earlier MIT-licensed `tldr-code` components that this project includes or derives from.

## Fork Direction

The current fork direction is to build a more agent-oriented context system:

- global Claude Code and Codex integration
- installable hook runtime
- MCP hardening
- budgeted task context packs
- safer daemon/cache behavior
- attribution-preserving packaging and docs

Implementation planning lives in:

- `docs/plans/2026-05-16-agent-context-integration.md`

## Publishing Rules

Before publishing this fork publicly or distributing packages:

1. Preserve upstream Git history where possible.
2. Keep `LICENSE` and `NOTICE`.
3. Do not imply the original maintainer endorses this fork.
4. Rename/rebrand if the fork diverges enough to confuse users.
5. Do not publish to PyPI under `llm-tldr` unless ownership/maintainer rights and naming are clearly resolved.
6. Document source availability for any network-accessible deployment, consistent with AGPL-3.0 obligations.

## Upstream Sync Practice

Use:

    git fetch upstream
    git log --oneline --left-right --graph upstream/main...main

Then selectively merge or cherry-pick upstream changes when they are relevant to the fork.

