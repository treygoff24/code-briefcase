from code_briefcase.hooks.outcome import classify_from_response, ok
from code_briefcase.hooks.runtime import HookEvent, HookResponse


def test_ok_preserves_explicit_empty_surfaced_files():
    result = ok(
        HookResponse(message="context"),
        trigger_files=["src/app.py"],
        surfaced_files=[],
    )

    assert result.surfaced_files == []
    assert result.trigger_files == ["src/app.py"]


def test_ok_falls_back_to_trigger_files_when_surfaced_files_omitted():
    result = ok(
        HookResponse(message="context"),
        trigger_files=["src/app.py"],
    )

    assert result.surfaced_files == ["src/app.py"]


def test_classify_from_response_leaves_surfaced_files_empty_by_default():
    event = HookEvent(client="claude", event_name="PreToolUse")
    result = classify_from_response(
        event,
        "pre-read",
        HookResponse(message="context"),
        trigger_files=["src/app.py"],
    )

    assert result.trigger_files == ["src/app.py"]
    assert result.surfaced_files == []


def test_classify_from_response_honors_explicit_surfaced_files():
    event = HookEvent(client="claude", event_name="PreToolUse")
    result = classify_from_response(
        event,
        "pre-read",
        HookResponse(message="context"),
        trigger_files=["src/app.py"],
        surfaced_files=["src/other.py"],
    )

    assert result.surfaced_files == ["src/other.py"]
