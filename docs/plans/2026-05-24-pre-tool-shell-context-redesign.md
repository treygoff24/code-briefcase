# Pre-Tool Shell-Context Redesign — Spec

**Date:** 2026-05-24
**Branch:** `fix/pre-edit-hook-model-confusion` (continuation)
**Owner:** Trey
**Status:** Draft for review (Codex via `/delegate-agent` + Opus `plan-reviewer`)

## Summary

The PreToolUse hook on Bash/Execute/Shell tools (`tldr/hooks/tool.py:build_pre_tool_response`) emitted a giant blob to the model when a shell command mentioned source files. Real output captured from a Codex run in `agent-memory` was three full edit-style symbol skeletons concatenated into one message, framed with "Pre-edit snapshot" / "preserve signatures" copy on a `cargo test` invocation. The fan-out has been disabled in code (`tool.py` short-circuits after the destructive command guard). This spec covers the redesign that brings shell context back in a useful, bounded form.

## Captured incident

Codex ran a Bash command in `agent-memory` referencing three Rust files. The hook returned a single message of ~6KB that:

- Concatenated three full `_format_edit_structure` outputs (`file_context.py:411-449`) with `\n\n` separators (`tool.py:269`).
- Each block declared "Pre-existing file structure / Pre-edit snapshot only / preserve signatures unless the task requires an API change" — edit-tool framing on a shell-tool call.
- Dumped the full symbol skeleton (functions, methods, classes) plus the full imports list per file.

