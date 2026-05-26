from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

from code_briefcase.telemetry import project_hash

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "watch_diagnostics_checkpoint.py"
)
SPEC = importlib.util.spec_from_file_location(
    "watch_diagnostics_checkpoint", SCRIPT_PATH
)
assert SPEC is not None and SPEC.loader is not None
checkpoint = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = checkpoint
SPEC.loader.exec_module(checkpoint)


def test_percentile_uses_conservative_nearest_rank():
    values = [10, 20, 30, 40]

    assert checkpoint.nearest_rank(values, 50) == 20
    assert checkpoint.nearest_rank(values, 95) == 40


def test_load_jsonl_tolerates_missing_and_malformed_lines(tmp_path):
    missing = tmp_path / "missing.jsonl"
    assert checkpoint.load_jsonl(missing) == ([], 0)

    path = tmp_path / "telemetry.jsonl"
    path.write_text('{"ok": true}\nnot-json\n{"ok": false}\n', encoding="utf-8")
    records, errors = checkpoint.load_jsonl(path)

    assert records == [{"ok": True}, {"ok": False}]
    assert errors == 1


def test_probe_selection_excludes_generated_and_declaration_files(tmp_path):
    for rel in (
        "node_modules/pkg/a.ts",
        "dist/a.ts",
        "src/types.d.ts",
        "src/app.config.ts",
        "src/app.ts",
    ):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("const x = 1;\n", encoding="utf-8")

    assert checkpoint.choose_probe_file(tmp_path) == tmp_path / "src" / "app.ts"


def test_checkpoint_report_redacts_paths_by_default(tmp_path):
    repo = tmp_path / "secret-repo-name"
    repo.mkdir()
    probe = repo / "src" / "app.ts"
    probe.parent.mkdir()
    probe.write_text("const x = 1;\n", encoding="utf-8")

    args = argparse.Namespace(
        local_rich=False,
        exercise_edits=False,
        allow_dirty=True,
        baseline_iterations=1,
        watch_iterations=1,
        warmups=0,
        warmup_settle_timeout_ms=0,
        min_watch_samples=1,
        min_settle_events=1,
        hook_p50_ms=200,
        hook_p95_ms=500,
        settle_p50_ms=600,
        settle_p95_ms=2000,
    )

    report = checkpoint.repo_report(
        checkpoint.RepoSpec("fixture", repo),
        args=args,
        telemetry_path=tmp_path / "telemetry.jsonl",
        probe_override=probe,
    )
    raw = json.dumps(report)

    assert "secret-repo-name" not in raw
    assert str(tmp_path) not in raw
    assert project_hash(repo.resolve()) in raw


def test_dry_run_does_not_modify_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    probe = repo / "app.ts"
    probe.write_text("const x = 1;\n", encoding="utf-8")
    before = probe.read_bytes()
    args = argparse.Namespace(
        repo=[checkpoint.RepoSpec("fixture", repo)],
        probe_file=[],
        exercise_edits=False,
        allow_dirty=True,
        baseline_iterations=1,
        watch_iterations=1,
        warmups=0,
        warmup_settle_timeout_ms=0,
        telemetry_path=tmp_path / "telemetry.jsonl",
        json_out=None,
        local_rich=False,
        fail_on_threshold=False,
        min_watch_samples=1,
        min_settle_events=1,
        hook_p50_ms=200,
        hook_p95_ms=500,
        settle_p50_ms=600,
        settle_p95_ms=2000,
    )

    report = checkpoint.build_report(args)

    assert probe.read_bytes() == before
    assert report["repos"][0]["skipped_reason"] == "exercise_edits_required"


