from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.evaluate_tldr_usage import (  # noqa: E402
    TokenTotals,
    TelemetryRecord,
    apply_cumulative_token_count,
    apply_token_count,
    discover_sessions,
    extract_token_usage,
    load_jsonl,
    match_telemetry,
    parse_claude_file,
    parse_codex_file,
    parse_tool_arguments,
    parse_telemetry_file,
    normalize_cwd,
    path_context_hit,
    project_hash,
    resolve_claude_roots,
    session_path_may_overlap_window,
    telemetry_context_hit,
    telemetry_path_hash,
    token_usage_is_cumulative,
    verdict_for,
)

FIXTURES = Path(__file__).parent / "fixtures" / "eval"


def test_load_jsonl_tolerates_malformed_lines():
    records, errors = load_jsonl(FIXTURES / "codex_session.jsonl")
    assert len(records) >= 8
    assert errors == 1


def test_parse_codex_fixture_tokens_and_tools():
    summary = parse_codex_file(FIXTURES / "codex_session.jsonl", cohort="treatment")
    assert summary is not None
    assert summary.session_id == "codex-fixture-1"
    assert summary.tokens.total_tokens > 0
    assert summary.tokens.non_cached_input_tokens == summary.tokens.input_tokens - summary.tokens.cached_input_tokens
    assert summary.tools.by_category["explore"] >= 1
    assert summary.tools.by_category["edit"] >= 1
    assert summary.tools.by_category["verify"] >= 1
    assert summary.tools.total_calls == 3
    assert summary.parse_errors == 1
    assert summary.unknown_records >= 1


def test_codex_ignores_function_call_output_for_tool_counts():
    summary = parse_codex_file(FIXTURES / "codex_session.jsonl", cohort="treatment")
    assert summary is not None
    assert summary.tools.total_calls == 3


def test_apply_token_count_treats_monotonic_totals_as_cumulative():
    tokens = TokenTotals()
    apply_token_count(tokens, {"input_tokens": 1000, "total_tokens": 1500})
    apply_token_count(tokens, {"input_tokens": 1200, "total_tokens": 1800})
    assert tokens.input_tokens == 1200
    assert tokens.total_tokens == 1800


def test_extract_token_usage_reads_current_codex_nested_shape():
    payload = {
        "type": "token_count",
        "info": {
            "total_token_usage": {
                "input_tokens": 1000,
                "cached_input_tokens": 400,
                "output_tokens": 200,
                "reasoning_output_tokens": 50,
                "total_tokens": 1200,
            },
            "last_token_usage": {"total_tokens": 300},
        },
    }

    tokens = TokenTotals()
    assert token_usage_is_cumulative(payload)
    apply_cumulative_token_count(tokens, extract_token_usage(payload))

    assert tokens.input_tokens == 1000
    assert tokens.cached_input_tokens == 400
    assert tokens.total_tokens == 1200


def test_cumulative_token_usage_uses_max_observed_total_after_small_dip():
    tokens = TokenTotals()
    apply_cumulative_token_count(tokens, {"input_tokens": 1000, "total_tokens": 1200})
    apply_cumulative_token_count(tokens, {"input_tokens": 990, "total_tokens": 1190})

    assert tokens.input_tokens == 1000
    assert tokens.total_tokens == 1200


def test_parse_tool_arguments_accepts_codex_json_string_arguments():
    parsed = parse_tool_arguments('{"cmd":"rg -n main app.py","yield_time_ms":1000}')

    assert parsed["cmd"] == "rg -n main app.py"


def test_path_context_hit_avoids_unsafe_substring_matches():
    assert not path_context_hit("app", {"wrapper/app.py"})
    assert path_context_hit("app.py", {"src/app.py"})


def test_redacted_path_context_hit_matches_hashed_session_paths():
    codex = parse_codex_file(FIXTURES / "codex_session.jsonl", cohort="treatment")
    assert codex is not None
    repo = normalize_cwd(codex.cwd)
    repo_hash = project_hash(repo)
    trigger = f"<redacted>/{repo_hash}/{telemetry_path_hash(repo, 'src/app.py')}"
    record = TelemetryRecord(
        timestamp=codex.start,
        client="codex",
        event="pre-read",
        project=f"<redacted>/{repo_hash}",
        project_hash=repo_hash,
        duration_ms=1,
        status="ok",
        error_kind=None,
        injected_bytes=0,
        trigger_files=[trigger],
        recommended_related_files=[],
        surfaced_files=[],
        diagnostics_count=0,
        daemon_state=None,
        noop_reason=None,
        session_id=None,
    )

    assert telemetry_context_hit(trigger, session=codex, record=record, later_reads={"src/app.py"})


