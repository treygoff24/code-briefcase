from __future__ import annotations

import json
import sys
from typing import Any

from tldr.hooks.runtime import parse_hook_event, render_hook_response


def run_hook(event_name: str, payload: dict[str, Any] | None, client: str = "generic") -> dict[str, Any]:
    event = parse_hook_event(payload, client=client)
    if event_name == "session-start":
        from tldr.hooks.session import build_session_start_response

        response = build_session_start_response(event)
    elif event_name == "pre-read":
        from tldr.hooks.read import build_read_response

        response = build_read_response(event)
    elif event_name == "pre-edit":
        from tldr.hooks.edit import build_pre_edit_response

        response = build_pre_edit_response(event)
    elif event_name == "post-edit":
        from tldr.hooks.post_edit import build_post_edit_response

        response = build_post_edit_response(event)
    else:
        from tldr.hooks.runtime import HookResponse

        response = HookResponse.noop()

    return render_hook_response(response, client=client)


def run_hook_from_stdin(event_name: str, client: str = "generic") -> int:
    raw = sys.stdin.read().strip()
    payload = json.loads(raw) if raw else {}
    rendered = run_hook(event_name, payload, client=client)
    sys.stdout.write(json.dumps(rendered))
    sys.stdout.write("\n")
    return 0
