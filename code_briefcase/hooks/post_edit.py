from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from code_briefcase.diagnostics import (
    _detect_language,
    get_diagnostics,
    get_lint_format_diagnostics,
)
from code_briefcase.hooks.edit import EDIT_TOOLS, extract_apply_patch_paths
from code_briefcase.hooks.path_policy import (
    CODE_EXTENSIONS,
    classify_context_path,
    looks_secret_path,
    resolve_event_path,
)
from code_briefcase.hooks.outcome import HookExecutionResult, event_relative_path, noop, ok, skipped
from code_briefcase.hooks.runtime import HookEvent, HookResponse

logger = logging.getLogger(__name__)

TRUE_VALUES = {"1", "true", "yes", "on", "enabled", "enable", "y", "t"}
FALSE_VALUES = {"0", "false", "no", "off", "disabled", "disable", "n", "f", ""}
WATCH_ENV = "CODE_BRIEFCASE_WATCH_DIAGNOSTICS"
LEGACY_WATCH_ENV = "TLDR_WATCH_DIAGNOSTICS"
WATCH_BUDGET_ENV = "CODE_BRIEFCASE_WATCH_DIAGNOSTICS_BUDGET_MS"
WATCH_FALLBACK_STATUSES = {"fallback_required", "unhealthy"}
WATCH_USED_STATUSES = {"fresh", "stale", "pending"}


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
        decision = classify_context_path(event.cwd, path, include_tests=True)
        if decision.reason == "markdown_unsupported":
            return
        if not decision.allowed:
            if decision.reason == "missing_file" and path.suffix.lower() in CODE_EXTENSIONS:
                paths.append(path)
                seen.add(path)
            return
        if not path.exists() and looks_secret_path(path):
            return
        paths.append(path)
        seen.add(path)

    for source in sources:
        for key in ("file_path", "path", "filePath"):
            path = resolve_event_path(event, source.get(key))
            add(path)
    if paths:
        return paths

    for path in extract_apply_patch_paths(event):
        decision = classify_context_path(event.cwd, path, include_tests=True)
        if decision.reason == "markdown_unsupported":
            continue
        if path.suffix.lower() not in CODE_EXTENSIONS and decision.file_kind not in {
            "code",
            "test",
        }:
            continue
        if path.exists() and not decision.allowed:
            continue
        if not path.exists() and looks_secret_path(path):
            continue
        add(path)
    return paths


def _diagnostic_message_for_file(
    event: HookEvent,
    file_path: Path,
    *,
    watch_enabled: bool | None = None,
) -> tuple[str | None, int, int, dict[str, Any] | None]:
    decision = classify_context_path(event.cwd, file_path, include_tests=True)
    if decision.reason == "markdown_unsupported":
        return None, 0, 0, None
    if file_path.suffix.lower() not in CODE_EXTENSIONS and decision.file_kind not in {
        "code",
        "test",
    }:
        return None, 0, 0, None

    notify_daemon(event.cwd, file_path)
    if not file_path.exists():
        return None, 0, 0, None

    language = _detect_language(str(file_path))
    if language == "unknown":
        return None, 0, 0, None

    enabled = _watch_diagnostics_enabled() if watch_enabled is None else watch_enabled
    watch_info: dict[str, Any] | None = None
    if enabled:
        watch_info = _query_watch_diagnostics(event, file_path, language=language)
        status = str(watch_info.get("status") or "")
        if status in WATCH_USED_STATUSES:
            watch_info["used"] = True
            if status == "pending":
                watch_info["notice"] = _format_pending_watch_notice(file_path)
                return None, 0, 0, watch_info
            result = _result_from_watch_payload(file_path, language, watch_info)
            result = _merge_lint_format_diagnostics(file_path, language, result)
            message = format_diagnostic_message(file_path, result)
            if status == "stale" and message:
                message = f"{message}\n[showing watcher diagnostics from {_age_label(watch_info)} ago]"
            return (
                message,
                int(result.get("error_count") or 0),
                int(result.get("warning_count") or 0),
                watch_info,
            )

    try:
        result = get_diagnostics(str(file_path), language=language)
    except Exception:
        return None, 0, 0, watch_info

    error_count = int(result.get("error_count") or 0)
    warning_count = int(result.get("warning_count") or 0)
    message = format_diagnostic_message(file_path, result)
    if message is None:
        return None, 0, 0, watch_info
    if watch_info is not None:
        watch_info["used"] = False
        watch_info["fallback_reason"] = watch_info.get("fallback_reason") or status
    return message, error_count, warning_count, watch_info