def test_match_telemetry_redacted_project_uses_project_hash():
    codex = parse_codex_file(FIXTURES / "codex_session.jsonl", cohort="treatment")
    assert codex is not None
    repo_hash = project_hash(normalize_cwd(codex.cwd))
    record = TelemetryRecord(
        timestamp=codex.start,
        client="codex",
        event="session-start",
        project=f"<redacted>/{repo_hash}",
        project_hash=repo_hash,
        duration_ms=1,
        status="ok",
        error_kind=None,
        injected_bytes=0,
        trigger_files=[],
        recommended_related_files=[],
        surfaced_files=[],
        diagnostics_count=0,
        daemon_state=None,
        noop_reason=None,
        session_id=None,
    )
    matched, unmatched, _ = match_telemetry([codex], [record])
    assert matched == [record]
    assert unmatched == []


def test_parse_claude_fixture_hooks_and_tools():
    summary = parse_claude_file(FIXTURES / "claude_session.jsonl", cohort="treatment")
    assert summary is not None
    assert summary.session_id == "claude-fixture-1"
    assert summary.tldr_hook_events >= 1
    assert summary.tools.by_category["explore"] >= 1
    assert summary.tools.by_category["edit"] >= 1
    assert summary.parse_errors == 1
    assert summary.unknown_records >= 1


def test_parse_telemetry_fixture_statuses():
    records = parse_telemetry_file(FIXTURES / "tldr_telemetry.jsonl")
    statuses = {record.status for record in records}
    assert statuses >= {"ok", "skipped"}


def test_match_telemetry_by_session_id():
    codex = parse_codex_file(FIXTURES / "codex_session.jsonl", cohort="treatment")
    claude = parse_claude_file(FIXTURES / "claude_session.jsonl", cohort="treatment")
    telemetry = parse_telemetry_file(FIXTURES / "tldr_telemetry.jsonl")
    matched, unmatched, hit_stats = match_telemetry([codex, claude], telemetry)
    assert len(matched) >= 2
    assert hit_stats["trigger_total"] >= 1


def test_resolve_claude_roots_accepts_repeated_and_comma_separated_values(tmp_path):
    work = tmp_path / "claude-work"
    personal = tmp_path / "claude-personal"

    roots = resolve_claude_roots([str(work), f"{personal},{work}"])

    assert roots == [work, personal]


def test_discover_sessions_reads_nested_codex_archives(tmp_path):
    archived_session = tmp_path / "codex" / "archived_sessions" / "old" / "codex_session.jsonl"
    archived_session.parent.mkdir(parents=True)
    archived_session.write_text((FIXTURES / "codex_session.jsonl").read_text(encoding="utf-8"), encoding="utf-8")

    sessions = discover_sessions(
        codex_root=tmp_path / "codex",
        claude_roots=[],
        baseline_start=datetime(2026, 5, 19, tzinfo=timezone.utc),
        baseline_end=datetime(2026, 5, 20, tzinfo=timezone.utc),
        treatment_end=datetime(2026, 5, 21, tzinfo=timezone.utc),
    )

    assert [session.session_id for session in sessions] == ["codex-fixture-1"]


def test_session_path_may_overlap_window_skips_dated_old_archives():
    start = datetime(2026, 5, 19, tzinfo=timezone.utc)
    end = datetime(2026, 5, 21, tzinfo=timezone.utc)

    assert session_path_may_overlap_window(
        Path("archived_sessions/keep-codex-fast/2026/05/20/rollout-2026-05-20T12-00-00.jsonl"),
        start,
        end,
    )
    assert not session_path_may_overlap_window(
        Path("archived_sessions/keep-codex-fast/2025/11/04/rollout-2025-11-04T12-00-00.jsonl"),
        start,
        end,
    )


def test_verdict_insufficient_data_with_small_sample():
    codex = parse_codex_file(FIXTURES / "codex_session.jsonl", cohort="baseline")
    assert verdict_for([codex], [], has_annotations=False) == "insufficient data"


