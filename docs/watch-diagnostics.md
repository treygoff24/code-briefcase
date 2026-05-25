# Watch diagnostics

Watch diagnostics are an opt-in TypeScript/JavaScript post-edit path that lets the daemon hold a warm `tsc --watch` process instead of spawning cold `tsc` on every edit.

## Status

- Default: **off**
- Enable: `CODE_BRIEFCASE_WATCH_DIAGNOSTICS=1`
- Legacy alias: `TLDR_WATCH_DIAGNOSTICS=1` works when the new env var is unset.
- Kill switch: `CODE_BRIEFCASE_WATCH_DIAGNOSTICS=0` wins over the legacy alias.
- Repo-local `node_modules/.bin/tsc` is not spawned by the watcher unless
  `CODE_BRIEFCASE_WATCH_DIAGNOSTICS_TRUST_REPO_BINARIES=1` is set. Without that
  explicit trust opt-in, the hook falls back to synchronous diagnostics.
- The daemon caps concurrent TypeScript watcher adapters per project with
  `CODE_BRIEFCASE_WATCH_DIAGNOSTICS_MAX_ADAPTERS` (default: `4`).
- Project-file coverage discovery is bounded by
  `CODE_BRIEFCASE_WATCH_PROJECT_FILES_TIMEOUT_MS` (default: `750`) so a cold
  watcher cannot stall the daemon for a long `tsc --listFilesOnly` run.

The watcher replaces only the TypeScript typecheck leg. `oxlint` and `oxfmt` still run through the synchronous lint/format leg when installed.

## Operator commands

```bash
code-briefcase daemon watchers status --project /path/to/repo --json
code-briefcase daemon watchers start src/app.ts --project /path/to/repo --json
code-briefcase daemon watchers stop --project /path/to/repo --json
```

The post-edit hook auto-starts the project daemon when watch diagnostics are enabled and the daemon is unreachable.

## Hook behavior

Watcher query statuses:

- `fresh`: use watcher diagnostics for the edited file.
- `stale`: use last-known watcher diagnostics and annotate the message.
- `pending`: surface a short “warming” message without blocking on cold `tsc`.
- `fallback_required` / `unhealthy`: run local synchronous diagnostics in the hook process. This fallback never calls the daemon `diagnostics` command.

## Telemetry

Hook records carrying watcher metadata use `schema_version: 3` and include:

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

Daemon watcher lifecycle events are emitted as `event: "watch-diagnostics-event"` with `adapter_key_hash` instead of raw adapter paths in privacy-safe mode.

## Real-repo checkpoint

The checkpoint script is read-only unless `--exercise-edits` is passed, refuses dirty repos unless `--allow-dirty` is passed, and writes isolated telemetry instead of appending to the default telemetry file.

```bash
mkdir -p reports
RUN_ID="$(date +%Y%m%d-%H%M%S)"
REPO_ARGS=()

[ -d /Users/treygoff/Code/atlasos ] && \
  REPO_ARGS+=(--repo atlasos=/Users/treygoff/Code/atlasos)

[ -d /Users/treygoff/Code/llm-council ] && \
  REPO_ARGS+=(--repo llm-council=/Users/treygoff/Code/llm-council)

python3 scripts/watch_diagnostics_checkpoint.py \
  "${REPO_ARGS[@]}" \
  --exercise-edits \
  --baseline-iterations 5 \
  --watch-iterations 10 \
  --warmups 2 \
  --telemetry-path "reports/watch-diagnostics-${RUN_ID}.jsonl" \
  --json-out "reports/watch-diagnostics-${RUN_ID}.json" \
  --fail-on-threshold
```

Threshold defaults:

- hook p50: 200 ms
- hook p95: 500 ms
- watcher settle p50: 600 ms
- watcher settle p95: 2000 ms

Interpret results conservatively: do not mix sync samples with watcher-used samples, and do not treat `pending`, `fallback_required`, or `unhealthy` as warm success.
