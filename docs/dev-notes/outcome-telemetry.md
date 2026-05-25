# Code Briefcase outcome telemetry and backfill

## What live telemetry captures

With `CODE_BRIEFCASE_TELEMETRY=1`, each hook execution appends one JSONL record. Existing hook records use `schema_version: 2`; post-edit records that carry watch-diagnostics metadata use `schema_version: 3`.

- Hook status, duration, injected bytes, context kind, and `noop_reason`
- Hashed trigger, recommended, and surfaced file paths
- Candidate lifecycle metadata (`candidate_files` with reason, rank, score, surfaced flag)
- Optional `session_id` and `hook_run_id` for offline joins
- Watch-diagnostics metadata on v3 post-edit records: enabled/attempted/used flags, watcher status, wait/age/batch fields, fallback reason, and diagnostics backend

`surfaced=True` means the hook response actually mentioned or injected that path. Ranked but not injected candidates stay `surfaced=False`.
`candidate_files_later_used`, `recommended_files_used`, and `surfaced_files_used`
only count session file activity timestamped strictly after the hook execution;
prior reads/edits are not credited as Code Briefcase-attributed use.

## Privacy guarantees

Default telemetry is privacy-safe:

- No absolute paths, repo names, raw commands, outputs, prompts, or source snippets
- Project and file identifiers use stable hashes (`<redacted>/<project_hash>/<path_hash>`)
- Human-readable local paths require explicit `CODE_BRIEFCASE_TELEMETRY_REDACT_PATHS=0`

Outcome JSON/Markdown/HTML reports follow the same rules.
Command hashes are used internally for repeat counting, but exported reports only include aggregate command-shape counts.

## Local-rich mode

For short local-only dogfood runs, opt into richer evidence:

```bash
export CODE_BRIEFCASE_TELEMETRY=1
export CODE_BRIEFCASE_TELEMETRY_MODE=local-rich
```

`local-rich` keeps the default telemetry counters, but also records local-only evidence that makes debugging and interpretation easier:

- raw project path and readable file paths
- hook tool name and sanitized tool input
- raw recommended/surfaced/candidate paths plus stable path hashes
- retroactive raw tool-call evidence when backfill is run with `--include-local-evidence`

Basic safety rails still apply:

- telemetry files are written with `0600` permissions
- generated `reports/*.json`, `reports/*.md`, and `reports/*.html` stay gitignored
- obvious secrets, tokens, private-key paths, `.env` paths, and secret-looking values are redacted
- long local evidence strings are capped by `CODE_BRIEFCASE_TELEMETRY_LOCAL_STRING_LIMIT` (default: 8000 chars)

Local-rich reports are intentionally **not shareable**. Use them for local diagnosis, then produce a privacy-safe report by omitting `--include-local-evidence`.

## Watch diagnostics telemetry

Watch diagnostics add two v3 record shapes:

1. Post-edit hook records with fields:
   - `watch_diagnostics_enabled`
   - `watch_diagnostics_attempted`
   - `watch_diagnostics_used`
   - `watch_diagnostics_status`
   - `watch_diagnostics_statuses`
   - `watch_diagnostics_age_ms`
   - `watch_diagnostics_wait_ms`
   - `watch_diagnostics_query_budget_ms`
   - `watch_diagnostics_batch_seq`
   - `watch_diagnostics_fallback_reason`
   - `diagnostics_backend`
2. Daemon lifecycle records with `event="watch-diagnostics-event"`, `action`, `adapter_key_hash`, and optional batch/queue/exit metadata.

Schema v2 records mean watcher state is unknown or not recorded; do not count them as watcher-enabled samples.

See [../watch-diagnostics.md](../watch-diagnostics.md) for operator commands and the real-repo checkpoint script.

## Retroactive limits

Backfilled sessions use the best evidence available in historical logs. For newer `schema_version: 2` telemetry, candidate lifecycle metrics come from explicit `candidate_files`. For older telemetry that only recorded surfaced or recommended paths, backfill treats those visible paths as the legacy candidate set. That preserves useful hit-rate evidence without inventing non-surfaced candidates that were never recorded.

## Confidence labels

| Label | Meaning |
| --- | --- |
| `match_confidence` | How confidently telemetry joined to a session |
| `attribution_confidence` | How confidently surfaced Code Briefcase context links to later agent action |
| `causal_confidence` | Counterfactual strength (`proxy-only`, `manual-annotation`, `ab-test`, `matched-baseline`) |

Historical backfill defaults to `causal_confidence=proxy-only`.

## Skip and clean-check reasons

Backfill rollups aggregate low-cardinality hook abstention reasons:

- `tldr_skip_reason_counts` — e.g. `markdown_unsupported`, `outside_project`, `secret_like`
- `tldr_noop_reason_counts` — e.g. `clean_no_diagnostics`, `clean`, `bypass`
- `tldr_clean_checks` — post-edit runs with `noop_reason=clean_no_diagnostics` (successful clean checks, not failures)

Markdown (`.md`/`.mdx`) is intentionally unsupported for Code Briefcase read/edit context hooks.
Line-specific reads are intentionally conservative: tiny files and repeated
targeted reads for the same file/session are skipped with explicit reason codes.

## Verdict values

- `helpful` / `neutral` / `harmful` — proxy outcome signals from session behavior
- `proxy-only` — metrics without strong attribution or causal proof
- `insufficient-data` — not enough hook or tool activity

## Fixture-safe commands

```bash
python3 scripts/backfill_tldr_outcomes.py \
  --start 2026-05-20T00:00:00Z \
  --end 2026-05-21T00:00:00Z \
  --codex-root tests/fixtures/eval/backfill_codex_root \
  --claude-root tests/fixtures/eval/backfill_claude_root \
  --code-briefcase-telemetry tests/fixtures/eval/backfill_tldr_telemetry.jsonl \
  --json-out /tmp/code-briefcase-backfill-fixture.json

python3 scripts/render_tldr_outcome_report.py \
  --input /tmp/code-briefcase-backfill-fixture.json \
  --markdown-out /tmp/code-briefcase-outcome-fixture.md \
  --html-out /tmp/code-briefcase-outcome-fixture.html
```

## Real local data (not committed)

```bash
python3 scripts/backfill_tldr_outcomes.py \
  --start 2026-05-20T00:00:00-05:00 \
  --end 2026-05-21T00:00:00-05:00 \
  --json-out reports/code-briefcase-backfill-2026-05-20.json

python3 scripts/render_tldr_outcome_report.py \
  --input reports/code-briefcase-backfill-2026-05-20.json \
  --markdown-out reports/code-briefcase-outcome-2026-05-20.md \
  --html-out reports/code-briefcase-outcome-2026-05-20.html
```

For local-rich raw evidence:

```bash
python3 scripts/backfill_tldr_outcomes.py \
  --start 2026-05-20T00:00:00-05:00 \
  --end 2026-05-21T00:00:00-05:00 \
  --json-out reports/code-briefcase-backfill-2026-05-20-rich.json \
  --include-local-evidence
```

Generated files under `reports/` are local artifacts. Do not stage `reports/*.json`, `reports/*.md`, or `reports/*.html` by default.