def test_build_report_filters_telemetry_outside_window(tmp_path, monkeypatch):
    from scripts.evaluate_tldr_usage import TelemetryRecord, build_report
    from argparse import Namespace

    inside = TelemetryRecord(
        timestamp=datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc),
        client="codex",
        event="pre-read",
        project="<redacted>/abcd",
        project_hash="abcd",
        duration_ms=1,
        status="ok",
        error_kind=None,
        injected_bytes=0,
        trigger_files=[],
        recommended_related_files=[],
        surfaced_files=[],
        diagnostics_count=0,
        daemon_state=None,
        noop_reason=None,
        session_id="codex-fixture-1",
    )
    outside = TelemetryRecord(
        timestamp=datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc),
        client="codex",
        event="pre-read",
        project="<redacted>/abcd",
        project_hash="abcd",
        duration_ms=1,
        status="ok",
        error_kind=None,
        injected_bytes=0,
        trigger_files=[],
        recommended_related_files=[],
        surfaced_files=[],
        diagnostics_count=0,
        daemon_state=None,
        noop_reason=None,
        session_id="codex-fixture-1",
    )
    monkeypatch.setattr(
        "scripts.evaluate_tldr_usage.parse_telemetry_file",
        lambda _path: [inside, outside],
    )
    codex = parse_codex_file(FIXTURES / "codex_session.jsonl", cohort="treatment")
    assert codex is not None
    monkeypatch.setattr(
        "scripts.evaluate_tldr_usage.discover_sessions",
        lambda **kwargs: [codex],
    )

    args = Namespace(
        baseline_start="2026-05-20T00:00:00Z",
        treatment_start="2026-05-20T12:00:00Z",
        baseline_end=None,
        treatment_end="2026-05-21T00:00:00Z",
        codex_root=str(tmp_path / "codex"),
        claude_root=[],
        tldr_telemetry=str(FIXTURES / "tldr_telemetry.jsonl"),
        annotations=str(tmp_path / "missing-annotations.jsonl"),
        rollups_json=None,
    )
    report = build_report(args)
    matched = report["json"]["telemetry_matched"]
    assert len(matched) == 1
    assert matched[0]["session_id"] == "codex-fixture-1"


def test_parse_telemetry_v2_candidate_metadata():
    records = parse_telemetry_file(FIXTURES / "backfill_tldr_telemetry.jsonl")
    v2 = [record for record in records if (record.schema_version or 1) >= 2]
    assert v2
    assert v2[0].candidate_files
    assert v2[0].hook_run_id
    assert v2[0].context_kind == "read_nav_map"


def test_parse_telemetry_tolerates_malformed_schema_fields(tmp_path):
    telemetry = tmp_path / "telemetry.jsonl"
    telemetry.write_text(
        json.dumps(
            {
                "timestamp": "2026-05-20T14:30:00+00:00",
                "schema_version": "bad",
                "duration_ms": "slow",
                "injected_bytes": ["bad"],
                "diagnostics_count": {"bad": "shape"},
                "trigger_files": "src/app.py",
                "recommended_related_files": None,
                "surfaced_files": [],
                "candidate_files": [{"path": "src/auth.py"}, "bad-candidate"],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    records = parse_telemetry_file(telemetry)

    assert len(records) == 1
    assert records[0].schema_version is None
    assert records[0].duration_ms == 0
    assert records[0].injected_bytes == 0
    assert records[0].diagnostics_count == 0
    assert records[0].trigger_files == []
    assert records[0].candidate_files == [{"path": "src/auth.py"}]


def test_parse_telemetry_v1_fixture_without_schema_version():
    records = parse_telemetry_file(FIXTURES / "tldr_telemetry.jsonl")
    assert records
    assert all(record.schema_version is None for record in records)


def test_evaluate_script_writes_markdown_and_json(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_tldr_usage.py"
    out_md = tmp_path / "report.md"
    out_json = tmp_path / "report.json"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--baseline-start",
            "2026-05-13",
            "--treatment-start",
            "2026-05-19T20:07:24-05:00",
            "--codex-root",
            str(tmp_path / "missing-codex"),
            "--claude-root",
            str(tmp_path / "missing-claude"),
            "--tldr-telemetry",
            str(FIXTURES / "tldr_telemetry.jsonl"),
            "--out",
            str(out_md),
            "--json-out",
            str(out_json),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Code Briefcase efficacy report" in out_md.read_text(encoding="utf-8")
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert "verdict" in payload
    json.loads(out_json.read_text(encoding="utf-8"))
