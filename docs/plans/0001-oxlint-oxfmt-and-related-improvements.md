# Plan 0001 — oxlint + oxfmt support for JS/TS, plus adjacent fixes

Status: **implemented**
Owner: trey + claude
Upstream: parcadei/llm-tldr · Fork: treygoff24/llm-tldr

## Revision log

- **2026-05-16 (initial)** — first draft.
- **2026-05-16 (revised)** — folded in 5 of 8 Codex review points: oxlint JSON shape must be empirically verified, monorepo walk fixed, subprocess timeouts added, per-language function extraction folded into consolidation, step order reversed so oxlint ships first under existing shape with tests as the safety net before refactor. Two Codex claims were verified and rejected: (a) `LANG_TOOLS` is **not** imported anywhere else — `grep` confirms it's defined once in `diagnostics.py:32` and that `TOOL_INFO` is a function-local variable in `cli.py:990`, so the "fix imports across 4 files" risk is fictional; (b) `extract` does **not** already accept `--format` — `cli.py:221-226` shows only `--class/--function/--method/--lang`, so this plan's compat fix is real, not redundant. One Codex point flagged for empirical verification before deciding: tsc `--allowJs` behavior on a no-config file (Codex's diagnosis of the existing code's tsconfig guard is false; the underlying behavioral question stands).
- **2026-05-16 (implemented)** — Step 0 fixtures captured with oxlint 1.65.0, oxfmt 0.50.0, and TypeScript 6.0.3. `oxlint --format=json` emits a top-level `diagnostics` array with `filename`, `code`, `severity`, and first-label `span.line`/`span.column`. `oxfmt --check` exits 0 for formatted files and 1 for drift; `.d.ts` remains skipped in the implementation per the beta/upstream-risk caveat. `tsc --noEmit --allowJs --pretty false` works without tsconfig for valid `.js`, `// @ts-check` type errors, and `.jsx`, so JavaScript type checking is wired directly.

## Why

