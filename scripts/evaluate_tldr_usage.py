#!/usr/bin/env python3
"""Evaluate Code Briefcase hook efficacy from local Codex/Claude logs and optional telemetry."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

MIN_COMPARABLE_SESSIONS = 20
DEFAULT_CLAUDE_ROOTS = (
    "~/.claude",
    "~/.claude-work",
    "~/.claude-personal",
    "~/.claude-space",
)
TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)

EXPLORE_PATTERNS = (
    r"\brg\b",
    r"\bgrep\b",
    r"\bfind\b",
    r"\bls\b",
    r"\bsed\b",
    r"\bcat\b",
    r"\bread\b",
    r"\bopen\b",
)
EDIT_PATTERNS = (r"\bapply_patch\b", r"\bwrite\b", r"\bedit\b", r"\bpython\b.*\.py")
VERIFY_PATTERNS = (
    r"\bpytest\b",
    r"\bnpm test\b",
    r"\bpnpm\b",
    r"\bruff\b",
    r"\bmypy\b",
    r"\bnpm run\b",
)
GIT_PATTERNS = (
    r"\bgit status\b",
    r"\bgit diff\b",
    r"\bgit log\b",
    r"\bgit add\b",
    r"\bgit commit\b",
)
CODE_BRIEFCASE_PATTERNS = (
    r"\btldr\b",
    r"\bcode-briefcase-mcp\b",
    r"Code Briefcase",
    r"hook_success",
)


@dataclass
class TokenTotals:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0

    @property
    def non_cached_input_tokens(self) -> int:
        return max(0, self.input_tokens - self.cached_input_tokens)


@dataclass
class ToolMetrics:
    total_calls: int = 0
    by_category: Counter = field(default_factory=Counter)
    repeated_calls: int = 0
    unique_files_read: set[str] = field(default_factory=set)
    unique_files_edited: set[str] = field(default_factory=set)
    explore_before_first_edit: int = 0
    first_edit_ts: datetime | None = None
    first_verify_ts: datetime | None = None
    _seen_commands: list[str] = field(default_factory=list)
    _normalized_commands: list[str] = field(default_factory=list)

    def record_command(
        self, command: str, category: str, ts: datetime | None, repo_token: str
    ) -> None:
        normalized = normalize_command(command, repo_token)
        if normalized in self._normalized_commands:
            self.repeated_calls += 1
        self._normalized_commands.append(normalized)
        self.total_calls += 1
        self.by_category[category] += 1
        if category == "explore" and self.first_edit_ts is None:
            self.explore_before_first_edit += 1
        if category == "edit" and self.first_edit_ts is None and ts is not None:
            self.first_edit_ts = ts
        if category == "verify" and self.first_verify_ts is None and ts is not None:
            self.first_verify_ts = ts

    def record_file_read(self, path: str) -> None:
        self.unique_files_read.add(path)

    def record_file_edit(self, path: str) -> None:
        self.unique_files_edited.add(path)


@dataclass
class ReworkMetrics:
    repeated_file_reads: int = 0
    failed_commands: int = 0
    patch_attempts: int = 0
    verification_reruns: int = 0
    _file_read_counts: Counter = field(default_factory=Counter)


@dataclass
class SessionSummary:
    session_id: str
    client: str
    cwd: str
    start: datetime
    end: datetime | None
    cohort: str
    model: str | None = None
    cli_version: str | None = None
    turns: int = 0
    tokens: TokenTotals = field(default_factory=TokenTotals)
    tools: ToolMetrics = field(default_factory=ToolMetrics)
    rework: ReworkMetrics = field(default_factory=ReworkMetrics)
    tldr_hook_events: int = 0
    parse_errors: int = 0
    unknown_records: int = 0

    @property
    def day(self) -> str:
        return self.start.date().isoformat()


@dataclass
class TelemetryRecord:
    timestamp: datetime
    client: str
    event: str
    project: str
    project_hash: str
    duration_ms: int
    status: str
    error_kind: str | None
    injected_bytes: int
    trigger_files: list[str]
    recommended_related_files: list[str]
    surfaced_files: list[str]
    diagnostics_count: int
    daemon_state: str | None
    noop_reason: str | None
    session_id: str | None = None
    schema_version: int | None = None
    candidate_files: list[dict[str, Any]] = field(default_factory=list)
    hook_run_id: str | None = None
    context_kind: str | None = None


def parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    errors = 0
    if not path.exists():
        return records, errors
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            errors += 1
    return records, errors


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


def iter_session_files(root: Path, patterns: Iterable[str]) -> Iterator[Path]:
    if not root.exists():
        return
    for pattern in patterns:
        yield from root.glob(pattern)


def session_path_may_overlap_window(path: Path, start: datetime, end: datetime) -> bool:
    """Cheaply skip dated archive files that are clearly outside the report window."""
    dates = []
    for year, month, day in re.findall(
        r"(?<!\d)(20\d{2})[-/](\d{2})[-/](\d{2})(?!\d)", path.as_posix()
    ):
        try:
            dates.append(
                datetime(int(year), int(month), int(day), tzinfo=timezone.utc).date()
            )
        except ValueError:
            continue
    if not dates:
        return True
    start_day = (start - timedelta(days=1)).date()
    end_day = (end + timedelta(days=1)).date()
    return any(start_day <= value <= end_day for value in dates)


def resolve_claude_roots(values: list[str] | None) -> list[Path]:
    raw_roots = values or list(DEFAULT_CLAUDE_ROOTS)
    roots: list[Path] = []
    seen: set[Path] = set()
    for raw in raw_roots:
        for item in str(raw).split(","):
            item = item.strip()
            if not item:
                continue
            root = Path(item).expanduser()
            try:
                key = root.resolve()
            except Exception:
                key = root
            if key in seen:
                continue
            seen.add(key)
            roots.append(root)
    return roots


def normalize_command(command: str, repo_token: str) -> str:
    text = " ".join(str(command).split())
    if repo_token:
        text = text.replace(repo_token, "<repo>")
    return text


def project_hash(project: str | Path) -> str:
    return hashlib.sha256(str(project).encode("utf-8")).hexdigest()[:8]


def path_key(project: str | Path, value: str) -> str:
    project_path = Path(project).expanduser()
    try:
        project_path = project_path.resolve()
    except Exception:
        pass
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = project_path / path
    try:
        resolved = path.resolve()
    except Exception:
        resolved = path
    try:
        return str(resolved.relative_to(project_path)).replace("\\", "/")
    except Exception:
        return str(resolved).replace("\\", "/")


def telemetry_path_hash(project: str | Path, value: str) -> str:
    return hashlib.sha256(path_key(project, value).encode("utf-8")).hexdigest()[:12]


def apply_token_count(tokens: TokenTotals, payload: dict[str, Any]) -> None:
    incoming = {field: safe_int(payload.get(field)) for field in TOKEN_FIELDS}
    if tokens.total_tokens and incoming["total_tokens"] >= tokens.total_tokens:
        for field in TOKEN_FIELDS:
            setattr(tokens, field, incoming[field])
        return
    for field in TOKEN_FIELDS:
        setattr(tokens, field, getattr(tokens, field) + incoming[field])


def apply_cumulative_token_count(tokens: TokenTotals, payload: dict[str, Any]) -> None:
    for token_field in TOKEN_FIELDS:
        setattr(
            tokens,
            token_field,
            max(getattr(tokens, token_field), safe_int(payload.get(token_field))),
        )


def extract_token_usage(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the best available token usage object from a Codex token_count event."""
    info = payload.get("info")
    if isinstance(info, dict):
        total_usage = info.get("total_token_usage")
        if isinstance(total_usage, dict):
            return total_usage
    return payload


