# TLDR Outcome Telemetry and Backfill Implementation Plan

**Goal:** Build a privacy-preserving, data-rich measurement system that can tell whether TLDR helps, is neutral, or hurts real agent work, using both future hook telemetry and retroactive Codex/Claude session backfills.

**Architecture:** Keep hook-time telemetry fast and best-effort, but enrich it with candidate/surfacing metadata that cannot be reconstructed later. Put heavier outcome attribution in offline analysis scripts that join TLDR telemetry with Codex/Claude session JSONL and emit sanitized per-session rollups plus daily Markdown/HTML reports. Use one shared rollup schema for future telemetry-derived and retroactive backfilled records, with separate confidence labels for matching, attribution, and causality.

**Tech Stack:** Python 3.14-compatible stdlib-first implementation, existing TLDR hook runtime, JSONL telemetry at `~/.tldr/telemetry.jsonl`, pytest, existing `scripts/evaluate_tldr_usage.py` parser patterns, local Codex/Claude JSONL session stores.

---

## Non-negotiable product requirements

1. **Privacy-safe by default.** Plain `TLDR_TELEMETRY=1` must not write absolute paths, repo names, raw commands, command outputs, prompt/user text, or source snippets. Project and file identifiers must be stable hashes by default. If a future human-readable local debug mode is wanted, it must be a separate explicit opt-in, not the default.
2. **No raw text in reports.** Outcome JSON/Markdown/HTML may include categories, hashes, counts, durations, and reason codes; it must not include raw prompts, raw user messages, raw commands, raw command output, or source text.
3. **Do not make hooks brittle.** Telemetry writes remain best-effort; hook stdout/stderr and decisions must not change if telemetry fails.
4. **Capture candidate lifecycle going forward.** For every hook that can produce context, record what files TLDR considered, which files it surfaced, and why.
5. **Support retroactive backfill.** Use existing Codex/Claude transcripts plus existing telemetry to reconstruct comparable sanitized session rollups for May 20 and any requested window.
6. **Do not overclaim causality.** Candidate/surfaced-file use is attribution evidence, not counterfactual proof. Causal confidence remains `proxy-only` unless supported by A/B alternation, manual annotations, or comparable baseline evidence.
7. **Make reports actionable.** Every metric should map to a tuning decision: reduce noise, improve recommendations, lower latency, adjust budgets, disable harmful hooks, or keep dogfooding.
8. **Generated reports are local artifacts.** Source changes may be committed later if requested, but generated `reports/*.json`, `reports/*.md`, and `reports/*.html` are not staged/committed by default.

## Current live surfaces verified before planning

- Hook telemetry is centralized in `tldr/hooks/runner.py`, which calls `record_hook_execution(...)` after every dispatched hook.
- Existing telemetry writer is `tldr/telemetry.py`; it supports opt-in enablement, redacted stable path hashes, locked append, file mode `0600`, and rotation.
- Existing hook result metadata lives in `tldr/hooks/outcome.py` as `HookExecutionResult`, including `trigger_files`, `recommended_files`, and `surfaced_files` fields.
- `recommended_related_files` currently exists in the telemetry JSON schema but is not populated by current `pre-read`/`pre-edit` hooks.
- Existing evaluator is `scripts/evaluate_tldr_usage.py`; it parses Codex/Claude sessions and telemetry but currently performs session-window matching rather than strict per-record timestamp filtering.
- Existing tests covering telemetry/evaluator behavior are `tests/test_telemetry.py` and `tests/test_evaluate_tldr_usage.py`.

## Target evidence chain

For each session, the new system should answer:

1. **Opportunity:** What hooks fired and what hashed files/actions triggered them?
2. **Decision:** What candidate context did TLDR consider, rank, surface, skip, or exclude?
3. **Agent action:** What hashed files did the agent later read/edit and what categories of tools did it run?
4. **Outcome:** Did the agent reach edits/checks faster, with fewer repeated reads, fewer failed commands, fewer user corrections, and acceptable hook/token overhead?
5. **Verdict:** Helpful, neutral, harmful, proxy-only, or insufficient data, with separate match/attribution/causal confidence and reason codes.

## Canonical schema tables

### HookTelemetryV2 JSONL record

The live JSON key remains `recommended_related_files` for backward compatibility, even though hook internals may call the field `recommended_files`.

