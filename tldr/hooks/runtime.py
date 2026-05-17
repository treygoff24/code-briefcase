from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

ClientName = Literal["claude", "codex", "generic"]


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
    event_name = str(payload.get("hook_event_name") or payload.get("event") or "")
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


def render_hook_response(response: HookResponse, client: str = "generic") -> dict[str, Any]:
    if response.is_noop():
        return {}

    rendered: dict[str, Any] = {
        "continue": True,
        "suppressOutput": response.suppress_output,
    }
    system_message = response.message or response.additional_context
    if system_message:
        rendered["systemMessage"] = system_message

    if client == "claude":
        hook_specific: dict[str, Any] = {}
        if response.permission_decision is not None:
            hook_specific["permissionDecision"] = response.permission_decision
        if response.updated_input is not None:
            hook_specific["updatedInput"] = response.updated_input
        if response.additional_context is not None:
            hook_specific["additionalContext"] = response.additional_context
        if hook_specific:
            rendered["hookSpecificOutput"] = hook_specific

    return rendered