def build_post_edit_response(event: HookEvent) -> HookExecutionResult:
    if event.tool_name not in EDIT_TOOLS:
        return skipped(reason="wrong_tool")

    edited_files = extract_edited_files(event)
    trigger = [
        display_path
        for path in edited_files
        if (display_path := event_relative_path(event, path)) is not None
    ]
    if not edited_files:
        raw_path = resolve_event_path(
            event,
            event.tool_input.get("file_path") or event.tool_input.get("path"),
        )
        if raw_path is not None:
            decision = classify_context_path(event.cwd, raw_path, include_tests=True)
            if decision.reason == "markdown_unsupported":
                rel = event_relative_path(event, raw_path)
                return skipped(
                    reason="markdown_unsupported",
                    trigger_files=[rel] if rel else [],
                )
        return skipped(reason="no_edit_targets")

    messages: list[str] = []
    watcher_notices: list[str] = []
    diagnostics_count = 0
    watch_enabled = _watch_diagnostics_enabled()
    watch_infos: list[dict[str, Any]] = []
    for file_path in edited_files:
        message, error_count, warning_count, watch_info = _diagnostic_message_for_file(
            event,
            file_path,
            watch_enabled=watch_enabled,
        )
        if watch_info is not None:
            watch_infos.append(watch_info)
            notice = watch_info.get("notice")
            if isinstance(notice, str) and notice:
                watcher_notices.append(notice)
        if message is None:
            continue
        messages.append(message)
        diagnostics_count += error_count + warning_count
    watch_summary = _summarize_watch_infos(watch_enabled, watch_infos)
    watcher_section = _format_watcher_notices(watcher_notices)
    if not messages:
        if os.environ.get("CODE_BRIEFCASE_POST_EDIT_CLEAN_CONFIRM") == "0":
            return noop(reason="clean_no_diagnostics", trigger_files=trigger, **watch_summary)
        confirmation = _format_clean_edit_confirmation(edited_files)
        if watcher_section:
            confirmation = f"{confirmation}\n\n{watcher_section}"
        return ok(
            HookResponse(
                message=confirmation,
                additional_context=confirmation,
                suppress_output=False,
            ),
            trigger_files=trigger,
            noop_reason="clean_no_diagnostics",
            **watch_summary,
        )

    message = "\n\n".join(messages)
    if watcher_section:
        message = f"{message}\n\n{watcher_section}"
    return ok(
        HookResponse(message=message, additional_context=message, suppress_output=False),
        trigger_files=trigger,
        diagnostics_count=diagnostics_count,
        **watch_summary,
    )


def _watch_diagnostics_enabled() -> bool:
    raw = os.environ.get(WATCH_ENV)
    env_var_name = WATCH_ENV
    if raw is None:
        raw = os.environ.get(LEGACY_WATCH_ENV)
        env_var_name = LEGACY_WATCH_ENV
    if raw is None:
        return False
    value = raw.strip().lower()
    if value in FALSE_VALUES:
        return False
    if value in TRUE_VALUES:
        return True
    logger.warning(
        "Unrecognized %s value: %r — treating as disabled",
        env_var_name,
        raw,
    )
    return False


def _watch_query_budget_ms() -> int:
    raw = os.environ.get(WATCH_BUDGET_ENV)
    if not raw:
        return 150
    try:
        return min(5000, max(0, int(raw)))
    except ValueError:
        return 150


def _query_watch_diagnostics(
    event: HookEvent,
    file_path: Path,
    *,
    language: str,
) -> dict[str, Any]:
    budget_ms = _watch_query_budget_ms()
    info: dict[str, Any] = {
        "enabled": True,
        "attempted": True,
        "status": "fallback_required",
        "query_budget_ms": budget_ms,
        "backend": "watcher",
    }
    try:
        from code_briefcase.daemon import query_or_start_daemon

        response = query_or_start_daemon(
            event.cwd,
            {
                "cmd": "watchers",
                "action": "query",
                "file": str(file_path),
                "language": language,
                "budget_ms": budget_ms,
            },
            connect_timeout_ms=200,
            response_timeout_ms=max(1000, budget_ms + 500),
        )
    except Exception as exc:
        info["fallback_reason"] = exc.__class__.__name__
        return info

    if not response.ok or response.payload is None:
        reason = response.message or response.kind.value
        if response.payload and response.payload.get("message"):
            reason = str(response.payload["message"])
        logger.warning(
            "Watcher daemon query failed (%s), using sync fallback",
            reason,
        )
        info["fallback_reason"] = reason
        return info

    payload = response.payload
    status = str(payload.get("watcher_status") or payload.get("status") or "fallback_required")
    info.update(
        {
            "status": status,
            "diagnostics": list(payload.get("diagnostics") or []),
            "error_count": int(payload.get("error_count") or 0),
            "warning_count": int(payload.get("warning_count") or 0),
            "age_ms": payload.get("age_ms"),
            "wait_ms": payload.get("wait_ms"),
            "batch_seq": payload.get("batch_seq"),
            "fallback_reason": payload.get("fallback_reason"),
            "backend": payload.get("backend") or "watcher",
        }
    )
    return info


