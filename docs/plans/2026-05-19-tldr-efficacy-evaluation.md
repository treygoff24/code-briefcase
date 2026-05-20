# TLDR Efficacy Evaluation Plan

**Purpose:** Build a local, privacy-preserving measurement loop that tells us whether TLDR's Claude Code and Codex hooks actually improve agent work after several days of heavy use.

**Goal:** Produce a repeatable before/after report that compares baseline agent sessions against TLDR-enabled sessions across tool-call behavior, token usage, latency, rework, hook reliability, and task outcomes. The report should answer the practical question: **does TLDR reduce blind exploration and rework enough to justify the hook context it injects?**

**Status:** Reviewed and revised after plan-reviewer + GLM critique.

---

## Success Criteria

After implementation, Trey can run one command like:

```bash
python scripts/evaluate_tldr_usage.py \
  --baseline-start 2026-05-13 \
  --treatment-start 2026-05-19T20:07:24-05:00 \
  --out reports/tldr-efficacy-$(date +%F).md
```

The generated report must include:

- Codex and Claude Code sections.
- Baseline vs TLDR-enabled comparisons.
- Per-repo and per-session breakdowns.
- Tool-call counts by category.
- Token usage where available.
- TLDR hook invocation count, injected context size, failures, and latency.
- Rework/thrash indicators.
- A short verdict: helpful, neutral, harmful, proxy-only, or insufficient data.

The report must show sample sizes before any verdict. If there are fewer than
20 baseline and 20 treatment sessions for a client/repo cohort, the report must
default to `insufficient data` or `proxy-only`.

---

## Core Evaluation Questions

1. **Tool behavior:** Are agents calling file/search/read tools more or less after TLDR is installed?
2. **Token usage:** Do total tokens, non-cached input tokens, output tokens, or tool-output bytes move meaningfully?
3. **Exploration quality:** Does TLDR reduce repeated reads, broad greps, and duplicate inspection of the same files?
4. **Edit quality:** Do agents get to plausible patches faster, touch fewer irrelevant files, and need fewer patch/revert cycles?
5. **Verification quality:** Do agents run fewer redundant gates while still reaching green checks?
6. **Hook reliability:** Are hooks fast, safe, and silent when they should be? Any timeouts or noisy context?
7. **Context hit rate:** Do files surfaced by TLDR later appear in read/edit/test activity?
8. **Net context tradeoff:** Are TLDR-injected bytes/tokens repaid by reduced exploratory tool output and fewer blind reads?
9. **First useful action:** Does TLDR reduce time-to-first-edit or time-to-first-targeted-verification?

---

## Data Sources

### Codex

Read local JSONL sessions from:

- `~/.codex/sessions/**/*.jsonl`
- `~/.codex/archived_sessions/*.jsonl`

Useful event shapes already observed:

- `session_meta`: session id, cwd, cli version, model.
- `event_msg` with `task_started`: turn starts.
- `event_msg` with `token_count`: input/output/cached/reasoning/total tokens.
- `response_item`: tool calls, tool outputs, messages, hook outputs if present.

### Claude Code

Read local JSONL sessions from:

- `~/.claude/projects/**/*.jsonl`

Useful event shapes already observed:

- Hook records such as `attachment.type == "hook_success"`.
- Hook metadata: hook name, event, output location, preview.
- Tool/result records vary by Claude version, so parser must be tolerant.

### TLDR local telemetry

Add optional local JSONL telemetry because it will be cleaner than reconstructing all hook behavior from agent logs.

Telemetry is **opt-in** for the publishable package. Dogfood runs can enable it
with `TLDR_TELEMETRY=1`. The evaluator can still run in proxy-only mode from
agent logs when telemetry is absent.

Default path:

- `~/.tldr/telemetry.jsonl`

Each record should avoid code content by default and include:

