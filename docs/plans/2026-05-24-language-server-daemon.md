# Watch-Diagnostics Adapters for Amortized Post-Edit Latency

**Date:** 2026-05-24
**Branch:** TBD (`feat/watch-diagnostics` recommended)
**Owner:** Trey
**Status:** v3 — folds plan-reviewer audit of v2 (see §11 for full review history)

---

## 1. Problem

`tldr/hooks/post_edit.py::_diagnostic_message_for_file` ([post_edit.py:84-100](../../tldr/hooks/post_edit.py#L84)) synchronously calls `tldr.diagnostics.get_diagnostics(path)` on every edit. For TypeScript files this routes through [`_run_js_ts_diagnostics`](../../tldr/diagnostics.py#L1104), which on every invocation:

1. Resolves the nearest project `tsconfig.json` via [`_find_js_ts_project_config`](../../tldr/diagnostics.py#L371).
2. Writes a **brand-new** temp-dir tsconfig via [`_write_single_file_tsconfig`](../../tldr/diagnostics.py#L384) — deliberately overriding the root file set to one file so project-wide errors don't leak into the LLM context.
3. Invokes `tsc --noEmit --project <new-temp>` (30 s timeout).
4. Runs `oxlint` + `oxfmt` in parallel inside `ThreadPoolExecutor`.
5. Tears the temp dir down in `finally`.

Because step 2 regenerates the config in a fresh `mkdtemp` every call, there is no `.tsbuildinfo` for tsc to reuse — **every invocation is fully cold**. Cost scales with the file's transitive import graph.

### Empirical evidence (2026-05-24)

Per-file `get_diagnostics()` cost on `/Users/treygoff/Code/atlasos` (1,452 TS files), measured against the actual hook codepath:

| target | duration |
| --- | --- |
| `src/lib/assistant/default-tool-policy.ts` (shallow) | 320 ms |
| `src/lib/genui/action-context.tsx` | 319 ms |
| `src/lib/assistant/services/__tests__/user-preferences-service.test.ts` | 920 ms |
| `src/components/freshness/freshness-summary.tsx` | 1.89 s |
| `src/app/(app)/campaigns/[id]/strategy/components/use-risk-form-submit.ts` | 2.84 s |
| `src/app/(auth)/login/mfa/page.test.tsx` | 3.24 s |

Production telemetry (`~/.tldr/telemetry.jsonl`, codex `post-edit`, last 2 h):

| project | n | p50 | p95 | p99 | max |
| --- | --- | --- | --- | --- | --- |
| atlasos | 56 | 1,384 ms | 4,226 ms | 7,336 ms | 7,336 ms |
| llm-council | 23 | 2,639 ms | 8,114 ms | 8,832 ms | 8,832 ms |
| Python-only / small TS | hundreds | 9–30 ms | 50–70 ms | 70 ms | 74 ms |

8.8 s is uncomfortably close to the 30 s tsc timeout — bigger codebases will silently time out.

### Why patching in place is wrong

Any synchronous per-edit `tsc` invocation from a hook is bounded by tsc's cold-start + transitive-graph cost. The TypeScript team's answer is `tsc --watch` (or `tsserver` for finer-grained queries). Synthesizing a fresh tsconfig per edit guarantees we pay full cold cost forever.

The Phase 0 mitigation (per-file stable buildinfo cache) is empirically validated — 3.1 s → 1.0 s on warm repeated edits to the same file in atlasos — but it does **not** change the architecture. The durable fix is to **hold compiler state across edits**.

---

## 2. Why now, and OSS trajectory

llm-tldr is becoming a popular hook layer for agentic clients (Claude, Codex, Cursor, Droid, OpenCode). TS post-edit latency is the largest tail-latency contributor in the hook surface and the most visible to users on real production codebases. Consequences:

1. **Tool-roundtrip blocking.** Agentic clients wait synchronously for hook output. 5–8 s waits read as "TLDR is slow / why is my edit hanging."
2. **Silent truncation at scale.** Big repos hit the 30 s tsc timeout, producing `RuntimeError` (one seen in today's telemetry) or `clean_no_diagnostics` — false-clean defeats the hook's value.
3. **Architectural ceiling.** Every new language inherits the cold-start tax. We need the amortization pattern in place before extending coverage.

---

## 3. Goals & non-goals

### Goals

1. **Hook response latency** on TS projects: p50 < 200 ms, p95 < 500 ms after the watch adapter has warmed. Tested against repos up to 5,000 TS files (atlasos / llm-council scale). Repos larger than 5K files are out of v1 scope; perf targets may need recalibration above that.
2. **Fresh diagnostic settle latency** (separate metric): p50 < 600 ms, p95 < 2 s for incremental rechecks on warm tsc.
3. **First-edit cost bounded** — fresh project pays one-time cold-start, but no single hook call exceeds `warmup_budget_ms` (default 8 s) before returning a `pending` response and continuing warming async.
4. **Architecture extends to other languages** via well-defined adapter capabilities (not assuming all tools have CLI-watch output shape).
5. **Graceful degradation.** If anything fails (daemon down, tool missing, parser unhealthy, fs watcher unreliable, CI environment), the hook falls back to today's synchronous path. No new user-visible failure.
6. **Opt-in rollout.** Phase 1 ships **default-off** behind `TLDR_WATCH_DIAGNOSTICS=1`. Default-on only after one minor release of field telemetry shows the subsystem is stable.
7. **Operational properties suitable for OSS distribution:** no new required dependencies; background processes have visible status via `tldr daemon watchers status`; manual stop via `tldr daemon watchers stop`; kill switch via `TLDR_WATCH_DIAGNOSTICS=0`; resource caps documented and enforced.
8. **Behavior-preserving for diagnostics surface.** Hook only ever surfaces diagnostics for files touched by the current edit; the existing single-file scoping in `_write_single_file_tsconfig` is preserved (cross-file project errors are not leaked into LLM context).

### Non-goals

- **LSP-protocol implementation in Phase 1.** Decision deferred. pyright/gopls/rust-analyzer all speak LSP natively, which would give structured diagnostics without per-tool text parsing. We will run an LSP feasibility spike before committing to Phase 3 (see §5 Phase 1.5).
- Replacing the existing `get_diagnostics` synchronous API — it stays as fallback and CLI entry point.
- Project-wide error reporting in the hook. We only return diagnostics for the file the edit targeted, even though the watcher sees the whole project.
- Cross-language project graphs (TS ↔ Python ↔ Rust monorepos).
- Editor integration. Adapters serve the hook layer only.
- A meta-daemon coordinating multiple project daemons (see §4.6 — we drop the global cap in v2).

---

## 4. Architecture

### 4.1 Recap of existing daemon

`tldr.daemon` is already a per-project, socket-based, long-lived Python process:

- Started lazily; auto-shuts down after `IDLE_TIMEOUT = 30 min` of inactivity.
- Holds a Salsa-style cached query layer ([cached_queries.py](../../tldr/daemon/cached_queries.py)) and a dirty-file tracker.
- Dispatches commands via `TLDRDaemon.handle_command()` ([core.py:174](../../tldr/daemon/core.py#L174)).
- `_handle_diagnostics` ([core.py:886](../../tldr/daemon/core.py#L886)) today is a thin pass-through that calls `get_diagnostics()` — i.e., runs cold tsc inside the daemon process. Not amortized.
- **Single-threaded request handler** ([core.py:1203](../../tldr/daemon/core.py#L1203)). One connection at a time. Any handler that blocks blocks everything.
- `query_daemon()` ([startup.py:454](../../tldr/daemon/startup.py#L454)) does one `recv(65536)` with no socket timeouts and no message framing. Large or split responses will fail.

This is the foundation. We **harden the transport** before extending, then build the watch subsystem on top.

### 4.2 New subsystem name and structure

We call this subsystem **`tldr.daemon.watchers`** (not "language servers" — Phase 1 deliberately does not implement LSP). Structure:

- `tldr/daemon/watchers/__init__.py` — registry + lifecycle entry points
- `tldr/daemon/watchers/base.py` — `WatchAdapter` ABC + capability enum + response schema
- `tldr/daemon/watchers/typescript.py` — `tsc --watch` adapter (first implementation)
- `tldr/daemon/watchers/supervisor.py` — owns running adapters, in-memory diagnostic map, batch sequence numbers
- `tldr/daemon/watchers/test_fixtures/` — fixture TS projects for integration tests

### 4.3 Adapter capabilities

Adapters declare capabilities the supervisor uses to route correctly. v1's `Protocol` was tuned to tsc-watch and would not fit LSP-native servers (gopls, rust-analyzer). v2:

```python
class AdapterCapability(StrEnum):
    COMPILER_WATCH_TEXT = "compiler_watch_text"   # tsc --watch, mypy --watch
    LSP_DIAGNOSTICS     = "lsp_diagnostics"       # gopls, rust-analyzer, pyright-langserver
    ONE_SHOT_CACHED     = "one_shot_cached"       # Phase 0 fallback path
```

Phase 1 only implements `COMPILER_WATCH_TEXT`. Phase 3 evaluates `LSP_DIAGNOSTICS` for pyright.

### 4.4 Adapter contract (v2)

```python
@dataclass(frozen=True)
class AdapterKey:
    """Identity by which the supervisor distinguishes adapter instances.
    Two edits resolving to the same key share a watcher; different keys spawn
    separate watchers (which is correct for monorepos with multiple tsconfigs)."""
    language: str
    tool_path: Path        # e.g. /repo/node_modules/.bin/tsc
    config_path: Path      # e.g. /repo/packages/web/tsconfig.json
    mode: str              # e.g. "noemit", "allowjs"

@dataclass
class FileVersion:
    """Captured at query time; the supervisor stamps adapter outputs with the
    batch sequence number, so the hook can detect 'I edited at version N+1
    but you're returning diagnostics for N'."""
    mtime_ns: int
    content_sha256: str | None  # optional, captured only when hook supplies it

class WatchAdapter(ABC):
    CAPABILITY: AdapterCapability
    LANGUAGE: str

    @abstractmethod
    def can_start(self, key: AdapterKey) -> CanStartResult: ...
    # returns (ok, reason, version) — tool installed, version supported, project config present

    @abstractmethod
    def start(self, key: AdapterKey, supervisor: Supervisor) -> None: ...
    # spawn the long-lived process IN A BACKGROUND THREAD. The supervisor's
    # request-handler thread must NEVER block on start().

    @abstractmethod
    def notify_edit(self, file_path: Path, version: FileVersion) -> None: ...
    # Hook tells us "file edited at version V"; supervisor marks subsequent
    # queries on file_path as potentially-stale until the next batch covers V.
    # Replaces v1's "request_recheck-as-no-op" — was incorrect for tsc --watch.

    @abstractmethod
    def query(self, file_path: Path, version: FileVersion, budget_ms: int) -> QueryResponse: ...

    @abstractmethod
    def stop(self, grace_ms: int = 3000) -> None: ...
    # SIGTERM the process group, wait grace_ms, SIGKILL stragglers.

    @abstractmethod
    def health(self) -> AdapterHealth: ...
```

### 4.5 Query response schema

```python
class QueryStatus(StrEnum):
    FRESH              = "fresh"               # diagnostics current as of version V
    STALE              = "stale"               # last_known result, but adapter hasn't seen V
    PENDING            = "pending"             # adapter is mid-batch and budget elapsed
    FALLBACK_REQUIRED  = "fallback_required"   # adapter unhealthy or absent
    UNHEALTHY          = "unhealthy"           # parser failed, must restart

@dataclass
class QueryResponse:
    status: QueryStatus
    diagnostics: list[Diagnostic]         # may be []
    batch_seq: int | None                 # supervisor's batch counter
    last_check_at: float | None           # epoch seconds
    age_ms: int | None                    # ms since last_check_at
```

The hook chooses behavior per status:

| Status | Hook behavior |
| --- | --- |
| FRESH | Use `diagnostics` as today. Telemetry `langserver_fresh=true`. |
| STALE | Use `diagnostics`, annotate output `[showing diagnostics from <age_ms>ms ago — refresh in progress]`. Telemetry `langserver_fresh=false`. |
| PENDING | Surface `Post-edit check in progress.` Don't show stale. |
| FALLBACK_REQUIRED | Sync `get_diagnostics()` (existing path). |
| UNHEALTHY | Sync `get_diagnostics()`; telemetry surfaces `adapter_unhealthy`. |

### 4.6 TypeScript adapter (Phase 1 implementation)

**Key change from v1:** the watch process spawns against a **persistent per-project tsconfig** that mirrors the project's own (extends + incremental + stable `tsBuildInfoFile`), not against the project's raw tsconfig and not against a per-file synth. Rationale (folds qwen 2.1, codex C1, plus single-file-scope concern):

- One `tsc --watch` per `AdapterKey = (tool_path, tsconfig_path, "noemit")`. A monorepo with three `tsconfig.json` files will have three adapters (correct).
- The watcher holds the **whole project** program state (cheap memory once warm; high amortization).
- The supervisor maintains `dict[Path, list[Diagnostic]]` keyed by absolute path, plus a parallel `dict[Path, BatchMetadata]` carrying `(last_batch_seq, last_check_at, file_mtime_ns_at_check)`. Type names match §4.5's response schema.
- On `query(file_path)`, the supervisor **filters** the in-memory map to that file only. **Cross-file project errors are never returned to the hook** — preserves the existing single-file scoping intent in `_write_single_file_tsconfig`.
- The Phase 0 per-file cache and the Phase 1 watcher **share** the on-disk `tsBuildInfoFile` location. The canonical cache layout is specified once in §4.13 — both phases must reference §4.13 rather than re-deriving paths.

**Spawn details:**

- Command: `tsc --noEmit --watch --pretty false --project <persistent-config-path>`.
  - Drop `--preserveWatchOutput` (qwen nit — flag governs emitted JS retention, irrelevant under `--noEmit`).
- cwd: `<config_path>.parent`.
- Env: `LC_ALL=C`, `LANG=C`, `TZ=UTC` — force English sentinels, avoid locale-specific output that breaks the parser (qwen 3.2). Inherit `PATH`.
- Process group: spawn with `start_new_session=True` (POSIX) / `CREATE_NEW_PROCESS_GROUP` (Windows) so we can signal the entire tree.
- Stdout/stderr: line-buffered, captured by a background reader thread per adapter.

**Output parser:**

- Sentinel-line regex: `re.compile(r"^Found (\d+) errors?\. Watching for file changes\.$")` and `re.compile(r"^File change detected\. Starting incremental")`.
- Between sentinels, accumulate output into a `batch_buffer`. On end-of-batch sentinel, hand `batch_buffer` to a parser that produces `list[Diagnostic]`.
- The parser is the existing `_parse_tsc_output` ([diagnostics.py:495](../../tldr/diagnostics.py#L495)) wrapped to handle multi-line diagnostics (qwen 3.2 — TS 5.x can embed newlines in error messages). New helper `_accumulate_tsc_diagnostic_lines()` groups continuation lines (those that start with whitespace) with their parent error.
- **Parser fail-closed (codex I3, qwen 3.2):** if the parser produces N diagnostics but the "Found N errors" sentinel says M, and `N != M`, the adapter marks itself UNHEALTHY and the hook falls back. We never return zero when the truth is non-zero.
- **Stalled-batch fallback (qwen 5.3):** if 5 s pass after "File change detected" with no closing sentinel, emit a synthetic `BATCH_STALLED` event, mark the in-flight queries as `PENDING`, and do **not** restart the adapter on first occurrence (only on three consecutive stalls inside 5 min).
- **RSS-cap thrash protection:** the same 3-strike-in-5-min rule applies to `MAX_RSS_MB` exits. After three RSS-cap exits within 5 min for the same `AdapterKey`, the adapter goes UNHEALTHY-permanent until manual restart or daemon restart, preventing thrash loops where a chronically over-budget watcher OOMs every 30 s.

**Batch lifecycle:**

- `batch_seq` increments on every "File change detected".
- On `notify_edit(file, version)`, the supervisor records `(file, version, edit_seq)` in a pending-edits queue.
- On batch-complete, the supervisor:
  - **Replaces** (not merges) the adapter's diagnostic map for files mentioned in the batch (codex I4).
  - Advances `last_completed_batch_seq`.
  - Wakes any threads waiting on a `Condition` for that file.
- On `query(file, version, budget_ms)`:
  - If `version.mtime_ns <= last_completed_batch_mtime_ns_for(file)` → FRESH.
  - Else wait on the Condition for up to `budget_ms`; if a batch completes covering this file → FRESH; if not → return STALE (with `last_known`) or PENDING (if no `last_known`).

**Watcher invalidation:**

- Poll mtime of `tsconfig.json`, `package.json`, and the resolved tsc binary every 2 s in a side thread. On change, stop+restart the adapter.
- Also watch `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml` — dependency updates change types.

**Restart policy:**

- On unexpected exit: exponential backoff 1 s → 30 s, cap 5 restarts in 10 min, then UNHEALTHY-permanent until manual restart or daemon restart.
- All adapter activity logged to `~/.tldr/logs/watchers/<adapter>-<projhash>-<configkey>.log` (rotated 10 MB, keep 3). Directory created on daemon start (qwen nit).

### 4.7 Daemon transport hardening (Phase 0.5, ships before Phase 1)

v1 assumed `query_daemon` was safe for langserver-scale responses. It isn't. Phase 0.5 ships a **separate PR** that fixes the transport:

1. **Length-prefix framing** for daemon responses: 8-byte big-endian payload length, then JSON. Client and server agree on this in a versioned handshake (`{"cmd": "hello", "protocol_version": 2}`).
2. **Socket timeouts** on the client: `connect_timeout_ms` (default 200) and `response_timeout_ms` (default 1000), configurable per call.
3. **New helper** `query_or_start_daemon(project, command, *, connect_timeout_ms, response_timeout_ms, auto_start=True) -> DaemonResponse`. Replaces ad-hoc `query_daemon(...)` calls from hooks. If `auto_start=True` and the daemon isn't reachable, it requests startup via the existing `start_daemon` helper, then retries the query within an additional `startup_budget_ms` (default 2000).
4. **`DaemonResponse`** distinguishes `OK`, `Unreachable`, `Timeout`, `ProtocolMismatch`, `FallbackRequired` — the hook layer reads this to choose sync fallback vs. propagating partial results (codex I3, qwen 3.5).
5. **Backwards-compat:** legacy clients (handshake protocol_version=1) still get the old unframed response for commands that already exist. New commands (`diagnostics-watch`) require v2.

This is a small, independently shippable PR that gives the rest of the daemon better operational properties even if we never built the watcher.

### 4.8 Hook integration (post-edit)

`tldr/hooks/post_edit.py::_diagnostic_message_for_file` becomes:

```python
def _diagnostic_message_for_file(event, file_path):
    # ... existing markdown/language/exists gates unchanged ...
    notify_daemon(event.cwd, file_path)  # existing dirty tracking
    if not _watch_diagnostics_enabled():
        return _sync_diagnostics(file_path, language)

    version = FileVersion(mtime_ns=file_path.stat().st_mtime_ns, content_sha256=None)
    resp = query_or_start_daemon_diagnostics(
        event.cwd, file_path, language=language, version=version,
        budget_ms=int(os.environ.get("TLDR_WATCH_DIAGNOSTICS_QUERY_BUDGET_MS", "400")),
    )

    if resp.status in (QueryStatus.FRESH, QueryStatus.STALE):
        tsc_diag = resp.diagnostics
        lint_fmt_diag = _run_lint_format_legs(file_path, language)
        diag = tsc_diag + lint_fmt_diag
        return _format_diagnostic_message_from_struct(file_path, diag, status=resp.status)
    if resp.status == QueryStatus.PENDING:
        # Don't fall back to sync — would duplicate the slow tsc invocation
        # the watcher is already mid-flight on.
        return _format_pending_message(file_path)
    # FALLBACK_REQUIRED / UNHEALTHY → full sync
    diag = _sync_diagnostics_full(file_path, language)
    return _format_diagnostic_message_from_struct(file_path, diag, status=resp.status)
```

The `_diagnostic_message_for_file` function preserves its existing `tuple[str | None, int, int]` return shape. The hook builds the result with these helpers (all newly added, signatures spec'd here so an external contributor can implement this section without guessing):

```python
def _sync_diagnostics_full(path: Path, language: str) -> list[Diagnostic]:
    """Today's full sync path: tsc + oxlint + oxfmt. Used as the watcher
    fallback. Implementation moves out of get_diagnostics() unchanged."""

def _run_lint_format_legs(path: Path, language: str) -> list[Diagnostic]:
    """oxlint + oxfmt only. Always runs when the watcher succeeded, so the
    lint/format diagnostics surface continues to match today's behavior even
    though the typecheck leg moved to the watcher."""

def _format_diagnostic_message_from_struct(
    path: Path, diagnostics: list[Diagnostic], status: QueryStatus
) -> tuple[str | None, int, int]:
    """Formatter used for FRESH and STALE responses. Mirrors the existing
    format_diagnostic_message() shape but accepts a QueryStatus so it can
    annotate STALE results ('[showing diagnostics from <age_ms>ms ago]')."""

def _format_pending_message(path: Path) -> tuple[str, int, int]:
    """Returns ('Post-edit check in progress.', 0, 0). Distinct from
    clean-no-diagnostics because the model should know to expect a follow-up."""
```

**Critical:** the watcher returns only the tsc leg; `_run_lint_format_legs` runs unconditionally on every watcher-success path. On watcher failure (`FALLBACK_REQUIRED` / `UNHEALTHY`), the hook uses `_sync_diagnostics_full` which still runs all three legs. This addresses codex C2: today's diagnostics include tsc+oxlint+oxfmt, and the watcher only replaces the tsc leg. The lint/format legs are cheap (~50 ms total) and run on every post-edit regardless of watcher state. The supervisor does not "own" lint/format.

Concretely, `_run_js_ts_diagnostics` is refactored into three independently-callable legs:

- `_run_tsc_diagnostics_sync(path)` — today's full path, used as fallback.
- `_run_oxlint_diagnostics(path)` — runs in <50 ms always.
- `_run_oxfmt_diagnostics(path)` — runs in <50 ms always.

The watcher returns the tsc result; the hook merges with the always-on lint/format legs.

### 4.9 Lifecycle, resource control, and environment detection

| Event | Behavior |
| --- | --- |
| First hook fires for a project with `TLDR_WATCH_DIAGNOSTICS=1` | Daemon starts (existing flow). Supervisor evaluates registry: for each `AdapterKey` discoverable from project, calls `can_start()` in a background thread; spawns adapters that pass. Initial hook call returns sync (watcher not yet warm). |
| `can_start()` returns false | Logged once, retried **on relevant mtime change** (codex I6 — addresses npm install case). Specifically: watch `package.json` + `node_modules/.bin/tsc` mtime in the supervisor; re-evaluate on change. |
| Adapter watch process exits | Restart with backoff (§4.6 restart policy). |
| Daemon idle for `IDLE_TIMEOUT` | Adapters stopped first (`stop()` invoked with grace), then daemon exits. Supervisor `__exit__` is called from the existing shutdown finally (codex I7). |
| User runs `tldr daemon watchers stop [--project PATH]` | All adapters SIGTERMed, 3 s grace, SIGKILLed; supervisor enters `disabled` state. Hook falls back to sync until `tldr daemon watchers start`. (qwen 5.8) |
| User runs `tldr daemon watchers status [--project PATH]` | Prints per-adapter: key, PID, uptime, last batch seq, last batch duration, queued edits, parser health, restart history, RSS. |
| Daemon process killed externally | Existing PID-file resurrection. On startup the supervisor checks for orphaned child processes whose PPID matches the dead PID-file and kills them (qwen 5.4). **Additionally**, every `tldr` invocation (not just daemon start) runs a cheap PID sweep over `~/.tldr/run/watchers/*.pid` — any PID whose parent is gone is killed. This handles the laptop-suspend scenario where the daemon died overnight and no new daemon has started yet (plan-reviewer Risk: launchd/systemd). |
| `CI=true`, `GITHUB_ACTIONS=true`, `BUILDKITE=true`, `CIRCLECI=true`, or any of the GHA-standard CI env vars are set | Watchers disabled by default (CI = no benefit from amortization; hook reverts to sync). User can override with `TLDR_WATCH_DIAGNOSTICS=1`. (qwen 6.5) |
| Project is on NFS, FUSE, SMB, or in a Dropbox-style path | Detected via `statfs`/`statvfs` and path heuristics. Watcher refuses to start; logs once; falls back to sync. (qwen + codex implied) |
| Git branch switch (mass mtime change) | Supervisor detects "many files invalidated within 1 s window" → marks all adapters PENDING and waits up to `branch_switch_budget_ms` (default 4 s) for the resulting big batch before returning STALE. (qwen 6.3) |

**Resource caps (per-project, not global):**

- Adapter RSS check every 30 s (using `os.stat`/`ps -o rss=` shim — no new dependency; psutil is optional optimization).
- Configurable per-adapter cap (`TLDR_WATCH_DIAGNOSTICS_MAX_RSS_MB`, default 1500). On exceed: log, stop the adapter, supervisor goes UNHEALTHY for that key.
- **No global daemon cap in v2** (codex I11, qwen 2.6). Cross-daemon coordination requires a shared registry which is its own design problem; deferred to future work.

### 4.10 Configuration

| Mechanism | Knob | Default | Effect |
| --- | --- | --- | --- |
| Env | `TLDR_WATCH_DIAGNOSTICS` | `0` in Phase 1, will flip to `1` in Phase 2.5 | Master switch |
| Env | `TLDR_WATCH_DIAGNOSTICS_WARMUP_BUDGET_MS` | `8000` | Per-adapter cold-start budget |
| Env | `TLDR_WATCH_DIAGNOSTICS_QUERY_BUDGET_MS` | `400` | Per-query wait when mid-batch |
| Env | `TLDR_WATCH_DIAGNOSTICS_MAX_RSS_MB` | `1500` | Per-adapter RSS cap |
| Env | `TLDR_WATCH_DIAGNOSTICS_IDLE_TIMEOUT_S` | `7200` (2 hr) | Adapter-specific idle (longer than daemon IDLE_TIMEOUT — addresses qwen 3.7) |
| Per-project | `.tldr/config.json` `"watchers": {…}` key | — | Same JSON file the daemon already reads ([core.py:137](../../tldr/daemon/core.py#L137)). JSON object key, not a TOML section. v1 said `.toml`; corrected to match existing convention. (codex I12, plan-reviewer Nit) |
| CLI | `tldr daemon watchers {status,start,stop} [--project PATH]` | — | Operator controls |
| CLI | `tldr cache clean` | — | Prunes Phase 0 buildinfo cache + watcher logs |

Precedence: env > per-project config > defaults (qwen 6.4).

### 4.11 Observability

- New telemetry event `watch-diagnostics-event`: `{adapter_key, action, duration_ms, project_hash, batch_seq?, exit_code?, restart_count?, queue_depth?, status?, ...}` for `start | first_check_complete | recheck_start | recheck_complete | recheck_stalled | exit | restart | unhealthy | rss_cap`. All paths redacted via existing `project_hash` mechanism in [telemetry.py:340](../../tldr/telemetry.py#L340) (codex I8).
- Extend post-edit telemetry: `langserver_used: bool`, `langserver_status: str`, `langserver_age_ms: int|None`, `langserver_wait_ms: int`. Requires field additions to `HookExecutionResult` ([outcome.py:12](../../tldr/hooks/outcome.py#L12)), `record_hook_execution()` ([runner.py:86](../../tldr/hooks/runner.py#L86)), and telemetry schema. Tests must cover happy path, fallback path, stale, pending — and confirm `redact_paths` behavior on the new fields.
- **Schema versioning:** bump `schema_version` in the telemetry record from `2` to `3` for any record carrying the new fields. The eval scripts (`scripts/evaluate_tldr_usage.py`, `scripts/backfill_tldr_outcomes.py`) must read both `schema_version: 2` (treating `langserver_*` as missing/sync) and `schema_version: 3`. This lets pre/post-Phase-1 latency comparisons run on the same query: "missing fields means sync; not missing means watcher." (plan-reviewer Risk: HookExecutionResult schema migration.)
- `tldr daemon watchers status` plus a `--json` flag for scripting.

### 4.12 Failure modes

| Failure | Detection | Mitigation |
| --- | --- | --- |
| tsc crashes/OOMs | Process exit observed | Backoff restart; sync fallback while restarting |
| Parser produces wrong diagnostic count | N from parser ≠ M from sentinel | Mark adapter UNHEALTHY; sync fallback; surface `parser_count_mismatch` telemetry; do NOT silently return clean (codex I3) |
| Stalled batch | 5 s past "File change detected" with no closing sentinel | Emit `BATCH_STALLED`; queries return PENDING; restart only on 3rd consecutive stall in 5 min |
| tsconfig / lockfile change | mtime poll every 2 s | Restart adapter |
| Daemon SIGKILL externally | PID-file orphan check on next start | Kill orphan tsc processes; restart fresh |
| Mass mtime change (branch switch) | >100 invalidations within 1 s | All-PENDING with `branch_switch_budget_ms` |
| TS version skew (output format change) | Parser sentinel-miss + count mismatch | Mark UNHEALTHY; sync fallback; CI matrix N-2/N/N+1 should catch in advance |
| Fs watcher unreliable (NFS, FUSE, SMB, Dropbox) | statfs/statvfs check | Refuse to start adapter; sync fallback |
| CI environment | env var check | Refuse to start adapter by default |
| Battery-conscious env (laptop on battery) | Optional: `pmset`/`upower` shim; user-disabled by default | Document `TLDR_WATCH_DIAGNOSTICS=0` |
| Locked-down corporate env (no spawn of node_modules binaries) | Tool fails to spawn (errno EACCES/EPERM) | Adapter `UNHEALTHY-permanent`; sync fallback; one-line user-facing log |
| **Untrusted `tsc` binary from cloned repo** (security — plan-reviewer Risk: spawning node_modules binaries by default) | Default: `TLDR_WATCH_DIAGNOSTICS_TRUST_REPO_BINARIES` is unset/`0` → only spawn `tsc` resolved from a **trusted source**: `$PATH` outside the repo, or a path explicitly listed in `~/.tldr/trusted-bins.txt`. node_modules/.bin is **not** trusted by default. | When the trust check fails, the adapter refuses to start, logs `untrusted_tsc_binary`, and the hook falls back to sync. Users who want the watcher for their work repos set `TLDR_WATCH_DIAGNOSTICS_TRUST_REPO_BINARIES=1` or list specific repo roots in `~/.tldr/trusted-roots.txt`. README and Phase 2 docs explain the threat model. |
| Concurrent hooks racing on Phase 0 buildinfo | Two hooks check the same file simultaneously | Per-cache-key `flock` on `<cache>/lockfile`; second hook waits up to 1 s for first to finish, then reads cached result (codex I5) |
| Watcher returns "all clean" on a file the LLM is mid-edit (race between FS event and hook query) | `notify_edit` records pending edit; queries on that file with `version > last_batch_version` return STALE/PENDING, never FRESH-clean (codex C5, qwen 2.3) | — |

### 4.13 Canonical cache layout

Phase 0 and Phase 1 share one cache tree. Both phases write here; the LRU prune lives here; `tldr cache clean` operates on this tree. **This section is the single source of truth — Phase 0 and Phase 1 must both reference it rather than re-deriving paths.**

```
~/.tldr/cache/tsc/
  <projhash>/                          # sha256(project_path)[:8]
    <configkey>/                       # sha256(tsconfig_abs_path + tsc_version + tsc_path)[:12]
      tsconfig.json                    # synthesized stable config (extends project tsconfig)
      buildinfo                        # tsBuildInfoFile target — shared by Phase 0 & Phase 1
      meta.json                        # {tsc_version, tsconfig_mtime, last_use_ns, owner: "phase0"|"watcher"}
      lockfile                         # flock target for cross-process write coordination
```

**Key derivation:**

- `<configkey>` is identical whether reached by Phase 0 (one-shot cache) or Phase 1 (watcher persistent config) for the same `(tsconfig.json, tsc_version, tsc_path)` triple. They write the same `tsconfig.json`. They share the same `buildinfo`.
- Phase 0 stays in the same `<configkey>/` directory regardless of which file is being checked — the synthesized tsconfig overrides the root file set per call via the existing `--files [...]` indirection done in the spawn command, not via separate directories. (v2's `<file_hash>` granularity was wrong; corrected here.)
- Phase 1 watcher does **not** override the root file set; it watches the whole project. The same on-disk `buildinfo` is reused, so a project that warmed Phase 0 reads and then enables Phase 1 inherits the cached state.

**Prune coordination protocol (LRU prune ↔ live watcher):**

- LRU prune walks `<projhash>/<configkey>/meta.json` entries oldest-first by `last_use_ns`.
- For each candidate, the prune acquires `<configkey>/lockfile` non-blocking; if the lock is held (by a live watcher or another hook), the candidate is skipped.
- Additionally, prune skips any `<configkey>/` whose `meta.json` has `owner: "watcher"` AND `last_use_ns` is within `IDLE_TIMEOUT` (30 min) — a live watcher may not be in the middle of a write but its in-memory state is invalidated if we delete the buildinfo under it.
- Phase 1 supervisor updates `meta.json.owner = "watcher"` and `last_use_ns` on every batch completion. On adapter `stop()`, it sets `owner = "phase0"` so prune can reclaim the dir once it ages out.
- `tldr cache clean --force` ignores all skipping rules. Documented as "stops any running watchers first, then clears the cache."

---

## 5. Phased rollout

Each phase is independently shippable, behavior-preserving with default config, and gated by `pytest -q && ruff check && mypy` baseline + a new perf gate (see §6).

### Phase 0 — Per-config stable buildinfo cache (1-2 days)

- Modify `_write_single_file_tsconfig` to write under the **canonical cache layout defined in §4.13** (`<projhash>/<configkey>/`). The synthesized tsconfig sets `incremental: true`, `tsBuildInfoFile: <same-dir>/buildinfo`, and inherits the root file override at spawn time (via `tsc --project … <target_file>` argument shape) rather than via separate per-file directories.
- `<configkey>` derivation: sha256 of `(tsconfig_abs_path + tsc_version + tsc_path)[:12]`. This guarantees Phase 0 and Phase 1 hash to the same directory for the same project config.
- Cross-process locking: `flock` on `<configkey>/lockfile` (POSIX) / `msvcrt.locking` (Windows). Second caller waits up to 1 s. (codex I5)
- Replace `TemporaryDirectory` lifecycle with on-disk persistence + LRU prune (default cap 500 MB, configurable). Prune **must** observe the §4.13 coordination protocol — no exceptions even though Phase 1 isn't shipped yet (we lay this groundwork now so Phase 1 doesn't have to retroactively add it).
- **Ship `tldr cache clean` (and `tldr cache clean --force`) CLI in the same PR.** (qwen 3.3)
- Write a one-line `meta.json` per cache dir with `{tsc_version, tsconfig_mtime, last_use_ns, owner: "phase0"}` (Phase 1 will flip `owner` to `"watcher"`; Phase 0 sets it to `"phase0"`).
- Unit tests: cache hit reuse, invalidation on tsconfig mtime change, invalidation on TS version change, concurrent-write race (two hooks contend for the same lockfile), LRU prune respects locks and `meta.json.last_use_ns`, prune skips dirs with `owner: "watcher"` recency.
- Expected impact: repeated edits to same file 3.1 s → ~1.0 s on atlasos. First edit per new file: unchanged.
- **Behavior-preserving. Ships as a standalone PR with its own changelog entry.** (codex sequencing, qwen 5.7)

### Phase 0.5 — Daemon transport hardening (3-5 days)

- Length-prefix framing + versioned handshake (§4.7).
- Socket timeouts + `query_or_start_daemon` helper.
- Typed `DaemonResponse` with explicit `Unreachable`/`Timeout`/`FallbackRequired`.
- Backwards-compat for v1 protocol on existing commands.
- Tests: protocol downgrade, oversized responses, partial-recv reassembly, connect/response timeout boundaries, killed-server-mid-response.
- **Ships as a standalone PR.** Independently useful — fixes latent bugs in current `query_daemon`.

### Phase 1 — Watch subsystem + TS adapter, opt-in (2-3 weeks)

- Scaffold `tldr/daemon/watchers/`.
- `WatchAdapter` ABC + capability enum + `QueryResponse` schema (§4.4–4.5).
- Supervisor with background-thread start, batch-seq tracking, file-version freshness, batch-replace semantics (not merge).
- `typescript.py` adapter with locale forcing, multi-line parser, fail-closed count check, stalled-batch handling, process-group cleanup.
- Hook integration: `_diagnostic_message_for_file` rewritten; sync diagnostics refactored into typecheck/lint/format legs; lint+format always run regardless of watcher state. (codex C2)
- New telemetry events + `HookExecutionResult` extension. (codex I8)
- `tldr daemon watchers {status,start,stop}` CLI.
- CI: TS version matrix (currently latest stable LTS, latest stable, latest canary) for the parser tests.
- Integration tests: tmp 50-file TS project; happy-path edit→query→FRESH; stalled-batch; restart-on-tsconfig-change; orphan-cleanup; fail-closed count mismatch; locale-set-to-fr-doesnt-break.
- **Default-off** behind `TLDR_WATCH_DIAGNOSTICS=1`. (codex C7, qwen 5.6)
- Real-repo perf bench (not in CI): atlasos + llm-council, 100 simulated edits each, measure hook latency p50/p95 and fresh-settle p50/p95.

### Phase 1.5 — LSP feasibility spike (1 week, **after Phase 1, before Phase 2** — resolved from v2 open Q3)

- Prototype `typescript-language-server` (tsserver wrapper) adapter against the same `WatchAdapter` ABC.
- Compare against Phase 1 tsc-watch: cold-start latency, recheck latency, parser robustness, dependency footprint (`pygls`/`lsprotocol` are ~500 LOC each but they're new deps).
- Output: decision document; either commit to Phase 3 going LSP for pyright/gopls/rust-analyzer, or stay text-parser-per-tool.
- **Does block Phase 2** (sequencing change in v3): if the spike reveals the `WatchAdapter` ABC needs to change to accommodate LSP, we want to know before locking in Phase 2's hardening work.

### Phase 2 — Hardening & default flip (2 weeks)

- Field telemetry analysis from Phase 1 opt-in adopters.
- Battery / CPU profiling on laptops.
- NFS/FUSE/SMB/Dropbox detection codepaths.
- Branch-switch detection.
- Tighten budgets based on observed p95 vs target.
- Documentation:
  - README paragraph on the daemon subsystem
  - `docs/watch-diagnostics.md` ops guide
  - Troubleshooting flowchart for "my edits feel slow / fast"
  - Explicit kill-switch documentation
- **Default flip to `TLDR_WATCH_DIAGNOSTICS=1`** if and only if: p95 hook latency on atlasos & llm-council < 500 ms over a 24 h window AND zero net-new `RuntimeError` attributable to the subsystem AND no open issues tagged `watch-diagnostics-stuck`.

### Phase 3 — Pyright adapter (1-2 weeks)

- Decision from Phase 1.5 dictates implementation route:
  - If LSP: `pyright-langserver` over LSP, reusing the LSP adapter base from Phase 1.5.
  - If text: `pyright --watch` parser; explicit verification that watch-mode output is parseable (codex I2 — current `_parse_pyright_output` expects JSON which `--watch` does not emit).
- Reuse supervisor + tests.

### Phase 4 — Additional adapters as demand surfaces

- gopls, rust-analyzer — driven by user telemetry / issue volume, not speculative.
- These almost certainly require LSP; outcome of Phase 1.5 spike informs investment.

### Phase 5 — Windows full support

- Verify existing Windows daemon shims (TCP sockets, msvcrt locking) work with the watcher subsystem.
- Spawn semantics: `CREATE_NEW_PROCESS_GROUP` for process tree control; verify `taskkill /T` behavior.
- The existing daemon code already has Windows paths (qwen nit, codex I9); Phase 1 should explicitly fail-clean on Windows with telemetry while preserving the sync fallback. Windows users see no regression vs. today.

---

## 6. Testing strategy

- **Unit:** parser (multi-line, locale-forced, sentinel-regex), supervisor state machine, adapter lifecycle, freshness comparison. Mock subprocess; deterministic output sequences.
- **Integration:** real `tsc --watch` against `tests/fixtures/ts-watcher/` (50 files, 3 tsconfigs to exercise monorepo keying). Drive edits via `Path.write_text`; assert query results, batch sequencing, restart-on-config-change.
- **End-to-end:** `scripts/smoke_current_cli_hooks.py` extended to spin up a daemon with watchers and fire 50 simulated edits across mixed-language projects.
- **Performance regression gate (new CI job `watcher-bench`):** runs the integration fixture 10×, reports hook-response p50/p95 and fresh-settle p50/p95, fails if either exceeds targets (§3 goals 1–2). **The job sets `TLDR_WATCH_DIAGNOSTICS=1` explicitly to override the CI auto-disable** described in §4.9 — otherwise the gate would silently measure the sync path. A separate test (`tests/test_env_precedence.py`) asserts the explicit env var wins over CI detection; this test must pass for the perf gate to be meaningful. The bench job is skipped on platforms where `node`+`tsc` aren't installable in CI (currently Windows runners); a follow-up Phase 5 enables it there.
- **Chaos:** integration test SIGKILLs the tsc subprocess mid-query; asserts supervisor recovers, hook falls back, orphan is cleaned.
- **Parser-fuzz:** feed parser samples of TS 5.0–latest output (captured separately, version-pinned) + locale-forced French samples + samples with embedded newlines + ANSI-leaked samples. Parser must either parse correctly or mark UNHEALTHY — never return zero when truth is non-zero.
- **Fixture regeneration is committed and reproducible.** `tests/fixtures/tsc-output/regenerate.sh` runs `pnpm dlx typescript@<version> tsc --noEmit --watch < fixture-input | tee tests/fixtures/tsc-output/<version>.txt` for each pinned TS version (currently 5.0, 5.4, latest, latest-canary). The script is committed alongside the fixtures; updating to a new TS version is `bash regenerate.sh 5.7`. Without this script the parser-fuzz suite drifts silently on every TS release. (plan-reviewer Risk: fixture stability.)
- **Telemetry assertions:** confirm `langserver_used`, `langserver_status`, `langserver_age_ms`, `langserver_wait_ms` populate correctly across all status paths.
- **CI environment test:** with `CI=true` set, hook must use sync path even when `TLDR_WATCH_DIAGNOSTICS=1`.
- **Real-repo bench (manual, not CI):** atlasos + llm-council, 100 simulated edits each, before/after numbers reported in the Phase 1 PR description.

---

## 7. Migration story

For existing single-machine devs:

- Phase 0 ships first, free speedup, no opt-in.
- Phase 0.5 ships transport hardening — no user-visible change.
- Phase 1 ships behind `TLDR_WATCH_DIAGNOSTICS=1`. Power users who want the speedup turn it on. Default behavior unchanged.
- Phase 2 flips the default if and only if field data clears the bar (§5 Phase 2 criteria).
- A one-time first-edit on a new TS project pays the cold-start (~8 s warmup budget). Subsequent edits are fast.
- `~/.tldr/cache/tsc/` LRU-bounded at 500 MB; `tldr cache clean` lets users wipe it.
- Watcher logs at `~/.tldr/logs/watchers/`; rotated and bounded.

For the OSS user population:

- Phase 1 release notes explain the opt-in env var and what the watcher process is, where its logs live, how to disable.
- Phase 2 release notes explain the default flip, the kill switch, the CI auto-disable, and the resource caps.

---

## 8. Open questions resolved in v2 (vs. v1)

| v1 question | v2 decision |
| --- | --- |
| Phase 0 separate PR vs. combined with Phase 1? | **Separate PR**, with `tldr cache clean` shipped in the same PR. (codex sequencing, qwen 5.7) |
| TS version compatibility matrix vs. runtime probing? | **Both**: CI matrix N-2/N/N+1-canary; runtime sentinel-regex tolerance; parser fail-closed on count mismatch. (qwen 3.2) |
| Share `tsbuildinfo` between Phase 0 cache and watch mode? | **Yes, share** under `~/.tldr/cache/tsc/<projhash>/<configkey>/`. (qwen 5.7) |
| Memory cap mechanism? | `os.stat`/`ps -o rss=` shim; no new required dep; psutil optional. Per-adapter cap only; no global daemon cap. (codex I11, qwen 2.6) |
| `query_budget_ms` default? | `400 ms`. Tune from telemetry. (consistent across §4.6 and §4.10 — fixes v1's 300/400 discrepancy that qwen flagged) |
| Telemetry redaction for new fields? | Reuse existing `project_hash` mechanism in telemetry.py. Adapter logs stay local (file system only). Diagnostic file paths returned to the LLM are already not redacted (existing behavior); we surface this in the README. (qwen 4.6) |
| Pyright watch parser reuse? | **Unverified, deferred to Phase 3 with explicit verification.** Phase 1 adapter contract does not assume pyright-shaped output. (codex I2, qwen 2.4) |
| LSP vs text-parser? | Phase 1 ships text-parser; Phase 1.5 spike evaluates LSP for Phase 3+. Considered, not dismissed. (codex 4-step seq, qwen 3.1, 5.9) |

---

## 9. Remaining open questions (for plan-reviewer subagent and Trey)

1. **`MAX_CONCURRENT_DAEMONS` deferred.** Is it acceptable that a developer with 12 active project shells could end up with 12 daemons × N watchers each? Per-project memory caps bound each, but aggregate may surprise users. Mitigation: documentation + the manual `tldr daemon watchers stop` knob. Reopen if telemetry shows it bites.
2. **Branch-switch detection threshold.** "100 invalidations within 1 s" is a guess. Should this be tunable? Should we hook git events explicitly via `.git/HEAD` mtime?
3. ~~**Phase 1.5 (LSP spike) timing.**~~ **Resolved in v3:** Phase 1.5 runs **after Phase 1 ships and before Phase 2 begins**, not in parallel. Rationale: if the spike changes the `WatchAdapter` ABC, Phase 2's hardening work would be partially invalidated. Sequencing is now strictly Phase 0 → 0.5 → 1 → 1.5 → 2 → 3 → 4 → 5. Phase 2's calendar moves out 1 week to accommodate.
4. **`oxlint` / `oxfmt` — should those eventually move under the watcher subsystem too?** Currently they run sync and cost <50 ms each. Not on the critical path, but if we ever add a JS linter that's slower (eslint), we may want to amortize it the same way.
5. ~~**CI environment override.**~~ **Resolved in v3:** explicit `TLDR_WATCH_DIAGNOSTICS=1` **does** override CI auto-disable (needed by `watcher-bench`). The precedence (explicit env var > CI detection > defaults) is asserted by `tests/test_env_precedence.py` so it can't silently break.
6. **Battery detection on macOS.** Should we shim `pmset -g batt` and auto-disable, or leave that to the user? Adds a per-startup ms; probably not worth it.
7. **Daemon-internal sync fallback footgun (plan-reviewer Risk).** The legacy `_handle_diagnostics` command at [core.py:886](../../tldr/daemon/core.py#L886) still runs `get_diagnostics()` synchronously inside the daemon's single-threaded handler. The new code path takes its sync fallback **client-side** in the hook process, not by re-routing through the daemon — verify in code review that no Phase 1 helper accidentally calls back into `_handle_diagnostics` for the sync leg. If it does, a single slow tsc invocation will stall the daemon for up to 30 s. Worth a dedicated test: `tests/test_daemon_never_blocks_on_sync_fallback.py`.

---

## 10. Rollback plan

- Phase 0 reversible via single-commit revert; cache directory persists harmlessly until `tldr cache clean` is run.
- Phase 0.5 reversible by reverting transport changes; legacy unframed protocol still works because of the versioned handshake.
- Phase 1 reversible at runtime via `TLDR_WATCH_DIAGNOSTICS=0` (no code revert needed). Code revert: drop `tldr/daemon/watchers/`, revert `post_edit.py` and `outcome.py`/`runner.py`/`telemetry.py` field additions.
- Telemetry records sync-vs-watch path on every post-edit, so any latency regression after Phase 2 flip is attributable.

---

## 11. Review history

- **v1** (initial draft, 2026-05-24): Architecture sketch covering daemon + tsc-watch + adapter contract, with Phase 1 default-on after dogfood.
- **delegate droid qwen safe** review (2026-05-24): verdict **revise**. Surfaced project-wide-watch-vs-single-file-scope tension (§2.1), file-granular "checking set" impossibility under tsc batch semantics (§2.2), notify_daemon ↔ tsc FS-watcher race (§2.3), pyright watch parser unverified (§2.4), default-on too aggressive for OSS (§2.5), MAX_CONCURRENT_DAEMONS coordination gap (§2.6), LSP not seriously evaluated (§3.1), parser fragility (§3.2), missing `tldr cache clean` in Phase 0 (§3.3), resource cap underspec (§3.4), notify_daemon ambiguity (§3.5), signal handling for orphans (§3.6), 30-min IDLE_TIMEOUT may be short (§3.7), Phase 4 "one file per adapter" wishful (§3.8). 12 concrete edit suggestions. 7 open questions.
- **delegate codex safe** review (2026-05-24): verdict **revise. Do not default-on Phase 1 as written.** 7 critical issues (C1 monorepo adapter keying, C2 lost lint/format diagnostics, C3 post-edit doesn't start daemon today, C4 transport unsafe for larger payloads, C5 freshness undefined, C6 single-threaded daemon blocks on warmup, C7 default-on premature). 12 important issues (LSP-unfriendly adapter contract, pyright unverified, parser fail-closed missing, batch-merge vs replace, Phase 0 lock missing, can_start retry policy, no shutdown cleanup, telemetry schema impact understated, Windows already partially supported, latency targets conflate metrics, global cap unimplementable, config format mismatch with daemon JSON convention). 3 nits, 12 concrete edits, 10 open questions.
- **v2** folds: explicit opt-in rollout (§3 goal 6, §5 Phase 1); transport hardening as Phase 0.5 (§4.7, §5); split diagnostics into typecheck/lint/format legs (§4.8); adapter keyed per `(language, tool_path, config_path, mode)` (§4.4); response schema with FRESH/STALE/PENDING/FALLBACK/UNHEALTHY (§4.5); persistent per-project tsconfig + single-file output filter preserving existing scoping intent (§4.6); batch-replace not merge (§4.6); locale forcing + multi-line parser + fail-closed count check + stalled-batch handling (§4.6); supervisor startup in background thread (§4.4); process-group cleanup + orphan detection (§4.6, §4.9); `.tldr/config.json` not `.toml` (§4.10); per-cache-key locking in Phase 0 (§5 Phase 0); CI/NFS/branch-switch detection (§4.9); `tldr cache clean` shipped in Phase 0 PR (§5); `tldr daemon watchers {status,start,stop}` CLI (§4.9); LSP feasibility spike before Phase 3 (§5 Phase 1.5); two perf targets (§3 goals 1–2); telemetry schema extension scope spelled out (§4.11); global daemon cap dropped (§4.9, §9); shared `tsBuildInfoFile` location between Phase 0 cache and Phase 1 watcher (§4.6); `--preserveWatchOutput` removed from tsc invocation (§4.6); README + ops guide + troubleshooting flowchart added to Phase 2 (§5).
- **plan-reviewer (opus)** audit of v2 (2026-05-24): 4 blockers (Phase 0/Phase 1 cache key shape mismatch; LRU prune ↔ live watcher coordination missing; CI auto-disable contradicts perf gate; DoD attribution unfalsifiable). 8 risks (legacy daemon `_handle_diagnostics` sync foot-gun; launchd/systemd orphan handling; node_modules/.bin/tsc security threat; HookExecutionResult schema migration; fixture regeneration unspec'd; RSS-cap thrash loop; hook integration code sample wrong shape; perf gate runnability on non-node CI). 6 nits (unbounded "any project size"; dict naming drift; JSON section syntax leftover; Phase 1.5 timing left as open question; v1 not linked; prose tells).
- **v3 (this document)** folds plan-reviewer findings: canonical cache layout extracted to new §4.13 (single source of truth used by both Phase 0 and Phase 1); LRU prune coordination protocol with `meta.json owner` flag (§4.13); perf gate explicit `TLDR_WATCH_DIAGNOSTICS=1` override + `tests/test_env_precedence.py` (§6); DoD now has runnable jq queries for every claim (§12); orphan-PID sweep on every `tldr` invocation, not just daemon start (§4.9); untrusted-tsc-binary threat model + `TLDR_WATCH_DIAGNOSTICS_TRUST_REPO_BINARIES` opt-in + `~/.tldr/trusted-bins.txt` allowlist (§4.12); telemetry `schema_version 2 → 3` migration (§4.11); fixture regeneration script committed at `tests/fixtures/tsc-output/regenerate.sh` (§6); 3-strike RSS-cap rule mirrors stalled-batch rule (§4.6); hook integration code sample now spec's all four helper signatures (§4.8); §3 goal 1 bounded to "up to 5K files"; §4.6 dict naming corrected to `dict[Path, list[Diagnostic]]`; §4.10 JSON syntax `"watchers": {…}`; §9 Q3 resolved (Phase 1.5 runs between Phase 1 and Phase 2, not parallel); §9 Q5 resolved (explicit env var wins); §9 Q7 added (daemon-internal sync fallback footgun, with mitigation test); §3 goal 7 prose tightened.

---

## 12. Definition of done

All criteria below must be falsifiable via a query against `~/.tldr/telemetry.jsonl` or against committed artifacts. No squishy success metrics.

- **Hook-response latency** on atlasos & llm-council: p50 < 200 ms, p95 < 500 ms in production telemetry over a 24 h window with `TLDR_WATCH_DIAGNOSTICS=1`. Query: `jq 'select(.event=="post-edit" and .project_hash | IN("a38cea4e","91abfcd6") and .langserver_used==true) | .duration_ms' ~/.tldr/telemetry.jsonl | datamash perc:50 perc:95`.
- **Fresh-settle latency** measured at the supervisor: p50 < 600 ms, p95 < 2 s for incremental rechecks. Recorded via `watch-diagnostics-event` records of `action=recheck_complete`. Query: `jq 'select(.event=="watch-diagnostics-event" and .action=="recheck_complete") | .duration_ms' ~/.tldr/telemetry.jsonl | datamash perc:50 perc:95`.
- **Zero net-new `RuntimeError` attributable to the watcher subsystem.** Attribution rule: a record qualifies as watcher-attributable iff `langserver_used == true AND error_kind == "RuntimeError"`. Query: `jq 'select(.langserver_used==true and .error_kind=="RuntimeError") | .timestamp' ~/.tldr/telemetry.jsonl | wc -l` must return `0` over the 24 h DoD window. (Pre-existing `RuntimeError`s from sync-path post-edit do not count.)
- `tldr daemon watchers status` exists and is documented in `docs/watch-diagnostics.md`.
- `tldr cache clean` (and `--force`) exists and is documented.
- README updated with a paragraph on the daemon subsystem and `TLDR_WATCH_DIAGNOSTICS=0` kill switch.
- All Phase 1 tests pass on CI; the `watcher-bench` perf gate passes; TS version matrix tests pass; `tests/test_env_precedence.py` confirms the bench's explicit-override semantics.
- One-paragraph migration note in `CHANGELOG.md` per shipped phase.
- Phase 2 default flip only after the above criteria all green for one minor release of opt-in field data **and** zero open issues labeled `watch-diagnostics-stuck` filed during that release.
