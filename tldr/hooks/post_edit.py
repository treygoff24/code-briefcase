from __future__ import annotations

from pathlib import Path
from typing import Any

from tldr.diagnostics import _detect_language, get_diagnostics
from tldr.hooks.edit import EDIT_TOOLS, extract_apply_patch_paths
from tldr.hooks.path_policy import (
    BYPASS_PARTS,
    CODE_EXTENSIONS,
    resolve_event_path,
    should_exclude_context_path,
    _looks_secret,
)
from tldr.hooks.outcome import HookExecutionResult, event_relative_path, ok, skipped
from tldr.hooks.runtime import HookEvent, HookResponse


def extract_edited_files(event: HookEvent) -> list[Path]:
    sources: list[dict[str, Any]] = [
        event.tool_input,
        event.tool_result,
    ]
    for raw_key in ("tool_response", "toolResponse"):
        value = event.raw.get(raw_key)
        if isinstance(value, dict):
            sources.append(value)

    paths: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path | None) -> None:
        if path is None or path in seen:
            return
        if path.exists():
            if should_exclude_context_path(event.cwd, path):
                return
        elif set(path.parts) & BYPASS_PARTS or _looks_secret(path):
            return
        paths.append(path)
        seen.add(path)

    for source in sources:
        for key in ("file_path", "path", "filePath"):
            path = resolve_event_path(event, source.get(key))
            add(path)
    if paths:
        return paths

    patch_paths = [
        path
        for path in extract_apply_patch_paths(event)
        if path.suffix.lower() in CODE_EXTENSIONS
        and (
            not path.exists()
            or not should_exclude_context_path(event.cwd, path)
        )
        and not (
            not path.exists() and (set(path.parts) & BYPASS_PARTS or _looks_secret(path))
        )
    ]
    for path in patch_paths:
        add(path)
    return paths


def _diagnostic_message_for_file(event: HookEvent, file_path: Path) -> tuple[str | None, int, int]:
    if file_path.suffix.lower() not in CODE_EXTENSIONS:
        return None, 0, 0

    notify_daemon(event.cwd, file_path)
    if not file_path.exists():
        return None, 0, 0

    language = _detect_language(str(file_path))
    if language == "unknown":
        return None, 0, 0

    try:
        result = get_diagnostics(str(file_path), language=language)
    except Exception:
        return None, 0, 0

    error_count = int(result.get("error_count") or 0)
    warning_count = int(result.get("warning_count") or 0)
    message = format_diagnostic_message(file_path, result)
    if message is None:
        return None, 0, 0
    return message, error_count, warning_count


def build_post_edit_response(event: HookEvent) -> HookExecutionResult:
    if event.tool_name not in EDIT_TOOLS:
        return skipped(reason="wrong_tool")

    edited_files = extract_edited_files(event)
    trigger = [
        display_path
        for path in edited_files
        if (display_path := event_relative_path(event, path)) is not None
    ]
    messages: list[str] = []
    diagnostics_count = 0
    for file_path in edited_files:
        message, error_count, warning_count = _diagnostic_message_for_file(event, file_path)
        if message is None:
            continue
        messages.append(message)
        diagnostics_count += error_count + warning_count
    if not messages:
        return skipped(reason="no_diagnostics", trigger_files=trigger)

    message = "\n\n".join(messages)
    return ok(
        HookResponse(message=message, additional_context=message, suppress_output=False),
        trigger_files=trigger,
        diagnostics_count=diagnostics_count,
    )


def notify_daemon(project: Path, file_path: Path) -> None:
    try:
        from tldr.daemon import query_daemon

        query_daemon(project, {"cmd": "notify", "file": str(file_path)})
        return
    except Exception:
        pass

    try:
        from tldr.dirty_flag import mark_dirty

        try:
            edited = str(file_path.relative_to(project))
        except ValueError:
            edited = str(file_path)
        mark_dirty(project, edited)
    except Exception:
        pass


def format_diagnostic_message(file_path: Path, result: dict[str, Any], limit: int = 10) -> str | None:
    error_count = int(result.get("error_count") or 0)
    warning_count = int(result.get("warning_count") or 0)
    if error_count == 0 and warning_count == 0:
        return None

    lines = [
        f"TLDR diagnostics for {file_path.name}: {error_count} errors, {warning_count} warnings"
    ]
    for diag in (result.get("diagnostics") or [])[:limit]:
        location = f"{diag.get('file') or file_path}:{diag.get('line', 0)}:{diag.get('column', 0)}"
        source = diag.get("source") or diag.get("rule") or "diagnostic"
        lines.append(f"- {location} [{source}] {diag.get('message', '')}")
    return "\n".join(lines)
