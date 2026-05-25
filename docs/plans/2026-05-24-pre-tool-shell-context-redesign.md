# Pre-Tool Shell-Context Redesign — Spec v2

**Date:** 2026-05-24
**Branch:** `fix/pre-edit-hook-model-confusion` (continuation)
**Owner:** Trey
**Status:** v2 — folded reviews from Opus `plan-reviewer` and DeepSeek V4 Pro (safe). Codex review hung at 58 min and was killed; substituted DeepSeek.

## What changed v1 → v2

Both reviewers independently flagged the same three structural defects in v1:
1. **D3 pseudocode bug:** the token-level write-likeness check declared `WRITE_TOKENS` and `WRITE_FLAGS` but the body only consulted `WRITE_TOKENS`. The `sed -i` case described in prose never made it into the control flow.
2. **Budget math inconsistency:** v1 specified three independent caps (per-file `max(120, budget // n)`, global `SHELL_CONTEXT_HARD_CAP = 2000`, formatter-internal 250 chars/file) without saying which binds. With defaults the global cap never fires.
3. **Default-off rollout generates no signal.** The hook is already off; "soaking" with the flag off only soaks the destructive guard, which D4 doesn't change.

DeepSeek additionally caught:
- **D1 dispatch gap:** the spec said "make shell a real mode" but never specified whether shell mode skips `discover_related_candidates` (`file_context.py:553-558`). If it doesn't, shell mode pays import-resolution latency AND emits related-file suggestions with read/edit framing — partial regression of the captured incident.
- **D3 pipeline-boundary bug:** `any(t in WRITE_FLAGS[tok] for t in tokens[i+1:])` walks past `|`, `;`, `&&`, `||` separators, so `sed -n 'p' file | something -i other` false-positives.
- **D5 telemetry plumbing:** adding `shell_context_truncated_count` requires touching `HookExecutionResult`, `record_hook_execution` signature, the record dict, and the runner call site — four places, not "wire through" as v1 implied.
- **`context_kind` collision:** `"shell_file_context"` is already used by `format_shell_summary` for `.sh` file structured summaries (`file_context.py:371-391`). Re-using it for shell-tool orientation makes telemetry ambiguous.

Opus additionally caught:
- **Missed write tokens:** `&>` (all-streams), `>|` (clobber-force), `&>>` (append all streams), `|&` (bash 4 stderr-pipe), `1>`, `2>`.
- **`noop_reason` literal migration:** when the flag flips, `noop_reason == "shell_file_context_disabled"` disappears and any external consumer breaks silently.
- **Perception eval is negative-only:** asserting "doesn't contain 'preserve signatures'" doesn't prove the model interprets the output correctly. Need a positive eval ("is the model being told to edit anything? expect NO").
- **Hedging copy** ("useful, bounded form") in the summary — replaced with the concrete invariant.

## Captured incident (unchanged)

Codex ran a Bash command in `agent-memory` referencing three Rust files. The hook returned a single message of ~6KB that:

- Concatenated three full `_format_edit_structure` outputs (`file_context.py:411-449`) with `\n\n` separators (`tool.py:269`).
- Each block declared "Pre-existing file structure / Pre-edit snapshot only / preserve signatures unless the task requires an API change" — edit-tool framing on a shell-tool call.
- Dumped the full symbol skeleton (functions, methods, classes) plus the full imports list per file.

The fan-out is currently disabled in `build_pre_tool_response` — it short-circuits after the destructive guard returning `noop("shell_file_context_disabled")`.

## Root causes (unchanged)

Three independent defects in `tldr/hooks/tool.py:build_pre_tool_response`:

1. **Wrong framing.** Shell-mode reused `_format_edit_structure` (`file_context.py:411`), hard-coded with edit-tool copy that doesn't apply to `cargo test`, `rg`, `git diff`, etc.
2. **Budget fan-out.** `build_pre_tool_response` passed the **full** budget per candidate, up to `MAX_SHELL_CANDIDATES = 5`. No global cap on the `"\n\n".join`.
3. **Naive write-like heuristic.** `_command_looks_write_like` substring-scanned the lowered command for `" >"`, false-positiving on `cargo test 2>&1`.

## Invariants (new — v2)