```json
{
  "timestamp": "2026-05-19T20:07:24-05:00",
  "version": "1.5.2",
  "client": "codex",
  "event": "session-start",
  "project": "/Users/treygoff/Code/llm-tldr",
  "project_hash": "02975e43",
  "duration_ms": 123,
  "status": "ok",
  "error_kind": null,
  "injected_bytes": 172,
  "surfaced_files": ["tldr/hook_installer.py"],
  "diagnostic_count": 0,
  "daemon_state": "ready"
}
```

Privacy rule: store file paths and metrics, not source snippets. Allow a future opt-in flag for richer debugging, but keep the default safe.

Privacy and durability rules:

- Never store source code snippets by default.
- Create telemetry files with mode `0600` where possible.
- Support `TLDR_TELEMETRY_REDACT_PATHS=1` to hash/redact absolute paths.
- Use append locking (`fcntl.flock` on macOS/Linux) or per-process temp records
  to avoid JSONL corruption from concurrent hooks.
- Add size rotation or a retention cap so `~/.tldr/telemetry.jsonl` cannot grow forever.
- Telemetry write failures must never alter hook stdout/stderr or block the agent.

---

## Metrics

### Session-level metrics

- Session id, client, cwd/repo, start time, end time.
- Wall-clock duration.
- Number of turns.
- Model and CLI version when available.
- Whether session is baseline or treatment.
- Whether TLDR hook activity was observed.
- Day-by-day bucket for trend analysis.
- TLDR version and hook configuration fingerprint when available.

### Token metrics

Codex has direct token events:

- `input_tokens`
- `cached_input_tokens`
- `output_tokens`
- `reasoning_output_tokens`
- `total_tokens`
- non-cached input tokens = `input_tokens - cached_input_tokens`

Claude token availability may be weaker in local logs. If exact token counts are unavailable, report `unknown` rather than estimating.

Primary token views:

- Gross token usage where available.
- Non-cached input token usage where available.
- TLDR injected bytes/tokens from telemetry.
- Exploration tool-output bytes before first edit.
- Approximate net context delta:
  - `tldr_injected_bytes - exploratory_tool_output_bytes_reduction`
  - clearly label this as approximate, not exact cost accounting.

### Tool-call metrics

Group tools into categories:

- **Explore:** `rg`, `grep`, `find`, `ls`, `sed`, `cat`, file read/open tools.
- **Edit:** `apply_patch`, write/edit tools, `python` scripts that mutate files.
- **Verify:** `pytest`, `npm test`, `pnpm check`, `ruff`, `mypy`, build/test/lint commands.
- **Git:** status, diff, log, add, commit, push.
- **Research:** web, docs, browser, connector calls.
- **Agent:** subagents, delegate, Claude workers.
- **TLDR:** `tldr`, `tldr-mcp`, hook invocations.

Track:

- Total tool calls.
- Tool calls per turn.
- Repeated identical calls after normalization.
- Unique files read.
- Unique files edited.
- Read-before-edit ratio.
- Broad search count before first edit.
- Time-to-first-edit.
- Time-to-first-targeted-verification.

Command normalization must be deterministic:

- Strip extra whitespace.
- Normalize absolute paths under the repo to `<repo>/...`.
- For shell commands, compare exact normalized command strings first.
- Do not use fuzzy "near-identical" matching in the MVP; add it later only with explicit rules.

### Rework/thrash metrics

- Repeated reads of the same file.
- Failed command count.
- Repeated failed command count.
- Number of patch attempts.
- Patch-to-verification ratio.
- Reverted/overwritten edits where detectable.
- Number of verification reruns.
- Strong user correction phrases, e.g. "that's wrong", "you missed", "stop doing", "do not run".
- Optional git-based rework signals: revert-like commits, `git restore`, `git checkout --`, or `git reset` after agent edits.

### TLDR-specific metrics

- Hook invocations by client/event.
- Hook status: ok, skipped, error, timeout.
- Hook duration p50/p95.
- Injected context bytes/tokens.
- Surfaced file count.
- Diagnostics emitted.
- Daemon start/warm count.
- Daemon failures.
- Hook no-op rate by event.
- Context hit rate:
  - trigger files later read/edited
  - recommended related files later read/edited
  - surfaced files later appear in verification failures

