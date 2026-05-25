#!/usr/bin/env python3
"""Real-repo checkpoint for watch-diagnostics hook latency.

Safe by default: without --exercise-edits this only reports what it would do.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import math
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

try:
    CODE_BRIEFCASE_VERSION = version("code-briefcase")
except PackageNotFoundError:
    CODE_BRIEFCASE_VERSION = "0.1.0"

ROOT = Path(__file__).resolve().parents[1]

WATCH_HOOK_STATUSES = {"fresh", "stale", "pending"}

CODE_SUFFIXES = {".ts", ".tsx", ".js", ".jsx"}
EXCLUDED_PARTS = {
    "node_modules",
    ".next",
    "dist",
    "build",
    "coverage",
    "vendor",
}


def project_hash(project: Path) -> str:
    return hashlib.sha256(str(project).encode("utf-8")).hexdigest()[:8]


@dataclass(frozen=True)
class RepoSpec:
    label: str
    path: Path


def nearest_rank(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil((percentile / 100) * len(ordered)))
    return ordered[min(len(ordered) - 1, rank - 1)]


def metric_summary(values: list[int]) -> dict[str, int | None]:
    return {
        "count": len(values),
        "p50": nearest_rank(values, 50),
        "p95": nearest_rank(values, 95),
        "max": max(values) if values else None,
    }


def watch_hook_samples(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        sample
        for sample in samples
        if sample.get("watch_diagnostics_used")
        and sample.get("watch_diagnostics_status") in WATCH_HOOK_STATUSES
    ]


def settle_durations(
    records: list[dict[str, Any]], project_digest: str | None
) -> list[int]:
    return [
        int(record["duration_ms"])
        for record in records
        if record.get("project_hash") == project_digest
        and record.get("event") == "watch-diagnostics-event"
        and record.get("action") == "recheck_complete"
        and isinstance(record.get("duration_ms"), int)
    ]


def summarize_repo_telemetry(
    records: list[dict[str, Any]], project_digest: str | None
) -> dict[str, int]:
    repo_records = [r for r in records if r.get("project_hash") == project_digest]
    return {
        "post_edit_records": sum(
            1 for r in repo_records if r.get("event") == "post-edit"
        ),
        "watch_event_records": sum(
            1 for r in repo_records if r.get("event") == "watch-diagnostics-event"
        ),
        "watch_used_records": sum(
            1 for r in repo_records if r.get("watch_diagnostics_used")
        ),
        "fresh_records": sum(
            1 for r in repo_records if r.get("watch_diagnostics_status") == "fresh"
        ),
        "stale_records": sum(
            1 for r in repo_records if r.get("watch_diagnostics_status") == "stale"
        ),
        "pending_records": sum(
            1 for r in repo_records if r.get("watch_diagnostics_status") == "pending"
        ),
        "fallback_records": sum(
            1
            for r in repo_records
            if r.get("watch_diagnostics_status") in {"fallback_required", "unhealthy"}
        ),
        "settle_event_records": len(settle_durations(repo_records, project_digest)),
        "runtime_errors": sum(
            1
            for r in repo_records
            if r.get("event") == "watch-diagnostics-event" and r.get("error_kind")
        ),
    }


def load_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    if not path.exists():
        return [], 0
    records: list[dict[str, Any]] = []
    parse_errors = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            parse_errors += 1
            continue
        if isinstance(value, dict):
            records.append(value)
    return records, parse_errors


def wait_for_settle_event_since(
    telemetry_path: Path,
    project_digest: str | None,
    start_len: int,
    *,
    timeout_ms: int,
) -> bool:
    deadline = time.monotonic() + max(0, timeout_ms) / 1000
    while True:
        records, _errors = load_jsonl(telemetry_path)
        if settle_durations(records[start_len:], project_digest):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def choose_probe_file(repo: Path) -> Path | None:
    candidates: list[Path] = []
    for path in repo.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(repo)
        parts = set(rel.parts)
        if parts & EXCLUDED_PARTS:
            continue
        if path.name.endswith(".d.ts") or ".config." in path.name:
            continue
        if path.suffix.lower() not in CODE_SUFFIXES:
            continue
        lowered = "/".join(part.lower() for part in rel.parts)
        if "secret" in lowered or "credential" in lowered or "/.env" in lowered:
            continue
        candidates.append(path)
    if not candidates:
        return None
    return sorted(
        candidates, key=lambda item: (len(item.relative_to(repo).parts), str(item))
    )[0]


def parse_repo(value: str) -> RepoSpec:
    if "=" not in value:
        raise argparse.ArgumentTypeError("--repo must be LABEL=PATH")
    label, raw_path = value.split("=", 1)
    if not label:
        raise argparse.ArgumentTypeError("--repo label cannot be empty")
    return RepoSpec(label=label, path=Path(raw_path).expanduser())


def parse_probe(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("--probe-file must be LABEL=PATH")
    label, raw_path = value.split("=", 1)
    return label, Path(raw_path).expanduser()


def git_clean(repo: Path) -> bool | None:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() == ""


def redacted_path(repo: Path, path: Path, *, local_rich: bool = False) -> str:
    if local_rich:
        return str(path.resolve())
    digest = project_hash(repo.resolve())
    try:
        rel = path.resolve().relative_to(repo.resolve())
    except ValueError:
        return f"<redacted>/{digest}/{path.name}"
    return f"<redacted>/{digest}/{rel.as_posix()}"


def append_reversible_edit(path: Path, marker: str) -> bytes:
    original = path.read_bytes()
    suffix = f"\n// code-briefcase watch checkpoint {marker}\n".encode("utf-8")
    path.write_bytes(original + suffix)
    return original


def stop_project_daemon(repo: Path) -> None:
    for command in (
        ["daemon", "watchers", "stop", "--project", str(repo), "--json"],
        ["daemon", "stop", "--project", str(repo)],
    ):
        try:
            subprocess.run(
                [sys.executable, "-m", "code_briefcase.cli", *command],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except Exception:
            continue


def remove_created_artifacts(repo: Path, preexisting: dict[Path, bool]) -> None:
    for path, existed in preexisting.items():
        if existed or not path.exists():
            continue
        if path.is_dir():
            import shutil

            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                path.unlink()
            except OSError:
                pass


def run_post_edit_hook(
    *,
    repo: Path,
    probe: Path,
    telemetry_path: Path,
    watch_enabled: bool,
    timeout: int = 45,
) -> tuple[int, dict[str, Any] | None, int]:
    env = dict(**__import__("os").environ)
    env.update(
        {
            "CODE_BRIEFCASE_TELEMETRY": "1",
            "CODE_BRIEFCASE_TELEMETRY_PATH": str(telemetry_path),
            "CODE_BRIEFCASE_WATCH_DIAGNOSTICS": "1" if watch_enabled else "0",
            "CODE_BRIEFCASE_WATCH_DIAGNOSTICS_TRUST_REPO_BINARIES": (
                "1" if watch_enabled else "0"
            ),
            "TLDR_WATCH_DIAGNOSTICS": "1" if watch_enabled else "0",
        }
    )
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(probe.relative_to(repo))},
        "cwd": str(repo),
    }
    before, _errors = load_jsonl(telemetry_path)
    started = time.perf_counter()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "code_briefcase.cli",
            "hooks",
            "run",
            "post-edit",
            "--client",
            "codex",
        ],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=repo,
        env=env,
        timeout=timeout,
        check=False,
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"hook exited {result.returncode}")
    after, errors = load_jsonl(telemetry_path)
    new_records = after[len(before) :]
    post_edit = next(
        (r for r in reversed(new_records) if r.get("event") == "post-edit"), None
    )
    return elapsed_ms, post_edit, errors


def sample_from_record(
    phase: str, iteration: int, elapsed_ms: int, record: dict[str, Any] | None
) -> dict[str, Any]:
    return {
        "phase": phase,
        "iteration": iteration,
        "hook_duration_ms": (
            int(record.get("duration_ms", elapsed_ms)) if record else elapsed_ms
        ),
        "watch_diagnostics_used": (
            bool(record.get("watch_diagnostics_used")) if record else False
        ),
        "watch_diagnostics_status": (
            record.get("watch_diagnostics_status") if record else None
        ),
        "watch_diagnostics_age_ms": (
            record.get("watch_diagnostics_age_ms") if record else None
        ),
        "watch_diagnostics_wait_ms": (
            record.get("watch_diagnostics_wait_ms") if record else None
        ),
        "status": record.get("status") if record else "unknown",
        "error_kind": record.get("error_kind") if record else None,
    }


def repo_report(
    spec: RepoSpec,
    *,
    args: argparse.Namespace,
    telemetry_path: Path,
    probe_override: Path | None,
) -> dict[str, Any]:
    repo = spec.path.expanduser().resolve()
    digest = project_hash(repo) if repo.exists() else None
    report: dict[str, Any] = {
        "label": spec.label,
        "project_hash": digest,
        "path": (
            redacted_path(repo, repo, local_rich=args.local_rich)
            if repo.exists()
            else str(repo)
        ),
        "present": repo.exists(),
        "skipped_reason": None,
        "probe": None,
        "git": {"clean_before": None, "clean_after": None},
        "metrics": {},
        "telemetry": {
            "path": (
                str(telemetry_path)
                if args.local_rich
                else f"<redacted>/{digest or 'unknown'}/{telemetry_path.name}"
            ),
            "post_edit_records": 0,
            "watch_event_records": 0,
        },
        "samples": [],
        "passed": True,
        "failures": [],
    }
    if not repo.exists():
        report["skipped_reason"] = "repo_missing"
        return report

    clean_before = git_clean(repo)
    report["git"]["clean_before"] = clean_before
    if clean_before is False and not args.allow_dirty:
        report["skipped_reason"] = "repo_dirty"
        return report

    probe = probe_override
    if probe is not None and not probe.is_absolute():
        probe = repo / probe
    probe = probe or choose_probe_file(repo)
    if probe is None:
        report["skipped_reason"] = "no_probe_file"
        return report
    created_artifacts = {
        repo / ".code-briefcase": (repo / ".code-briefcase").exists(),
        repo / ".code-briefcaseignore": (repo / ".code-briefcaseignore").exists(),
    }
    report["probe"] = {
        "file": redacted_path(repo, probe, local_rich=args.local_rich),
        "language": (
            "typescript" if probe.suffix.lower() in {".ts", ".tsx"} else "javascript"
        ),
    }
    if not args.exercise_edits:
        report["skipped_reason"] = "exercise_edits_required"
        return report

    baseline_samples: list[dict[str, Any]] = []
    watch_samples: list[dict[str, Any]] = []
    original = probe.read_bytes()
    records_before_run, _ = load_jsonl(telemetry_path)
    current_run_start_len = len(records_before_run)
    watch_measurement_start_len = current_run_start_len
    try:
        stop_project_daemon(repo)
        for index in range(args.baseline_iterations):
            saved = append_reversible_edit(probe, f"baseline-{index}")
            try:
                elapsed, record, errors = run_post_edit_hook(
                    repo=repo,
                    probe=probe,
                    telemetry_path=telemetry_path,
                    watch_enabled=False,
                )
            finally:
                probe.write_bytes(saved)
            report["telemetry"]["parse_errors"] = errors
            baseline_samples.append(
                sample_from_record("baseline", index + 1, elapsed, record)
            )

        stop_project_daemon(repo)
        records_after_baseline, _ = load_jsonl(telemetry_path)
        watch_measurement_start_len = len(records_after_baseline)
        total_watch = args.warmups + args.watch_iterations
        for index in range(total_watch):
            if index == args.warmups:
                if args.warmups:
                    wait_for_settle_event_since(
                        telemetry_path,
                        digest,
                        watch_measurement_start_len,
                        timeout_ms=args.warmup_settle_timeout_ms,
                    )
                records_before_measured_watch, _ = load_jsonl(telemetry_path)
                watch_measurement_start_len = len(records_before_measured_watch)
            append_reversible_edit(probe, f"watch-{index}")
            elapsed, record, errors = run_post_edit_hook(
                repo=repo,
                probe=probe,
                telemetry_path=telemetry_path,
                watch_enabled=True,
            )
            report["telemetry"]["parse_errors"] = errors
            sample = sample_from_record("watch", index + 1, elapsed, record)
            if index >= args.warmups:
                watch_samples.append(sample)
    except Exception as exc:
        report["passed"] = False
        report["failures"].append(str(exc))
    finally:
        stop_project_daemon(repo)
        remove_created_artifacts(repo, created_artifacts)
        probe.write_bytes(original)
        report["git"]["clean_after"] = git_clean(repo)

    samples = baseline_samples + watch_samples
    report["samples"] = samples
    watch_used_samples = watch_hook_samples(watch_samples)
    records, _errors = load_jsonl(telemetry_path)
    current_records = records[current_run_start_len:]
    measured_watch_records = records[watch_measurement_start_len:]
    settle_ms = settle_durations(measured_watch_records, digest)
    report["metrics"] = {
        "sync_hook_ms": metric_summary(
            [int(s["hook_duration_ms"]) for s in baseline_samples]
        ),
        "watch_hook_ms": metric_summary(
            [int(s["hook_duration_ms"]) for s in watch_used_samples]
        ),
        "fresh_settle_ms": metric_summary(settle_ms),
    }
    report["metrics"]["delta"] = {
        "hook_p50_ms": _delta(
            report["metrics"]["watch_hook_ms"]["p50"],
            report["metrics"]["sync_hook_ms"]["p50"],
        ),
        "hook_p95_ms": _delta(
            report["metrics"]["watch_hook_ms"]["p95"],
            report["metrics"]["sync_hook_ms"]["p95"],
        ),
    }
    report["telemetry"].update(summarize_repo_telemetry(current_records, digest))
    _apply_thresholds(report, args)
    return report


def _delta(left: int | None, right: int | None) -> int | None:
    if left is None or right is None:
        return None
    return left - right


def _apply_thresholds(report: dict[str, Any], args: argparse.Namespace) -> None:
    if report.get("skipped_reason"):
        return
    failures: list[str] = report["failures"]
    if failures:
        report["passed"] = False
        return
    watch_metrics = report["metrics"].get("watch_hook_ms", {})
    settle_metrics = report["metrics"].get("fresh_settle_ms", {})
    if watch_metrics.get("count", 0) < args.min_watch_samples:
        failures.append("insufficient_watch_hook_samples")
    if settle_metrics.get("count", 0) < args.min_settle_events:
        failures.append("insufficient_settle_events")
    if report.get("telemetry", {}).get("runtime_errors", 0) > 0:
        failures.append("watcher_runtime_errors")
    if watch_metrics.get("p50") is not None and watch_metrics["p50"] > args.hook_p50_ms:
        failures.append(f"watch_hook_p50>{args.hook_p50_ms}")
    if watch_metrics.get("p95") is not None and watch_metrics["p95"] > args.hook_p95_ms:
        failures.append(f"watch_hook_p95>{args.hook_p95_ms}")
    if (
        settle_metrics.get("p50") is not None
        and settle_metrics["p50"] > args.settle_p50_ms
    ):
        failures.append(f"settle_p50>{args.settle_p50_ms}")
    if (
        settle_metrics.get("p95") is not None
        and settle_metrics["p95"] > args.settle_p95_ms
    ):
        failures.append(f"settle_p95>{args.settle_p95_ms}")
    report["passed"] = not failures


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", action="append", type=parse_repo, default=[])
    parser.add_argument("--probe-file", action="append", type=parse_probe, default=[])
    parser.add_argument("--exercise-edits", action="store_true")
    parser.add_argument("--baseline-iterations", type=int, default=5)
    parser.add_argument("--watch-iterations", type=int, default=10)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--warmup-settle-timeout-ms", type=int, default=15000)
    parser.add_argument(
        "--telemetry-path",
        type=Path,
        default=Path("reports/watch-diagnostics-telemetry.jsonl"),
    )
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--local-rich", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--fail-on-threshold", action="store_true")
    parser.add_argument("--hook-p50-ms", type=int, default=200)
    parser.add_argument("--hook-p95-ms", type=int, default=500)
    parser.add_argument("--settle-p50-ms", type=int, default=600)
    parser.add_argument("--settle-p95-ms", type=int, default=2000)
    parser.add_argument("--min-watch-samples", type=int, default=5)
    parser.add_argument("--min-settle-events", type=int, default=1)
    return parser


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    telemetry_path = args.telemetry_path.expanduser().resolve()
    probe_overrides = {label: path for label, path in args.probe_file}
    repos = args.repo or []
    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "tool": "watch_diagnostics_checkpoint",
        "code_briefcase_version": CODE_BRIEFCASE_VERSION,
        "config": {
            "baseline_iterations": args.baseline_iterations,
            "watch_iterations": args.watch_iterations,
            "warmups": args.warmups,
            "warmup_settle_timeout_ms": args.warmup_settle_timeout_ms,
            "exercise_edits": bool(args.exercise_edits),
            "redacted": not args.local_rich,
        },
        "thresholds": {
            "hook_response_p50_ms": args.hook_p50_ms,
            "hook_response_p95_ms": args.hook_p95_ms,
            "fresh_settle_p50_ms": args.settle_p50_ms,
            "fresh_settle_p95_ms": args.settle_p95_ms,
            "min_watch_samples": args.min_watch_samples,
            "min_settle_events": args.min_settle_events,
        },
        "summary": {
            "passed": True,
            "repos_present": 0,
            "repos_checked": 0,
            "failures": [],
        },
        "repos": [],
    }
    for spec in repos:
        item = repo_report(
            spec,
            args=args,
            telemetry_path=telemetry_path,
            probe_override=probe_overrides.get(spec.label),
        )
        report["repos"].append(item)
    report["summary"]["repos_present"] = sum(
        1 for item in report["repos"] if item["present"]
    )
    report["summary"]["repos_checked"] = sum(
        1 for item in report["repos"] if not item["skipped_reason"]
    )
    failures = [
        f"{item['label']}:{failure}"
        for item in report["repos"]
        for failure in item.get("failures", [])
    ]
    report["summary"]["failures"] = failures
    report["summary"]["passed"] = not failures
    return report


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    report = build_report(args)
    payload = json.dumps(report, indent=2, sort_keys=True)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(payload + "\n", encoding="utf-8")
        print(f"wrote {args.json_out}")
    else:
        print(payload)
    if args.fail_on_threshold and not report["summary"]["passed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