def test_probe_file_is_restored_when_hook_command_fails(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    probe = repo / "app.ts"
    probe.write_text("const x = 1;\n", encoding="utf-8")
    before = probe.read_bytes()

    def fail_hook(**_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(checkpoint, "run_post_edit_hook", fail_hook)
    args = argparse.Namespace(
        local_rich=False,
        exercise_edits=True,
        allow_dirty=True,
        baseline_iterations=1,
        watch_iterations=0,
        warmups=0,
        warmup_settle_timeout_ms=0,
        min_watch_samples=1,
        min_settle_events=1,
        hook_p50_ms=200,
        hook_p95_ms=500,
        settle_p50_ms=600,
        settle_p95_ms=2000,
    )

    report = checkpoint.repo_report(
        checkpoint.RepoSpec("fixture", repo),
        args=args,
        telemetry_path=tmp_path / "telemetry.jsonl",
        probe_override=probe,
    )

    assert probe.read_bytes() == before
    assert report["passed"] is False
    assert report["failures"] == ["boom"]


def test_checkpoint_trusts_repo_binaries_only_for_watch_samples(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    probe = repo / "app.ts"
    probe.write_text("const x = 1;\n", encoding="utf-8")
    seen_envs = []

    def fake_run(*_args, **kwargs):
        seen_envs.append(kwargs["env"])
        return subprocess.CompletedProcess(args=kwargs.get("args", []), returncode=0)

    monkeypatch.setattr(checkpoint.subprocess, "run", fake_run)

    checkpoint.run_post_edit_hook(
        repo=repo,
        probe=probe,
        telemetry_path=tmp_path / "telemetry.jsonl",
        watch_enabled=True,
    )
    checkpoint.run_post_edit_hook(
        repo=repo,
        probe=probe,
        telemetry_path=tmp_path / "telemetry.jsonl",
        watch_enabled=False,
    )

    assert seen_envs[0]["CODE_BRIEFCASE_WATCH_DIAGNOSTICS_TRUST_REPO_BINARIES"] == "1"
    assert seen_envs[1]["CODE_BRIEFCASE_WATCH_DIAGNOSTICS_TRUST_REPO_BINARIES"] == "0"


def test_watch_hook_metrics_count_pending_stale_and_fresh():
    samples = [
        {
            "watch_diagnostics_used": True,
            "watch_diagnostics_status": "pending",
            "hook_duration_ms": 160,
        },
        {
            "watch_diagnostics_used": True,
            "watch_diagnostics_status": "stale",
            "hook_duration_ms": 170,
        },
        {
            "watch_diagnostics_used": True,
            "watch_diagnostics_status": "fresh",
            "hook_duration_ms": 180,
        },
        {
            "watch_diagnostics_used": False,
            "watch_diagnostics_status": "fallback_required",
            "hook_duration_ms": 900,
        },
    ]

    assert checkpoint.watch_hook_samples(samples) == samples[:3]


def test_fresh_settle_metrics_come_from_recheck_complete_events(tmp_path):
    telemetry = tmp_path / "telemetry.jsonl"
    telemetry.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event": "watch-diagnostics-event",
                        "project_hash": "abc",
                        "action": "start",
                    }
                ),
                json.dumps(
                    {
                        "event": "watch-diagnostics-event",
                        "project_hash": "abc",
                        "action": "recheck_complete",
                        "duration_ms": 240,
                    }
                ),
                json.dumps(
                    {
                        "event": "watch-diagnostics-event",
                        "project_hash": "abc",
                        "action": "recheck_complete",
                        "duration_ms": 410,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    records, _ = checkpoint.load_jsonl(telemetry)

    assert checkpoint.settle_durations(records, "abc") == [240, 410]


def test_repo_report_stops_daemon_before_watch_phase(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    probe = repo / "src" / "app.ts"
    probe.parent.mkdir()
    probe.write_text("const x = 1;\n", encoding="utf-8")
    events = []
    monkeypatch.setattr(checkpoint, "git_clean", lambda _repo: True)
    monkeypatch.setattr(
        checkpoint, "stop_project_daemon", lambda _repo: events.append("stop")
    )

    def fake_run_post_edit_hook(**kwargs):
        events.append("watch" if kwargs["watch_enabled"] else "baseline")
        return (
            10,
            {
                "event": "post-edit",
                "status": "ok",
                "duration_ms": 10,
                "watch_diagnostics_used": kwargs["watch_enabled"],
                "watch_diagnostics_status": (
                    "pending" if kwargs["watch_enabled"] else None
                ),
            },
            0,
        )

    monkeypatch.setattr(checkpoint, "run_post_edit_hook", fake_run_post_edit_hook)
    args = checkpoint.build_arg_parser().parse_args(
        [
            "--repo",
            f"r={repo}",
            "--probe-file",
            f"r={probe}",
            "--exercise-edits",
            "--baseline-iterations",
            "1",
            "--watch-iterations",
            "1",
            "--warmups",
            "0",
            "--allow-dirty",
        ]
    )

    checkpoint.repo_report(
        args.repo[0],
        args=args,
        telemetry_path=tmp_path / "t.jsonl",
        probe_override=probe,
    )

    assert events[0] == "stop"
    assert events.index("stop", 1) < events.index("watch")
    assert events[-1] == "stop"


def test_thresholds_fail_without_settle_events():
    report = {
        "skipped_reason": None,
        "failures": [],
        "metrics": {
            "watch_hook_ms": {"count": 5, "p50": 100, "p95": 120},
            "fresh_settle_ms": {"count": 0, "p50": None, "p95": None},
        },
        "telemetry": {"runtime_errors": 0},
        "passed": True,
    }
    args = argparse.Namespace(
        min_watch_samples=5,
        min_settle_events=1,
        hook_p50_ms=200,
        hook_p95_ms=500,
        settle_p50_ms=600,
        settle_p95_ms=2000,
    )

    checkpoint._apply_thresholds(report, args)

    assert "insufficient_settle_events" in report["failures"]
    assert report["passed"] is False


def test_thresholds_fail_on_watcher_runtime_errors():
    report = {
        "skipped_reason": None,
        "failures": [],
        "metrics": {
            "watch_hook_ms": {"count": 5, "p50": 100, "p95": 120},
            "fresh_settle_ms": {"count": 1, "p50": 100, "p95": 100},
        },
        "telemetry": {"runtime_errors": 1},
        "passed": True,
    }
    args = argparse.Namespace(
        min_watch_samples=5,
        min_settle_events=1,
        hook_p50_ms=200,
        hook_p95_ms=500,
        settle_p50_ms=600,
        settle_p95_ms=2000,
    )

    checkpoint._apply_thresholds(report, args)

    assert "watcher_runtime_errors" in report["failures"]
    assert report["passed"] is False


def test_settle_durations_ignores_historical_records():
    records = [
        {
            "event": "watch-diagnostics-event",
            "project_hash": "abc",
            "action": "recheck_complete",
            "duration_ms": 999,
        },
        {"event": "post-edit", "project_hash": "abc"},
        {
            "event": "watch-diagnostics-event",
            "project_hash": "abc",
            "action": "recheck_complete",
            "duration_ms": 120,
        },
    ]

    current_records = records[1:]

    assert checkpoint.settle_durations(current_records, "abc") == [120]


def test_wait_for_settle_event_since_uses_tail_records(tmp_path):
    telemetry = tmp_path / "telemetry.jsonl"
    telemetry.write_text(
        json.dumps(
            {
                "event": "watch-diagnostics-event",
                "project_hash": "abc",
                "action": "recheck_complete",
                "duration_ms": 999,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    records, _errors = checkpoint.load_jsonl(telemetry)
    start_len = len(records)

    assert (
        checkpoint.wait_for_settle_event_since(
            telemetry, "abc", start_len, timeout_ms=0
        )
        is False
    )

    with telemetry.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "event": "watch-diagnostics-event",
                    "project_hash": "abc",
                    "action": "recheck_complete",
                    "duration_ms": 120,
                }
            )
            + "\n"
        )

    assert (
        checkpoint.wait_for_settle_event_since(
            telemetry, "abc", start_len, timeout_ms=0
        )
        is True
    )


def test_repo_report_excludes_warmup_settle_events(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    probe = repo / "src" / "app.ts"
    probe.parent.mkdir()
    probe.write_text("const x = 1;\n", encoding="utf-8")
    digest = checkpoint.project_hash(repo.resolve())
    watch_settle_durations = iter([999, 100, 120])
    monkeypatch.setattr(checkpoint, "git_clean", lambda _repo: True)
    monkeypatch.setattr(checkpoint, "stop_project_daemon", lambda _repo: None)

    def append_record(path: Path, record: dict[str, object]) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    def fake_run_post_edit_hook(**kwargs):
        telemetry_path = kwargs["telemetry_path"]
        watch_enabled = kwargs["watch_enabled"]
        post_edit = {
            "event": "post-edit",
            "project_hash": digest,
            "status": "ok",
            "duration_ms": 10,
            "watch_diagnostics_used": watch_enabled,
            "watch_diagnostics_status": "stale" if watch_enabled else None,
        }
        append_record(telemetry_path, post_edit)
        if watch_enabled:
            append_record(
                telemetry_path,
                {
                    "event": "watch-diagnostics-event",
                    "project_hash": digest,
                    "action": "recheck_complete",
                    "duration_ms": next(watch_settle_durations),
                },
            )
        return 10, post_edit, 0

    monkeypatch.setattr(checkpoint, "run_post_edit_hook", fake_run_post_edit_hook)
    args = checkpoint.build_arg_parser().parse_args(
        [
            "--repo",
            f"r={repo}",
            "--probe-file",
            f"r={probe}",
            "--exercise-edits",
            "--baseline-iterations",
            "0",
            "--watch-iterations",
            "2",
            "--warmups",
            "1",
            "--allow-dirty",
            "--min-watch-samples",
            "1",
        ]
    )

    report = checkpoint.repo_report(
        args.repo[0],
        args=args,
        telemetry_path=tmp_path / "t.jsonl",
        probe_override=probe,
    )

    assert report["metrics"]["fresh_settle_ms"] == {
        "count": 2,
        "p50": 100,
        "p95": 120,
        "max": 120,
    }


def test_repo_report_fails_when_warmup_settle_never_arrives_before_measurement(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    probe = repo / "src" / "app.ts"
    probe.parent.mkdir()
    probe.write_text("const x = 1;\n", encoding="utf-8")
    digest = checkpoint.project_hash(repo.resolve())
    watch_calls = 0
    monkeypatch.setattr(checkpoint, "git_clean", lambda _repo: True)
    monkeypatch.setattr(checkpoint, "stop_project_daemon", lambda _repo: None)

    def append_record(path: Path, record: dict[str, object]) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    def fake_run_post_edit_hook(**kwargs):
        nonlocal watch_calls
        telemetry_path = kwargs["telemetry_path"]
        watch_enabled = kwargs["watch_enabled"]
        if watch_enabled:
            watch_calls += 1
        post_edit = {
            "event": "post-edit",
            "project_hash": digest,
            "status": "ok",
            "duration_ms": 10,
            "watch_diagnostics_used": watch_enabled,
            "watch_diagnostics_status": "stale" if watch_enabled else None,
        }
        append_record(telemetry_path, post_edit)
        if watch_calls == 2:
            append_record(
                telemetry_path,
                {
                    "event": "watch-diagnostics-event",
                    "project_hash": digest,
                    "action": "recheck_complete",
                    "duration_ms": 999,
                },
            )
            append_record(
                telemetry_path,
                {
                    "event": "watch-diagnostics-event",
                    "project_hash": digest,
                    "action": "recheck_complete",
                    "duration_ms": 100,
                },
            )
        return 10, post_edit, 0

    monkeypatch.setattr(checkpoint, "run_post_edit_hook", fake_run_post_edit_hook)
    args = checkpoint.build_arg_parser().parse_args(
        [
            "--repo",
            f"r={repo}",
            "--probe-file",
            f"r={probe}",
            "--exercise-edits",
            "--baseline-iterations",
            "0",
            "--watch-iterations",
            "1",
            "--warmups",
            "1",
            "--warmup-settle-timeout-ms",
            "0",
            "--allow-dirty",
            "--min-watch-samples",
            "1",
        ]
    )

    report = checkpoint.repo_report(
        args.repo[0],
        args=args,
        telemetry_path=tmp_path / "t.jsonl",
        probe_override=probe,
    )

    assert report["failures"] == ["warmup_settle_timeout"]
    assert watch_calls == 1


def test_watch_phase_restores_probe_once_after_accumulated_edits(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    probe = repo / "src" / "app.ts"
    probe.parent.mkdir()
    original = b"const x = 1;\n"
    probe.write_bytes(original)
    seen_contents: list[bytes] = []
    monkeypatch.setattr(checkpoint, "git_clean", lambda _repo: True)
    monkeypatch.setattr(checkpoint, "stop_project_daemon", lambda _repo: None)

    def fake_run_post_edit_hook(**kwargs):
        seen_contents.append(probe.read_bytes())
        return (
            10,
            {
                "event": "post-edit",
                "status": "ok",
                "duration_ms": 10,
                "watch_diagnostics_used": kwargs["watch_enabled"],
                "watch_diagnostics_status": (
                    "stale" if kwargs["watch_enabled"] else None
                ),
            },
            0,
        )

    monkeypatch.setattr(checkpoint, "run_post_edit_hook", fake_run_post_edit_hook)
    args = checkpoint.build_arg_parser().parse_args(
        [
            "--repo",
            f"r={repo}",
            "--probe-file",
            f"r={probe}",
            "--exercise-edits",
            "--baseline-iterations",
            "0",
            "--watch-iterations",
            "2",
            "--warmups",
            "0",
            "--allow-dirty",
            "--min-watch-samples",
            "1",
            "--min-settle-events",
            "0",
        ]
    )

    checkpoint.repo_report(
        args.repo[0],
        args=args,
        telemetry_path=tmp_path / "t.jsonl",
        probe_override=probe,
    )

    assert b"watch-0" in seen_contents[0]
    assert b"watch-0" in seen_contents[1]
    assert b"watch-1" in seen_contents[1]
    assert probe.read_bytes() == original


def test_report_counts_watch_statuses_and_settle_events_consistently(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    digest = checkpoint.project_hash(repo.resolve())
    records = [
        {
            "event": "post-edit",
            "project_hash": digest,
            "watch_diagnostics_status": "fresh",
            "watch_diagnostics_used": True,
        },
        {
            "event": "post-edit",
            "project_hash": digest,
            "watch_diagnostics_status": "stale",
            "watch_diagnostics_used": True,
        },
        {
            "event": "post-edit",
            "project_hash": digest,
            "watch_diagnostics_status": "pending",
            "watch_diagnostics_used": True,
        },
        {
            "event": "post-edit",
            "project_hash": digest,
            "watch_diagnostics_status": "fallback_required",
            "watch_diagnostics_used": False,
        },
        {
            "event": "watch-diagnostics-event",
            "project_hash": digest,
            "action": "recheck_complete",
            "duration_ms": 100,
        },
        {
            "event": "post-edit",
            "project_hash": digest,
            "error_kind": "HookError",
        },
        {
            "event": "watch-diagnostics-event",
            "project_hash": digest,
            "action": "unhealthy",
            "error_kind": "tsc_exited",
        },
    ]

    summary = checkpoint.summarize_repo_telemetry(records, digest)

    assert summary["fresh_records"] == 1
    assert summary["stale_records"] == 1
    assert summary["pending_records"] == 1
    assert summary["fallback_records"] == 1
    assert summary["watch_used_records"] == 3
    assert summary["settle_event_records"] == 1
    assert summary["runtime_errors"] == 1


def test_fail_on_threshold_exits_nonzero(tmp_path, monkeypatch):
    report = {
        "summary": {"passed": False, "failures": ["fixture:watch_hook_p50>200"]},
    }
    monkeypatch.setattr(checkpoint, "build_report", lambda _args: report)

    exit_code = checkpoint.main(
        ["--repo", f"fixture={tmp_path}", "--fail-on-threshold"]
    )

    assert exit_code == 1
