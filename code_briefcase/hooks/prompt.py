from __future__ import annotations

import re

from code_briefcase.hooks.outcome import HookExecutionResult, noop, ok
from code_briefcase.hooks.runtime import HookEvent, HookResponse

# High-confidence API key patterns (allowlisted detectors only)
_KEY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Anthropic: sk-ant-... (at least 20 chars after prefix)
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"), "possible Anthropic API key"),
    # OpenAI: sk-... (at least 40 chars after prefix)
    (re.compile(r"\bsk-[A-Za-z0-9_-]{40,}\b"), "possible OpenAI API key"),
    # GitHub: ghp_ / gho_ / ghu_ / ghs_ / ghr_ (at least 30 chars)
    (re.compile(r"\bgh[phousr]_[A-Za-z0-9]{30,}\b"), "possible GitHub token"),
    # Slack: xox[bpors]-... (at least 10 chars after prefix)
    (re.compile(r"\bxox[bpors]-[A-Za-z0-9-]{10,}\b"), "possible Slack token"),
    # AWS: AKIA... (20 uppercase alphanumeric)
    (re.compile(r"\bAKIA[A-Z0-9]{16}\b"), "possible AWS access key"),
]

# PEM private key block
_PEM_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN\s+(?:RSA\s+)?PRIVATE\s+KEY-----", re.IGNORECASE
)

# .env-style credential lines: KEY_NAME=VALUE where KEY_NAME contains
# a recognized secret name and VALUE has high entropy (long + mixed chars)
_ENV_SECRET_KEY_FRAGMENTS = (
    "secret",
    "secret_key",
    "private_key",
    "api_key",
    "access_key",
    "access_token",
    "auth_token",
    "token",
    "password",
    "passwd",
    "credentials",
    "credential",
    "apikey",
    "authkey",
)
_ENV_LINE_RE = re.compile(
    r"""^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*['"]?([^\s'"]{8,})['"]?\s*$""",
    re.MULTILINE,
)


def _is_high_entropy(value: str) -> bool:
    """Heuristic: value has high enough entropy to look like a real secret."""
    if len(value) < 16:
        return False
    has_upper = any(c.isupper() for c in value)
    has_lower = any(c.islower() for c in value)
    has_digit = any(c.isdigit() for c in value)
    has_special = any(not c.isalnum() for c in value)
    variety = sum(1 for cond in (has_upper, has_lower, has_digit, has_special) if cond)
    # Real secrets typically use 3+ character classes and contain digits
    if variety < 3:
        return False
    if not has_digit:
        return False
    return True


def check_prompt_for_secrets(prompt: str) -> str | None:
    """Return a redacted reason string if a high-confidence secret is detected, else None."""
    for pattern, label in _KEY_PATTERNS:
        if pattern.search(prompt):
            return label

    if _PEM_PRIVATE_KEY_RE.search(prompt):
        return "possible PEM private key"

    for match in _ENV_LINE_RE.finditer(prompt):
        key_name = match.group(1).lower()
        value = match.group(2)
        if any(fragment in key_name for fragment in _ENV_SECRET_KEY_FRAGMENTS) and _is_high_entropy(value):
            return "possible .env credential"

    return None


def build_user_prompt_submit_response(event: HookEvent) -> HookExecutionResult:
    """Prompt-secret guard for UserPromptSubmit events."""
    prompt = str(
        event.raw.get("prompt")
        or event.raw.get("user_prompt")
        or event.raw.get("userPrompt")
        or event.raw.get("message")
        or event.tool_input.get("prompt")
        or event.tool_input.get("user_prompt")
        or event.tool_input.get("userPrompt")
        or event.tool_input.get("message")
        or ""
    )
    if not prompt:
        return noop("no_prompt")

    reason = check_prompt_for_secrets(prompt)
    if reason is None:
        return noop("clean")

    return ok(
        HookResponse(
            decision="block",
            reason=reason,
            additional_context=None,
            suppress_output=True,
        ),
    )
