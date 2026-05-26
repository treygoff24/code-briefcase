from __future__ import annotations

from code_briefcase.hooks.file_context import build_file_context_for_path
from code_briefcase.hooks.outcome import (
    HookExecutionResult,
    event_relative_path,
    ok,
    skipped,
)
from code_briefcase.hooks.path_policy import resolve_event_path
from code_briefcase.hooks.runtime import HookEvent, HookResponse


def build_read_response(event: HookEvent, budget: int = 1200) -> HookExecutionResult:
    if event.tool_name != "Read":
        return skipped(reason="wrong_tool")

    raw_path = event.tool_input.get("file_path") or event.tool_input.get("path")
    file_path = resolve_event_path(event, raw_path)
    trigger_path = event_relative_path(event, file_path)
    trigger = [trigger_path] if trigger_path is not None else []
    if file_path is None:
        return skipped(reason="bypass", trigger_files=trigger)

    result = build_file_context_for_path(
        event,
        file_path,
        mode="read",
        budget=budget,
        tool_input=event.tool_input,
    )
    if result.status != "ok":
        return skipped(
            reason=result.reason or "bypass", trigger_files=result.trigger_files
        )

    context = result.context or ""
    if event.client == "claude":
        updated_input = dict(event.tool_input)
        updated_input["file_path"] = str(file_path)
        if result.context_kind != "targeted_read_orientation":
            updated_input.setdefault("limit", 200)
        return ok(
            HookResponse(
                permission_decision="allow",
                updated_input=updated_input,
                additional_context=context,
                suppress_output=True,
            ),
            trigger_files=result.trigger_files,
            recommended_files=result.recommended_files,
            surfaced_files=result.surfaced_files,
            candidate_files=result.candidate_files,
            context_kind=result.context_kind,
        )

    return ok(
        HookResponse(
            message=context, additional_context=context, suppress_output=False
        ),
        trigger_files=result.trigger_files,
        recommended_files=result.recommended_files,
        surfaced_files=result.surfaced_files,
        candidate_files=result.candidate_files,
        context_kind=result.context_kind,
    )