def token_usage_is_cumulative(payload: dict[str, Any]) -> bool:
    info = payload.get("info")
    return isinstance(info, dict) and isinstance(info.get("total_token_usage"), dict)


def parse_tool_arguments(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return {"command": arguments}
        if isinstance(parsed, dict):
            return parsed
    return {}


def command_from_arguments(arguments: dict[str, Any]) -> str:
    command = arguments.get("command") or arguments.get("cmd")
    if command:
        return str(command)
    return json.dumps(arguments, sort_keys=True)


def categorize_command(command: str, tool_name: str | None = None) -> str:
    haystack = f"{tool_name or ''} {command}".lower()
    for pattern in CODE_BRIEFCASE_PATTERNS:
        if re.search(pattern, haystack, re.IGNORECASE):
            return "code-briefcase"
    for pattern in EDIT_PATTERNS:
        if re.search(pattern, haystack, re.IGNORECASE):
            return "edit"
    for pattern in VERIFY_PATTERNS:
        if re.search(pattern, haystack, re.IGNORECASE):
            return "verify"
    for pattern in GIT_PATTERNS:
        if re.search(pattern, haystack, re.IGNORECASE):
            return "git"
    for pattern in EXPLORE_PATTERNS:
        if re.search(pattern, haystack, re.IGNORECASE):
            return "explore"
    if tool_name and tool_name.lower() in {"read", "grep", "glob", "list"}:
        return "explore"
    if tool_name and tool_name.lower() in {"edit", "write", "apply_patch", "multiedit"}:
        return "edit"
    return "other"


def parse_codex_file(path: Path, cohort: str) -> SessionSummary | None:
    records, parse_errors = load_jsonl(path)
    if not records:
        return None
    session_id = path.stem
    cwd = "."
    model = None
    cli_version = None
    start: datetime | None = None
    end: datetime | None = None
    tokens = TokenTotals()
    tools = ToolMetrics()
    rework = ReworkMetrics()
    tldr_hooks = 0
    unknown = 0
    turns = 0
    repo_token = ""

    for record in records:
        ts = parse_timestamp(record.get("timestamp"))
        if ts is not None:
            start = ts if start is None or ts < start else start
            end = ts if end is None or ts > end else end
        record_type = str(record.get("type") or "")
        if record_type == "session_meta":
            payload = record.get("payload") or {}
            session_id = str(payload.get("id") or session_id)
            cwd = str(payload.get("cwd") or cwd)
            model = payload.get("model") or model
            cli_version = payload.get("cli_version") or cli_version
            repo_token = cwd
            continue
        if record_type == "event_msg":
            payload = record.get("payload") or {}
            if payload.get("type") == "task_started":
                turns += 1
            if payload.get("type") == "token_count":
                usage = extract_token_usage(payload)
                if token_usage_is_cumulative(payload):
                    apply_cumulative_token_count(tokens, usage)
                else:
                    apply_token_count(tokens, usage)
            continue
        if record_type == "response_item":
            payload = record.get("payload") or {}
            payload_type = str(payload.get("type") or "")
            if payload_type == "function_call_output":
                output = str(payload.get("output") or "")
                if "error" in output.lower() or "failed" in output.lower():
                    rework.failed_commands += 1
                if "code-briefcase" in output.lower() or "hook" in output.lower():
                    tldr_hooks += 1
                continue
            if payload_type != "function_call":
                unknown += 1
                continue
            name = str(payload.get("name") or "")
            arguments = parse_tool_arguments(payload.get("arguments") or {})
            command = command_from_arguments(arguments)
            category = categorize_command(command, name)
            tools.record_command(command, category, ts, repo_token)
            if category == "edit":
                rework.patch_attempts += 1
                for match in re.findall(r"(?:Update|Add) File: ([^\n]+)", command):
                    tools.record_file_edit(match.strip())
            if category == "explore":
                for match in re.findall(r"([A-Za-z0-9_./-]+\.[A-Za-z0-9]+)", command):
                    tools.record_file_read(match)
                    rework._file_read_counts[match] += 1
            continue
        unknown += 1

    rework.repeated_file_reads = sum(
        count - 1 for count in rework._file_read_counts.values() if count > 1
    )
    if start is None:
        return None
    return SessionSummary(
        session_id=session_id,
        client="codex",
        cwd=cwd,
        start=start,
        end=end,
        cohort=cohort,
        model=model,
        cli_version=cli_version,
        turns=turns,
        tokens=tokens,
        tools=tools,
        rework=rework,
        tldr_hook_events=tldr_hooks,
        parse_errors=parse_errors,
        unknown_records=unknown,
    )


def _claude_tool_uses(record: dict[str, Any]) -> list[dict[str, Any]]:
    message = record.get("message") or {}
    content = message.get("content")
    if not isinstance(content, list):
        return []
    uses = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "tool_use":
            uses.append(item)
    return uses


def parse_claude_file(path: Path, cohort: str) -> SessionSummary | None:
    records, parse_errors = load_jsonl(path)
    if not records:
        return None
    session_id = path.stem
    cwd = "."
    start: datetime | None = None
    end: datetime | None = None
    tools = ToolMetrics()
    rework = ReworkMetrics()
    tldr_hooks = 0
    unknown = 0
    turns = 0
    repo_token = ""

    for record in records:
        ts = parse_timestamp(record.get("timestamp"))
        if ts is not None:
            start = ts if start is None or ts < start else start
            end = ts if end is None or ts > end else end
        session_id = str(
            record.get("sessionId") or record.get("session_id") or session_id
        )
        cwd = str(record.get("cwd") or record.get("project_dir") or cwd)
        repo_token = cwd
        attachment = record.get("attachment")
        record_type = record.get("type")
        if isinstance(attachment, dict) and attachment.get("type") == "hook_success":
            tldr_hooks += 1
        elif record_type == "user":
            turns += 1
        elif record_type == "assistant":
            pass
        elif record_type is None:
            unknown += 1
        else:
            unknown += 1
        for tool in _claude_tool_uses(record):
            name = str(tool.get("name") or "")
            tool_input = tool.get("input") or {}
            command = (
                json.dumps(tool_input)
                if isinstance(tool_input, dict)
                else str(tool_input)
            )
            category = categorize_command(command, name)
            tools.record_command(command, category, ts, repo_token)
            if category == "edit":
                rework.patch_attempts += 1
                file_path = ""
                if isinstance(tool_input, dict):
                    file_path = str(
                        tool_input.get("file_path") or tool_input.get("path") or ""
                    )
                if file_path:
                    tools.record_file_edit(file_path)
            if category == "explore":
                if isinstance(tool_input, dict):
                    file_path = str(
                        tool_input.get("file_path") or tool_input.get("path") or ""
                    )
                    if file_path:
                        tools.record_file_read(file_path)
                        rework._file_read_counts[file_path] += 1

    rework.repeated_file_reads = sum(
        count - 1 for count in rework._file_read_counts.values() if count > 1
    )
    if start is None:
        return None
    return SessionSummary(
        session_id=session_id,
        client="claude",
        cwd=cwd,
        start=start,
        end=end,
        cohort=cohort,
        turns=turns,
        tools=tools,
        rework=rework,
        tldr_hook_events=tldr_hooks,
        parse_errors=parse_errors,
        unknown_records=unknown,
    )


def parse_telemetry_file(path: Path) -> list[TelemetryRecord]:
    records, _ = load_jsonl(path)
    parsed: list[TelemetryRecord] = []
    for record in records:
        ts = parse_timestamp(record.get("timestamp"))
        if ts is None:
            continue
        schema_version = safe_optional_int(record.get("schema_version"))
        parsed.append(
            TelemetryRecord(
                timestamp=ts,
                client=str(record.get("client") or "generic"),
                event=str(record.get("event") or ""),
                project=str(record.get("project") or ""),
                project_hash=str(record.get("project_hash") or ""),
                duration_ms=safe_int(record.get("duration_ms")),
                status=str(record.get("status") or "unknown"),
                error_kind=record.get("error_kind"),
                injected_bytes=safe_int(record.get("injected_bytes")),
                trigger_files=str_list(record.get("trigger_files")),
                recommended_related_files=str_list(
                    record.get("recommended_related_files")
                ),
                surfaced_files=str_list(record.get("surfaced_files")),
                diagnostics_count=safe_int(record.get("diagnostics_count")),
                daemon_state=record.get("daemon_state"),
                noop_reason=record.get("noop_reason"),
                session_id=record.get("session_id"),
                schema_version=schema_version,
                candidate_files=candidate_file_items(record.get("candidate_files")),
                hook_run_id=record.get("hook_run_id"),
                context_kind=record.get("context_kind"),
            )
        )
    return parsed


def _normalize_path_key(path: str) -> str:
    return str(Path(path)).replace("\\", "/").lstrip("./")


def path_context_hit(trigger: str, later_reads: set[str]) -> bool:
    key = _normalize_path_key(trigger)
    if not key:
        return False
    for item in later_reads:
        normalized = _normalize_path_key(item)
        if key == normalized:
            return True
        if normalized.endswith("/" + key):
            return True
    return False


def redacted_path_context_hit(
    trigger: str,
    *,
    session_project: str,
    telemetry_project_hash: str,
    later_reads: set[str],
) -> bool:
    prefix = f"<redacted>/{telemetry_project_hash}/"
    if not telemetry_project_hash or not trigger.startswith(prefix):
        return False
    target_hash = trigger.removeprefix(prefix)
    if not target_hash:
        return False
    return any(
        telemetry_path_hash(session_project, item) == target_hash
        for item in later_reads
    )


def telemetry_context_hit(
    trigger: str,
    *,
    session: SessionSummary,
    record: TelemetryRecord,
    later_reads: set[str],
) -> bool:
    if trigger.startswith("<redacted>/"):
        return redacted_path_context_hit(
            trigger,
            session_project=normalize_cwd(session.cwd),
            telemetry_project_hash=record.project_hash,
            later_reads=later_reads,
        )
    return path_context_hit(trigger, later_reads)


def session_matches_telemetry_project(
    session: SessionSummary, record: TelemetryRecord
) -> bool:
    session_project = normalize_cwd(session.cwd)
    record_project = normalize_cwd(record.project)
    if session_project == record_project:
        return True
    if record.project.startswith("<redacted>/"):
        return project_hash(session_project) == record.project_hash
    if record.project_hash:
        return project_hash(session_project) == record.project_hash
    return False


def normalize_cwd(cwd: str) -> str:
    path = Path(cwd).expanduser()
    try:
        return str(path.resolve())
    except Exception:
        return str(path)


def assign_cohort(
    ts: datetime,
    baseline_start: datetime,
    baseline_end: datetime,
    treatment_end: datetime,
) -> str | None:
    if baseline_start <= ts < baseline_end:
        return "baseline"
    if baseline_end <= ts < treatment_end:
        return "treatment"
    return None


def discover_sessions(
    *,
    codex_root: Path,
    claude_roots: Iterable[Path],
    baseline_start: datetime,
    baseline_end: datetime,
    treatment_end: datetime,
) -> list[SessionSummary]:
    sessions: list[SessionSummary] = []
    seen_codex_files: set[Path] = set()
    for path in iter_session_files(
        codex_root, ("sessions/**/*.jsonl", "archived_sessions/**/*.jsonl")
    ):
        if not session_path_may_overlap_window(path, baseline_start, treatment_end):
            continue
        try:
            key = path.resolve()
        except Exception:
            key = path
        if key in seen_codex_files:
            continue
        seen_codex_files.add(key)
        summary = parse_codex_file(path, cohort="baseline")
        if summary is None:
            continue
        cohort = assign_cohort(
            summary.start, baseline_start, baseline_end, treatment_end
        )
        if cohort is None:
            continue
        summary.cohort = cohort
        sessions.append(summary)
    seen_claude_files: set[Path] = set()
    for claude_root in claude_roots:
        for path in iter_session_files(claude_root, ("projects/**/*.jsonl",)):
            try:
                key = path.resolve()
            except Exception:
                key = path
            if key in seen_claude_files:
                continue
            seen_claude_files.add(key)
            summary = parse_claude_file(path, cohort="baseline")
            if summary is None:
                continue
            cohort = assign_cohort(
                summary.start, baseline_start, baseline_end, treatment_end
            )
            if cohort is None:
                continue
            summary.cohort = cohort
            sessions.append(summary)
    return sessions


def match_telemetry(
    sessions: list[SessionSummary], telemetry: list[TelemetryRecord]
) -> tuple[list[TelemetryRecord], list[TelemetryRecord], dict[str, Any]]:
    by_session = {session.session_id: session for session in sessions}
    matched: list[TelemetryRecord] = []
    unmatched: list[TelemetryRecord] = []
    hit_stats = {
        "trigger_hits": 0,
        "trigger_total": 0,
        "recommended_hits": 0,
        "recommended_total": 0,
    }

    for record in telemetry:
        session = None
        if record.session_id and record.session_id in by_session:
            session = by_session[record.session_id]
        if session is None:
            for candidate in sessions:
                if candidate.client != record.client:
                    continue
                if not session_matches_telemetry_project(candidate, record):
                    continue
                if (
                    candidate.start
                    <= record.timestamp
                    <= (candidate.end or candidate.start)
                ):
                    session = candidate
                    break
        if session is None:
            unmatched.append(record)
            continue
        matched.append(record)
        session.tldr_hook_events += 1
        later_reads = (
            session.tools.unique_files_read | session.tools.unique_files_edited
        )
        for path in record.trigger_files:
            hit_stats["trigger_total"] += 1
            if telemetry_context_hit(
                path, session=session, record=record, later_reads=later_reads
            ):
                hit_stats["trigger_hits"] += 1
        for path in record.recommended_related_files:
            hit_stats["recommended_total"] += 1
            if telemetry_context_hit(
                path, session=session, record=record, later_reads=later_reads
            ):
                hit_stats["recommended_hits"] += 1
    return matched, unmatched, hit_stats


def median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def ratio_delta(baseline: float | None, treatment: float | None) -> str:
    if baseline is None or baseline == 0 or treatment is None:
        return "n/a"
    delta = (treatment - baseline) / baseline
    return f"{delta:+.1%}"


def cohort_sessions(
    sessions: list[SessionSummary], client: str, cohort: str
) -> list[SessionSummary]:
    return [
        session
        for session in sessions
        if session.client == client and session.cohort == cohort
    ]


def verdict_for(
    sessions: list[SessionSummary],
    telemetry: list[TelemetryRecord],
    *,
    has_annotations: bool,
) -> str:
    baseline = [session for session in sessions if session.cohort == "baseline"]
    treatment = [session for session in sessions if session.cohort == "treatment"]
    if (
        len(baseline) < MIN_COMPARABLE_SESSIONS
        or len(treatment) < MIN_COMPARABLE_SESSIONS
    ):
        return "insufficient data"
    if not has_annotations and not telemetry:
        return "proxy-only"
    base_explore = median(
        [session.tools.by_category["explore"] for session in baseline]
    )
    treat_explore = median(
        [session.tools.by_category["explore"] for session in treatment]
    )
    base_tokens = median(
        [
            session.tokens.total_tokens
            for session in baseline
            if session.tokens.total_tokens
        ]
    )
    treat_tokens = median(
        [
            session.tokens.total_tokens
            for session in treatment
            if session.tokens.total_tokens
        ]
    )
    hook_errors = sum(1 for record in telemetry if record.status == "error")
    if hook_errors and hook_errors / max(1, len(telemetry)) > 0.1:
        return "harmful"
    if (
        base_explore is not None
        and treat_explore is not None
        and treat_explore < base_explore * 0.9
    ):
        if (
            base_tokens is None
            or treat_tokens is None
            or treat_tokens <= base_tokens * 1.05
        ):
            return "helpful"
    if (
        base_tokens is not None
        and treat_tokens is not None
        and treat_tokens > base_tokens * 1.15
    ):
        return "harmful"
    return "neutral"


def render_markdown(
    sessions: list[SessionSummary],
    telemetry: list[TelemetryRecord],
    unmatched_telemetry: list[TelemetryRecord],
    hit_stats: dict[str, Any],
    *,
    baseline_start: datetime,
    baseline_end: datetime,
    treatment_end: datetime,
    has_annotations: bool,
) -> str:
    lines = [
        "# Code Briefcase efficacy report",
        "",
        f"- Baseline window: `{baseline_start.isoformat()}` → `{baseline_end.isoformat()}`",
        f"- Treatment window: `{baseline_end.isoformat()}` → `{treatment_end.isoformat()}`",
        f"- Sessions parsed: {len(sessions)}",
        f"- Telemetry records: {len(telemetry)} (unmatched: {len(unmatched_telemetry)})",
        "",
        "## Verdict",
        "",
        f"**{verdict_for(sessions, telemetry, has_annotations=has_annotations)}**",
        "",
        "_Historical before/after comparisons are proxy-only and not causal._",
        "",
    ]
    for client in ("codex", "claude"):
        lines.extend(_client_section(sessions, client))
    lines.extend(_repo_table(sessions))
    lines.extend(_daily_trends(sessions))
    lines.extend(_top_sessions(sessions))
    lines.extend(_telemetry_section(telemetry, hit_stats))
    lines.extend(_recommendations(sessions, telemetry))
    return "\n".join(lines) + "\n"


def _client_section(sessions: list[SessionSummary], client: str) -> list[str]:
    base = cohort_sessions(sessions, client, "baseline")
    treat = cohort_sessions(sessions, client, "treatment")
    lines = [
        f"## {client.title()}",
        "",
        f"- Baseline sessions: {len(base)}",
        f"- Treatment sessions: {len(treat)}",
    ]
    if client == "codex":
        lines.append(
            f"- Median total tokens: baseline={median([s.tokens.total_tokens for s in base])}, "
            f"treatment={median([s.tokens.total_tokens for s in treat])} "
            f"({ratio_delta(median([s.tokens.total_tokens for s in base]), median([s.tokens.total_tokens for s in treat]))})"
        )
    else:
        lines.append("- Token metrics: unknown (not present in local Claude logs)")
    lines.append(
        f"- Median explore tool calls: baseline={median([s.tools.by_category['explore'] for s in base])}, "
        f"treatment={median([s.tools.by_category['explore'] for s in treat])}"
    )
    lines.append("")
    return lines


def _repo_table(sessions: list[SessionSummary]) -> list[str]:
    lines = [
        "## Per-repo breakdown",
        "",
        "| repo | baseline sessions | treatment sessions |",
        "| --- | ---: | ---: |",
    ]
    repos: dict[str, Counter] = defaultdict(Counter)
    for session in sessions:
        repos[normalize_cwd(session.cwd)][session.cohort] += 1
    for repo, counts in sorted(repos.items()):
        lines.append(f"| `{repo}` | {counts['baseline']} | {counts['treatment']} |")
    lines.append("")
    return lines


def _daily_trends(sessions: list[SessionSummary]) -> list[str]:
    lines = [
        "## Per-day trends",
        "",
        "| day | cohort | sessions | median tool calls |",
        "| --- | --- | ---: | ---: |",
    ]
    buckets: dict[tuple[str, str], list[SessionSummary]] = defaultdict(list)
    for session in sessions:
        buckets[(session.day, session.cohort)].append(session)
    for (day, cohort), items in sorted(buckets.items()):
        med = median([float(item.tools.total_calls) for item in items])
        lines.append(
            f"| {day} | {cohort} | {len(items)} | {med if med is not None else 'n/a'} |"
        )
    lines.append("")
    return lines


def _top_sessions(sessions: list[SessionSummary]) -> list[str]:
    lines = ["## Highest-cost sessions", ""]
    ranked = sorted(
        sessions, key=lambda session: session.tokens.total_tokens, reverse=True
    )[:10]
    for session in ranked:
        lines.append(
            f"- `{session.session_id}` ({session.client}, {session.cohort}): "
            f"tokens={session.tokens.total_tokens}, tools={session.tools.total_calls}"
        )
    lines.append("")
    lines.extend(["## Most repeated-read sessions", ""])
    ranked_reads = sorted(
        sessions, key=lambda session: session.rework.repeated_file_reads, reverse=True
    )[:10]
    for session in ranked_reads:
        lines.append(
            f"- `{session.session_id}` ({session.client}, {session.cohort}): "
            f"repeated_reads={session.rework.repeated_file_reads}"
        )
    lines.append("")
    return lines


def _telemetry_section(
    telemetry: list[TelemetryRecord], hit_stats: dict[str, Any]
) -> list[str]:
    if not telemetry:
        return [
            "## Code Briefcase hook reliability",
            "",
            "_No telemetry found (proxy-only from agent logs)._",
            "",
        ]
    durations = [record.duration_ms for record in telemetry]
    statuses = Counter(record.status for record in telemetry)
    lines = [
        "## Code Briefcase hook reliability",
        "",
        f"- Records: {len(telemetry)}",
        f"- Status counts: {dict(statuses)}",
        f"- Duration p50/p95 (ms): {median([float(d) for d in durations])}/"
        f"{sorted(durations)[int(max(0, len(durations) * 0.95 - 1))] if durations else 'n/a'}",
        f"- Injected bytes (median): {median([float(record.injected_bytes) for record in telemetry])}",
        f"- Context hit rate (approximate): trigger={hit_stats['trigger_hits']}/{hit_stats['trigger_total']}, "
        f"recommended={hit_stats['recommended_hits']}/{hit_stats['recommended_total']}",
        "",
    ]
    return lines


def _recommendations(
    sessions: list[SessionSummary], telemetry: list[TelemetryRecord]
) -> list[str]:
    lines = ["## What to try next", ""]
    if any(record.status == "error" for record in telemetry):
        lines.append("- Investigate hook errors in telemetry before expanding rollout.")
    if any(session.tools.by_category["explore"] > 20 for session in sessions):
        lines.append(
            "- High explore volume: tighten pre-read bypass rules or increase nav-map budgets."
        )
    if not telemetry:
        lines.append(
            "- Enable `CODE_BRIEFCASE_TELEMETRY=1` during dogfood to unlock hook latency and injection metrics."
        )
    if not lines[-1].startswith("-"):
        lines.append(
            "- Continue proxy-only monitoring until sample sizes reach 20+ sessions per cohort."
        )
    lines.append("")
    return lines


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    baseline_start = parse_timestamp(args.baseline_start)
    treatment_start = parse_timestamp(args.treatment_start)
    if baseline_start is None or treatment_start is None:
        raise SystemExit("Invalid baseline or treatment timestamp")
    baseline_end = (
        parse_timestamp(args.baseline_end) if args.baseline_end else treatment_start
    )
    treatment_end = (
        parse_timestamp(args.treatment_end)
        if args.treatment_end
        else datetime.now(timezone.utc)
    )
    if baseline_end is None or treatment_end is None:
        raise SystemExit("Invalid end timestamp")

    sessions = discover_sessions(
        codex_root=Path(args.codex_root).expanduser(),
        claude_roots=resolve_claude_roots(args.claude_root),
        baseline_start=baseline_start,
        baseline_end=baseline_end,
        treatment_end=treatment_end,
    )
    telemetry_all = parse_telemetry_file(Path(args.tldr_telemetry).expanduser())
    telemetry = [
        record
        for record in telemetry_all
        if baseline_start <= record.timestamp < treatment_end
    ]
    annotations_path = Path(args.annotations).expanduser()
    has_annotations = annotations_path.exists()
    matched, unmatched, hit_stats = match_telemetry(sessions, telemetry)
    markdown = render_markdown(
        sessions,
        matched,
        unmatched,
        hit_stats,
        baseline_start=baseline_start,
        baseline_end=baseline_end,
        treatment_end=treatment_end,
        has_annotations=has_annotations,
    )

    def session_payload(session: SessionSummary) -> dict[str, Any]:
        data = asdict(session)
        data["tools"]["by_category"] = dict(session.tools.by_category)
        data["tools"]["unique_files_read"] = sorted(session.tools.unique_files_read)
        data["tools"]["unique_files_edited"] = sorted(session.tools.unique_files_edited)
        data["rework"]["_file_read_counts"] = dict(session.rework._file_read_counts)
        return data

    outcome_summary = _outcome_rollup_summary(sessions, matched)
    payload = {
        "sessions": [session_payload(session) for session in sessions],
        "telemetry_matched": [asdict(record) for record in matched],
        "telemetry_unmatched": [asdict(record) for record in unmatched],
        "hit_stats": hit_stats,
        "verdict": verdict_for(sessions, matched, has_annotations=has_annotations),
        "outcome_summary": outcome_summary,
    }
    result = {"markdown": markdown, "json": payload}
    if getattr(args, "rollups_json", None):
        rollups_path = Path(args.rollups_json).expanduser()
        rollups_path.parent.mkdir(parents=True, exist_ok=True)
        rollups_path.write_text(json.dumps(outcome_summary, indent=2), encoding="utf-8")
    return result


def _outcome_rollup_summary(
    sessions: list[SessionSummary], telemetry: list[TelemetryRecord]
) -> dict[str, Any]:
    hit_stats = _rollup_hit_stats(sessions, telemetry)
    harmful_sessions = sum(
        1
        for session in sessions
        if session.rework.failed_commands >= 3 or session.parse_errors > 2
    )
    telemetry_session_ids = {
        record.session_id for record in telemetry if record.session_id
    }
    proxy_only_sessions = sum(
        1 for session in sessions if session.session_id not in telemetry_session_ids
    )
    return {
        "recommendation_hit_rate": _safe_rate(
            hit_stats["recommended_hits"], hit_stats["recommended_total"]
        ),
        "surfaced_hit_rate": _safe_rate(
            hit_stats["surfaced_hits"], hit_stats["surfaced_total"]
        ),
        "proxy_only_sessions": proxy_only_sessions,
        "harmful_sessions": harmful_sessions,
        "telemetry_records": len(telemetry),
        "candidate_records": sum(1 for record in telemetry if record.candidate_files),
        "v2_records": sum(
            1 for record in telemetry if (record.schema_version or 1) >= 2
        ),
    }


def _rollup_hit_stats(
    sessions: list[SessionSummary], telemetry: list[TelemetryRecord]
) -> dict[str, int]:
    by_session = {session.session_id: session for session in sessions}
    stats = {
        "recommended_hits": 0,
        "recommended_total": 0,
        "surfaced_hits": 0,
        "surfaced_total": 0,
    }
    for record in telemetry:
        session = by_session.get(record.session_id or "")
        if session is None:
            continue
        later_reads = (
            session.tools.unique_files_read | session.tools.unique_files_edited
        )
        for path in record.recommended_related_files:
            stats["recommended_total"] += 1
            if telemetry_context_hit(
                path, session=session, record=record, later_reads=later_reads
            ):
                stats["recommended_hits"] += 1
        for path in record.surfaced_files:
            stats["surfaced_total"] += 1
            if telemetry_context_hit(
                path, session=session, record=record, later_reads=later_reads
            ):
                stats["surfaced_hits"] += 1
    return stats


def _safe_rate(hits: int, total: int) -> float | None:
    if total == 0:
        return None
    return hits / total


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate Code Briefcase efficacy from local agent logs."
    )
    parser.add_argument("--baseline-start", required=True)
    parser.add_argument("--treatment-start", required=True)
    parser.add_argument("--baseline-end")
    parser.add_argument("--treatment-end")
    parser.add_argument("--codex-root", default="~/.codex")
    parser.add_argument(
        "--claude-root",
        action="append",
        help=(
            "Claude profile root to scan. Can be repeated or comma-separated. "
            "Defaults to ~/.claude, ~/.claude-work, ~/.claude-personal, and ~/.claude-space."
        ),
    )
    parser.add_argument(
        "--code-briefcase-telemetry",
        "--tldr-telemetry",
        dest="tldr_telemetry",
        default="~/.code-briefcase/telemetry.jsonl",
    )
    parser.add_argument(
        "--annotations", default="reports/code-briefcase-efficacy-annotations.jsonl"
    )
    parser.add_argument("--out", required=True)
    parser.add_argument("--json-out")
    parser.add_argument("--rollups-json")
    args = parser.parse_args()

    report = build_report(args)
    out_path = Path(args.out).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report["markdown"], encoding="utf-8")
    if args.json_out:
        json_path = Path(args.json_out).expanduser()
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(report["json"], indent=2, default=str), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
