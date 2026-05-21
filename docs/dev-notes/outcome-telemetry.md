# TLDR outcome telemetry and backfill

## What live telemetry captures

With `TLDR_TELEMETRY=1`, each hook execution appends one JSONL record (`schema_version: 2`) including:

- Hook status, duration, injected bytes, and context kind
- Hashed trigger, recommended, and surfaced file paths
- Candidate lifecycle metadata (`candidate_files` with reason, rank, score, surfaced flag)
- Optional `session_id` and `hook_run_id` for offline joins

`surfaced=True` means the hook response actually mentioned or injected that path. Ranked but not injected candidates stay `surfaced=False`.

## Privacy guarantees

Default telemetry is privacy-safe:

- No absolute paths, repo names, raw commands, outputs, prompts, or source snippets
- Project and file identifiers use stable hashes (`<redacted>/<project_hash>/<path_hash>`)
- Human-readable local paths require explicit `TLDR_TELEMETRY_REDACT_PATHS=0`

Outcome JSON/Markdown/HTML reports follow the same rules.
Command hashes are used internally for repeat counting, but exported reports only include aggregate command-shape counts.

## Local-rich mode

For short local-only dogfood runs, opt into richer evidence:

```bash
export TLDR_TELEMETRY=1
export TLDR_TELEMETRY_MODE=local-rich
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
- long local evidence strings are capped by `TLDR_TELEMETRY_LOCAL_STRING_LIMIT` (default: 8000 chars)

Local-rich reports are intentionally **not shareable**. Use them for local diagnosis, then produce a privacy-safe report by omitting `--include-local-evidence`.

## Retroactive limits

Backfilled sessions use the best evidence available in historical logs. For newer `schema_version: 2` telemetry, candidate lifecycle metrics come from explicit `candidate_files`. For older telemetry that only recorded surfaced or recommended paths, backfill treats those visible paths as the legacy candidate set. That preserves useful hit-rate evidence without inventing non-surfaced candidates that were never recorded.

## Confidence labels

| Label | Meaning |
| --- | --- |
| `match_confidence` | How confidently telemetry joined to a session |
| `attribution_confidence` | How confidently surfaced TLDR context links to later agent action |
| `causal_confidence` | Counterfactual strength (`proxy-only`, `manual-annotation`, `ab-test`, `matched-baseline`) |

Historical backfill defaults to `causal_confidence=proxy-only`.

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
  --tldr-telemetry tests/fixtures/eval/backfill_tldr_telemetry.jsonl \
  --json-out /tmp/tldr-backfill-fixture.json

python3 scripts/render_tldr_outcome_report.py \
  --input /tmp/tldr-backfill-fixture.json \
  --markdown-out /tmp/tldr-outcome-fixture.md \
  --html-out /tmp/tldr-outcome-fixture.html
```

## Real local data (not committed)

```bash
python3 scripts/backfill_tldr_outcomes.py \
  --start 2026-05-20T00:00:00-05:00 \
  --end 2026-05-21T00:00:00-05:00 \
  --json-out reports/tldr-backfill-2026-05-20.json

python3 scripts/render_tldr_outcome_report.py \
  --input reports/tldr-backfill-2026-05-20.json \
  --markdown-out reports/tldr-outcome-2026-05-20.md \
  --html-out reports/tldr-outcome-2026-05-20.html
```

For local-rich raw evidence:

```bash
python3 scripts/backfill_tldr_outcomes.py \
  --start 2026-05-20T00:00:00-05:00 \
  --end 2026-05-21T00:00:00-05:00 \
  --json-out reports/tldr-backfill-2026-05-20-rich.json \
  --include-local-evidence
```

Generated files under `reports/` are local artifacts. Do not stage `reports/*.json`, `reports/*.md`, or `reports/*.html` by default.