Do not treat trigger-file hit rate as proof of usefulness. The valuable signal is
whether TLDR recommended related files that agents later used without broad
exploration.

---

## Design

### New files

- `scripts/evaluate_tldr_usage.py`
  - Parses Codex sessions, Claude sessions, and TLDR telemetry.
  - Emits Markdown and optional JSON.
- `tests/test_evaluate_tldr_usage.py`
  - Fixture-driven parser tests.
- `tests/fixtures/eval/codex_session.jsonl`
  - Minimal Codex session with token_count, tool calls, and hook-like records.
- `tests/fixtures/eval/claude_session.jsonl`
  - Minimal Claude session with hook_success and tool-like records.
- `tests/fixtures/eval/tldr_telemetry.jsonl`
  - Minimal TLDR telemetry records.
- `reports/.gitkeep`
  - Keeps reports dir present; generated reports should be ignored unless intentionally committed.

### Modified files

- `tldr/hooks/runtime.py`
  - Add telemetry write helper or call into a new module.
- `tldr/hooks/session.py`, `tldr/hooks/read.py`, `tldr/hooks/edit.py`, `tldr/hooks/post_edit.py`
  - Emit telemetry after each hook run.
- `tldr/daemon/startup.py` or `tldr/session_warm.py`
  - Include daemon/warm telemetry when initiated by hooks.
- `.gitignore`
  - Ignore generated reports and local telemetry if repo-local paths are used.

Optional extraction:

- `tldr/telemetry.py`
  - Central local JSONL writer.
  - Handles env flags and path resolution.
  - Keeps hook modules simple.
- `tldr/hooks/outcome.py`
  - Defines structured hook execution metadata, e.g.
    `HookExecutionResult(response, status, error_kind, trigger_files,
    recommended_files, diagnostics_count)`.

Instrumentation should be centralized in `tldr/hooks/runner.py`, because it is
the common hook dispatch point. Individual hook modules may annotate
domain-specific metadata, but they should not each implement independent
telemetry writing.

Timeout caveat: client-side hook timeouts cannot be reliably self-reported by a
killed hook process. Timeout detection must come from Claude/Codex hook failure
logs where available, not TLDR telemetry alone.

---

## Evaluation Protocol

The initial report can compare a historical baseline against TLDR-enabled
sessions, but that comparison is not causal because task mix, repo state, model
versions, and user behavior drift over time.

Use three confidence tiers:

1. **Proxy-only historical comparison**
   - Uses existing baseline sessions and post-install sessions.
   - Good for finding obvious regressions and rough signals.
   - Must not claim causal proof.
2. **Interleaved dogfood comparison**
   - Alternate TLDR telemetry/hook usage on and off across comparable days or
     sessions where practical.
   - Best short-run way to reduce temporal confounding.
3. **Paired task replay/manual annotation**
   - For higher confidence, label sessions or task families manually in a small
     annotations file.

Add optional annotations:

- `reports/tldr-efficacy-annotations.jsonl`
- Fields: `session_id`, `client`, `task_family`, `difficulty`, `completed`,
  `quality_notes`, `manual_verdict`.

The MVP may ship without annotations, but then outcome reporting must be labeled
`proxy-only`.

Session matching rules:

- Parse all timestamps into timezone-aware UTC.
- Treatment window is `[treatment_start, treatment_end)`.
- Baseline window is `[baseline_start, baseline_end)`.
- Normalize cwd via `Path.resolve()` when the path still exists.
- Match telemetry to sessions by session id if available; otherwise use client,
  normalized cwd, and timestamp containment within the session interval.
- For subdirectories, also allow matching to the nearest ancestor repo root.
- Report unmatched telemetry/session records separately.

---

## Implementation Plan

### Step 1 — Parser and report MVP first

Build `scripts/evaluate_tldr_usage.py` before adding new telemetry. This proves we can get signal from existing Codex/Claude logs.

Label this first report as **proxy-only** because existing logs cannot fully
provide hook latency, hook status, injected bytes, or structured surfaced-file
metadata.