| Key | Required | Privacy class | Notes |
| --- | --- | --- | --- |
| `schema_version` | yes | public | Integer `2`; old records without the key are treated as v1. |
| `timestamp` | yes | public | ISO timestamp. |
| `version` | yes | public | TLDR package version. |
| `client` | yes | low-cardinality | `codex`, `claude`, `droid`, `opencode`, etc. |
| `event` | yes | low-cardinality | Hook event name. |
| `project` | yes | hash | Always `<redacted>/<project_hash>` by default. |
| `project_hash` | yes | hash | Existing 8-char stable project hash. |
| `duration_ms` | yes | metric | Hook runtime. |
| `status` | yes | low-cardinality | `ok`, `skipped`, `noop`, `error`. |
| `error_kind` | nullable | low-cardinality | Exception class or sanitized reason only. |
| `injected_bytes` | yes | metric | Context size, not content. |
| `trigger_files` | yes | path-hash list | Stable hashed paths. |
| `recommended_related_files` | yes | path-hash list | Stable hashed paths. |
| `surfaced_files` | yes | path-hash list | Stable hashed paths. |
| `candidate_files` | yes | sanitized metadata | List of `{path, reason, rank, score?, surfaced, excluded_reason?}`; `path` is hashed. |
| `diagnostics_count` | yes | metric | Count only. |
| `daemon_state` | nullable | low-cardinality | e.g. `start_requested`, `ready`. |
| `noop_reason` | nullable | low-cardinality | e.g. `bypass`, `no_diagnostics`, `missing_file`. |
| `session_id` | nullable | session ID | Existing local session ID; not path/source content. |
| `hook_run_id` | nullable | opaque ID | Unique-ish hook run ID. |
| `context_kind` | nullable | low-cardinality | e.g. `read_nav_map`, `edit_structure`, `post_edit_diagnostics`. |

### SessionRollupV1 JSON object

| Key | Required | Privacy class | Notes |
| --- | --- | --- | --- |
| `session_id` | yes | session ID | Local session identifier. |
| `client` | yes | low-cardinality | Agent client. |
| `project_hash` | yes | hash | No repo path/name by default. |
| `window_start`, `window_end` | yes | public | Analysis window. |
| `match_confidence` | yes | enum | `none`, `low`, `medium`, `high`: how confidently logs/telemetry were joined. |
| `attribution_confidence` | yes | enum | `none`, `low`, `medium`, `high`: how confidently TLDR-surfaced context can be linked to later action. |
| `causal_confidence` | yes | enum | Allowed values: `proxy-only`, `manual-annotation`, `ab-test`, `matched-baseline`. Historical backfills default to `proxy-only`; upgraded labels require explicit evidence source. |
| `verdict` | yes | enum | `helpful`, `neutral`, `harmful`, `proxy-only`, `insufficient-data`. |
| `verdict_reasons` | yes | enum list | Sanitized reason codes. |
| `tool_counts_by_category` | yes | counts | No raw commands. |
| `command_hash_counts` | yes | hash/count map | Hash of normalized command, not command text. |
| `files_read_count`, `files_edited_count` | yes | counts | Unique hashed file counts. |
| `explore_before_first_edit` | yes | count | Behavior metric. |
| `repeated_file_reads` | yes | count | Behavior metric. |
| `verification_runs`, `verification_failures`, `verification_reruns` | yes | counts | Outcome proxy. |
| `failed_tool_outputs` | yes | count | Count only; no output. |
| `failure_kind_counts` | yes | counts | Sanitized classes like `error`, `failed`, `timeout`. |
| `user_corrections` | yes | count | Count only. |
| `user_correction_kind_counts` | yes | counts | Sanitized classes like `missed_requirement`, `wrong_direction`, `stop_request`. |
| `tldr_hooks`, `tldr_errors`, `tldr_skips`, `tldr_noops` | yes | counts | Hook behavior. |
| `injected_bytes_total`, `injected_bytes_p50`, `injected_bytes_p95` | yes | metrics | Cost/overhead. |
| `trigger_files_used`, `surfaced_files_used`, `recommended_files_used` | yes | counts | Hashed match counts only. |
| `candidate_files_total`, `candidate_files_surfaced`, `candidate_files_later_used` | yes | counts | Candidate lifecycle. |

---

### Task 1: Outcome schema and privacy-safe telemetry writer extensions

**Parallel:** no
**Blocked by:** none
**Owned files:** `tldr/telemetry.py`, `tests/test_telemetry.py`
**Invariants:** Existing telemetry records remain parseable; existing JSON keys keep their names; telemetry remains opt-in and best-effort; default telemetry does not write absolute paths/repo names; redacted path format stays `<redacted>/<project_hash>/<path_hash>`.
**Out of scope:** Changing hook behavior, adding live report generation, or parsing session logs.

**Files:**
- Modify: `tldr/telemetry.py`
- Modify: `tests/test_telemetry.py`

**Step 1: Add failing test for privacy-safe default telemetry**
Append a test to `tests/test_telemetry.py` proving plain `TLDR_TELEMETRY=1` does not write absolute paths or repo names:

```python
def test_telemetry_redacts_paths_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("TLDR_TELEMETRY", "1")
    monkeypatch.delenv("TLDR_TELEMETRY_REDACT_PATHS", raising=False)
    project = tmp_path / "secret-repo-name"
    project.mkdir()
    telemetry_path = tmp_path / "telemetry.jsonl"
    monkeypatch.setenv("TLDR_TELEMETRY_PATH", str(telemetry_path))

    record_hook_execution(
        client="codex",
        hook_event="pre-read",
        project=project,
        duration_ms=1,
        status="ok",
        trigger_files=["src/app.py"],
        recommended_files=["src/auth.py"],
        surfaced_files=["src/auth.py"],
    )

    raw = telemetry_path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert str(tmp_path) not in raw
    assert "secret-repo-name" not in raw
    assert "src/app.py" not in raw
    assert payload["project"] == f"<redacted>/{payload['project_hash']}"
    assert payload["trigger_files"][0].startswith(f"<redacted>/{payload['project_hash']}/")
```

