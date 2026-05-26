from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.tldr_outcome_model import (  # noqa: E402
    SessionRollup,
    TldrHookEvent,
    ToolEvent,
    UserCorrectionEvent,
    VerificationEvent,
)


def test_session_rollup_computes_proxy_metrics_without_raw_text() -> None:
    rollup = SessionRollup(session_id="s1", client="codex", project_hash="abc")
    t0 = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    rollup.record_tool(
        ToolEvent(
            timestamp=t0, category="explore", command_hash="h1", files_read=["file-a"]
        )
    )
    rollup.record_tool(
        ToolEvent(
            timestamp=t0, category="explore", command_hash="h1", files_read=["file-a"]
        )
    )
    rollup.record_tool(
        ToolEvent(
            timestamp=t0, category="edit", command_hash="h2", files_edited=["file-a"]
        )
    )
    rollup.record_verification(
        VerificationEvent(timestamp=t0, command_hash="h3", passed=False)
    )
    rollup.record_user_correction(
        UserCorrectionEvent(timestamp=t0, kind="missed_requirement")
    )

    summary = rollup.to_dict()
    serialized = json.dumps(summary)

    assert summary["explore_before_first_edit"] == 2
    assert summary["repeated_file_reads"] == 1
    assert summary["verification_runs"] == 1
    assert summary["verification_failures"] == 1
    assert summary["user_corrections"] == 1
    assert summary["verdict"] == "proxy-only"
    assert summary["unique_command_shapes"] == 2
    assert summary["repeated_command_runs"] == 1
    assert "command_hash_counts" not in summary
    assert "rg auth" not in serialized
    assert "you missed" not in serialized


def test_session_rollup_harmful_case_has_reason_code() -> None:
    rollup = SessionRollup(session_id="s2", client="codex", project_hash="abc")
    rollup.failed_tool_outputs = 10
    rollup.tldr_errors = 3
    rollup.tldr_hooks = 1
    summary = rollup.to_dict()
    assert summary["verdict"] == "harmful"
    assert (
        "hook_errors" in summary["verdict_reasons"]
        or "failed_tool_outputs" in summary["verdict_reasons"]
    )


def test_session_rollup_insufficient_case() -> None:
    rollup = SessionRollup(session_id="s3", client="codex", project_hash="abc")
    summary = rollup.to_dict()
    assert summary["verdict"] == "insufficient-data"


def test_record_hook_tracks_skip_noop_reasons_and_clean_checks() -> None:
    rollup = SessionRollup(session_id="s7", client="codex", project_hash="abc")
    t0 = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    rollup.record_hook(
        TldrHookEvent(
            timestamp=t0,
            event="pre-read",
            status="skipped",
            noop_reason="markdown_unsupported",
        )
    )
    rollup.record_hook(
        TldrHookEvent(
            timestamp=t0,
            event="post-edit",
            status="noop",
            noop_reason="clean_no_diagnostics",
        )
    )
    # ok-status clean edit confirmation also counts toward clean_checks
    rollup.record_hook(
        TldrHookEvent(
            timestamp=t0,
            event="post-edit",
            status="ok",
            noop_reason="clean_no_diagnostics",
        )
    )
    summary = rollup.to_dict()
    assert summary["tldr_skip_reason_counts"] == {"markdown_unsupported": 1}
    assert summary["tldr_noop_reason_counts"] == {"clean_no_diagnostics": 2}
    assert summary["tldr_clean_checks"] == 2


def test_causal_confidence_uses_allowed_values_only() -> None:
    rollup = SessionRollup(
        session_id="s4",
        client="codex",
        project_hash="abc",
        causal_confidence="proxy-only",
    )
    assert rollup.to_dict()["causal_confidence"] == "proxy-only"
    with pytest.raises(ValueError):
        SessionRollup(session_id="s5", client="codex", project_hash="abc", causal_confidence="high")  # type: ignore[arg-type]


def test_candidate_files_later_used_counts_all_candidates_not_only_surfaced() -> None:
    rollup = SessionRollup(session_id="s6", client="codex", project_hash="abc")
    t0 = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    rollup.record_tool(
        ToolEvent(
            timestamp=t0 + timedelta(seconds=1),
            category="edit",
            command_hash="edit",
            files_edited=["candidate-b"],
        )
    )

    rollup.record_hook(
        TldrHookEvent(
            timestamp=t0,
            event="pre-read",
            status="ok",
            candidate_files=["candidate-a", "candidate-b"],
            surfaced_files=[],
            candidate_files_total=2,
            candidate_files_surfaced=0,
        )
    )

    summary = rollup.to_dict()
    assert summary["candidate_files_total"] == 2
    assert summary["candidate_files_surfaced"] == 0
    assert summary["candidate_files_later_used"] == 1


def test_candidate_files_later_used_ignores_prior_file_activity() -> None:
    rollup = SessionRollup(session_id="s8", client="codex", project_hash="abc")
    t0 = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    rollup.record_tool(
        ToolEvent(
            timestamp=t0,
            category="explore",
            command_hash="read",
            files_read=["candidate-b"],
        )
    )

    rollup.record_hook(
        TldrHookEvent(
            timestamp=t0 + timedelta(seconds=1),
            event="pre-read",
            status="ok",
            trigger_files=["candidate-b"],
            recommended_files=["candidate-b"],
            surfaced_files=["candidate-b"],
            candidate_files=["candidate-b"],
            candidate_files_total=1,
            candidate_files_surfaced=1,
        )
    )

    summary = rollup.to_dict()
    assert summary["trigger_files_used"] == 0
    assert summary["recommended_files_used"] == 0
    assert summary["surfaced_files_used"] == 0
    assert summary["candidate_files_later_used"] == 0


def test_session_rollup_exports_hook_duration_summary() -> None:
    rollup = SessionRollup(session_id="s7", client="codex", project_hash="abc")
    t0 = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    for duration_ms in (10, 20, 40):
        rollup.record_hook(
            TldrHookEvent(
                timestamp=t0,
                event="pre-read",
                status="ok",
                duration_ms=duration_ms,
            )
        )

    summary = rollup.to_dict()

    assert summary["hook_duration_p50"] == 20
    assert summary["hook_duration_p95"] == 20
