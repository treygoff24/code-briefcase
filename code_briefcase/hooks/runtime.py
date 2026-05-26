from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

ClientName = Literal["claude", "codex", "droid", "factory", "opencode", "generic"]

STABLE_CLIENTS = {"claude", "codex", "droid", "factory", "opencode", "generic"}

EVENT_NAME_ALIASES = {
    # Session
    "session-start": "SessionStart",
    "sessionstart": "SessionStart",
    "SessionStart": "SessionStart",
    # PreToolUse
    "pre-read": "PreToolUse",
    "pre-edit": "PreToolUse",
    "pre-tool": "PreToolUse",
    "pretooluse": "PreToolUse",
    "preToolUse": "PreToolUse",
    "PreToolUse": "PreToolUse",
    # PostToolUse
    "post-edit": "PostToolUse",
    "post-tool": "PostToolUse",
    "posttooluse": "PostToolUse",
    "postToolUse": "PostToolUse",
    "PostToolUse": "PostToolUse",
    # PermissionRequest
    "permission-request": "PermissionRequest",
    "permissionrequest": "PermissionRequest",
    "permissionRequest": "PermissionRequest",
    "PermissionRequest": "PermissionRequest",
    # UserPromptSubmit
    "user-prompt-submit": "UserPromptSubmit",
    "userpromptsubmit": "UserPromptSubmit",
    "userPromptSubmit": "UserPromptSubmit",
    "UserPromptSubmit": "UserPromptSubmit",
    # Stop
    "stop": "Stop",
    "Stop": "Stop",
    # SessionEnd
    "session-end": "SessionEnd",
    "sessionend": "SessionEnd",
    "sessionEnd": "SessionEnd",
    "SessionEnd": "SessionEnd",
    # Notification
    "notification": "Notification",
    "Notification": "Notification",
    # SubagentStart
    "subagent-start": "SubagentStart",
    "subagentstart": "SubagentStart",
    "subagentStart": "SubagentStart",
    "SubagentStart": "SubagentStart",
    # SubagentStop
    "subagent-stop": "SubagentStop",
    "subagentstop": "SubagentStop",
    "subagentStop": "SubagentStop",
    "SubagentStop": "SubagentStop",
    # PreCompact
    "pre-compact": "PreCompact",
    "precompact": "PreCompact",
    "preCompact": "PreCompact",
    "PreCompact": "PreCompact",
}

# Codex events: fields forbidden from renderer output per spec
CODEX_FORBIDDEN_FIELDS: dict[str, set[str]] = {
    "PreToolUse": {"continue", "stopReason", "suppressOutput", "updatedPermissions"},
    "PermissionRequest": {
        "updatedInput",
        "updatedPermissions",
        "interrupt",
        "permissionDecision",
    },
    "Stop": set(),  # plain text stdout is forbidden at protocol level, not field-level
}

# Clients that use JSON decisions rather than exit-code blocking
JSON_CONTROL_CLIENTS = {"claude", "codex", "droid", "factory", "opencode"}


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
    decision: Literal["block"] | None = None
    reason: str | None = None
    exit_code: int | None = None

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
            and self.decision is None
            and self.reason is None
        )


def _client_name(client: str) -> ClientName:
    if client in STABLE_CLIENTS:
        return client  # type: ignore[return-value]
    return "generic"


def canonical_event_name(event_name: str | None) -> str:
    if not event_name:
        return ""
    normalized = str(event_name)
    return EVENT_NAME_ALIASES.get(
        normalized, EVENT_NAME_ALIASES.get(normalized.replace("_", ""), normalized)
    )


def _dict_value(payload: dict[str, Any], *keys: str) -> dict[str, Any]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return {}


