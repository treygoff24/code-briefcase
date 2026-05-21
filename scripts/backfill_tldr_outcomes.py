#!/usr/bin/env python3
"""Backfill privacy-safe TLDR outcome rollups from Codex/Claude JSONL and telemetry."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.evaluate_tldr_usage import (  # noqa: E402
    categorize_command,
    command_from_arguments,
    parse_claude_file,
    parse_codex_file,
    parse_timestamp,
    parse_tool_arguments,
    project_hash,
    resolve_claude_roots,
    telemetry_path_hash,
)
from scripts.tldr_outcome_model import (  # noqa: E402
    SessionRollup,
    TldrHookEvent,
    ToolEvent,
    UserCorrectionEvent,
    VerificationEvent,
)
from tldr.telemetry import sanitize_local_evidence  # noqa: E402

CORRECTION_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"you missed", "missed_requirement"),
    (r"wrong direction", "wrong_direction"),
    (r"stop\b", "stop_request"),
)


def iter_jsonl(path: Path) -> Iterator[tuple[int, dict[str, Any] | None, str | None]]:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_no, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                yield line_no, json.loads(text), None
            except json.JSONDecodeError as exc:
                yield line_no, None, str(exc)


def command_hash(normalized: str) -> str:
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def normalize_command_for_hash(command: str, repo_token: str = "") -> str:
    text = " ".join(str(command).split())
    if repo_token:
        text = text.replace(repo_token, "<repo>")
    return text


def hash_path_like(project: str, value: str) -> str:
    try:
        return telemetry_path_hash(project, value)
    except Exception:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item not in (None, "")]


def candidate_file_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def normalize_telemetry_path_for_rollup(project: str, project_hash_value: str, value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if project_hash_value:
        prefix = f"<redacted>/{project_hash_value}/"
        if text.startswith(prefix):
            return text.removeprefix(prefix)
    if text.startswith("<redacted>/"):
        return text.rsplit("/", 1)[-1]
    if project and not project.startswith("<redacted>/"):
        return hash_path_like(project, text)
    return text


def normalize_telemetry_paths_for_rollup(
    project: str, project_hash_value: str, values: Any
) -> list[str]:
    normalized: list[str] = []
    for value in str_list(values):
        path = normalize_telemetry_path_for_rollup(project, project_hash_value, value)
        if path:
            normalized.append(path)
    return normalized


def normalize_candidate_files_for_rollup(
    project: str, project_hash_value: str, values: Any
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in candidate_file_items(values):
        path = normalize_telemetry_path_for_rollup(
            project, project_hash_value, item.get("path")
        )
        if not path:
            continue
        entry = dict(item)
        entry["path"] = path
        normalized.append(entry)
    return normalized


def detect_user_correction(text: str) -> str | None:
    lowered = text.lower()
    for pattern, kind in CORRECTION_PATTERNS:
        if re.search(pattern, lowered):
            return kind
    return None


@dataclass
class ParsedTelemetry:
    timestamp: datetime
    client: str
    event: str
    project_hash: str
    status: str
    session_id: str | None
    duration_ms: int
    injected_bytes: int
    trigger_files: list[str]
    recommended_files: list[str]
    surfaced_files: list[str]
    candidate_files: list[dict[str, Any]]
    schema_version: int | None
    hook_run_id: str | None
    context_kind: str | None
    local_evidence: dict[str, Any] | None = None


def parse_telemetry_records(path: Path, start: datetime, end: datetime) -> list[ParsedTelemetry]:
    if not path.exists():
        return []
    records: list[ParsedTelemetry] = []
    for _, payload, _ in iter_jsonl(path):
        if payload is None:
            continue
        ts = parse_timestamp(payload.get("timestamp"))
        if ts is None or ts < start or ts >= end:
            continue
        project = str(payload.get("project") or "")
        project_hash_value = str(payload.get("project_hash") or "")
        records.append(
            ParsedTelemetry(
                timestamp=ts,
                client=str(payload.get("client") or "generic"),
                event=str(payload.get("event") or ""),
                project_hash=project_hash_value,
                status=str(payload.get("status") or "unknown"),
                session_id=payload.get("session_id"),
                duration_ms=safe_int(payload.get("duration_ms")),
                injected_bytes=safe_int(payload.get("injected_bytes")),
                trigger_files=normalize_telemetry_paths_for_rollup(
                    project, project_hash_value, payload.get("trigger_files")
                ),
                recommended_files=normalize_telemetry_paths_for_rollup(
                    project,
                    project_hash_value,
                    payload.get("recommended_related_files"),
                ),
                surfaced_files=normalize_telemetry_paths_for_rollup(
                    project, project_hash_value, payload.get("surfaced_files")
                ),
                candidate_files=normalize_candidate_files_for_rollup(
                    project, project_hash_value, payload.get("candidate_files")
                ),
                schema_version=safe_optional_int(payload.get("schema_version")),
                hook_run_id=payload.get("hook_run_id"),
                context_kind=payload.get("context_kind"),
                local_evidence=sanitize_local_evidence(payload.get("local_evidence"))
                if isinstance(payload.get("local_evidence"), dict)
                else None,
            )
        )
    return records


@dataclass
class SessionContext:
    session_id: str
    client: str
    project_hash: str
    cwd: str
    start: datetime
    end: datetime | None
    rollup: SessionRollup


def discover_backfill_sessions(
    *,
    codex_root: Path,
    claude_roots: list[Path],
    start: datetime,
    end: datetime,
) -> list[SessionContext]:
    contexts: list[SessionContext] = []
    if codex_root.exists():
        for path in codex_root.glob("sessions/**/*.jsonl"):
            summary = parse_codex_file(path, cohort="treatment")
            if summary is None or summary.start < start or summary.start >= end:
                continue
            contexts.append(
                SessionContext(
                    session_id=summary.session_id,
                    client="codex",
                    project_hash=project_hash(summary.cwd),
                    cwd=summary.cwd,
                    start=summary.start,
                    end=summary.end,
                    rollup=_rollup_from_session_summary(summary, start, end),
                )
            )
    for claude_root in claude_roots:
        if not claude_root.exists():
            continue
        for path in claude_root.glob("projects/**/*.jsonl"):
            summary = parse_claude_file(path, cohort="treatment")
            if summary is None or summary.start < start or summary.start >= end:
                continue
            contexts.append(
                SessionContext(
                    session_id=summary.session_id,
                    client="claude",
                    project_hash=project_hash(summary.cwd),
                    cwd=summary.cwd,
                    start=summary.start,
                    end=summary.end,
                    rollup=_rollup_from_session_summary(summary, start, end),
                )
            )
    return contexts


def _rollup_from_session_summary(summary, start: datetime, end: datetime) -> SessionRollup:
    rollup = SessionRollup(
        session_id=summary.session_id,
        client=summary.client,
        project_hash=project_hash(summary.cwd),
        window_start=start,
        window_end=end,
        causal_confidence="proxy-only",
        match_confidence="medium",
    )
    repo = summary.cwd
    hashed_reads = sorted(hash_path_like(repo, path) for path in summary.tools.unique_files_read)
    hashed_edits = sorted(hash_path_like(repo, path) for path in summary.tools.unique_files_edited)
    for category, count in summary.tools.by_category.items():
        for _ in range(count):
            rollup.record_tool(
                ToolEvent(
                    timestamp=summary.start,
                    category=category,
                    command_hash=command_hash(f"{category}-session"),
                    files_read=hashed_reads if category == "explore" else [],
                    files_edited=hashed_edits if category == "edit" else [],
                )
            )
    rollup.explore_before_first_edit = summary.tools.explore_before_first_edit
    rollup.repeated_file_reads = summary.rework.repeated_file_reads
    rollup.failed_tool_outputs = summary.rework.failed_commands
    if summary.rework.failed_commands:
        rollup.failure_kind_counts["failed"] = summary.rework.failed_commands
    return rollup


def _record_local_evidence(
    context: SessionContext,
    evidence: dict[str, Any],
    *,
    include_local_evidence: bool,
    max_per_session: int,
) -> None:
    if not include_local_evidence:
        return
    context.rollup.record_local_evidence(
        sanitize_local_evidence(evidence),
        limit=max_per_session,
    )


def enrich_from_raw_jsonl(
    context: SessionContext,
    path: Path,
    *,
    include_local_evidence: bool = False,
    max_local_evidence_per_session: int = 100,
) -> None:
    repo_token = context.cwd
    for _, record, _ in iter_jsonl(path):
        if record is None:
            continue
        ts = parse_timestamp(record.get("timestamp"))
        if ts is None:
            continue
        if context.client == "claude":
            message = record.get("message") or {}
            content = message.get("content")
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "text":
                        kind = detect_user_correction(str(item.get("text") or ""))
                        if kind:
                            context.rollup.record_user_correction(
                                UserCorrectionEvent(timestamp=ts, kind=kind)
                            )
                            _record_local_evidence(
                                context,
                                {
                                    "timestamp": ts.isoformat(),
                                    "source": "claude_jsonl",
                                    "kind": "user_correction",
                                    "correction_kind": kind,
                                    "text": str(item.get("text") or ""),
                                },
                                include_local_evidence=include_local_evidence,
                                max_per_session=max_local_evidence_per_session,
                            )
                    if item.get("type") == "tool_use":
                        tool_input = item.get("input") or {}
                        command = json.dumps(tool_input) if isinstance(tool_input, dict) else str(tool_input)
                        category = categorize_command(command, str(item.get("name") or ""))
                        files_read: list[str] = []
                        files_edited: list[str] = []
                        if isinstance(tool_input, dict):
                            rel = str(tool_input.get("file_path") or tool_input.get("path") or "")
                            if rel:
                                hashed = hash_path_like(context.cwd, rel)
                                if category == "edit":
                                    files_edited = [hashed]
                                else:
                                    files_read = [hashed]
                        context.rollup.record_tool(
                            ToolEvent(
                                timestamp=ts,
                                category=category,
                                command_hash=command_hash(normalize_command_for_hash(command, repo_token)),
                                files_read=files_read,
                                files_edited=files_edited,
                            )
                        )
                        _record_local_evidence(
                            context,
                            {
                                "timestamp": ts.isoformat(),
                                "source": "claude_jsonl",
                                "kind": "tool_use",
                                "tool_name": str(item.get("name") or ""),
                                "category": category,
                                "command": command,
                                "tool_input": tool_input,
                                "files_read": files_read,
                                "files_edited": files_edited,
                            },
                            include_local_evidence=include_local_evidence,
                            max_per_session=max_local_evidence_per_session,
                        )
            continue
        record_type = str(record.get("type") or "")
        if record_type == "response_item":
            payload = record.get("payload") or {}
            payload_type = str(payload.get("type") or "")
            if payload_type == "function_call_output":
                output = str(payload.get("output") or "")
                failed = "error" in output.lower() or "failed" in output.lower()
                if failed:
                    context.rollup.record_tool(
                        ToolEvent(
                            timestamp=ts,
                            category="other",
                            command_hash=command_hash("output"),
                            failed=True,
                            failure_kind="failed",
                        )
                    )
                    _record_local_evidence(
                        context,
                        {
                            "timestamp": ts.isoformat(),
                            "source": "codex_jsonl",
                            "kind": "tool_output",
                            "failed": True,
                            "output": output,
                        },
                        include_local_evidence=include_local_evidence,
                        max_per_session=max_local_evidence_per_session,
                    )
                continue
            if payload_type == "function_call":
                arguments = parse_tool_arguments(payload.get("arguments") or {})
                command = command_from_arguments(arguments)
                category = categorize_command(command, str(payload.get("name") or ""))
                files_read = [
                    hash_path_like(context.cwd, match)
                    for match in re.findall(r"([A-Za-z0-9_./-]+\.[A-Za-z0-9]+)", command)
                ]
                files_edited = []
                if category == "edit":
                    files_edited = [
                        hash_path_like(context.cwd, match.strip())
                        for match in re.findall(r"(?:Update|Add) File: ([^\n]+)", command)
                    ]
                if category == "verify":
                    context.rollup.record_verification(
                        VerificationEvent(
                            timestamp=ts,
                            command_hash=command_hash(normalize_command_for_hash(command, repo_token)),
                            passed="passed" in command.lower(),
                        )
                    )
                context.rollup.record_tool(
                    ToolEvent(
                        timestamp=ts,
                        category=category,
                        command_hash=command_hash(normalize_command_for_hash(command, repo_token)),
                        files_read=files_read,
                        files_edited=files_edited,
                    )
                )
                _record_local_evidence(
                    context,
                    {
                        "timestamp": ts.isoformat(),
                        "source": "codex_jsonl",
                        "kind": "tool_call",
                        "tool_name": str(payload.get("name") or ""),
                        "category": category,
                        "command": command,
                        "arguments": arguments,
                        "files_read": files_read,
                        "files_edited": files_edited,
                    },
                    include_local_evidence=include_local_evidence,
                    max_per_session=max_local_evidence_per_session,
                )


def match_telemetry_to_sessions(
    sessions: list[SessionContext],
    telemetry: list[ParsedTelemetry],
    *,
    include_local_evidence: bool = False,
    max_local_evidence_per_session: int = 100,
) -> dict[str, list[ParsedTelemetry]]:
    by_id = {session.session_id: session for session in sessions}
    matched: dict[str, list[ParsedTelemetry]] = {session.session_id: [] for session in sessions}

    for record in telemetry:
        target: SessionContext | None = None
        if record.session_id and record.session_id in by_id:
            candidate = by_id[record.session_id]
            if candidate.client == record.client:
                target = candidate
        if target is None:
            candidates = [
                session
                for session in sessions
                if session.client == record.client and session.project_hash == record.project_hash
            ]
            in_range = [
                session
                for session in candidates
                if session.start <= record.timestamp <= (session.end or session.start)
            ]
            if len(in_range) == 1:
                target = in_range[0]
            elif candidates:
                target = min(
                    candidates,
                    key=lambda session: abs(
                        (record.timestamp - session.start).total_seconds()
                    ),
                )
                if abs((record.timestamp - target.start).total_seconds()) > 30 * 60:
                    target = None
        if target is None:
            continue
        matched[target.session_id].append(record)
        target.rollup.match_confidence = "high"
        surfaced = len(record.surfaced_files)
        candidate_paths = [
            str(item.get("path")) for item in record.candidate_files if item.get("path")
        ] or list(record.recommended_files) or list(record.surfaced_files)
        candidate_files_surfaced = (
            sum(1 for item in record.candidate_files if item.get("surfaced"))
            if record.candidate_files
            else surfaced
        )
        target.rollup.record_hook(
            TldrHookEvent(
                timestamp=record.timestamp,
                event=record.event,
                status=record.status,
                trigger_files=record.trigger_files,
                recommended_files=record.recommended_files,
                surfaced_files=record.surfaced_files,
                candidate_files=candidate_paths,
                injected_bytes=record.injected_bytes,
                duration_ms=record.duration_ms,
                candidate_files_total=len(candidate_paths),
                candidate_files_surfaced=candidate_files_surfaced,
            )
        )
        if surfaced or record.recommended_files:
            target.rollup.attribution_confidence = "medium"
        if record.local_evidence:
            _record_local_evidence(
                target,
                {
                    "timestamp": record.timestamp.isoformat(),
                    "source": "tldr_telemetry",
                    "kind": "hook_execution",
                    "event": record.event,
                    "status": record.status,
                    "context_kind": record.context_kind,
                    "hook_run_id": record.hook_run_id,
                    **record.local_evidence,
                },
                include_local_evidence=include_local_evidence,
                max_per_session=max_local_evidence_per_session,
            )


def build_backfill_report(
    *,
    start: datetime,
    end: datetime,
    codex_root: Path,
    claude_roots: list[Path],
    telemetry_path: Path,
    include_local_evidence: bool = False,
    max_local_evidence_per_session: int = 100,
) -> dict[str, Any]:
    sessions = discover_backfill_sessions(
        codex_root=codex_root, claude_roots=claude_roots, start=start, end=end
    )
    session_paths: dict[tuple[str, str], Path] = {}
    if codex_root.exists():
        for path in codex_root.glob("sessions/**/*.jsonl"):
            summary = parse_codex_file(path, cohort="treatment")
            if summary is not None:
                session_paths[(summary.session_id, "codex")] = path
    for claude_root in claude_roots:
        for path in claude_root.glob("projects/**/*.jsonl"):
            summary = parse_claude_file(path, cohort="treatment")
            if summary is not None:
                session_paths[(summary.session_id, "claude")] = path

    for context in sessions:
        key = (context.session_id, context.client)
        raw_path = session_paths.get(key)
        if raw_path is not None:
            enrich_from_raw_jsonl(
                context,
                raw_path,
                include_local_evidence=include_local_evidence,
                max_local_evidence_per_session=max_local_evidence_per_session,
            )

    telemetry_missing = not telemetry_path.exists()
    telemetry = parse_telemetry_records(telemetry_path, start, end)
    match_telemetry_to_sessions(
        sessions,
        telemetry,
        include_local_evidence=include_local_evidence,
        max_local_evidence_per_session=max_local_evidence_per_session,
    )

    rollups = [
        context.rollup.to_dict(include_local_evidence=include_local_evidence)
        for context in sessions
    ]
    verdicts = Counter(item["verdict"] for item in rollups)
    return {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "privacy": {
            "includes_local_evidence": include_local_evidence,
            "warning": (
                "local-rich evidence may contain private project details; do not commit or share"
                if include_local_evidence
                else None
            ),
        },
        "summary": {
            "sessions": len(rollups),
            "telemetry_records": len(telemetry),
            "telemetry_missing": telemetry_missing,
            "verdict_counts": dict(verdicts),
        },
        "rollups": rollups,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill TLDR outcome rollups.")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--codex-root", default="~/.codex")
    parser.add_argument("--claude-root", action="append")
    parser.add_argument("--tldr-telemetry", default="~/.tldr/telemetry.jsonl")
    parser.add_argument("--json-out", required=True)
    parser.add_argument(
        "--include-local-evidence",
        action="store_true",
        help="Include opt-in local-rich raw evidence in the JSON report. Do not commit or share.",
    )
    parser.add_argument("--max-local-evidence-per-session", type=int, default=100)
    args = parser.parse_args()

    start = parse_timestamp(args.start)
    end = parse_timestamp(args.end)
    if start is None or end is None:
        raise SystemExit("Invalid --start or --end timestamp")

    report = build_backfill_report(
        start=start,
        end=end,
        codex_root=Path(args.codex_root).expanduser(),
        claude_roots=resolve_claude_roots(args.claude_root),
        telemetry_path=Path(args.tldr_telemetry).expanduser(),
        include_local_evidence=args.include_local_evidence,
        max_local_evidence_per_session=args.max_local_evidence_per_session,
    )
    out_path = Path(args.json_out).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