The redesigned shell pre-tool hook satisfies, on every Bash/Execute/Shell call that reaches it:

- Total emitted `additional_context` ≤ **2000 chars** including separators.
- No emitted context contains the strings: `"preserve signatures"`, `"Pre-edit snapshot"`, `"your edit will apply"`, `"pending edit"`.
- Every emitted file block carries the header `[TLDR shell context: <name>] — orientation only; your shell command is unchanged.`
- Shell mode never invokes `discover_related_candidates` and never appends `format_related_files_section`.
- Destructive command guard runs **before** any candidate extraction or file IO; a deny short-circuits with no shell context.
- Telemetry `context_kind` distinguishes shell-tool orientation from structured `.sh` summaries.

## Goals (v2)

- Restore a useful shell pre-tool hook that surfaces compact, accurate file orientation for code-aware shell calls.
- Strict 2000-char total cap regardless of candidate count.
- Shell-appropriate framing — never edit-tool copy on plain Bash.
- Keep the destructive command guard untouched (already preserved by the disable patch).

## Non-goals (v2)

- Re-architect `extract_shell_file_candidates`. The extractor is correct; the rendering pipeline was the problem.
- Change destructive command guard behavior.
- Change PreToolUse for non-shell tools (Edit, Write, Read paths unaffected).
- Eliminate per-call latency from the still-firing destructive-guard hook spawn. Separate follow-up.

## Design

### D1 — `mode="shell"` branch with explicit dispatch (revised)

`FileContextMode = Literal["read", "edit", "shell"]` already exists at `file_context.py:144`; only filling in the branch.

In `build_file_context_for_path`, after extraction succeeds and the targeted-read branches are evaluated, add:

```python
if mode == "shell":
    context = format_shell_orientation(path, info, budget=budget)
    context_kind = "shell_tool_orientation"
    return FileContextResult(
        status="ok",
        reason=None,
        context=context,
        context_kind=context_kind,
        trigger_files=trigger,
        recommended_files=[],   # explicit — no related-files in shell mode
        surfaced_files=[],
        candidate_files=[],     # explicit — no candidate discovery
    )
```

Critically: this branch is **before** the read/edit branch that calls `discover_related_candidates`. Shell mode never pays import-resolution latency and never emits related-file suggestions.

New formatter `format_shell_orientation(path, info, budget)`:

- Header: `[TLDR shell context: <name>] — orientation only; your shell command is unchanged.`
- One line: `- kind: <code|test|config>` (from `path_policy.classify_context_path` result).
- One line: `- top symbols: <sig1>, <sig2>, <sig3>` (truncated to 60 chars each). Skip line if none.
- One line: `- imports: <count>`. Skip line if zero.
- No "pre-edit," no "preserve signatures," no "your edit will apply normally."

Per-file output cap: 250 chars hard. The formatter calls `_truncate(text, budget=60)` (yielding 240 max chars), then truncates to 250 if the formatter's own overhead pushes past.

### D2 — Shared budget + global cap in `build_pre_tool_response` (revised math)

Single binding rule, replacing v1's three-cap confusion:

```python
SHELL_CONTEXT_HARD_CAP = 2000  # total chars across all files
SHELL_PER_FILE_HARD = 250      # max chars per file, before separators

# In build_pre_tool_response after extracting candidates:
emitted = 0
truncated = 0
for path in candidates:
    if emitted >= SHELL_CONTEXT_HARD_CAP:
        truncated = len(candidates) - candidates.index(path)
        break
    # ... call build_file_context_for_path(mode="shell", budget=…) …
    block = file_result.context or ""
    if len(block) > SHELL_PER_FILE_HARD:
        block = block[:SHELL_PER_FILE_HARD - 20].rstrip() + "\n... [truncated]"
    if emitted + len(block) + 2 > SHELL_CONTEXT_HARD_CAP:
        truncated = len(candidates) - candidates.index(path)
        break
    contexts.append(block)
    emitted += len(block) + 2  # account for "\n\n" join

if truncated:
    contexts.append(f"... +{truncated} more file(s) not shown — shell context cap reached")
```

