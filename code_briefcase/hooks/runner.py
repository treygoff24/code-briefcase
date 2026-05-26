from __future__ import annotations

import json
import sys
import time
import uuid
from typing import Any

from code_briefcase.hooks.outcome import HookExecutionResult, error, injected_bytes
from code_briefcase.hooks.runtime import (
    JSON_CONTROL_CLIENTS,
    parse_hook_event,
    render_hook_response,
)
from code_briefcase.telemetry import record_hook_execution


def _dispatch(event_name: str, event: Any) -> HookExecutionResult:
    if event_name == "session-start":
        from code_briefcase.hooks.session import build_session_start_response

        return build_session_start_response(event)
    if event_name == "pre-read":
        from code_briefcase.hooks.read import build_read_response

        return build_read_response(event)
    if event_name == "pre-edit":
        from code_briefcase.hooks.edit import build_pre_edit_response

        return build_pre_edit_response(event)
    if event_name == "post-edit":
        from code_briefcase.hooks.post_edit import build_post_edit_response

        return build_post_edit_response(event)
    if event_name == "user-prompt-submit":
        from code_briefcase.hooks.prompt import build_user_prompt_submit_response

        return build_user_prompt_submit_response(event)
    if event_name == "permission-request":
        from code_briefcase.hooks.permission import build_permission_request_response

        return build_permission_request_response(event)
    if event_name == "pre-tool":
        from code_briefcase.hooks.tool import build_pre_tool_response

        return build_pre_tool_response(event)
    if event_name == "post-tool":
        from code_briefcase.hooks.outcome import noop

        return noop("post_tool_unhandled")
    if event_name == "stop":
        from code_briefcase.hooks.outcome import noop

        return noop("stop_noop")
    if event_name == "session-end":
        from code_briefcase.hooks.outcome import noop

        return noop("session_end_noop")
    if event_name == "notification":
        from code_briefcase.hooks.outcome import noop

        return noop("notification_noop")
    if event_name == "subagent-start":
        from code_briefcase.hooks.outcome import noop

        return noop("subagent_start_noop")
    if event_name == "subagent-stop":
        from code_briefcase.hooks.outcome import noop

        return noop("subagent_stop_noop")
    if event_name == "pre-compact":
        from code_briefcase.hooks.compact import build_pre_compact_response

        return build_pre_compact_response(event)
    from code_briefcase.hooks.outcome import noop

    return noop("unknown_event")


def run_hook(
    event_name: str, payload: dict[str, Any] | None, client: str = "generic"
) -> dict[str, Any]:
    event = parse_hook_event(payload, client=client)
    started = time.perf_counter()
    execution: HookExecutionResult
    try:
        execution = _dispatch(event_name, event)
    except Exception as exc:
        execution = error(type(exc).__name__)
    duration_ms = int((time.perf_counter() - started) * 1000)

    hook_run_id = execution.hook_run_id or str(uuid.uuid4())
    try:
        record_hook_execution(
            client=client,
            hook_event=event_name,
            project=event.cwd,
            duration_ms=duration_ms,
            status=execution.status,
            error_kind=execution.error_kind,
            injected_bytes=injected_bytes(execution),
            trigger_files=execution.trigger_files,
            recommended_files=execution.recommended_files,
            surfaced_files=execution.surfaced_files,
            diagnostics_count=execution.diagnostics_count,
            daemon_state=execution.daemon_state,
            noop_reason=execution.noop_reason,
            session_id=event.session_id,
            candidate_files=execution.candidate_files,
            context_kind=execution.context_kind,
            hook_run_id=hook_run_id,
            tool_name=event.tool_name,
            tool_input=event.tool_input,
            watch_diagnostics_enabled=execution.watch_diagnostics_enabled,
            watch_diagnostics_attempted=execution.watch_diagnostics_attempted,
            watch_diagnostics_used=execution.watch_diagnostics_used,
            watch_diagnostics_status=execution.watch_diagnostics_status,
            watch_diagnostics_statuses=execution.watch_diagnostics_statuses,
            watch_diagnostics_age_ms=execution.watch_diagnostics_age_ms,
            watch_diagnostics_wait_ms=execution.watch_diagnostics_wait_ms,
            watch_diagnostics_query_budget_ms=execution.watch_diagnostics_query_budget_ms,
            watch_diagnostics_batch_seq=execution.watch_diagnostics_batch_seq,
            watch_diagnostics_fallback_reason=execution.watch_diagnostics_fallback_reason,
            diagnostics_backend=execution.diagnostics_backend,
        )
    except Exception:
        pass

    return render_hook_response(
        execution.response,
        client=client,
        event_name=event.event_name or event_name,
        raw_payload=payload,
    )


def run_hook_from_stdin(event_name: str, client: str = "generic") -> int:
    """Run a hook from stdin JSON and return the process exit code.

    JSON-control clients/events always return exit 0 with JSON decisions.
    Exit-code fallback clients return exit 2 only for blocking decisions
    where JSON control is not available.
    """
    raw = sys.stdin.read().strip()
    payload = json.loads(raw) if raw else {}
    rendered = run_hook(event_name, payload, client=client)
    sys.stdout.write(json.dumps(rendered))
    sys.stdout.write("\n")

    # JSON-control clients always exit 0; decisions are in the JSON payload
    if client in JSON_CONTROL_CLIENTS:
        return 0

    # Fallback for generic clients: exit 2 if the response represents a blocking decision
    if rendered.get("decision") == "block" or (
        rendered.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
    ):
        return 2

    return 0
