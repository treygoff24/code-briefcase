from typing import Any
import subprocess
import sys
from pathlib import Path


def test_pack_help_smoke() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "code_briefcase.cli", "pack", "--help"],
        capture_output=True,
        text=True,
        check=True,
    )

    assert "context pack" in result.stdout.lower()


def test_hooks_help_smoke() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "code_briefcase.cli", "hooks", "--help"],
        capture_output=True,
        text=True,
        check=True,
    )

    assert "hooks" in result.stdout.lower()


def test_outcome_backfill_fixture_command(tmp_path: Any) -> None:
    out_json = tmp_path / "rollups.json"
    fixtures = Path(__file__).resolve().parent / "fixtures" / "eval"
    result = subprocess.run(
        [
            sys.executable,
            str(
                Path(__file__).resolve().parents[1]
                / "scripts"
                / "backfill_tldr_outcomes.py"
            ),
            "--start",
            "2026-05-20T00:00:00Z",
            "--end",
            "2026-05-21T00:00:00Z",
            "--codex-root",
            str(fixtures / "backfill_codex_root"),
            "--claude-root",
            str(fixtures / "backfill_claude_root"),
            "--code-briefcase-telemetry",
            str(fixtures / "backfill_tldr_telemetry.jsonl"),
            "--json-out",
            str(out_json),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_outcome_render_fixture_command(tmp_path: Any) -> None:
    fixtures = Path(__file__).resolve().parent / "fixtures" / "eval"
    backfill_json = tmp_path / "rollups.json"
    subprocess.run(
        [
            sys.executable,
            str(
                Path(__file__).resolve().parents[1]
                / "scripts"
                / "backfill_tldr_outcomes.py"
            ),
            "--start",
            "2026-05-20T00:00:00Z",
            "--end",
            "2026-05-21T00:00:00Z",
            "--codex-root",
            str(fixtures / "backfill_codex_root"),
            "--claude-root",
            str(fixtures / "backfill_claude_root"),
            "--code-briefcase-telemetry",
            str(fixtures / "backfill_tldr_telemetry.jsonl"),
            "--json-out",
            str(backfill_json),
        ],
        check=True,
    )
    result = subprocess.run(
        [
            sys.executable,
            str(
                Path(__file__).resolve().parents[1]
                / "scripts"
                / "render_tldr_outcome_report.py"
            ),
            "--input",
            str(backfill_json),
            "--markdown-out",
            str(tmp_path / "report.md"),
            "--html-out",
            str(tmp_path / "report.html"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_tldr_mcp_help_smoke() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "code_briefcase.mcp_server", "--help"],
        capture_output=True,
        text=True,
        check=True,
    )

    assert "--project" in result.stdout