def _result_from_watch_payload(
    file_path: Path,
    language: str,
    watch_info: dict[str, Any],
) -> dict[str, Any]:
    diagnostics = list(watch_info.get("diagnostics") or [])
    error_count = int(watch_info.get("error_count") or 0)
    warning_count = int(watch_info.get("warning_count") or 0)
    return {
        "file": str(file_path),
        "language": language,
        "tools": [str(watch_info.get("backend") or "watcher")],
        "diagnostics": diagnostics,
        "error_count": error_count,
        "warning_count": warning_count,
    }


def _merge_lint_format_diagnostics(
    file_path: Path,
    language: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    try:
        lint_result = get_lint_format_diagnostics(str(file_path), language=language)
    except Exception:
        return result
    diagnostics = list(result.get("diagnostics") or [])
    diagnostics.extend(lint_result.get("diagnostics") or [])
    tools = list(result.get("tools") or [])
    tools.extend(lint_result.get("tools") or [])
    merged = dict(result)
    merged["tools"] = tools
    merged["diagnostics"] = diagnostics
    merged["error_count"] = sum(1 for d in diagnostics if d.get("severity") == "error")
    merged["warning_count"] = sum(1 for d in diagnostics if d.get("severity") == "warning")
    return merged


def _format_pending_watch_notice(file_path: Path) -> str:
    return (
        f"[Code Briefcase watcher] {file_path.name}: "
        "watch diagnostics are warming; fresh results are still pending."
    )


def _format_watcher_notices(notices: list[str]) -> str | None:
    if not notices:
        return None
    return "\n".join(notices)


def _age_label(watch_info: dict[str, Any]) -> str:
    age = watch_info.get("age_ms")
    if isinstance(age, int):
        return f"{age}ms"
    return "an unknown interval"


def _summarize_watch_infos(
    enabled: bool,
    infos: list[dict[str, Any]],
) -> dict[str, Any]:
    statuses = [str(item.get("status")) for item in infos if item.get("status")]
    used = any(bool(item.get("used")) for item in infos)
    first = infos[0] if infos else {}
    fallback_reason = next(
        (str(item.get("fallback_reason")) for item in infos if item.get("fallback_reason")),
        None,
    )
    return {
        "watch_diagnostics_enabled": enabled,
        "watch_diagnostics_attempted": bool(infos),
        "watch_diagnostics_used": used,
        "watch_diagnostics_status": statuses[0] if statuses else None,
        "watch_diagnostics_statuses": statuses,
        "watch_diagnostics_age_ms": _first_int(infos, "age_ms"),
        "watch_diagnostics_wait_ms": _sum_int(infos, "wait_ms"),
        "watch_diagnostics_query_budget_ms": first.get("query_budget_ms"),
        "watch_diagnostics_batch_seq": _first_int(infos, "batch_seq"),
        "watch_diagnostics_fallback_reason": fallback_reason,
        "diagnostics_backend": first.get("backend") if infos else None,
    }


def _first_int(items: list[dict[str, Any]], key: str) -> int | None:
    for item in items:
        value = item.get(key)
        if isinstance(value, int):
            return value
    return None


def _sum_int(items: list[dict[str, Any]], key: str) -> int | None:
    values = [item.get(key) for item in items if isinstance(item.get(key), int)]
    if not values:
        return None
    return int(sum(values))


def notify_daemon(project: Path, file_path: Path) -> None:
    try:
        from code_briefcase.daemon import query_daemon

        query_daemon(project, {"cmd": "notify", "file": str(file_path)})
        return
    except Exception:
        pass

    try:
        from code_briefcase.dirty_flag import mark_dirty

        try:
            edited = str(file_path.relative_to(project))
        except ValueError:
            edited = str(file_path)
        mark_dirty(project, edited)
    except Exception:
        pass


def _format_clean_edit_confirmation(edited_files: list[Path]) -> str:
    names = ", ".join(p.name for p in edited_files[:5])
    suffix = f" (+{len(edited_files) - 5} more)" if len(edited_files) > 5 else ""
    return (
        f"[Code Briefcase post-edit] Edit completed for {names}{suffix}. "
        "Post-edit check ran; no diagnostics were surfaced."
    )


def format_diagnostic_message(file_path: Path, result: dict[str, Any], limit: int = 10) -> str | None:
    error_count = int(result.get("error_count") or 0)
    warning_count = int(result.get("warning_count") or 0)
    if error_count == 0 and warning_count == 0:
        return None

    lines = [
        f"Code Briefcase diagnostics for {file_path.name}: {error_count} errors, {warning_count} warnings"
    ]
    for diag in (result.get("diagnostics") or [])[:limit]:
        location = f"{diag.get('file') or file_path}:{diag.get('line', 0)}:{diag.get('column', 0)}"
        source = diag.get("source") or diag.get("rule") or "diagnostic"
        lines.append(f"- {location} [{source}] {diag.get('message', '')}")
    return "\n".join(lines)