**Step 2: Add failing test for rich hook metadata**
Append a test that proves structured candidate metadata is redacted and content-free:

```python
def test_hook_telemetry_records_candidate_lifecycle_without_content(monkeypatch, tmp_path):
    monkeypatch.setenv("TLDR_TELEMETRY", "1")
    telemetry_path = tmp_path / "telemetry.jsonl"
    monkeypatch.setenv("TLDR_TELEMETRY_PATH", str(telemetry_path))

    record_hook_execution(
        client="codex",
        hook_event="pre-edit",
        project=tmp_path,
        duration_ms=5,
        status="ok",
        trigger_files=["src/app.py"],
        recommended_files=["src/auth.py"],
        surfaced_files=["src/auth.py"],
        candidate_files=[
            {"path": "src/auth.py", "reason": "importer", "rank": 1, "score": 0.91, "surfaced": True},
            {"path": "tests/test_auth.py", "reason": "test_neighbor", "rank": 2, "score": 0.74, "surfaced": False, "excluded_reason": "budget"},
        ],
        context_kind="edit_structure",
        hook_run_id="run-1",
    )

    raw = telemetry_path.read_text(encoding="utf-8")
    payload = json.loads(raw.splitlines()[-1])
    assert payload["schema_version"] == 2
    assert payload["hook_run_id"] == "run-1"
    assert payload["context_kind"] == "edit_structure"
    assert payload["candidate_files"][0]["path"].startswith(f"<redacted>/{payload['project_hash']}/")
    assert payload["candidate_files"][0]["surfaced"] is True
    assert "def " not in raw
    assert "content" not in raw.lower()
```

**Step 3: Run tests to verify failure**
Run:

```bash
python3 -m pytest -q \
  tests/test_telemetry.py::test_telemetry_redacts_paths_by_default \
  tests/test_telemetry.py::test_hook_telemetry_records_candidate_lifecycle_without_content
```

Expected: FAIL because default redaction and candidate fields are not implemented yet.

**Step 4: Implement privacy-safe default and schema v2 fields**
In `tldr/telemetry.py`:

- Make `redact_paths_enabled()` default to `True` unless an explicit future local debug flag opts out. Preserve `TLDR_TELEMETRY_REDACT_PATHS=0` only if the project intentionally wants human-readable local debugging; tests should assert default is redacted.
- Add `_prepare_candidate_files(...)` that redacts candidate paths and only preserves reason/rank/score/surfaced/excluded_reason.
- Add optional args: `candidate_files`, `context_kind`, `hook_run_id`.
- Add `schema_version: 2` to new records.
- Keep JSON key `recommended_related_files`.

**Step 5: Run telemetry tests**
Run:

```bash
python3 -m pytest -q tests/test_telemetry.py
```

Expected: PASS.

**Verification plan:**
- Primary: `python3 -m pytest -q tests/test_telemetry.py`
- Privacy smoke:

```bash
TLDR_TELEMETRY=1 TLDR_TELEMETRY_PATH=/tmp/tldr-privacy.jsonl \
python3 -m tldr.cli hooks run pre-read --client codex <<'JSON'
{"tool_name":"Read","tool_input":{"file_path":"tldr/hooks/read.py"},"cwd":"/Users/treygoff/Code/llm-tldr","session_id":"privacy-smoke"}
JSON
! grep -E '/Users/|llm-tldr|tldr/hooks/read.py' /tmp/tldr-privacy.jsonl
```

Expected: hook exits 0 and grep finds no forbidden raw path/repo strings.

---

### Task 2: Candidate recommendation lifecycle in hook results

**Parallel:** no
**Blocked by:** Task 1
**Owned files:** `tldr/hooks/outcome.py`, `tldr/hooks/path_policy.py`, `tldr/hooks/read.py`, `tldr/hooks/edit.py`, `tldr/hooks/post_edit.py`, `tldr/hooks/runner.py`, `tests/test_hooks_read.py`, `tests/test_hooks_edit.py`, `tests/test_hooks_post_edit.py`, `tests/test_hooks_runtime.py`
**Invariants:** Hooks must still produce the same user-visible response shape; candidate generation must be bounded and must not require daemon availability; secret/test/vendor bypass rules are centralized and respected.
**Out of scope:** Semantic model downloads, remote APIs, changing hook installer surfaces.

**Files:**
- Create: `tldr/hooks/path_policy.py`
- Modify: `tldr/hooks/outcome.py`
- Modify: `tldr/hooks/read.py`
- Modify: `tldr/hooks/edit.py`
- Modify: `tldr/hooks/post_edit.py`
- Modify: `tldr/hooks/runner.py`
- Modify: `tests/test_hooks_read.py`
- Modify: `tests/test_hooks_edit.py`
- Modify: `tests/test_hooks_post_edit.py`
- Modify: `tests/test_hooks_runtime.py`

**Step 1: Add failing tests for candidate metadata and shared path policy**
Add tests that assert:

- `pre-read` records local import candidates.
- `pre-edit` records local import/same-directory candidates.
- Candidate generation excludes `node_modules`, `.git`, `.env`, secrets, missing files, and test files by default.
- `HookExecutionResult` carries `candidate_files`, `recommended_files`, `surfaced_files`, and `context_kind`.
- `candidate_files[*].surfaced=True` and `surfaced_files` mean the hook response actually mentioned or injected that file. Internally ranked but not injected candidates stay in `candidate_files` with `surfaced=False`; they may also appear in `recommended_files` for lifecycle accounting, but not in `surfaced_files`.

Example test shape:

```python
def test_pre_read_records_related_candidates(monkeypatch, tmp_path):
    source = tmp_path / "src" / "app.py"
    related = tmp_path / "src" / "auth.py"
    source.parent.mkdir()
    source.write_text("from .auth import login\n" + "x = 1\n" * 400, encoding="utf-8")
    related.write_text("def login():\n    return True\n", encoding="utf-8")

    def fake_extract(path: str, base_path: str):
        return {
            "imports": [{"module": ".auth", "names": ["login"], "is_from": True}],
            "functions": [{"name": "handler", "signature": "def handler()", "line_number": 1}],
            "classes": [],
        }

    monkeypatch.setattr("tldr.hooks.read.extract_file", fake_extract)
    event = parse_hook_event({"tool_name": "Read", "tool_input": {"file_path": str(source)}, "cwd": str(tmp_path)}, client="codex")

    result = build_read_response(event)

    assert result.status == "ok"
    assert "src/app.py" in result.trigger_files
    assert "src/auth.py" in result.recommended_files
    assert any(candidate["path"] == "src/auth.py" and candidate["reason"] == "import" for candidate in result.candidate_files)
```

**Step 2: Create shared path policy and update live importers**
Create `tldr/hooks/path_policy.py` with shared helpers:

- `CODE_EXTENSIONS`
- `should_exclude_context_path(project: Path, path: Path, *, include_tests: bool = False) -> bool`
- `event_relative_path(event, path)` may stay in `outcome.py`, but exclusion rules should be shared.

Update `read.py`, `edit.py`, and `post_edit.py` to import from this shared policy rather than reaching through `read.py` for path constants/helpers. Add a post-edit regression test proving diagnostics still run for code files and skip excluded paths after the move.

Rules must exclude:

- non-code extensions
- `.git`, `.tldr`, virtualenvs, `node_modules`, `dist`, `build`, `coverage`, `__pycache__`
- secret-looking paths
- tests unless `include_tests=True`
- missing files when a candidate needs real content

**Step 3: Extend `HookExecutionResult`**
Add metadata fields to `tldr/hooks/outcome.py`:

```python
candidate_files: list[dict[str, object]] = field(default_factory=list)
context_kind: str | None = None
hook_run_id: str | None = None
```

Update `ok(...)`, `skipped(...)`, `noop(...)`, and `error(...)` to accept and forward these fields.

**Step 4: Implement bounded related-file discovery**
Inside `tldr/hooks/read.py` and `tldr/hooks/edit.py`, add bounded local helpers:

- Resolve direct relative imports for Python/TS-style local modules.
- Prefer files in the same directory with matching stem.
- Include likely test neighbor as a candidate with `surfaced=False` and `excluded_reason="test_default_excluded"`.
- Cap candidates at 8 considered / 3 surfaced.
- Never include secret/vendor/bypassed paths.

Candidate dict shape:

```python
{
    "path": "src/auth.py",
    "reason": "import",
    "rank": 1,
    "score": 1.0,
    "surfaced": True,
}
```

**Step 5: Wire runner telemetry**
Pass the new fields from `HookExecutionResult` into `record_hook_execution(...)` in `tldr/hooks/runner.py`. Generate `hook_run_id` in the runner if the result did not set one.

**Step 6: Run hook tests**
Run:

```bash
timeout 30 python3 -m pytest -q tests/test_hooks_read.py tests/test_hooks_edit.py tests/test_hooks_post_edit.py tests/test_hooks_runtime.py tests/test_telemetry.py
```

Expected: PASS.

**Verification plan:**
- Primary: tests above.
- Bounded manual smoke:

```bash
timeout 20 bash -lc 'TLDR_TELEMETRY=1 TLDR_TELEMETRY_PATH=/tmp/tldr-candidates.jsonl python3 -m tldr.cli hooks run pre-read --client codex <<"JSON"
{"tool_name":"Read","tool_input":{"file_path":"tldr/hooks/read.py"},"cwd":"/Users/treygoff/Code/llm-tldr","session_id":"candidate-smoke"}
JSON
python3 - <<"PY"
import json
for line in open("/tmp/tldr-candidates.jsonl"):
    payload = json.loads(line)
    assert "candidate_files" in payload
    assert "recommended_related_files" in payload
    print(payload["status"], payload["candidate_files"][:3])
PY'
```

---

### Task 3: Shared sanitized outcome analysis model

**Parallel:** yes
**Blocked by:** none
**Owned files:** `scripts/tldr_outcome_model.py`, `tests/test_tldr_outcome_model.py`
**Invariants:** Analysis code must be importable by both future evaluator upgrades and retroactive backfill; rollups must not serialize raw commands, raw user text, raw outputs, or raw paths.
**Out of scope:** CLI argument parsing and Markdown rendering.

