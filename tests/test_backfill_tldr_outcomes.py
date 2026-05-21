from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "eval"
ROOT = Path(__file__).resolve().parents[1]


def test_rollup_from_session_summary_hashes_tool_paths():
    from scripts.backfill_tldr_outcomes import _rollup_from_session_summary, hash_path_like
    from scripts.evaluate_tldr_usage import parse_codex_file

    summary = parse_codex_file(
        FIXTURES / "backfill_codex_root/sessions/backfill_codex_session.jsonl",
        cohort="treatment",
    )
    assert summary is not None
    assert summary.tools.unique_files_read

    start = datetime(2026, 5, 20, tzinfo=timezone.utc)
    end = datetime(2026, 5, 21, tzinfo=timezone.utc)
    rollup = _rollup_from_session_summary(summary, start, end)

    for raw in summary.tools.unique_files_read:
        assert raw not in rollup.files_read
        assert hash_path_like(summary.cwd, raw) in rollup.files_read
    for raw in summary.tools.unique_files_edited:
        assert raw not in rollup.files_edited
        assert hash_path_like(summary.cwd, raw) in rollup.files_edited


def test_backfill_cli_outputs_sanitized_session_rollups(tmp_path):
    out_json = tmp_path / "rollups.json"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "backfill_tldr_outcomes.py"),
            "--start",
            "2026-05-20T00:00:00Z",
            "--end",
            "2026-05-21T00:00:00Z",
            "--codex-root",
            str(FIXTURES / "backfill_codex_root"),
            "--claude-root",
            str(FIXTURES / "backfill_claude_root"),
            "--tldr-telemetry",
            str(FIXTURES / "backfill_tldr_telemetry.jsonl"),
            "--json-out",
            str(out_json),
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


def test_backfill_cli_can_include_local_rich_evidence_with_redaction(tmp_path):
    out_json = tmp_path / "rollups-rich.json"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "backfill_tldr_outcomes.py"),
            "--start",
            "2026-05-20T00:00:00Z",
            "--end",
            "2026-05-21T00:00:00Z",
            "--codex-root",
            str(FIXTURES / "backfill_codex_root"),
            "--claude-root",
            str(FIXTURES / "backfill_claude_root"),
            "--tldr-telemetry",
            str(FIXTURES / "backfill_tldr_telemetry.jsonl"),
            "--json-out",
            str(out_json),
            "--include-local-evidence",
            "--max-local-evidence-per-session",
            "10",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    raw = out_json.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert payload["privacy"]["includes_local_evidence"] is True
    codex = next(item for item in payload["rollups"] if item["session_id"] == "backfill-codex-1")
    assert codex["local_evidence"]
    assert "rg -n main app.py" in raw
    assert "SECRET_FIXTURE_COMMAND" not in raw
    assert "SECRET_FIXTURE_OUTPUT" not in raw
    assert "SECRET_FIXTURE_USER_TEXT" not in raw


def test_backfill_cli_allows_missing_telemetry_file_for_proxy_only_report(tmp_path):
    out_json = tmp_path / "rollups.json"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "backfill_tldr_outcomes.py"),
            "--start",
            "2026-05-20T00:00:00Z",
            "--end",
            "2026-05-21T00:00:00Z",
            "--codex-root",
            str(FIXTURES / "backfill_codex_root"),
            "--claude-root",
            str(FIXTURES / "backfill_claude_root"),
            "--tldr-telemetry",
            str(tmp_path / "missing-telemetry.jsonl"),
            "--json-out",
            str(out_json),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload["summary"]["telemetry_missing"] is True
    assert payload["summary"]["telemetry_records"] == 0
    assert payload["summary"]["sessions"] >= 1
    assert all(item["causal_confidence"] == "proxy-only" for item in payload["rollups"])


def test_backfill_ignores_telemetry_outside_window(tmp_path):
    out_json = tmp_path / "rollups.json"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "backfill_tldr_outcomes.py"),
            "--start",
            "2026-05-20T00:00:00Z",
            "--end",
            "2026-05-21T00:00:00Z",
            "--codex-root",
            str(FIXTURES / "backfill_codex_root"),
            "--claude-root",
            str(tmp_path / "missing"),
            "--tldr-telemetry",
            str(FIXTURES / "backfill_tldr_telemetry.jsonl"),
            "--json-out",
            str(out_json),
        ],
        check=True,
    )
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    codex = next(item for item in payload["rollups"] if item["session_id"] == "backfill-codex-1")
    assert codex["tldr_hooks"] == 1


def test_parse_telemetry_records_tolerates_malformed_schema_fields(tmp_path):
    from scripts.backfill_tldr_outcomes import parse_telemetry_records

    telemetry = tmp_path / "telemetry.jsonl"
    telemetry.write_text(
        json.dumps(
            {
                "schema_version": "not-an-int",
                "timestamp": "2026-05-20T14:30:00+00:00",
                "client": "codex",
                "event": "pre-read",
                "project": "<redacted>/abc12345",
                "project_hash": "abc12345",
                "duration_ms": "slow",
                "injected_bytes": {"bad": "shape"},
                "status": "ok",
                "trigger_files": "src/app.py",
                "recommended_related_files": None,
                "surfaced_files": [],
                "candidate_files": [
                    {
                        "path": "<redacted>/abc12345/deadbeef0002",
                        "reason": "import",
                        "rank": 1,
                        "surfaced": False,
                    },
                    "bad-candidate",
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    records = parse_telemetry_records(
        telemetry,
        datetime(2026, 5, 20, tzinfo=timezone.utc),
        datetime(2026, 5, 21, tzinfo=timezone.utc),
    )

    assert len(records) == 1
    assert records[0].schema_version is None
    assert records[0].duration_ms == 0
    assert records[0].injected_bytes == 0
    assert records[0].trigger_files == []
    assert records[0].candidate_files == [
        {
            "path": "deadbeef0002",
            "reason": "import",
            "rank": 1,
            "surfaced": False,
        }
    ]


def test_match_telemetry_uses_visible_files_as_legacy_candidates():
    from scripts.backfill_tldr_outcomes import (
        ParsedTelemetry,
        SessionContext,
        match_telemetry_to_sessions,
    )
    from scripts.tldr_outcome_model import SessionRollup, ToolEvent

    ts = datetime(2026, 5, 20, 14, 30, tzinfo=timezone.utc)
    rollup = SessionRollup(session_id="s1", client="codex", project_hash="abc")
    rollup.record_tool(
        ToolEvent(
            timestamp=ts,
            category="explore",
            command_hash="read",
            files_read=["hash-related"],
        )
    )
    session = SessionContext(
        session_id="s1",
        client="codex",
        project_hash="abc",
        cwd="/repo",
        start=ts,
        end=ts,
        rollup=rollup,
    )
    record = ParsedTelemetry(
        timestamp=ts,
        client="codex",
        event="pre-read",
        project_hash="abc",
        status="ok",
        session_id="s1",
        duration_ms=1,
        injected_bytes=100,
        trigger_files=[],
        recommended_files=[],
        surfaced_files=["hash-related"],
        candidate_files=[],
        schema_version=None,
        hook_run_id=None,
        context_kind=None,
    )

    match_telemetry_to_sessions([session], [record])
    summary = rollup.to_dict()

    assert summary["candidate_files_total"] == 1
    assert summary["candidate_files_surfaced"] == 1
    assert summary["candidate_files_later_used"] == 1
