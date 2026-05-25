from code_briefcase.hooks.read import build_read_response
from code_briefcase.hooks.runtime import parse_hook_event


def _event(tmp_path, file_name, extra=None, session_id=None):
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": file_name, **(extra or {})},
        "cwd": str(tmp_path),
    }
    if session_id:
        payload["session_id"] = session_id
    return parse_hook_event(payload, client="claude")


def test_large_code_file_returns_context_and_limit(tmp_path):
    source = tmp_path / "app.py"
    source.write_text(
        "import os\n\n"
        "def helper(value: int) -> int:\n"
        "    return value + 1\n\n"
        "def main() -> int:\n"
        "    return helper(1)\n"
        + "\n".join(f"VALUE_{i} = {i}" for i in range(300))
    )

    response = build_read_response(_event(tmp_path, "app.py"))

    assert response.permission_decision == "allow"
    assert response.updated_input["limit"] == 200
    assert "helper" in response.additional_context


def test_small_code_file_noops(tmp_path):
    (tmp_path / "app.py").write_text("def main():\n    return 1\n")

    assert build_read_response(_event(tmp_path, "app.py")).is_noop()


def test_targeted_read_returns_context(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("def main():\n    return 1\n" + "x = 1\n" * 400)

    result = build_read_response(
        _event(tmp_path, "app.py", {"offset": 10, "limit": 20}, session_id="s1")
    )

    assert result.status == "ok"
    assert result.context_kind == "targeted_read_orientation"
    assert "main" in (result.additional_context or "")
    assert "Read specific lines with offset=N limit=M" not in (result.additional_context or "")


def test_targeted_offset_only_read_preserves_requested_input(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("def main():\n    return 1\n" + "x = 1\n" * 400)

    result = build_read_response(_event(tmp_path, "app.py", {"offset": 10}, session_id="s1"))

    assert result.status == "ok"
    assert result.updated_input["offset"] == 10
    assert "limit" not in result.updated_input


def test_repeated_targeted_read_same_session_is_throttled(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("def main():\n    return 1\n" + "x = 1\n" * 400)

    first = build_read_response(
        _event(tmp_path, "app.py", {"offset": 10, "limit": 20}, session_id="s1")
    )
    second = build_read_response(
        _event(tmp_path, "app.py", {"offset": 30, "limit": 20}, session_id="s1")
    )
    third = build_read_response(
        _event(tmp_path, "app.py", {"offset": 30, "limit": 20}, session_id="s2")
    )

    assert first.status == "ok"
    assert second.status == "skipped"
    assert second.noop_reason == "targeted_read_recently_surfaced"
    assert third.status == "ok"


def test_targeted_read_does_not_report_unsurfaced_related_files(monkeypatch, tmp_path):
    source = tmp_path / "src" / "app.py"
    related = tmp_path / "src" / "auth.py"
    source.parent.mkdir(parents=True)
    source.write_text("from .auth import login\n" + "x = 1\n" * 400, encoding="utf-8")
    related.write_text("def login():\n    return True\n", encoding="utf-8")

    def fake_extract(path: str, base_path: str):
        return {
            "imports": [{"module": ".auth", "names": ["login"], "is_from": True}],
            "functions": [{"name": "handler", "signature": "def handler()", "line_number": 1}],
            "classes": [],
        }

    monkeypatch.setattr("code_briefcase.hooks.file_context.extract_file", fake_extract)

    result = build_read_response(
        _event(tmp_path, "src/app.py", {"offset": 10, "limit": 20}, session_id="s1")
    )

    assert result.status == "ok"
    assert result.context_kind == "targeted_read_orientation"
    assert result.recommended_files == []
    assert result.surfaced_files == []
    assert result.candidate_files == []
    assert "src/auth.py" not in (result.additional_context or "")


def test_targeted_read_on_small_file_stays_quiet(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("def main():\n    return 1\n")

    result = build_read_response(
        _event(tmp_path, "app.py", {"offset": 1, "limit": 5}, session_id="s1")
    )

    assert result.status == "skipped"
    assert result.noop_reason == "targeted_read_small_file"


def test_malformed_limit_still_returns_context(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("def main():\n    return 1\n" + "x = 1\n" * 400)

    result = build_read_response(_event(tmp_path, "app.py", {"limit": "abc"}, session_id="s1"))

    assert result.status == "ok"


def test_read_markdown_stays_unsupported(tmp_path):
    (tmp_path / "README.md").write_text("# hi\n" * 400)

    result = build_read_response(_event(tmp_path, "README.md"))

    assert result.status == "skipped"
    assert result.noop_reason == "markdown_unsupported"


def test_test_file_read_returns_context(tmp_path):
    source = tmp_path / "test_app.py"
    source.write_text("def test_main():\n    assert True\n" + "x = 1\n" * 400)

    result = build_read_response(_event(tmp_path, "test_app.py"))

    assert result.status == "ok"


def test_html_read_returns_structural_summary(tmp_path):
    path = tmp_path / "templates" / "page.html"
    path.parent.mkdir(parents=True)
    path.write_text("<html><head><title>App</title></head><body><h1>Hi</h1></body></html>\n")

    result = build_read_response(_event(tmp_path, str(path.relative_to(tmp_path))))

    assert result.status == "ok"
    assert result.context_kind == "html_summary"
    assert "App" in (result.additional_context or "")


def test_pre_read_records_related_candidates(monkeypatch, tmp_path):
    source = tmp_path / "src" / "app.py"
    related = tmp_path / "src" / "auth.py"
    source.parent.mkdir(parents=True)
    source.write_text("from .auth import login\n" + "x = 1\n" * 400, encoding="utf-8")
    related.write_text("def login():\n    return True\n", encoding="utf-8")

    def fake_extract(path: str, base_path: str):
        return {
            "imports": [{"module": ".auth", "names": ["login"], "is_from": True}],
            "functions": [{"name": "handler", "signature": "def handler()", "line_number": 1}],
            "classes": [],
        }

    monkeypatch.setattr("code_briefcase.hooks.file_context.extract_file", fake_extract)
    event = _event(tmp_path, str(source.relative_to(tmp_path)))

    result = build_read_response(event)

    assert result.status == "ok"
    assert "src/app.py" in result.trigger_files
    assert "src/auth.py" in result.recommended_files
    assert any(
        candidate["path"] == "src/auth.py" and candidate["reason"] == "import"
        for candidate in result.candidate_files
    )


def test_external_path_skips_without_crashing(tmp_path):
    external = tmp_path.parent / "external_read.py"
    external.write_text("def main():\n    return 1\n")

    response = build_read_response(_event(tmp_path, str(external)))

    assert response.status == "skipped"
    assert response.trigger_files == []


def test_existing_large_external_path_skips_without_extracting(tmp_path):
    external = tmp_path.parent / "external_large_read.py"
    external.write_text("def main():\n    return 1\n" + "x = 1\n" * 400)

    response = build_read_response(_event(tmp_path, str(external)))

    assert response.status == "skipped"
    assert response.additional_context is None
    assert response.trigger_files == []