**Files:**
- Create: `scripts/tldr_outcome_model.py`
- Create: `tests/test_tldr_outcome_model.py`

**Step 1: Write deterministic tests for rollup metrics and sanitized output**
Create `tests/test_tldr_outcome_model.py` with deterministic cases:

```python
from datetime import datetime, timezone
import json
import pytest

from scripts.tldr_outcome_model import SessionRollup, ToolEvent, UserCorrectionEvent, VerificationEvent


def test_session_rollup_computes_proxy_metrics_without_raw_text():
    rollup = SessionRollup(session_id="s1", client="codex", project_hash="abc")
    t0 = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    rollup.record_tool(ToolEvent(timestamp=t0, category="explore", command_hash="h1", files_read=["file-a"]))
    rollup.record_tool(ToolEvent(timestamp=t0, category="explore", command_hash="h1", files_read=["file-a"]))
    rollup.record_tool(ToolEvent(timestamp=t0, category="edit", command_hash="h2", files_edited=["file-a"]))
    rollup.record_verification(VerificationEvent(timestamp=t0, command_hash="h3", passed=False))
    rollup.record_user_correction(UserCorrectionEvent(timestamp=t0, kind="missed_requirement"))

    summary = rollup.to_dict()
    serialized = json.dumps(summary)

    assert summary["explore_before_first_edit"] == 2
    assert summary["repeated_file_reads"] == 1
    assert summary["verification_runs"] == 1
    assert summary["verification_failures"] == 1
    assert summary["user_corrections"] == 1
    assert summary["verdict"] == "proxy-only"
    assert "rg auth" not in serialized
    assert "you missed" not in serialized


def test_session_rollup_harmful_case_has_reason_code():
    rollup = SessionRollup(session_id="s2", client="codex", project_hash="abc")
    rollup.failed_tool_outputs = 10
    rollup.tldr_errors = 3
    summary = rollup.to_dict()
    assert summary["verdict"] == "harmful"
    assert "hook_errors" in summary["verdict_reasons"] or "failed_tool_outputs" in summary["verdict_reasons"]


def test_session_rollup_insufficient_case():
    rollup = SessionRollup(session_id="s3", client="codex", project_hash="abc")
    summary = rollup.to_dict()
    assert summary["verdict"] == "insufficient-data"


def test_causal_confidence_uses_allowed_values_only():
    rollup = SessionRollup(session_id="s4", client="codex", project_hash="abc", causal_confidence="proxy-only")
    assert rollup.to_dict()["causal_confidence"] == "proxy-only"
    with pytest.raises(ValueError):
        SessionRollup(session_id="s5", client="codex", project_hash="abc", causal_confidence="high")
```

**Step 2: Implement clean dataclasses**
Create small dataclasses in `scripts/tldr_outcome_model.py` that store sanitized fields only:

- `ToolEvent(command_hash: str, category: str, files_read: list[str], files_edited: list[str], failed: bool = False, failure_kind: str | None = None)`
- `TldrHookEvent(... hashed trigger/recommended/surfaced paths only ...)`
- `VerificationEvent(command_hash: str, passed: bool | None)`
- `UserCorrectionEvent(kind: str)`
- `SessionRollup(...)`

Include methods for:

- `record_tool`
- `record_hook`
- `record_verification`
- `record_user_correction`
- `recommended_file_hit_rate`
- `surfaced_file_hit_rate`
- `to_dict`
- deterministic `verdict` with reason codes.

**Step 3: Run tests**
Run:

```bash
python3 -m pytest -q tests/test_tldr_outcome_model.py
```

Expected: PASS.

**Verification plan:**
- Primary: `python3 -m pytest -q tests/test_tldr_outcome_model.py`
- Privacy invariant: tests must assert forbidden raw strings are absent from serialized output.

---

### Task 4: Retroactive backfill JSON CLI

**Parallel:** no
**Blocked by:** Task 3
**Owned files:** `scripts/backfill_tldr_outcomes.py`, `tests/test_backfill_tldr_outcomes.py`, `tests/fixtures/eval/backfill_codex_root/sessions/backfill_codex_session.jsonl`, `tests/fixtures/eval/backfill_claude_root/projects/example/backfill_claude_session.jsonl`, `tests/fixtures/eval/backfill_tldr_telemetry.jsonl`
**Invariants:** Backfill must tolerate malformed JSONL lines, missing telemetry, missing token counts, and unknown record shapes; no local session files are modified; telemetry is filtered to the requested timestamp window before matching, even when session IDs match.
**Out of scope:** Live hook changes and Markdown/HTML rendering.

**Files:**
- Create: `scripts/backfill_tldr_outcomes.py`
- Create: `tests/test_backfill_tldr_outcomes.py`
- Create: `tests/fixtures/eval/backfill_codex_root/sessions/backfill_codex_session.jsonl`
- Create: `tests/fixtures/eval/backfill_claude_root/projects/example/backfill_claude_session.jsonl`
- Create: `tests/fixtures/eval/backfill_tldr_telemetry.jsonl`

**Step 1: Write fixture tests**
Create tests that invoke the CLI over tiny fixture roots and assert:

- It emits JSON rollups.
- Telemetry outside `--start/--end` is ignored even with same session ID.
- Raw fixture command/user/output strings do not appear in JSON.
- Historical records without candidate metadata remain `causal_confidence="proxy-only"`.

Example:

```python
def test_backfill_cli_outputs_sanitized_session_rollups(tmp_path):
    out_json = tmp_path / "rollups.json"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/backfill_tldr_outcomes.py",
            "--start", "2026-05-20T00:00:00Z",
            "--end", "2026-05-21T00:00:00Z",
            "--codex-root", str(FIXTURES / "backfill_codex_root"),
            "--claude-root", str(FIXTURES / "backfill_claude_root"),
            "--tldr-telemetry", str(FIXTURES / "backfill_tldr_telemetry.jsonl"),
            "--json-out", str(out_json),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    raw = out_json.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert payload["window"]["start"] == "2026-05-20T00:00:00+00:00"
    assert payload["summary"]["sessions"] >= 1
    assert payload["rollups"][0]["tldr_hooks"] >= 1
    assert payload["rollups"][0]["causal_confidence"] == "proxy-only"
    assert "SECRET_FIXTURE_COMMAND" not in raw
    assert "SECRET_FIXTURE_USER_TEXT" not in raw
    assert "SECRET_FIXTURE_OUTPUT" not in raw
```

**Step 2: Implement streaming JSONL parser for backfill**
Do not reuse `load_jsonl` for backfill because it reads whole files. Implement a local streaming helper:

```python
def iter_jsonl(path: Path) -> Iterator[tuple[int, dict[str, Any] | None, str | None]]:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_no, line in enumerate(handle, start=1):
            ...
```

You may still import safe helpers from `scripts.evaluate_tldr_usage`:

- `parse_timestamp`
- `parse_tool_arguments`
- `command_from_arguments` only as input to a hash, never output
- `categorize_command`
- `project_hash`
- `telemetry_path_hash`
- `resolve_claude_roots`

**Step 3: Normalize and hash sensitive values**
Backfill output must hash:

- normalized commands: `sha256(normalized_command).hexdigest()[:12]`
- paths: use existing telemetry path hash when project path is available; otherwise hash normalized path-like text

User message text is scanned for correction phrase classes but never stored.

**Step 4: Matching rules**
First filter all telemetry records:

```python
start <= telemetry.timestamp < end
```

Then match telemetry-to-session in this order:

1. Exact `(client, session_id)`.
2. Same client + same project hash + telemetry timestamp inside session event range.
3. Same client + same project hash + nearest session within ±30 minutes.

Exact session-id match must not override the requested time window.

**Step 5: Outcome metrics**
For each rollup emit the full `SessionRollupV1` schema table fields.

**Step 6: Run tests**
Run:

```bash
python3 -m pytest -q tests/test_backfill_tldr_outcomes.py tests/test_tldr_outcome_model.py
```

Expected: PASS.

**Verification plan:**
- Primary: `python3 -m pytest -q tests/test_backfill_tldr_outcomes.py`
- Bounded smoke on real local data:

```bash
timeout 120 python3 scripts/backfill_tldr_outcomes.py \
  --start 2026-05-20T00:00:00-05:00 \
  --end 2026-05-21T00:00:00-05:00 \
  --json-out reports/tldr-backfill-2026-05-20.json
```

Expected: exits 0 and reports nonzero sessions/telemetry records without parse crashes.

---

### Task 5: Upgrade existing efficacy evaluator to use strict telemetry windows and v2 candidate metadata

**Parallel:** no
**Blocked by:** Task 3, Task 4
**Owned files:** `scripts/evaluate_tldr_usage.py`, `tests/test_evaluate_tldr_usage.py`
**Invariants:** Existing CLI arguments keep working; old fixture tests keep passing; weak historical comparisons remain labeled proxy-only when annotations/candidate telemetry are absent.
**Out of scope:** Hook candidate generation.

**Files:**
- Modify: `scripts/evaluate_tldr_usage.py`
- Modify: `tests/test_evaluate_tldr_usage.py`

**Step 1: Add failing timestamp-filter regression test**
Add a test proving telemetry outside the requested window is not matched merely because its session overlaps.

**Step 2: Add v1/v2 telemetry parser tests**
Add tests that parse:

- old fixture telemetry without `schema_version`
- new v2 telemetry with `candidate_files`, `hook_run_id`, and `context_kind`

The parser should preserve candidate metadata for rollups while keeping old records valid.

**Step 3: Implement strict filtering before match**
In `build_report`, filter telemetry records before calling `match_telemetry`:

```python
telemetry = [
    record for record in parse_telemetry_file(Path(args.tldr_telemetry).expanduser())
    if baseline_start <= record.timestamp < treatment_end
]
```

**Step 4: Add optional outcome-rollup summary**
If a `--rollups-json` option is supplied, render a compact outcome section:

- recommendation hit rate
- surfaced hit rate
- proxy-only sessions
- harmful sessions
- confidence distributions

Do not make the existing evaluator dependent on real local logs beyond its current behavior.

**Step 5: Run evaluator tests**
Run:

```bash
python3 -m pytest -q tests/test_evaluate_tldr_usage.py
```

Expected: PASS.

**Verification plan:**
- Primary: `python3 -m pytest -q tests/test_evaluate_tldr_usage.py`
- Real smoke: run existing command with May 20 window and ensure telemetry matched max timestamp is before May 21 00:00 America/Chicago.

---

### Task 6: Daily outcome report and visual explainer generation

**Parallel:** yes
**Blocked by:** Task 4
**Owned files:** `scripts/render_tldr_outcome_report.py`, `tests/test_render_tldr_outcome_report.py`, `.gitignore`
**Invariants:** Reports are local files only; generated reports under `reports/` must not be required for tests; rendering must tolerate zero sessions; rendered reports must not contain raw paths/prompts/commands/outputs.
**Out of scope:** Browser automation and in-app browser opening.

**Files:**
- Create: `scripts/render_tldr_outcome_report.py`
- Create: `tests/test_render_tldr_outcome_report.py`
- Modify: `.gitignore`

**Step 1: Test HTML/Markdown rendering from a small rollup payload**
Create a test that writes a tiny sanitized JSON rollup and verifies Markdown + HTML files are created and forbidden fixture strings are absent.

**Step 2: Implement report renderer**
The renderer should show:

- Executive verdict with match/attribution/causal confidence distributions.
- Reliability: status counts, errors, p50/p95 latency.
- Recommendation lifecycle: candidates, surfaced, later-used, hit rates.
- Behavior: explore-before-edit, repeated reads, failed tools, verification failures.
- Cost/overhead: injected bytes/tokens, hook duration.
- Harm flags: high latency, high injected context, corrections, repeated failures.
- Top sessions by likely benefit/harm, identified by session ID/project hash only.

**Step 3: Ignore generated HTML reports**
Modify `.gitignore` to include:

```gitignore
reports/*.html
```

Keep existing `reports/.gitkeep` tracked.

**Step 4: Run render tests**
Run:

```bash
python3 -m pytest -q tests/test_render_tldr_outcome_report.py
```

Expected: PASS.

**Verification plan:**
- Primary: render test above.
- Smoke with real backfill JSON:

```bash
timeout 60 python3 scripts/render_tldr_outcome_report.py \
  --input reports/tldr-backfill-2026-05-20.json \
  --markdown-out reports/tldr-outcome-2026-05-20.md \
  --html-out reports/tldr-outcome-2026-05-20.html
```

---

### Task 7: Documentation and operator runbook

**Parallel:** yes
**Blocked by:** Task 4, Task 6
**Owned files:** `docs/dev-notes/outcome-telemetry.md`, `README.md`, `tests/test_readme_examples.py`
**Invariants:** README remains concise; detailed schema/runbook lives in dev notes; documented commands must be fixture-testable or clearly marked as real-local-data examples.
**Out of scope:** Marketing copy.

**Files:**
- Create: `docs/dev-notes/outcome-telemetry.md`
- Modify: `README.md`
- Modify: `tests/test_readme_examples.py`

**Step 1: Add dev note**
Create `docs/dev-notes/outcome-telemetry.md` with:

- What telemetry captures.
- Privacy guarantees.
- Match/attribution/causal confidence labels.
- How to run backfill.
- How to generate a daily report.
- How to interpret `helpful`, `neutral`, `harmful`, `proxy-only`, `insufficient-data`.

**Step 2: Add README pointer**
Add a short README section pointing to the dev note and showing one fixture-safe command plus one real-local-data command.

**Step 3: Add executable docs test**
Update `tests/test_readme_examples.py` or add a focused test that runs the fixture-safe backfill command and renderer command against `tests/fixtures/eval/backfill_*` paths.

**Step 4: Run docs tests**
Run:

```bash
python3 -m pytest -q tests/test_readme_examples.py
```

Expected: PASS and actually exercises the new commands against fixtures.

**Verification plan:**
- Primary: `python3 -m pytest -q tests/test_readme_examples.py`
- Manual: docs must state generated reports are local artifacts and should not be staged by default.

---

### Task 8: Full verification, privacy scan, and local dogfood report

**Parallel:** no
**Blocked by:** Tasks 1-7
**Owned files:** none
**Invariants:** Do not commit/push unless explicitly requested later; do not delete existing user/generated reports without approval; do not stage generated reports.
**Out of scope:** Installing global hooks or changing user configs.

**Files:**
- No source files owned.
- Generated outputs may be written under ignored `reports/` for smoke evidence.

**Step 1: Run focused tests**
Run:

```bash
python3 -m pytest -q \
  tests/test_telemetry.py \
  tests/test_hooks_read.py \
  tests/test_hooks_edit.py \
  tests/test_hooks_runtime.py \
  tests/test_hooks_post_edit.py \
  tests/test_tldr_outcome_model.py \
  tests/test_backfill_tldr_outcomes.py \
  tests/test_evaluate_tldr_usage.py \
  tests/test_render_tldr_outcome_report.py \
  tests/test_readme_examples.py
```

Expected: PASS.

**Step 2: Run broader fast test suite**
Run:

```bash
python3 -m pytest -q
```

