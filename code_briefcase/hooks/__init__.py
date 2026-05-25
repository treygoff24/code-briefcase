"""Agent hook runtime for Code Briefcase."""

from .runtime import HookEvent, HookResponse, parse_hook_event, render_hook_response

__all__ = [
    "HookEvent",
    "HookResponse",
    "parse_hook_event",
    "render_hook_response",
]

# Public stable client names for CLI and installer
STABLE_CLIENTS = {"claude", "codex", "droid", "factory", "opencode", "generic"}

# All recognized internal event command names for CLI hooks run
HOOK_EVENT_CHOICES = [
    "session-start",
    "pre-read",
    "pre-edit",
    "post-edit",
    "user-prompt-submit",
    "permission-request",
    "pre-tool",
    "post-tool",
    "stop",
    "session-end",
    "notification",
    "subagent-start",
    "subagent-stop",
    "pre-compact",
]
