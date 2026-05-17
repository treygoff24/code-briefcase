from __future__ import annotations

from pathlib import Path
from typing import Any

from tldr.diagnostics import _detect_language, get_diagnostics
from tldr.hooks.edit import EDIT_TOOLS
from tldr.hooks.read import CODE_EXTENSIONS, resolve_event_path
from tldr.hooks.runtime import HookEvent, HookResponse


def extract_edited_file(event: HookEvent) -> Path | None:
    sources: list[dict[str, Any]] = [
        event.tool_input,
        event.tool_result,
    ]
    for raw_key in ("tool_response", "toolResponse"):
        value = event.raw.get(raw_key)
        if isinstance(value, dict):
            sources.append(value)

    for source in sources:
        for key in ("file_path", "path", "filePath"):
            path = resolve_event_path(event, source.get(key))
            if path is not None:
                return path
    return None


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


def build_post_edit_response(event: HookEvent) -> HookResponse:
    if event.tool_name not in EDIT_TOOLS:
        return HookResponse.noop()

    file_path = extract_edited_file(event)
    if file_path is None or file_path.suffix.lower() not in CODE_EXTENSIONS:
        return HookResponse.noop()

    notify_daemon(event.cwd, file_path)
    if not file_path.exists():
        return HookResponse.noop()

    language = _detect_language(str(file_path))
    if language == "unknown":
        return HookResponse.noop()

    try:
        result = get_diagnostics(str(file_path), language=language)
    except Exception:
        return HookResponse.noop()

    message = format_diagnostic_message(file_path, result)
    if not message:
        return HookResponse.noop()
    return HookResponse(message=message, suppress_output=False)
