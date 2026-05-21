from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from tldr.hooks.runtime import HookEvent, HookResponse

HookStatus = Literal["ok", "skipped", "noop", "error"]


@dataclass
class HookExecutionResult:
    response: HookResponse
    status: HookStatus = "noop"
    error_kind: str | None = None
    noop_reason: str | None = None
    trigger_files: list[str] = field(default_factory=list)
    recommended_files: list[str] = field(default_factory=list)
    surfaced_files: list[str] = field(default_factory=list)
    diagnostics_count: int = 0
    daemon_state: str | None = None
    candidate_files: list[dict[str, object]] = field(default_factory=list)
    context_kind: str | None = None
    hook_run_id: str | None = None

    def is_noop(self) -> bool:
        return self.response.is_noop()

    @property
    def message(self) -> str | None:
        return self.response.message

    @property
    def additional_context(self) -> str | None:
        return self.response.additional_context

    @property
    def permission_decision(self):
        return self.response.permission_decision

    @property
    def updated_input(self):
        return self.response.updated_input

    @property
    def suppress_output(self) -> bool:
        return self.response.suppress_output


def event_relative_path(event: HookEvent, path) -> str | None:
    if path is None:
        return None
    try:
        resolved = Path(path).expanduser()
        if not resolved.is_absolute():
            resolved = event.cwd / resolved
        resolved = resolved.resolve()
        try:
            return str(resolved.relative_to(event.cwd))
        except ValueError:
            return None
    except Exception:
        return str(path)


def _rel_path(event: HookEvent, path) -> str | None:
    return event_relative_path(event, path)


def _context_bytes(response: HookResponse) -> int:
    text = response.additional_context or response.message or ""
    return len(text.encode("utf-8"))


def skipped(
    response: HookResponse | None = None,
    *,
    reason: str,
    trigger_files: list[str] | None = None,
    **kwargs,
) -> HookExecutionResult:
    return HookExecutionResult(
        response=response or HookResponse.noop(),
        status="skipped",
        noop_reason=reason,
        trigger_files=list(trigger_files or []),
        **kwargs,
    )


def noop(reason: str | None = None, **kwargs) -> HookExecutionResult:
    return HookExecutionResult(
        response=HookResponse.noop(),
        status="noop",
        noop_reason=reason,
        **kwargs,
    )


def ok(
    response: HookResponse,
    *,
    trigger_files: list[str] | None = None,
    recommended_files: list[str] | None = None,
    surfaced_files: list[str] | None = None,
    **kwargs,
) -> HookExecutionResult:
    if surfaced_files is not None:
        surfaced = list(surfaced_files)
    else:
        surfaced = list(trigger_files or [])
    return HookExecutionResult(
        response=response,
        status="ok",
        trigger_files=list(trigger_files or []),
        recommended_files=list(recommended_files or []),
        surfaced_files=surfaced,
        **kwargs,
    )


def error(error_kind: str, response: HookResponse | None = None, **kwargs) -> HookExecutionResult:
    return HookExecutionResult(
        response=response or HookResponse.noop(),
        status="error",
        error_kind=error_kind,
        **kwargs,
    )


def injected_bytes(result: HookExecutionResult) -> int:
    if result.status != "ok":
        return 0
    return _context_bytes(result.response)


def classify_from_response(
    event: HookEvent,
    hook_event: str,
    response: HookResponse,
    *,
    skip_reason: str | None = None,
    trigger_files: list[str] | None = None,
    surfaced_files: list[str] | None = None,
) -> HookExecutionResult:
    if skip_reason:
        return skipped(response, reason=skip_reason, trigger_files=trigger_files)
    if response.is_noop():
        return noop(skip_reason or "no_context")
    return ok(
        response,
        trigger_files=trigger_files,
        surfaced_files=[] if surfaced_files is None else list(surfaced_files),
    )
