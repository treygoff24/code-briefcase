from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

ClientName = Literal["claude", "codex", "generic"]

EVENT_NAME_ALIASES = {
    "session-start": "SessionStart",
    "sessionstart": "SessionStart",
    "SessionStart": "SessionStart",
    "pre-read": "PreToolUse",
    "pre-edit": "PreToolUse",
    "pretooluse": "PreToolUse",
    "preToolUse": "PreToolUse",
    "PreToolUse": "PreToolUse",
    "post-edit": "PostToolUse",
    "posttooluse": "PostToolUse",
    "postToolUse": "PostToolUse",
    "PostToolUse": "PostToolUse",
    "permissionrequest": "PermissionRequest",
    "permissionRequest": "PermissionRequest",
    "PermissionRequest": "PermissionRequest",
    "userpromptsubmit": "UserPromptSubmit",
    "userPromptSubmit": "UserPromptSubmit",
    "UserPromptSubmit": "UserPromptSubmit",
    "stop": "Stop",
    "Stop": "Stop",
}


@dataclass
class HookEvent:
    client: ClientName
    event_name: str
    tool_name: str | None = None
    tool_input: dict[str, Any] = field(default_factory=dict)
    tool_result: dict[str, Any] = field(default_factory=dict)
    cwd: Path = field(default_factory=Path.cwd)
    session_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class HookResponse:
    message: str | None = None
    permission_decision: Literal["allow", "deny", "ask"] | None = None
    updated_input: dict[str, Any] | None = None
    additional_context: str | None = None
    suppress_output: bool = True

    @classmethod
    def noop(cls) -> "HookResponse":
        return cls()

    def is_noop(self) -> bool:
        return (
            self.message is None
            and self.permission_decision is None
            and self.updated_input is None
            and self.additional_context is None
            and self.suppress_output is True
        )


def _client_name(client: str) -> ClientName:
    return client if client in {"claude", "codex"} else "generic"


def canonical_event_name(event_name: str | None) -> str:
    if not event_name:
        return ""
    normalized = str(event_name)
    return EVENT_NAME_ALIASES.get(normalized, EVENT_NAME_ALIASES.get(normalized.replace("_", ""), normalized))


def _dict_value(payload: dict[str, Any], *keys: str) -> dict[str, Any]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return {}


def parse_hook_event(payload: dict[str, Any] | None, client: str = "generic") -> HookEvent:
    payload = payload or {}
    cwd_value = (
        payload.get("cwd")
        or payload.get("project_dir")
        or payload.get("project")
        or "."
    )
    event_name = canonical_event_name(str(payload.get("hook_event_name") or payload.get("event") or ""))
    tool_result = _dict_value(payload, "tool_result", "toolResult", "tool_response", "toolResponse")

    return HookEvent(
        client=_client_name(client),
        event_name=event_name,
        tool_name=payload.get("tool_name") or payload.get("toolName"),
        tool_input=_dict_value(payload, "tool_input", "toolInput"),
        tool_result=tool_result,
        cwd=Path(str(cwd_value)).expanduser().resolve(),
        session_id=payload.get("session_id") or payload.get("sessionId"),
        raw=dict(payload),
    )


def _inferred_event_name(response: HookResponse, event_name: str | None) -> str:
    canonical = canonical_event_name(event_name)
    if canonical:
        return canonical
    if response.permission_decision is not None or response.updated_input is not None:
        return "PreToolUse"
    return ""


def render_hook_response(
    response: HookResponse,
    client: str = "generic",
    event_name: str | None = None,
) -> dict[str, Any]:
    if response.is_noop():
        return {}

    canonical = _inferred_event_name(response, event_name)

    if client == "codex":
        rendered: dict[str, Any] = {}
        context = response.additional_context or response.message
        hook_specific: dict[str, Any] = {}
        if canonical:
            hook_specific["hookEventName"] = canonical
        if context:
            hook_specific["additionalContext"] = context
        if response.permission_decision == "deny":
            hook_specific["permissionDecision"] = "deny"
        if hook_specific.get("additionalContext") or hook_specific.get("permissionDecision"):
            rendered["hookSpecificOutput"] = hook_specific
        elif response.message:
            rendered["systemMessage"] = response.message
        return rendered

    rendered = {"continue": True, "suppressOutput": response.suppress_output}
    if response.message and response.message != response.additional_context:
        rendered["systemMessage"] = response.message

    if client == "claude":
        hook_specific: dict[str, Any] = {}
        if canonical:
            hook_specific["hookEventName"] = canonical
        if response.permission_decision is not None:
            hook_specific["permissionDecision"] = response.permission_decision
        if response.updated_input is not None:
            hook_specific["updatedInput"] = response.updated_input
        if response.additional_context is not None:
            hook_specific["additionalContext"] = response.additional_context
        if hook_specific:
            rendered["hookSpecificOutput"] = hook_specific

    return rendered