The model could not tell which file it was "editing" (it wasn't editing any), why it was being told to "preserve signatures," or how to use the dump. The most likely failure modes: confused tool-routing, wasted tokens, dropped attention on the actual user prompt.

## Root causes

Three independent defects in `tldr/hooks/tool.py:build_pre_tool_response`:

1. **Wrong framing.** Shell-mode reuses `_format_edit_structure` (`file_context.py:411`), which is hard-coded with `[TLDR pre-edit context: ...]` / `"Pre-existing file structure:"` / `"preserve signatures unless the task requires an API change"`. None of that applies when the tool call is `cargo test`, `rg`, `git diff`, etc. The shell hook needs its own formatter that says "here's a quick orientation on the file you're about to touch with shell" — short, no "pending edit" verbiage.

2. **Budget fan-out.** `build_pre_tool_response` calls `build_file_context_for_path(..., budget=budget)` with the **full** budget per candidate, up to `MAX_SHELL_CANDIDATES = 5` (`tool.py:48`). A 5-file shell command can emit ~5× the configured cap. The `\n\n`.join at `tool.py:269` then concatenates them with no global cap.

3. **Naive write-like heuristic.** `_command_looks_write_like` (`tool.py:196-198`) does a substring scan on the lowered command for `(">>", " >", " tee ", "apply_patch", " sed -i")`. This false-positives on `cargo test 2>&1`, `pytest 2> /dev/null`, even quoted `echo "x > y"`. False positives route the shell context through the heavyweight `mode="edit"` path instead of the lighter `mode="read"`.

## Goals

- Restore a useful shell pre-tool hook that surfaces compact, accurate file orientation for code-aware shell calls.
- Never exceed a strict total output budget regardless of candidate count.
- Use shell-appropriate framing — never "pending edit" / "preserve signatures" copy on plain Bash.
- Keep the destructive command guard untouched (already preserved by the disable patch).

## Non-goals

- Re-architect `extract_shell_file_candidates`. The extractor is fine; the rendering pipeline is the problem.
- Change the destructive command guard behavior.
- Change PreToolUse for non-shell tools (Edit, Write, Read paths are unaffected).

## Design

### D1 — Add `mode="shell"` to `build_file_context_for_path`

`tldr/hooks/file_context.py:144` currently defines:
```python
FileContextMode = Literal["read", "edit", "shell"]
```
but the `"shell"` branch is never reached (the dispatcher in `tool.py` passes `"read"` or `"edit"`). Make `"shell"` a real mode that selects a new formatter `format_shell_orientation` (sibling to `format_nav_map`, `format_targeted_read_orientation`, `_format_edit_structure`). The new formatter:

- Header: `[TLDR shell context: <name>] — orientation only; your shell command is unchanged.`
- One-line per-file summary: `kind: <code|test|config>`, top 3 symbols (signature only, no methods), import count.
- No "pre-edit," no "preserve signatures," no "your edit will apply normally."
- Hard budget cap: 250 chars per file.

### D2 — Shared budget + global cap in `build_pre_tool_response`

In `tldr/hooks/tool.py`:

- Compute per-file budget as `max(120, budget // max(1, len(candidates)))` before iterating.
- Track running total of emitted bytes; stop appending once total ≥ `SHELL_CONTEXT_HARD_CAP = 2000` chars.
- If candidates were truncated, append a single line: `... +N more file(s) not shown`.

### D3 — Token-level write-likeness

Replace `_command_looks_write_like` with a token-level check that runs against the already-`shlex.split` token list:

```python
WRITE_TOKENS = frozenset({">", ">>", "tee", "apply_patch"})
WRITE_FLAGS = {"sed": frozenset({"-i", "--in-place"})}

def _command_is_write_like(tokens: list[str]) -> bool:
    for i, tok in enumerate(tokens):
        if tok in WRITE_TOKENS:
            return True
        if tok in WRITE_FLAGS and any(t in WRITE_FLAGS[tok] for t in tokens[i+1:]):
            return True
    return False
```

This excludes `2>&1`, `2>/dev/null`, and quoted-string false positives. Currently, even when "write-like" the answer is still `mode="shell"` — but the flag can be used to nudge wording ("you are about to modify <file>" vs "you are about to read <file>") inside the shell formatter.

### D4 — Re-enable wiring guarded by feature flag

Re-enable the file-context call in `build_pre_tool_response` behind an env-var feature flag `TLDR_SHELL_CONTEXT=1` for a soak period. Default off until perception evals (D6) show no regression. Once stable, flip default and remove the flag.

### D5 — Telemetry

Reuse existing `context_kind="shell_file_context"`. Add a `shell_context_truncated_count` field to the hook execution record so we can monitor when the hard cap fires. Wire through `tldr/telemetry.py:record_hook_execution`.

### D6 — Perception eval

Extend `scripts/perception_eval.py` and/or `tests/test_hook_framing_perception.py` with a Bash-tool fixture that asserts:
- The rendered context never contains `"preserve signatures"`, `"Pre-edit snapshot"`, `"your edit will apply"`.
- Total rendered context for a 5-candidate command is ≤ `SHELL_CONTEXT_HARD_CAP` chars.
- A `cargo test 2>&1 | tee log.txt` command is classified write-like; a bare `cargo test 2>&1` is not.

## Test plan

- `tests/test_hooks_tool.py` — replace the current `shell_file_context_disabled` no-op assertions with positive assertions on the new shell formatter under `TLDR_SHELL_CONTEXT=1`. Keep one test that asserts default-off behavior until D4 flips.
- `tests/test_file_context.py` (new file or extend) — direct unit tests for `format_shell_orientation` covering: code file, test file, config file, missing file.
- `tests/test_hooks_permission.py` — destructive guard tests stay green (no change to that code path).
- `tests/test_current_cli_hook_shapes.py:164-174` — already asserts the noop case; update to assert the shell context shape under the flag.
- `tests/test_hook_framing_perception.py` — add the assertions in D6.

## Rollout

1. Land D1–D3 + tests with `TLDR_SHELL_CONTEXT` defaulting off (this preserves the current "disabled" state for everyone).
2. Run perception eval locally (`scripts/perception_eval.py`) with the flag on. Capture before/after token counts and framing tokens.
3. Self-dogfood the flag on for one week in `~/.codex/hooks.json` by adding `TLDR_SHELL_CONTEXT=1` to the env. Watch telemetry for `shell_context_truncated_count` spikes and any model complaints.
4. Flip default to on, remove flag, document in `docs/TLDR.md`.

## Out of scope follow-ups

- Per-call latency from spawning Python for the still-firing destructive-guard hook. Could be addressed by a longer-lived daemon or by inlining the regex check into a lighter runtime.
- A larger redesign of `_format_edit_structure` itself for the actual edit path — separate concern, separate spec.

## Open questions

1. Should we surface a "related file" hint in shell mode the way `read`/`edit` modes do via `discover_related_candidates`? Initial position: no — keep shell mode strictly per-file and short. Revisit after soak.
2. Is `SHELL_CONTEXT_HARD_CAP = 2000` the right number? Trey's stated default `budget=1200` was per-file before; 2000 total is a deliberate reduction. Tune via telemetry.
3. Do we want a kill-switch env var separate from the feature flag (e.g., `TLDR_SHELL_CONTEXT=0` to force off even when default flips)? Probably yes — cheap and reversible.
