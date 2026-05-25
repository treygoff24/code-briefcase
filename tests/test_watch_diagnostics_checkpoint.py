from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

from code_briefcase.telemetry import project_hash

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "watch_diagnostics_checkpoint.py"
SPEC = importlib.util.spec_from_file_location("watch_diagnostics_checkpoint", SCRIPT_PATH)
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
        min_watch_samples=1,
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
        telemetry_path=tmp_path / "telemetry.jsonl",
        json_out=None,
        local_rich=False,
        fail_on_threshold=False,
        min_watch_samples=1,
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
        min_watch_samples=1,
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


def test_fail_on_threshold_exits_nonzero(tmp_path, monkeypatch):
    report = {
        "summary": {"passed": False, "failures": ["fixture:watch_hook_p50>200"]},
    }
    monkeypatch.setattr(checkpoint, "build_report", lambda _args: report)

    exit_code = checkpoint.main(["--repo", f"fixture={tmp_path}", "--fail-on-threshold"])

    assert exit_code == 1