MVP inputs:

- `--baseline-start`
- `--treatment-start`
- `--baseline-end` optional, defaults to treatment start.
- `--treatment-end` optional, defaults to now.
- `--codex-root` default `~/.codex`
- `--claude-root` default `~/.claude`
- `--tldr-telemetry` default `~/.tldr/telemetry.jsonl`
- `--out`
- `--json-out` optional

MVP output:

- Summary table.
- Codex section with token metrics.
- Claude section with available hook/tool metrics.
- Per-repo table.
- Per-day trend table.
- Top 10 highest-cost sessions.
- Top 10 most repeated-read sessions.
- TLDR hook reliability section if telemetry/logs exist.
- "What to try next" tuning suggestions based on observed bottlenecks.

### Step 2 — Fixture-driven parser tests

Add tests with synthetic JSONL fixtures that cover:

- Codex `token_count` events.
- Codex `response_item` tool calls and tool outputs.
- Claude hook success records.
- Malformed JSONL lines.
- Unknown event shapes.
- Missing token fields.
- Sanitized real Codex event shapes from at least one actual session.
- Sanitized real Claude event shapes from at least one actual session.

Parser behavior must be tolerant: unknown records are counted/skipped, never fatal by default.

Before writing broad parser logic, manually inspect 3-5 recent real JSONL
sessions for each client and capture sanitized fixture shapes. Treat those
fixtures as parser contracts for the MVP.

### Step 3 — Local TLDR telemetry

Add a small telemetry writer:

- Disabled by default for publishable builds.
- Enable with `TLDR_TELEMETRY=1`.
- Override path with `TLDR_TELEMETRY_PATH`.
- Redact absolute paths with `TLDR_TELEMETRY_REDACT_PATHS=1`.
- Never store code snippets by default.
- Use locked append or per-process temp records; telemetry must never break hooks.

Fields:

- timestamp
- version
- client
- hook event
- project path and stable hash
- duration
- status/error
- injected byte count
- surfaced files
- diagnostics count
- daemon state
- trigger files
- recommended related files
- no-op reason when applicable

### Step 4 — Wire telemetry into hooks

Hook runtime should measure wall time and write one record per invocation.

Rules:

- Telemetry failures are swallowed.
- Hook output behavior must not change.
- Context content is not logged.
- If a hook skips because the project/file is not relevant, log `status=skipped`.
- Add structured outcome metadata so `skipped`, `noop`, `error`, and successful
  context injection are distinguishable.
- Distinguish `trigger_files` from `recommended_related_files`.

### Step 5 — Context hit-rate analysis

Join TLDR telemetry with session logs:

- Match by client, cwd/project, timestamp window.
- For each hook's `trigger_files` and `recommended_related_files`, check whether
  the same files are later read, edited, or appear in verification commands.
- Report per-turn and per-session hit rates separately.
- Report hit rate with caveat: approximate matching, not causal proof.

Do not implement this until telemetry schema and hook metadata are tested.

### Step 6 — Report interpretation layer

Add a compact scoring rubric:

- **Helpful:** lower repeated exploration or lower total cost with neutral/better outcomes.
- **Neutral:** no meaningful movement, but low hook overhead/failure.
- **Harmful:** materially higher tokens/tool calls/latency without outcome improvement, or hook failures/noise.
- **Proxy-only:** trends exist but completion/outcome data is too weak for a real verdict.
- **Insufficient data:** fewer than N comparable sessions.

The report should avoid fake precision. Prefer medians and ratios over averages.
Include confidence intervals or bootstrap ranges for ratios when sample sizes are small.

---

## Validation

Run:

```bash
uv run pytest -q tests/test_evaluate_tldr_usage.py
uv run python scripts/evaluate_tldr_usage.py \
  --baseline-start 2026-05-13 \
  --treatment-start 2026-05-19T20:07:24-05:00 \
  --out /tmp/tldr-efficacy.md \
  --json-out /tmp/tldr-efficacy.json
```

Then inspect:

