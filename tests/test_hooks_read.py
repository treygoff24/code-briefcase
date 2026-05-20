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


def test_external_path_skips_without_crashing(tmp_path):
    external = tmp_path.parent / "external_read.py"
    external.write_text("def main():\n    return 1\n")

    response = build_read_response(_event(tmp_path, str(external)))

    assert response.status == "skipped"