Today `tldr diagnostics` for JS/TS only invokes `tsc --noEmit`. The `linter` slot for both languages is `None`/TODO. Modern JS/TS toolchains (this repo and most of Trey's projects) use the oxc family — `oxlint` for linting, `oxfmt` for formatting — both Rust-native, sub-second, and producing structured output. Wiring them in gives the post-edit hook real lint + format coverage where it currently has none, without forcing eslint/prettier on users who've moved on from them.

Three adjacent issues bundle into the same PR because they all touch the same code paths.

## Findings from exploration (verified)

### How diagnostics works

`tldr/diagnostics.py` and `tldr/cli.py` together encode each language's tool wiring twice:

1. **`tldr/diagnostics.py:32-93`** — `LANG_TOOLS: dict[str, dict]` maps language → `{type_checker, linter}` tool names. Used as documentation / metadata only; runtime ignores it. **Not imported anywhere else** (verified via `grep -rn "LANG_TOOLS" --include="*.py"`).
2. **`tldr/diagnostics.py:601-950`** — `get_diagnostics(file_path, ...)` has a giant `if/elif` over `lang`, each branch using `shutil.which(tool); subprocess.run(...); _parse_<tool>_output(stdout)`. This is what actually executes.
3. **`tldr/cli.py:985-1066`** — `TOOL_INFO` + `INSTALL_COMMANDS` are **function-local variables** inside the `doctor` subcommand handler. Not module-level, not imported.

Adding a new tool means touching (1), (2), (3), plus writing a `_parse_<tool>_output` helper and a test.

### JS/TS specifically (today)

- **TypeScript:** runs `tsc --noEmit --pretty false <file>` if `tsc` is on PATH. No tsconfig guard exists (confirmed: zero `tsconfig` references in `diagnostics.py`). No linter. (`diagnostics.py:654-667`)
- **JavaScript:** runs *nothing*. Both `type_checker` and `linter` are `None`. (`diagnostics.py` has no `elif lang == "javascript"` branch.)
- `shutil.which("tsc")` only finds globally-installed `tsc`; it doesn't look at `./node_modules/.bin/`. In any Node project where `typescript` is a devDep, this silently no-ops.

### oxlint / oxfmt CLI shape (claims to be verified empirically in step 0)

Per oxc.rs docs as of May 2026:
- **oxlint** is stable. `oxlint --format json [paths...]` emits structured JSON. Other formats: `default`, `github`, `gitlab`, `checkstyle`, `junit`, `sarif`, `stylish`, `unix`, `agent`. Exit 0 if clean, 1 otherwise.
- **oxfmt** is in beta as of 2026-02-24. `oxfmt --check [paths...]` exits 0 if formatted, 1 if drift. Known upstream bug oxc#19077 affects `.d.ts` files with `--check`.

The exact JSON envelope from oxlint is **not** assumed in this plan. Step 0 captures a real sample and the parser is derived from it.

## Scope

Bundled in this plan:

- Add **oxlint** as the JS/TS linter (`.ts`, `.tsx`, `.js`, `.jsx`).
- Add **oxfmt** as the JS/TS formatter check (same extensions, minus `.d.ts` per oxc#19077).
- Add **JS type checking via tsc** if step 0 confirms `tsc --noEmit --allowJs` works on no-config files (or with a guarded fallback if not).
- **Local-bin resolution** for JS/TS tools: walk up looking for `node_modules/.bin/<tool>`, with monorepo-aware root detection.
- **Subprocess timeouts** on every new tool invocation (matches existing pattern).
- **`tldr extract --format json`** accepted as a no-op arg (closes the silent-failure footgun from before).
- **Consolidation** of `LANG_TOOLS` + `TOOL_INFO` + `INSTALL_COMMANDS` into one structure, with **per-language dispatch functions** so the router shrinks from ~350 lines to ~15.
- Tests for each new behavior.

**Out of scope** (good follow-ups, not now):

- Biome support. Easy to add later using the same pattern.
- Vue/Svelte/Astro support in `extract`.
- eslint/prettier support — explicitly skipped per Trey's preference for oxc.
- Daemon caching of oxlint/oxfmt results.

## License caveat

`llm-tldr` is **AGPL-3.0**. Fine for Trey's local use. If we ever publish this fork as a package or run it as a hosted service, the modified source must be available under AGPL.

## Plan, dependency-ordered

### Step 0 — Empirical verification

Before writing code, capture real CLI behavior so the parsers and guards aren't built on guesses.

**Tasks:**

1. Run `oxlint --format=json sample.ts 2>&1 | head -80` against a TS file with one deliberate violation. Save the full JSON to `tests/fixtures/oxlint_sample.json`. The parser in step 2 derives field names from this fixture, not from assumptions.
2. Run `oxfmt --check sample.ts; echo "exit=$?"` against (a) a properly-formatted file, (b) a drifted file, (c) a `.d.ts` file (to confirm oxc#19077's scope). Save observed exit codes + stdout to `tests/fixtures/oxfmt_behavior.md`.
3. Run `tsc --noEmit --allowJs scratch.js` in an empty directory with no `tsconfig.json` present. Three sub-cases: (a) valid JS, (b) JS with a clear type error, (c) `.jsx` file. Capture exit codes + stderr. If tsc bails with TS5042/TS6504/similar in any case, step 4 ships **without** JS type checking; if it works, step 4 wires it up directly.
4. Document what was observed (one short note appended to this plan's revision log).

**Why first:** Codex's plan-review caught that I described parsers and behaviors without verifying them. The cost of step 0 is 10 minutes; the cost of building parsers against fabricated field names is a re-implementation.

**Gate:** Step 0 outputs are committed to the fork on a branch (e.g., `oxlint-oxfmt-fixtures`) before any code change.

### Step 1 — Local-bin resolution helper

**File:** `tldr/diagnostics.py`

**Change:** Add `_resolve_tool(name: str, file_path: Path) -> str | None`. Walk **all** ancestors of `file_path` collecting `node_modules/.bin/<name>` candidates. Return the **closest** existing executable. Workspace-aware root detection is informational only — we don't stop the walk early at `package.json`, because in pnpm/yarn/turbo monorepos the binary typically lives in the workspace root, not the innermost package.

```python
def _resolve_tool(name: str, start: Path) -> str | None:
    """
    Find tool, preferring node_modules/.bin from the file's directory outward.
    Walks ALL ancestors (no early stop on package.json — workspaces install
    tools at the root, not at every package).
    """
    for parent in [start.parent, *start.parents]:
        candidate = parent / "node_modules" / ".bin" / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which(name)
```

Replace `shutil.which(...)` calls in JS/TS branches with `_resolve_tool(...)`.

**Test:** `tests/test_diagnostics_local_bin.py` — three fixtures:
- Plain project: tool found in `./node_modules/.bin/`.
- Nested package in a workspace: tool found in workspace root's `node_modules/.bin/`, not in inner package's.
- No local install: falls back to `shutil.which`.

### Step 2 — oxlint integration

**File:** `tldr/diagnostics.py`

**Changes:**
- Add `_parse_oxlint_output(stdout: str) -> list[dict]`. Field names are derived from `tests/fixtures/oxlint_sample.json` captured in step 0. Map to the existing diagnostic shape `{file, line, column, severity, message, source: "oxlint", rule}`.
- In `get_diagnostics`, extend the TS branch and add a JS branch that runs `oxlint --format=json <file>` when `include_lint`. Use `_resolve_tool` from step 1. **Subprocess call uses `timeout=30`.**

**Test:** `tests/test_diagnostics_oxlint.py` —
- Fixture: TS file with a real oxlint violation (use a rule that's on by default).
- Expect oxlint diagnostic present with correct line number, severity, rule name.
- Skip if oxlint is not installed (matches existing TS test pattern at `tests/test_typescript_features.py:443`).
- Snapshot test: ensure parser handles the captured fixture from step 0.

### Step 3 — oxfmt integration

**File:** `tldr/diagnostics.py`

**Changes:**
- Add `_run_oxfmt(file_path: Path) -> list[dict]`. Calls `oxfmt --check <file>` with `timeout=15`. On exit code != 0, returns a single diagnostic `{line: 1, severity: "warning", message: "Formatting drift — run oxfmt to fix", source: "oxfmt"}`. Exit 0 returns empty list.
- Per step 0 observations, **skip `.d.ts`** files until oxc#19077 closes (track upstream issue in code comment).
- Call `_run_oxfmt` from the JS/TS branch when `include_lint` is True (formatter is conceptually lint-adjacent for the purpose of the post-edit hook).

**Test:** `tests/test_diagnostics_oxfmt.py` — fixtures for (a) formatted file → no diagnostic, (b) drifted file → 1 oxfmt diagnostic, (c) `.d.ts` file → no diagnostic (skipped).

### Step 4 — JS type checking (conditional on step 0)

**File:** `tldr/diagnostics.py`

**Two branches based on what step 0 observed:**

- **If `tsc --noEmit --allowJs scratch.js` worked standalone:** Add an `elif lang == "javascript":` branch that runs `tsc --noEmit --allowJs --pretty false <file>` with `timeout=30`, using `_resolve_tool("tsc", path)`. Reuse `_parse_tsc_output`.
- **If tsc bailed without a tsconfig:** Wrap the tsc call in a tsconfig presence check — walk up from `path` looking for `tsconfig.json`; skip type checking entirely if none found. Same logic applies to the existing TS branch as a hardening step (the current TS branch has no tsconfig guard either, so it silently fails on standalone files too).

**Test:** `tests/test_diagnostics_js_typecheck.py` — fixtures for both cases observed.

### Step 5 — `tldr extract --format` no-op acceptor

**File:** `tldr/cli.py:221-226`

**Change:** Add `extract_p.add_argument("--format", choices=["json"], default="json", help="Output format (currently only json supported)")`. No behavioral change. Closes the silent failure mode where callers pass `--format json` and argparse rejects.

**Test:** `tests/test_cli_args.py::test_extract_accepts_format_json` — invoke `tldr extract <file> --format json`, assert exit 0 and JSON-valid stdout.

### Step 6 — Consolidation + per-language dispatch functions

**Now tests from steps 1-5 are green. Refactor under that safety net.**

**File:** `tldr/diagnostics.py`

**Changes:**
- Promote `LANG_TOOLS` to canonical config with a `TypedDict`-defined schema (lists of tools per slot, install hints, extensions). Keep it module-level and importable.
- Extract each existing language's `elif` block into a named function: `_run_python_diagnostics(path, include_lint) -> list[dict]`, `_run_typescript_diagnostics(...)`, etc. The dispatcher in `get_diagnostics` becomes a ~15-line dict lookup that calls the right function.

**File:** `tldr/cli.py:985-1066`

**Changes:**
- Replace `TOOL_INFO` and `INSTALL_COMMANDS` (function-locals) with derivations from the imported `LANG_TOOLS`. The `doctor` handler shrinks by ~30 lines.

**Test:** `tests/test_diagnostics_config.py` — asserts no drift between what `doctor` reports and what `get_diagnostics` shells out to. Snapshot test on `LANG_TOOLS` shape so future changes fail loudly.

### Step 7 — `tldr doctor` correctness check

Falls out of step 6 automatically. Manual sanity:
```
$ tldr doctor | grep -A3 -iE "javascript|typescript"
```
Expected: `oxlint`, `oxfmt`, `tsc` listed with correct install hints derived from `LANG_TOOLS`.

### Step 8 — Docs

- Update `README.md` JS/TS line + Supported Languages table.
- Update top-of-file docstring in `diagnostics.py:1-21`.
- Add `docs/plans/0001-...` to the docs index if there is one (there isn't currently; skip).

## Verification gate (before opening upstream PR)

```bash
cd /Users/treygoff/Code/llm-tldr
uv pip install -e .
uv run pytest tests/test_diagnostics_*.py tests/test_typescript_features.py tests/test_cli_args.py
tldr doctor
# Manual: tldr diagnostics on a real file in modern-political-compass with a deliberate oxlint violation + a formatting drift
```

All passing → open PR `parcadei/llm-tldr` ← `treygoff24/llm-tldr:oxlint-oxfmt-support`.

## Open questions for Trey

1. **Upstream PR or fork-only?** Recommendation: upstream — the design fits their existing patterns.
2. **Biome too?** Recommendation: skip for now; add when needed.
3. **Hook integration after merge.** Once `llm-tldr` is upgraded, `~/.claude-shared/hooks/post-edit-diagnostics.mjs` will start surfacing oxlint + oxfmt diagnostics on every JS/TS edit. The current cap of 5 displayed errors is probably fine. No code change needed there — pipx upgrade is all.

## Effort estimate

- Step 0: 15 min (mostly running commands and saving fixtures)
- Steps 1-5 + tests: ~3 hours
- Step 6 refactor: ~1 hour
- Steps 7-8: 15 min
- PR + review cycle: variable

Total: half a day of focused work, plus upstream PR cycle if applicable.
