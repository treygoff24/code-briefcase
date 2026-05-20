from __future__ import annotations

import json
import sys
import time
from typing import Any

from tldr.hooks.outcome import HookExecutionResult, error, injected_bytes
from tldr.hooks.runtime import parse_hook_event, render_hook_response
from tldr.telemetry import record_hook_execution


def _dispatch(event_name: str, event) -> HookExecutionResult:
    if event_name == "session-start":
        from tldr.hooks.session import build_session_start_response

        return build_session_start_response(event)
    if event_name == "pre-read":
        from tldr.hooks.read import build_read_response

        return build_read_response(event)
    if event_name == "pre-edit":
        from tldr.hooks.edit import build_pre_edit_response

        return build_pre_edit_response(event)
    if event_name == "post-edit":
        from tldr.hooks.post_edit import build_post_edit_response

        return build_post_edit_response(event)
    from tldr.hooks.outcome import noop

    return noop("unknown_event")


def run_hook(event_name: str, payload: dict[str, Any] | None, client: str = "generic") -> dict[str, Any]:
    event = parse_hook_event(payload, client=client)
    started = time.perf_counter()
    execution: HookExecutionResult
    try:
        execution = _dispatch(event_name, event)
    except Exception as exc:
        execution = error(type(exc).__name__)
    duration_ms = int((time.perf_counter() - started) * 1000)

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
        )
    except Exception:
        pass

    return render_hook_response(
        execution.response,
        client=client,
        event_name=event.event_name or event_name,
    )


def run_hook_from_stdin(event_name: str, client: str = "generic") -> int:
    raw = sys.stdin.read().strip()
    payload = json.loads(raw) if raw else {}
    rendered = run_hook(event_name, payload, client=client)
    sys.stdout.write(json.dumps(rendered))
    sys.stdout.write("\n")
    return 0
