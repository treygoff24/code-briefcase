# Code Briefcase: Code Analysis for AI Agents

[![PyPI](https://img.shields.io/pypi/v/code-briefcase)](https://pypi.org/project/code-briefcase/)
[![Python](https://img.shields.io/pypi/pyversions/code-briefcase)](https://pypi.org/project/code-briefcase/)
[![License](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)

> **Fork status:** This repository is a divergent fork of
> [parcadei/llm-tldr](https://github.com/parcadei/llm-tldr), currently focused
> on agent-context workflows for Claude Code, Codex, hooks, MCP, and budgeted
> context packs. See [FORK.md](FORK.md) for attribution, license notes, and
> publishing rules.

**Give LLMs exactly the code they need. Nothing more.**

```bash
pipx install code-briefcase

# Manual use
code-briefcase pack "understand auth flow" --project . --budget 3000
code-briefcase context main --project .

# Agent integration dry-run (does not mutate global config)
code-briefcase hooks doctor
code-briefcase hooks install claude --scope global --dry-run
code-briefcase hooks install codex --scope global --dry-run
code-briefcase hooks install droid --scope global --dry-run
code-briefcase hooks install opencode --scope global --dry-run
```

Your codebase is 100K lines. Claude's context window is 200K tokens. Raw code won't fit—and even if it did, the LLM would drown in irrelevant details.

Code Briefcase extracts *structure* instead of dumping *text*. The result: **95% fewer tokens** while preserving everything needed to understand and edit code correctly.

```bash
pip install code-briefcase
code-briefcase warm .                    # Build the call graph cache
code-briefcase context main --project .  # Get LLM-ready summary
```

Semantic search is opt-in: run `code-briefcase semantic index .` when you want embeddings.
The session-start hook never downloads the semantic model.

### Agent Hook Integration

Code Briefcase can run as a package-owned hook runtime for Claude Code, Codex, Factory
Droid, and OpenCode:

```bash
code-briefcase pack "fix login bug" --project . --budget 3000
code-briefcase hooks run pre-read --client claude
code-briefcase hooks install claude --scope global --dry-run
code-briefcase hooks install codex --scope global --dry-run
code-briefcase hooks install droid --scope global --dry-run
code-briefcase hooks install opencode --scope global --dry-run
```

Claude hooks are the most automatic path because Claude hook JSON supports
permission decisions, updated tool input, and additional context before reads.
Codex hooks now cover session start, edit diagnostics, prompt-secret blocking,
optional permission guards, default non-blocking Bash/shell `pre-tool` file-intent
context, and no-op lifecycle hooks such as stop/session-end where the client
requires silence. Code Briefcase hook context supports code, tests, HTML, SQL, YAML/JSON,
shell/config files, and common shell command file references. Line-specific
reads get only a small orientation once per file/session; repeated targeted
reads stay quiet. Markdown/MDX, secrets, lockfiles, and generated dependency
trees remain excluded. Droid/Factory share the Claude-style hook config shape
for session, read/edit, prompt guard, tool guard, and compact-context events.
OpenCode uses a generated dependency-free JS plugin adapter instead of JSON hook
config. Cursor hook install remains disabled/experimental until a local hook
runtime is proven; use Cursor rules/MCP context for now.

---

## How It Works

Code Briefcase builds 5 analysis layers, each answering different questions:

```
┌─────────────────────────────────────────────────────────────┐
│ Layer 5: Program Dependence  → "What affects line 42?"      │
│ Layer 4: Data Flow           → "Where does this value go?"  │
│ Layer 3: Control Flow        → "How complex is this?"       │
│ Layer 2: Call Graph          → "Who calls this function?"   │
│ Layer 1: AST                 → "What functions exist?"      │
└─────────────────────────────────────────────────────────────┘
```

**Why layers?** Different tasks need different depth:
- Browsing code? Layer 1 (structure) is enough
- Refactoring? Layer 2 (call graph) shows what breaks
- Debugging null? Layer 5 (slice) shows only relevant lines

The daemon keeps indexes in memory for **100ms queries** instead of 30-second CLI spawns.

### Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                         YOUR CODE                                │
│  src/*.py, lib/*.ts, pkg/*.go                                    │
└───────────────────────────┬──────────────────────────────────────┘
                            │ tree-sitter
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│                     5-LAYER ANALYSIS                             │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐     │
│  │   AST   │→│  Calls  │→│   CFG   │→│   DFG   │→│   PDG   │     │
│  │   L1    │ │   L2    │ │   L3    │ │   L4    │ │   L5    │     │
│  └─────────┘ └─────────┘ └─────────┘ └─────────┘ └─────────┘     │
└───────────────────────────┬──────────────────────────────────────┘
                            │ bge-large-en-v1.5
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│                    SEMANTIC INDEX                                │
│  1024-dim embeddings in FAISS  →  "find JWT validation"          │
└───────────────────────────┬──────────────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│                       DAEMON                                     │
│  In-memory indexes  •  100ms queries  •  Auto-lifecycle          │
└──────────────────────────────────────────────────────────────────┘
```

### The Semantic Layer: Search by Behavior

The real power comes from combining all 5 layers into **searchable embeddings**.

Every function gets indexed with:
- Signature + docstring (L1)
- What it calls + who calls it (L2)
- Complexity metrics (L3)
- Data flow patterns (L4)
- Dependencies (L5)
- First ~10 lines of actual code

This gets encoded into **1024-dimensional vectors** using `bge-large-en-v1.5`. The result: search by *what code does*, not just what it says.

```bash
# "validate JWT" finds verify_access_token() even without that exact text
code-briefcase semantic "validate JWT tokens and check expiration" .
```

**Why this works:** Traditional search finds `authentication` in variable names and comments. Semantic search understands that `verify_access_token()` *performs* JWT validation because the call graph and data flow reveal its purpose.

### Setting Up Semantic Search

```bash
# Build the semantic index (one-time, explicit)
code-briefcase semantic index /path/to/project

# Search by behavior
code-briefcase semantic "database connection pooling" .
```

Embedding dependencies (`sentence-transformers`, `faiss-cpu`) are included with
`pip install code-briefcase`. The index is cached under `.code-briefcase/cache/semantic/`.

### Keeping the Index Fresh

The daemon tracks dirty files and auto-rebuilds after 20 changes, but you need to notify it when files change:

```bash
# Notify daemon of a changed file
code-briefcase daemon notify src/auth.py --project .
```

**Integration options:**

1. **Git hook** (post-commit):
   ```bash
   git diff --name-only HEAD~1 | xargs -I{} code-briefcase daemon notify {} --project .
   ```

2. **Editor hook** (on save):
   ```bash
   code-briefcase daemon notify "$FILE" --project .
   ```

3. **Manual rebuild** (when needed):
   ```bash
   code-briefcase warm .  # Full rebuild
   ```

The daemon auto-rebuilds semantic embeddings in the background once the dirty threshold (default: 20 files) is reached.

---

## The Workflow

### Before Reading Code
```bash
code-briefcase tree src/                      # See file structure
code-briefcase structure src/ --lang python   # See functions/classes
```

### Before Editing
```bash
code-briefcase extract src/auth.py            # Full file analysis
code-briefcase context login --project .      # LLM-ready summary (95% savings)
```

### Before Refactoring
```bash
code-briefcase impact login .                 # Who calls this? (reverse call graph)
code-briefcase change-impact                  # Which tests need to run?
```

### Debugging
```bash
code-briefcase slice src/auth.py login 42     # What affects line 42?
code-briefcase dfg src/auth.py login          # Trace data flow
```

### Finding Code by Behavior
```bash
code-briefcase semantic "validate JWT tokens" .   # Natural language search
```

---

## Quick Setup

### 1. Install

```bash
pip install code-briefcase
```

### 2. Index Your Project

```bash
code-briefcase warm /path/to/project
```

This builds all analysis layers and starts the daemon. Takes 30-60 seconds for a typical project, then queries are instant.

### 3. Start Using

```bash
code-briefcase context main --project .   # Get context for a function
code-briefcase impact helper_func .       # See who calls it
code-briefcase semantic "error handling"  # Find by behavior
```

---

## Real Example: Why This Matters

**Scenario:** Debug why `user` is null on line 42.

**Without Code Briefcase:**
1. Read the 150-line function
2. Trace every variable manually
3. Miss the bug because it's hidden in control flow

**With Code Briefcase:**
```bash
code-briefcase slice src/auth.py login 42
```

**Output:** Only 6 lines that affect line 42:
```python
3:   user = db.get_user(username)
7:   if user is None:
12:      raise NotFound
28:  token = create_token(user)  # ← BUG: skipped null check
35:  session.token = token
42:  return session
```

The bug is obvious. Line 28 uses `user` without going through the null check path.

---

## Command Reference

### Exploration
| Command | What It Does |
|---------|--------------|
| `code-briefcase tree [path]` | File tree |
| `code-briefcase structure [path] --lang <lang>` | Functions, classes, methods |
| `code-briefcase search <pattern> [path]` | Text pattern search |
| `code-briefcase extract <file>` | Full file analysis |

### Analysis
| Command | What It Does |
|---------|--------------|
| `code-briefcase context <func> --project <path>` | LLM-ready summary (95% savings) |
| `code-briefcase cfg <file> <function>` | Control flow graph |
| `code-briefcase dfg <file> <function>` | Data flow graph |
| `code-briefcase slice <file> <func> <line>` | Program slice |

### Cross-File
| Command | What It Does |
|---------|--------------|
| `code-briefcase calls [path]` | Build call graph |
| `code-briefcase impact <func> [path]` | Find all callers (reverse call graph) |
| `code-briefcase dead [path]` | Find unreachable code |
| `code-briefcase arch [path]` | Detect architecture layers |
| `code-briefcase imports <file>` | Parse imports |
| `code-briefcase importers <module> [path]` | Find files that import a module |

### Semantic
| Command | What It Does |
|---------|--------------|
| `code-briefcase warm <path>` | Build all indexes (including embeddings) |
| `code-briefcase semantic <query> [path]` | Natural language code search |

### Diagnostics
| Command | What It Does |
|---------|--------------|
| `code-briefcase diagnostics <file>` | Type check + lint/format diagnostics |
| `code-briefcase change-impact [files]` | Find tests affected by changes |
| `code-briefcase doctor` | Check/install diagnostic tools |

### Daemon
| Command | What It Does |
|---------|--------------|
| `code-briefcase daemon start` | Start background daemon |
| `code-briefcase daemon stop` | Stop daemon |
| `code-briefcase daemon status` | Check status |

---

## Supported Languages

Python, TypeScript, JavaScript, Go, Rust, Java, C, C++, Ruby, PHP, C#, Kotlin, Scala, Swift, Lua, Elixir

Diagnostics for TypeScript and JavaScript use `tsc` for type checking plus
`oxlint` and `oxfmt` for lint/format feedback when those tools are installed.
Single-file JS/TS diagnostics are project-aware: Code Briefcase extends the nearest
`tsconfig.json`/`jsconfig.json` in an ephemeral one-file config so path aliases
and JSX settings work without leaking unrelated project-wide errors.

Language is auto-detected or specify with `--lang`.

---

## MCP Integration

For AI tools (Claude Desktop, Claude Code, Codex), MCP exposes explicit manual
Code Briefcase tool calls. Hooks provide automatic context; MCP is the portable fallback.

**Claude Desktop** - Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "code-briefcase": {
      "command": "code-briefcase-mcp",
      "args": ["--project", "auto"]
    }
  }
}
```

**Claude Code** - Add to `.claude/settings.json`:
```json
{
  "mcpServers": {
    "code-briefcase": {
      "command": "code-briefcase-mcp",
      "args": ["--project", "auto"]
    }
  }
}
```

`--project auto` resolves from `CODE_BRIEFCASE_PROJECT`, `CLAUDE_PROJECT_DIR`,
`CODEX_PROJECT_DIR`, `CODEX_CWD`, `PWD`, then the current directory. Explicit
tool-level `project` arguments still override the environment.

---

## Configuration

### `.code-briefcaseignore` - Exclude Files

Code Briefcase respects `.code-briefcaseignore` (gitignore syntax) for all commands including `tree`, `structure`, `search`, `calls`, and semantic indexing:

```bash
# Auto-create with sensible defaults
code-briefcase warm .  # Creates .code-briefcaseignore if missing
```

**Default exclusions:**
- `node_modules/`, `.venv/`, `__pycache__/`
- `dist/`, `build/`, `*.egg-info/`
- Binary files (`*.so`, `*.dll`, `*.whl`)
- Security files (`.env`, `*.pem`, `*.key`)

**Customize** by editing `.code-briefcaseignore`:
```gitignore
# Add your patterns
large_test_fixtures/
vendor/
data/*.csv
```

**CLI Flags:**
```bash
# Add patterns from command line (can be repeated)
code-briefcase --ignore "packages/old/" --ignore "*.generated.ts" tree .

# Bypass all ignore patterns
code-briefcase --no-ignore tree .
```

### Settings - Daemon Behavior

Create `.code-briefcase/config.json` for daemon settings:

```json
{
  "semantic": {
    "enabled": true,
    "auto_reindex_threshold": 20
  }
}
```

| Setting | Default | Description |
|---------|---------|-------------|
| `enabled` | `true` | Enable semantic search |
| `auto_reindex_threshold` | `20` | Files changed before auto-rebuild |

### Monorepo Support

For monorepos, create `.claude/workspace.json` to scope indexing:

```json
{
  "active_packages": ["packages/core", "packages/api"],
  "exclude_patterns": ["**/fixtures/**"]
}
```

---

## Performance

| Metric | Raw Code | Code Briefcase | Improvement |
|--------|----------|------|-------------|
| Tokens for function context | 21,000 | 175 | **99% savings** |
| Tokens for codebase overview | 104,000 | 12,000 | **89% savings** |
| Query latency (daemon) | 30s | 100ms | **300x faster** |

---

## Outcome telemetry and reports

Enable privacy-safe hook telemetry with `CODE_BRIEFCASE_TELEMETRY=1`, then backfill sanitized session rollups and render daily outcome reports. See [docs/dev-notes/outcome-telemetry.md](./docs/dev-notes/outcome-telemetry.md) for schema details, confidence labels, and interpretation.

```bash
# Fixture-safe smoke (no real agent logs required)
python3 scripts/backfill_tldr_outcomes.py \
  --start 2026-05-20T00:00:00Z --end 2026-05-21T00:00:00Z \
  --codex-root tests/fixtures/eval/backfill_codex_root \
  --claude-root tests/fixtures/eval/backfill_claude_root \
  --code-briefcase-telemetry tests/fixtures/eval/backfill_tldr_telemetry.jsonl \
  --json-out /tmp/code-briefcase-backfill-fixture.json
```

Generated `reports/*.json`, `reports/*.md`, and `reports/*.html` are local artifacts and are gitignored by default.

For local-only dogfooding where you want richer raw evidence, opt into `CODE_BRIEFCASE_TELEMETRY_MODE=local-rich` and run backfill with `--include-local-evidence`. This keeps default secret hygiene but includes readable paths and sanitized tool-call evidence in local reports.

---

## Deep Dive

For the full architecture explanation, benchmarks, and advanced workflows:

**[Full Documentation](./docs/Code Briefcase.md)**

---

## License

AGPL-3.0 - See LICENSE file.