Worked example (defaults `budget=1200`, `n=5`):
- Per file: 250 chars × 5 = 1250 chars + 4×`\n\n` (8) = 1258. Under the 2000 cap.
- At `n=10` (if `MAX_SHELL_CANDIDATES` raised): would emit ~7–8 files then truncate the rest.

Per file budget passed into `build_file_context_for_path` becomes `60` (`SHELL_PER_FILE_HARD // 4`, matching the `_truncate` formula `max_chars = max(500, budget * 4)` — `_truncate` is a no-op for budgets ≥ 60).

### D3 — Token-level write-likeness (rewritten)

```python
WRITE_REDIRECT_TOKENS = frozenset({
    ">", ">>",      # stdout redirect / append
    "&>", "&>>",    # bash all-streams redirect / append
    ">|",           # clobber-force
    "|&",           # bash 4 stderr-pipe
    "1>", "2>",     # explicit fd redirect (when shlex separates them)
})
WRITE_COMMAND_TOKENS = frozenset({"tee", "apply_patch"})
WRITE_FLAGS = {
    "sed": frozenset({"-i", "--in-place"}),
}
PIPELINE_SEPARATORS = frozenset({"|", ";", "&&", "||", "&"})


def _command_is_write_like(tokens: list[str]) -> bool:
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok in WRITE_REDIRECT_TOKENS:
            return True
        if tok in WRITE_COMMAND_TOKENS:
            return True
        if tok in WRITE_FLAGS:
            # only scan up to the next pipeline boundary
            j = i + 1
            while j < n and tokens[j] not in PIPELINE_SEPARATORS:
                if tokens[j] in WRITE_FLAGS[tok]:
                    return True
                j += 1
            i = j
            continue
        i += 1
    return False
```

Acknowledged unhandled patterns (low severity, documented for future):
- `curl -o file`, `wget -O file` — downloads but not source modification.
- `patch < file.patch` — applies a patch but doesn't touch the repo as a redirect.
- Process substitution `>(cmd)` — vanishingly rare in agent commands.

Default behavior under v2: `mode="shell"` for every candidate regardless of write-likeness. The write-likeness flag is preserved only to nudge the per-file header wording from `orientation only; your command is unchanged` to `orientation only; your command will modify this file` (no behavioral change in the formatter shape).

### D4 — Rollout: default ON with kill-switch (revised)

v1 proposed default-off with a feature flag. v2 reverses this — both reviewers argued the soak generates no signal because the hook is already off.

- Land D1–D3 with the new behavior **on by default**.
- Add env var `TLDR_SHELL_CONTEXT=0` as a kill-switch that forces the old `noop("shell_file_context_disabled")` path.
- Three states explicit: unset/`1`/anything-else → on; `0` → forced off.

Justification: the redesigned hook is informational, capped at 2000 chars, and has none of the edit-tool framing. Risk of regression is bounded by the cap. Reversibility lives in the kill-switch env var, not in a feature flag the user has to opt into.

### D5 — Telemetry (revised plumbing)

Touches four places, all called out:

1. `tldr/hooks/outcome.py` — add `shell_context_truncated: bool` field to `HookExecutionResult` (default `False`). Single bool, not a count — the spec only cares whether the cap fired, not the value.
2. `tldr/hooks/tool.py` — set the bool when `truncated > 0` in `build_pre_tool_response`.
3. `tldr/telemetry.py:record_hook_execution` — add `shell_context_truncated: bool = False` kwarg, include in `_normalize_record`.
4. `tldr/hooks/runner.py` — pass `shell_context_truncated=execution.shell_context_truncated` in the `record_hook_execution` call.

Use new `context_kind="shell_tool_orientation"` (NOT `"shell_file_context"`, which is already used by `format_shell_summary` for `.sh` file structured summaries — verified at `file_context.py:386`). The old value remains for structured `.sh` summaries; the new value is exclusive to shell-tool orientation. Document the distinction in `docs/TLDR.md`.

### D6 — Perception eval (extended)

`tests/test_hook_framing_perception.py` and/or `scripts/perception_eval.py` add:

Negative assertions (regression guards):
- Rendered context never contains `"preserve signatures"`, `"Pre-edit snapshot"`, `"your edit will apply"`, `"pending edit"`.
- Total rendered context for a 5-candidate command is ≤ 2000 chars.
- A bare `cargo test 2>&1` is classified read-like; `cargo test 2>&1 | tee log.txt` and `cargo test &> log.txt` are classified write-like; `sed -n 'p' file | grep -i x` is classified read-like (no false positive from `-i` flag past a pipeline boundary).

Positive assertions (matches existing `perception_eval.py` model-probe pattern):
- Given the rendered shell context as `additional_context`, the perception probe model is asked "is the agent being instructed to edit any of the mentioned files?" — expect "no" with high agreement.
- Same probe asked "is this an informational hint?" — expect "yes."

## Test plan

- `tests/test_hooks_tool.py` — split into "kill-switch on" and "default" suites. Default suite asserts the new shell orientation shape. Kill-switch suite (env var `TLDR_SHELL_CONTEXT=0`) asserts `noop_reason == "shell_file_context_disabled"`. Stable across the flag removal because the disabled path stays as the kill-switch implementation.
- `tests/test_hooks_tool.py` — new test: `build_pre_tool_response` with destructive command returns deny without touching `extract_shell_file_candidates` (assert via mock that it isn't called).
- `tests/test_hooks_tool.py` — new test: zero path candidates returns `noop("clean")` not a synthetic orientation block.
- `tests/test_file_context.py` (new file or extend) — direct unit tests for `format_shell_orientation`: code file, test file, config file, empty file, file with zero symbols (only imports), file where `extract_file` raises.
- `tests/test_hooks_tool.py` — new test: 10-candidate Bash command (e.g., `rg foo a.py b.py … j.py`) emits ≤ 2000 chars total and the truncation message.
- `tests/test_hook_framing_perception.py` — D6 negative + positive assertions.
- `tests/test_current_cli_hook_shapes.py:164-174` — update to assert the shell context shape under default-on. Add a paired test that asserts the kill-switch path.
- `tests/test_telemetry.py` — assert `shell_context_truncated=True` round-trips when the cap fires; assert `context_kind="shell_tool_orientation"` (not `"shell_file_context"`).

## Rollout (revised)

1. Land D1–D5 with default on + kill-switch. All tests pass.
2. Run `scripts/perception_eval.py` locally with both eval shapes (negative + positive). Capture before/after token counts and the perception probe agreement rate.
3. Commit. Monitor telemetry for `shell_context_truncated` rate and `context_kind="shell_tool_orientation"` injected byte distributions over the next week.
4. If `shell_context_truncated` exceeds 5% of shell invocations, tune `MAX_SHELL_CANDIDATES` down (probably to 3) or `SHELL_PER_FILE_HARD` down (probably to 180) before considering further changes.
5. Document the new behavior + kill-switch in `docs/TLDR.md`.

## Out of scope follow-ups

- Per-call latency from spawning Python for the still-firing destructive-guard hook. Separate concern; a longer-lived daemon or a lighter runtime fixes it.
- Larger redesign of `_format_edit_structure` itself for the actual edit path — separate spec.
- Migration of historical telemetry records with the old `context_kind` value. Acceptable to leave as-is; eval pipeline can disambiguate by date.

## Open questions (closed in v2)

1. ~~Related-file hint in shell mode~~ → **No.** Locked into D1 dispatch.
2. ~~`SHELL_CONTEXT_HARD_CAP = 2000` right?~~ → **Yes**, defended: ~500 tokens, the threshold at which most chat-class models stop attending to unrequested context. Tune via telemetry per Rollout step 4.
3. ~~Kill-switch separate from feature flag?~~ → **Folded into D4 as the primary control.** No separate feature flag.

## Reviewer credits

- Opus `plan-reviewer` agent — D3 missed-token catalog, telemetry `context_kind` ambiguity, perception-eval positive-assertion gap, `noop_reason` migration risk, hedging copy.
- DeepSeek V4 Pro (`delegate droid "deepseek v4 pro" safe`) — D1 dispatch gap (HIGH), D3 pseudocode bug (HIGH), pipeline-boundary scan, telemetry plumbing four-location detail, `context_kind` collision with `format_shell_summary`.
- Codex review attempted via `delegate codex safe`; hung at 58 min and was killed. Substituted DeepSeek V4 Pro.
