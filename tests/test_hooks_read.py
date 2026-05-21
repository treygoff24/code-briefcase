from tldr.hooks.read import build_read_response
from tldr.hooks.runtime import parse_hook_event


def _event(tmp_path, file_name, extra=None):
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": file_name, **(extra or {})},
        "cwd": str(tmp_path),
    }
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


def test_targeted_read_noops(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("def main():\n    return 1\n" + "x = 1\n" * 400)

    assert build_read_response(_event(tmp_path, "app.py", {"offset": 10})).is_noop()


def test_malformed_limit_noops(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("def main():\n    return 1\n" + "x = 1\n" * 400)

    assert build_read_response(_event(tmp_path, "app.py", {"limit": "abc"})).is_noop()


def test_markdown_config_file_noops(tmp_path):
    (tmp_path / "README.md").write_text("# hi\n" * 400)

    assert build_read_response(_event(tmp_path, "README.md")).is_noop()


def test_test_file_noops(tmp_path):
    source = tmp_path / "test_app.py"
    source.write_text("def test_main():\n    assert True\n" + "x = 1\n" * 400)

    assert build_read_response(_event(tmp_path, "test_app.py")).is_noop()


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

    monkeypatch.setattr("tldr.hooks.read.extract_file", fake_extract)
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