Expected: PASS, or document unrelated pre-existing failures with exact tests.

**Step 3: Generate May 20 retroactive report**
Run:

```bash
timeout 120 python3 scripts/backfill_tldr_outcomes.py \
  --start 2026-05-20T00:00:00-05:00 \
  --end 2026-05-21T00:00:00-05:00 \
  --json-out reports/tldr-backfill-2026-05-20.json
timeout 60 python3 scripts/render_tldr_outcome_report.py \
  --input reports/tldr-backfill-2026-05-20.json \
  --markdown-out reports/tldr-outcome-2026-05-20.md \
  --html-out reports/tldr-outcome-2026-05-20.html
```

Expected: reports generated; summary counts are nonzero; confidence labels distinguish proxy-only from attribution evidence.

**Step 4: Run privacy scan on generated artifacts**
Run:

```bash
python3 - <<'PY'
from pathlib import Path
forbidden = [
    '/Users/treygoff/',
    'SECRET_FIXTURE_COMMAND',
    'SECRET_FIXTURE_USER_TEXT',
    'SECRET_FIXTURE_OUTPUT',
]
for path in [
    Path('reports/tldr-backfill-2026-05-20.json'),
    Path('reports/tldr-outcome-2026-05-20.md'),
    Path('reports/tldr-outcome-2026-05-20.html'),
]:
    text = path.read_text(encoding='utf-8', errors='replace')
    hits = [item for item in forbidden if item in text]
    assert not hits, (path, hits)

payload = __import__('json').loads(Path('reports/tldr-backfill-2026-05-20.json').read_text())
for rollup in payload.get('rollups', []):
    forbidden_keys = {'cwd', 'command', 'output', 'prompt', 'message_text', 'files_read', 'files_edited'}
    present = forbidden_keys & set(rollup)
    assert not present, present
print('privacy scan ok')
PY
```

Expected: `privacy scan ok`.

**Step 5: Optional live hook smoke**
Run with a temp telemetry path:

```bash
timeout 20 bash -lc 'TLDR_TELEMETRY=1 TLDR_TELEMETRY_PATH=/tmp/tldr-outcome-smoke.jsonl python3 -m tldr.cli hooks run pre-edit --client codex <<"JSON"
{"tool_name":"apply_patch","tool_input":{"command":"*** Begin Patch\n*** Update File: tldr/hooks/read.py\n@@\n pass\n*** End Patch\n"},"cwd":"/Users/treygoff/Code/llm-tldr","session_id":"outcome-smoke"}
JSON
python3 - <<"PY"
import json
raw = open("/tmp/tldr-outcome-smoke.jsonl").read()
assert "/Users/" not in raw
for line in raw.splitlines():
    payload = json.loads(line)
    assert payload["schema_version"] == 2
    assert "candidate_files" in payload
print("ok")
PY'
```

Expected: prints `ok`.

---

## Delegation plan

After this plan is approved by reviewer cycles:

1. **Cursor Composer implementation pass** using `delegate cursor work` with this plan as the source of truth. Cursor owns implementation across Tasks 1-8 but must keep changes scoped to listed owned files.
2. **GLM review pass** using `delegate droid glm safe` over the resulting diff. GLM must review correctness, privacy, telemetry schema stability, confidence labeling, report sanitization, and tests.
3. **Cursor Composer fix pass** using `delegate cursor work` for GLM findings only.
4. Repeat GLM/Cursor once more if GLM finds blocking issues.
5. **Final Codex clean-code review** using a Codex reviewer subagent instructed to apply the project-active `clean-code` skill.
6. Orchestrator personally fixes final review issues and runs verification.

## Plan acceptance criteria

- Plan-reviewer finds no blocking dependency/order issues.
- Owned files are clear and do not overlap across tasks marked `Parallel: yes`.
- Every implementation task has a test-first step and concrete verification command.
- Privacy invariants are explicit for both live telemetry and backfill outputs.
- Retroactive backfill and future telemetry share one sanitized rollup schema.
- Reports distinguish operational health, attribution evidence, and causal efficacy.
- Generated reports are ignored local artifacts and not staged by default.

## Initial risk register

| Risk | Why it matters | Mitigation |
| --- | --- | --- |
| Candidate telemetry increases hook latency | Hook overhead can make TLDR harmful | Bounded local candidate generation; no remote calls; telemetry writer remains best-effort |
| Backfill overclaims causality | Historical logs cannot prove counterfactuals | Separate `match_confidence`, `attribution_confidence`, and `causal_confidence`; default historical causal confidence to `proxy-only` |
| Privacy regression | Session logs can contain sensitive text | Telemetry/reports store hashes, categories, counts, and reason codes only; tests and smoke scans assert forbidden strings are absent |
| Parser brittleness | Codex/Claude JSONL schemas evolve | Tolerant streaming parsers, malformed line counters, unknown-record counters |
| Large local logs make reports slow | 8k+ Claude files observed locally | Date/path prefiltering, streaming JSONL, bounded matching windows, timeout-wrapped smokes |
| Duplicate candidate semantics drift | Hooks and analyzer may compute candidates differently | Live hook records candidate lifecycle; backfill never invents candidate scores not logged |