def parse_hook_event(
    payload: dict[str, Any] | None, client: str = "generic"
) -> HookEvent:
    payload = payload or {}
    cwd_value = (
        payload.get("cwd")
        or payload.get("project_dir")
        or payload.get("project")
        or "."
    )
    event_name = canonical_event_name(
        str(payload.get("hook_event_name") or payload.get("event") or "")
    )
    tool_result = _dict_value(
        payload, "tool_result", "toolResult", "tool_response", "toolResponse"
    )

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


def _stop_hook_active(payload: dict[str, Any] | None) -> bool:
    """Check stop-hook-active fuse from raw payload or process environment."""
    if payload:
        if payload.get("stop_hook_active"):
            return True
    if os.environ.get("CODE_BRIEFCASE_STOP_HOOK_ACTIVE") == "1":
        return True
    return False


def render_hook_response(
    response: HookResponse,
    client: str = "generic",
    event_name: str | None = None,
    *,
    raw_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if response.is_noop():
        return {}

    canonical = _inferred_event_name(response, event_name)

    if client == "codex":
        return _render_codex(response, canonical)

    if client in ("droid", "factory"):
        return _render_droid(response, canonical, raw_payload=raw_payload)

    if client == "opencode":
        return _render_opencode(response, canonical)

    # Claude and generic
    return _render_claude_generic(response, canonical, client=client)


def _render_codex(response: HookResponse, canonical: str) -> dict[str, Any]:
    """Codex event-aware rendering per spec output matrix."""
    rendered: dict[str, Any] = {}
    hook_specific: dict[str, Any] = {}

    if canonical == "PermissionRequest":
        # PermissionRequest: abstain via {}, deny via decision.behavior
        if response.permission_decision == "deny" or response.decision == "block":
            behavior = {
                "behavior": "deny",
                "message": response.reason
                or response.message
                or "blocked by Code Briefcase",
            }
            hook_specific["hookEventName"] = "PermissionRequest"
            hook_specific["decision"] = behavior
        # No hookEventName, no additionalContext for PermissionRequest deny
        # If only context (no deny), abstain
        if hook_specific:
            rendered["hookSpecificOutput"] = hook_specific
        return rendered

    if canonical == "UserPromptSubmit":
        # Block with decision + redacted reason; context via hookSpecificOutput
        if response.decision == "block":
            rendered["decision"] = "block"
            if response.reason:
                rendered["reason"] = response.reason
            if response.additional_context:
                hook_specific["additionalContext"] = response.additional_context
                rendered["hookSpecificOutput"] = hook_specific
        elif response.additional_context or response.message:
            context = response.additional_context or response.message
            hook_specific["hookEventName"] = canonical
            hook_specific["additionalContext"] = context
            rendered["hookSpecificOutput"] = hook_specific
        return rendered

    if canonical == "Stop":
        # MVP: {} only
        return {}

    # SessionStart, PreToolUse, PostToolUse
    context = response.additional_context or response.message
    if canonical:
        hook_specific["hookEventName"] = canonical
    if context:
        hook_specific["additionalContext"] = context
    if response.permission_decision == "deny":
        hook_specific["permissionDecision"] = "deny"
        if response.reason:
            hook_specific["permissionDecisionReason"] = response.reason
    if hook_specific.get("additionalContext") or hook_specific.get(
        "permissionDecision"
    ):
        rendered["hookSpecificOutput"] = hook_specific
    elif response.message:
        rendered["systemMessage"] = response.message
    return rendered


def _render_droid(
    response: HookResponse, canonical: str, *, raw_payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Droid/Factory event-aware rendering per spec output matrix."""
    # Stop/SubagentStop loop prevention
    if canonical in ("Stop", "SubagentStop"):
        if _stop_hook_active(raw_payload):
            return {}
        # MVP: no-op for Stop/SubagentStop
        return {}

    if canonical in ("SessionEnd", "Notification"):
        # No-op for MVP
        return {}

    if canonical == "SessionStart":
        # Context via hookSpecificOutput only when context exists
        if response.additional_context or response.message:
            hook_specific: dict[str, Any] = {"hookEventName": "SessionStart"}
            hook_specific["additionalContext"] = (
                response.additional_context or response.message
            )
            return {"hookSpecificOutput": hook_specific}
        return {}

    if canonical in ("PreToolUse", "PermissionRequest"):
        # Deny via permissionDecision + reason; no generic decision
        if response.permission_decision == "deny":
            hook_specific = {"hookEventName": canonical, "permissionDecision": "deny"}
            if response.reason:
                hook_specific["permissionDecisionReason"] = response.reason
            return {"hookSpecificOutput": hook_specific}
        if response.additional_context or response.message:
            context = response.additional_context or response.message
            hook_specific = {"hookEventName": canonical, "additionalContext": context}
            return {"hookSpecificOutput": hook_specific}
        return {}

    if canonical == "PostToolUse":
        # Diagnostics context only; no blocking
        if response.additional_context or response.message:
            context = response.additional_context or response.message
            hook_specific = {
                "hookEventName": "PostToolUse",
                "additionalContext": context,
            }
            return {"hookSpecificOutput": hook_specific}
        return {}

    if canonical == "UserPromptSubmit":
        # Block with decision + redacted reason; context via hookSpecificOutput
        if response.decision == "block":
            rendered: dict[str, Any] = {"decision": "block"}
            if response.reason:
                rendered["reason"] = response.reason
            if response.additional_context:
                rendered["hookSpecificOutput"] = {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": response.additional_context,
                }
            return rendered
        if response.additional_context or response.message:
            context = response.additional_context or response.message
            return {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": context,
                }
            }
        return {}

    if canonical == "PreCompact":
        # Context only behind opt-in
        if response.additional_context or response.message:
            context = response.additional_context or response.message
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreCompact",
                    "additionalContext": context,
                }
            }
        return {}

    # SubagentStart and unknown: no-op
    return {}


def _render_opencode(response: HookResponse, canonical: str) -> dict[str, Any]:
    """OpenCode adapter-internal rendering.

    OpenCode plugins do not consume this JSON directly. The generated JS
    adapter shells out to Code Briefcase, parses this small neutral shape, and then
    mutates OpenCode callback output or throws where the documented plugin
    hook supports that behavior.
    """
    rendered: dict[str, Any] = {}
    hook_specific: dict[str, Any] = {}
    if canonical:
        hook_specific["hookEventName"] = canonical

    context = response.additional_context or response.message
    if context:
        hook_specific["additionalContext"] = context

    if response.permission_decision is not None:
        hook_specific["permissionDecision"] = response.permission_decision
        if response.reason:
            hook_specific["permissionDecisionReason"] = response.reason

    if response.updated_input is not None:
        hook_specific["updatedInput"] = response.updated_input

    if hook_specific:
        rendered["hookSpecificOutput"] = hook_specific

    if response.decision == "block":
        rendered["decision"] = "block"
        if response.reason:
            rendered["reason"] = response.reason

    return rendered


def _render_claude_generic(
    response: HookResponse, canonical: str, *, client: str = "generic"
) -> dict[str, Any]:
    """Claude and generic rendering."""
    rendered: dict[str, Any] = {
        "continue": True,
        "suppressOutput": response.suppress_output,
    }
    if response.message and response.message != response.additional_context:
        rendered["systemMessage"] = response.message

    hook_specific: dict[str, Any] = {}
    if canonical:
        hook_specific["hookEventName"] = canonical
    if response.permission_decision is not None:
        hook_specific["permissionDecision"] = response.permission_decision
        if response.reason:
            hook_specific["permissionDecisionReason"] = response.reason
    if response.updated_input is not None:
        hook_specific["updatedInput"] = response.updated_input
    if response.additional_context is not None:
        hook_specific["additionalContext"] = response.additional_context
    if hook_specific:
        rendered["hookSpecificOutput"] = hook_specific

    if response.decision == "block":
        rendered["decision"] = "block"
        if response.reason:
            rendered["reason"] = response.reason

    return rendered
