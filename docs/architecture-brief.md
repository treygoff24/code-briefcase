# Code Briefcase — architecture brief for second-opinion review

You are being asked for an architectural opinion. The user wants the **most resilient, durable, and performant** answer to "if we rebuilt this tool greenfield, what language and architecture would we use?" — independent of switching costs, dev velocity tradeoffs, or familiarity to the current author. Optimize for the tool's long-term shape, not for migration practicality.

## What we're building and why

**Code Briefcase** is a context-decoration and diagnostics layer for AI coding agents — Claude Code, Codex CLI, Cursor Composer, opencode, and similar. The agent's native tool calls (`Read`, `Edit`, `Bash`, `SessionStart`) get intercepted via each harness's hook protocol, and code-briefcase injects:

1. **Structural nav maps** for files being read or edited — imports, functions, classes, signatures, related files
2. **Diagnostics in the loop** — after every edit, run tsc / oxlint / oxfmt / equivalent and surface errors back to the agent in the post-edit hook
3. **Call-graph awareness** — what does this symbol call, what calls it, what's the impact radius

The thesis: agents waste enormous token budgets on grep/find/Read loops trying to understand structure they could be handed. We give them that structure automatically on every tool call, push-mode, whether they ask for it or not.

The two non-negotiable differentiators relative to competitors:

- **Push-mode interception.** Most code-intelligence tools (CodeGraph, codedb, ctags, LSPs) are pull-mode: the agent has to know to call them. Hook-mode fires automatically on the agent's existing tool calls. This is load-bearing: when an explorer sub-agent does a dumb Read, pull-mode tools contribute nothing — hooks still fire.
- **Diagnostics-in-the-loop.** Post-edit feedback from the real type checker / linter / formatter, in the same context window as the edit. Nobody else does this end-to-end.

## Current state

- **Language:** Python 3.10+, AGPL-3.0
- **Size:** ~30k LOC in `code_briefcase/`
- **Distribution:** `pipx install code-briefcase` (Python install, not single-binary)

### Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Agent harness (Claude Code / Codex / Cursor / opencode)    │
│  fires hooks on Read / Edit / SessionStart / PreCompact     │
└────────────────────────┬────────────────────────────────────┘
                         │ stdin JSON
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  code-briefcase hook handlers (one Python process per       │
│  hook invocation — ~100-200ms cold start)                   │
│  code_briefcase/hooks/{read,edit,post_edit,session}.py      │
└────────────────────────┬────────────────────────────────────┘
                         │ Unix socket
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  Daemon (long-running, one per project)                     │
│  code_briefcase/daemon/                                     │
│    - holds extracted-file cache in memory                   │
│    - watches files via FSEvents/inotify                     │
│    - supervises tsc --watch / oxlint subprocesses           │
│    - serves queries over JSON-RPC-like Unix-socket protocol │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  Indexer (tree-sitter via Python bindings)                  │
│  code_briefcase/hybrid_extractor.py — 4000+ LOC monolithic  │
│  Extracts: functions, classes, imports, call graph          │
│  Languages: Python, TS/JS, Go, Rust, Java, C/C++, Ruby,     │
│  PHP, Kotlin, Swift, C#, Scala, Lua, Luau, Elixir (~15)     │
└─────────────────────────────────────────────────────────────┘
```

### Storage

- Per-file extraction cache (JSON in `.code-briefcase/cache/`)
- Targeted-read dedup state (JSON, session-scoped)
- Daemon state (PID lock, socket path)
- No SQL database currently (a half-built `stacked_db.py` exists but unused)
- Diagnostic results cached in `tsc_cache.py`

### Incremental computation

We have hand-rolled Salsa-inspired modules: `salsa.py`, `dirty_flag.py`, `incremental_parse.py`, `change_impact.py`. These reimplement (incompletely) what the Rust Salsa crate provides natively — memoized, dependency-tracked, incremental queries.

### MCP server

`code_briefcase/mcp_server.py` exposes tools (`context`, `impact`, `calls`, etc.) over MCP for agents that want to pull-query the index. Less developed than CodeGraph's MCP surface.

### Hooks layer

`code_briefcase/hooks/` — the differentiator. Handlers for:
- `SessionStart` — warm daemon, emit project orientation
- `PreToolUse:Read` — emit nav map, inject `limit=200` to discourage whole-file reads
- `PreToolUse:Edit` — emit pre-edit structure
- `PostToolUse:Edit` — run diagnostics, return errors as `additionalContext`
- `UserPromptSubmit`, `PreCompact`, `Stop`, etc.

Each hook is invoked as a fresh subprocess by the agent harness, reads JSON from stdin, writes JSON to stdout. Latency target: ≤100ms ideally; currently ~150-300ms due to Python interpreter startup before the hook even runs.

### Multi-language tooling

- Tree-sitter for AST extraction
- tsc (via `tsc --watch` daemon) for TypeScript diagnostics
- oxlint, oxfmt for JavaScript/TS lint+format
- mypy, ruff, black for Python diagnostics
- Pygments-tldr for signature extraction fallback

## Why we're considering a full refactor

Recent ergonomics review surfaced architectural debt and a competitive landscape we should reckon with:

### Two close competitors

**CodeGraph** (TypeScript, MIT, https://github.com/colbymchenry/codegraph)
- Pull-mode MCP server: `context`, `trace`, `explore`, `callers`, `callees`, `impact`, `search`, `node`
- SQLite knowledge graph with FTS5, two-pass resolution via `unresolved_refs` table
- 19 languages, each as a small declarative descriptor (~100 LOC each, ~2k total)
- **6936 LOC of framework-route awareness** across 21 frameworks (Django/Flask/FastAPI/Express/NestJS/Laravel/Rails/Spring/Rust axum+actix+rocket/Go gin+chi/ASP.NET/Vapor/Svelte/Nuxt) plus iOS/RN/Expo cross-language bridges (Swift↔ObjC, RN legacy bridge + TurboModules + Fabric, Expo Modules DSL)
- 7-agent installer with migration-aware config patching
- Native FSEvents/inotify watcher + 2s debounce + connect-time reconciliation
- Daemon-sharing across multi-agent sessions on the same project
- Battle-tuned MCP tool prompts with embedded Bad/Good examples
- Bundles its own Node runtime → curl-install → single binary
- Their published benchmark: 35% cheaper / 71% fewer tool calls on 7 real-world repos (VS Code, Django, Excalidraw, Tokio, etc.)
- Their README admits the killer weakness: "CodeGraph only helps when queried directly — otherwise a sub-agent reads files regardless and CodeGraph becomes overhead." This is exactly the gap our hooks layer closes.

**codedb** (Zig, BSD-3, https://github.com/justrach/codedb)
- 16 MCP tools including atomic line-range edits with version tracking and multi-agent locking
- In-memory trigram + inverted word index + dependency graph + structural outlines
- Sub-ms warm queries (vs ripgrep ~5ms)
- Polling file watcher (2s)
- Novel: `codedb_remote` queries already-indexed public GitHub repos via a cloud service — no local clone needed
- Zero dependencies, single Zig binary
- Alpha, ~1k stars, smaller community

Neither competitor has push-mode hooks. Neither has diagnostics-in-the-loop. Both have a substantially better indexer than ours.

### Where Python actively hurts us

- **Hook startup latency.** Python interpreter + module imports cost ~100-200ms per hook invocation, before the hook does any work. The daemon mitigates query latency but every hook spawn pays the Python startup tax.
- **Distribution.** `pipx install` requires a Python runtime on the user's machine. CodeGraph and codedb both ship as `curl install.sh | sh → single binary`. We can't, easily — PyInstaller/Nuitka produce 50MB+ artifacts and have their own bugs.
- **Memory footprint.** Python runtime + extracted-symbol dicts + tree-sitter native modules. With multi-agent orchestration (3+ AI agents on the same repo simultaneously becoming common), this compounds.
- **Salsa coincidence.** Our `salsa.py` / `dirty_flag.py` / `incremental_parse.py` are hand-ported reimplementations of the Rust Salsa crate that powers rust-analyzer. Strong signal we're fighting the language for the incremental-computation hot path.
- **Indexer monolith.** Our `hybrid_extractor.py` is 4000+ LOC in one file vs CodeGraph's modular ~100-LOC-per-language descriptors. This is a code organization problem, not a language problem, but the rewrite is a natural moment to fix it.

### What we want from a refactor

A merged best-of-all-worlds:
- Our **hook-mode interception** (push)
- Our **diagnostics-in-the-loop** (post-edit tsc/oxlint/oxfmt)
- CodeGraph's **modular language-extractor architecture** (declarative per-language descriptors)
- CodeGraph's **framework-route + cross-language-bridge awareness**
- CodeGraph's **two-pass resolution with unresolved_refs**
- CodeGraph's **battle-tuned MCP tool prompts and 7-agent installer**
- CodeGraph's **multi-agent daemon sharing**
- codedb's **atomic-edit + version-tracking** semantics (for safe concurrent multi-agent editing)
- codedb's **sub-ms warm query performance**
- codedb's **cloud index for public repos** (concept, not necessarily the implementation)
- **Single-binary distribution** like both competitors
- **20+ languages supported** for indexing

## Languages we've considered, and our reasoning

**Rust** — Tree-sitter's reference impl is C with Rust as the first-class binding. Sub-ms hook startup. Single-binary cross-compile is trivial. Memory-tight. Salsa exists as a real crate so we'd stop reimplementing it. rust-analyzer, ast-grep, watchexec are direct exemplars of the kind of program we're building. Downside: steeper learning curve, slower compile loop than dynamic languages, smaller pool of casual contributors.

**TypeScript / Node** — Best ecosystem fit. The agent ecosystem is TS: Claude Code, MCP SDK, Cursor, opencode, Hermes Agent all live in TS. CodeGraph picked TS — adopting their codebase or contributing upstream becomes mechanical. Bundle Node like CodeGraph does for single-binary distribution. ~50ms cold start is acceptable. Downside: heavier runtime than Rust, npm dependency churn.

**Go** — Solid middle. Single binary, fast startup, good concurrency, easy cross-compile. Sourcegraph's `scip-*` indexers are Go. Downside: tree-sitter binding (smacker/go-tree-sitter) is less actively maintained than Rust's; generics are limited; AST pattern-matching is uglier than Rust enums.

**Zig** — codedb's choice. Fastest, zero-dependency, tiny binaries. Downside: alpha language, small ecosystem, very high contributor-onboarding friction.

**Keeping Python** — Bottom of our list on the dimensions that actually matter for this workload (startup, distribution, memory).

## What we're asking you for

Set aside switching costs, dev velocity for the current author, comfort, and "what would be easiest to migrate to." **Optimize for the tool's long-term shape over the next decade.**

If we were greenfield, building this tool from zero, with no path-dependence and no constraints other than "make it the most resilient, durable, and performant version of itself":

1. **What language?** Pick one. If a polyglot answer is genuinely the right answer (hook layer in X, indexer in Y), say so, but defend it — multi-language stacks have real costs.

2. **What architecture?** How does the hook layer talk to the daemon? Where does the index live? Embedded SQLite? Bespoke memory-mapped store? Process model (single daemon, daemon-per-project, daemon-per-agent)? How do we share the daemon across multiple concurrent AI agents?

3. **What internal structure?** How modular should the language extractors be? Where does framework-awareness live (extractor-side or resolver-side)? How do we handle the two-pass resolution problem? How do we model incremental recomputation (Salsa-style query graph? Something else)?

4. **What's the install/distribution model?** Single binary? Bundled runtime? Per-agent MCP server vs per-project daemon?

5. **What would you keep from CodeGraph or codedb conceptually, even if reimplemented?** What ideas in those projects are first-principles correct vs accidents of their language/timing?

6. **What's a non-obvious architectural choice that would matter more than people expect?** Specifically — what would *you* design in that nobody currently does?

Be opinionated. We can afford to hear "you're wrong about X" — that's the value of asking. We don't need a balanced survey of options; we need *your* answer to "what is the best version of this tool."