```bash
sed -n '1,220p' /tmp/tldr-efficacy.md
python -m json.tool /tmp/tldr-efficacy.json >/dev/null
```

For telemetry:

```bash
TLDR_TELEMETRY_PATH=/tmp/tldr-telemetry.jsonl \
TLDR_TELEMETRY=1 \
  uv run python -m tldr.cli hooks run session-start --client codex \
  < tests/fixtures/eval/codex_session_start.json

uv run python - <<'PY'
import json
from pathlib import Path
for line in Path("/tmp/tldr-telemetry.jsonl").read_text().splitlines():
    json.loads(line)
PY
```

Hook safety checks:

```bash
uv run python scripts/smoke_current_cli_hooks.py
TLDR_TELEMETRY=1 uv run pytest -q tests/test_hooks_runtime.py
```

Required telemetry tests:

- Telemetry on/off preserves hook stdout JSON byte-for-byte except for expected
  timing/side-file effects.
- Unwritable telemetry path is swallowed.
- Malformed env path is swallowed.
- Concurrent hook writes produce parseable JSONL.
- `skipped`, `noop`, `error`, and success statuses are distinguishable.

---

## Risks and Mitigations

- **Selection bias:** post-install sessions may differ from baseline tasks.
  - Mitigation: per-repo breakdown, comparable-session notes, minimum N, and
    interleaved/toggle dogfood where practical.
- **Claude local logs may not expose exact token usage.**
  - Mitigation: report exact Codex tokens and mark Claude tokens unknown unless
    found; do not claim Claude token deltas without a source.
- **Hook telemetry could become creepy or too verbose.**
  - Mitigation: opt-in; no code content; local-only JSONL; redaction mode; 0600 permissions.
- **Added telemetry could slow hooks.**
  - Mitigation: locked append/per-process temp record; measure duration; swallow failures.
- **Context hit rate is correlation, not causation.**
  - Mitigation: label it as approximate and pair it with outcome metrics.
- **Version ambiguity remains while local fork reports 1.5.2.**
  - Mitigation: include package path and hook command surface in report metadata; separately bump version before publishing.
- **Before/after temporal confound can dominate results.**
  - Mitigation: clearly label historical comparison as proxy-only and support
    interleaved TLDR on/off dogfood windows.

---

## Non-Goals

- No remote telemetry.
- No network service.
- No code-content logging by default.
- No attempt to prove causal impact from passive logs alone.
- No automatic publication or dashboard in the MVP.

---

## Open Questions

1. Should generated reports live under `reports/` and stay ignored, or should daily summaries be committed when useful?
2. How aggressively should we classify shell commands into tool categories after the exact-normalized MVP?
3. Do we want manual annotations immediately, or only after the proxy-only report shows promising signal?
4. Should the report compare against all historical sessions or only sessions in the same repo family?
5. Can Claude token/cost data be recovered from local logs or command output, or should Claude stay qualitative/proxy-only?

---

## Reviewer Notes Incorporated

Plan-reviewer findings incorporated:

- Add structured hook outcome metadata instead of inferring status from `HookResponse.noop()`.
- Treat hook timeouts as client-log-derived, not self-reported by killed hook processes.
- Split `trigger_files` from `recommended_related_files` for context hit-rate.
- Downgrade weak outcome reporting to `proxy-only` unless annotations/completion data exist.
- Make telemetry opt-in for publishable builds.
- Centralize instrumentation in `tldr/hooks/runner.py`.
- Use `uv run python -m tldr.cli` in validation.
- Define session matching, privacy redaction, sample-size gates, and JSONL line validation.

GLM findings incorporated:

- Label simple before/after comparison as temporally confounded and proxy-only.
- Add interleaved/toggle dogfood protocol.
- Add time-to-first-edit, net context delta, hook no-op rate, day-by-day trends,
  and "what to try next" recommendations.
- Remove undefined fuzzy near-identical tool-call matching from the MVP.
- Add telemetry locking/rotation and 0600 file-permission requirements.
- Add minimum comparable-session counts before verdicts.
