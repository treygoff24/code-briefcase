from __future__ import annotations
from typing import Any

from pathlib import Path

from code_briefcase.hooks.path_policy import (
    MAX_CANDIDATES,
    MAX_SURFACED,
    classify_context_path,
    discover_related_candidates,
    should_exclude_context_path,
)
from code_briefcase.hooks.read import build_read_response
from code_briefcase.hooks.runtime import parse_hook_event


def _event(tmp_path: Path, file_name: str, extra: dict | None = None) -> Any:
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": file_name, **(extra or {})},
        "cwd": str(tmp_path),
    }
    return parse_hook_event(payload, client="claude")


def test_classify_context_path_keeps_markdown_unsupported(tmp_path: Any) -> None:
    project = tmp_path
    path = project / "README.md"
    path.write_text("# Hello\n", encoding="utf-8")

    result = classify_context_path(project, path)

    assert result.allowed is False
    assert result.reason == "markdown_unsupported"
    assert should_exclude_context_path(project, path) is True


def test_classify_context_path_allows_tests_and_structured_files(tmp_path: Any) -> None:
    project = tmp_path
    for rel in [
        "tests/test_app.py",
        "src/widget.test.tsx",
        "templates/page.html",
        "db/migration.sql",
        "config/watch.yml",
        "config/settings.json",
        ".gitignore",
        "scripts/run.sh",
    ]:
        path = project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")
        assert classify_context_path(project, path).allowed is True
        assert should_exclude_context_path(project, path) is False


def test_node_modules_env_and_secret_config_remain_excluded(tmp_path: Any) -> None:
    nm = tmp_path / "node_modules" / "pkg" / "index.ts"
    nm.parent.mkdir(parents=True)
    nm.write_text("export {}", encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text("KEY=1\n", encoding="utf-8")
    secret = tmp_path / "src" / "secret_config.py"
    secret.parent.mkdir(parents=True)
    secret.write_text("x = 1\n", encoding="utf-8")
    lock = tmp_path / "package-lock.json"
    lock.write_text("{}", encoding="utf-8")
    cred = tmp_path / "service-account-prod.json"
    cred.write_text("{}", encoding="utf-8")

    assert classify_context_path(tmp_path, nm).allowed is False
    assert classify_context_path(tmp_path, env).allowed is False
    assert classify_context_path(tmp_path, secret).allowed is False
    assert classify_context_path(tmp_path, lock).allowed is False
    assert classify_context_path(tmp_path, cred).allowed is False


def test_tests_can_be_excluded_when_include_tests_false(tmp_path: Any) -> None:
    test_file = tmp_path / "test_app.py"
    test_file.write_text("def test_x():\n    pass\n")

    assert (
        classify_context_path(tmp_path, test_file, include_tests=False).allowed is False
    )
    assert should_exclude_context_path(tmp_path, test_file, include_tests=False) is True


def test_existing_external_project_path_excluded(tmp_path: Any) -> None:
    external = tmp_path.parent / "external_project_path.py"
    external.write_text("def main():\n    return 1\n", encoding="utf-8")

    assert should_exclude_context_path(tmp_path, external)


def test_relative_from_import_without_module_discovers_sibling(tmp_path: Any) -> None:
    source = tmp_path / "src" / "app.py"
    related = tmp_path / "src" / "auth.py"
    source.parent.mkdir(parents=True)
    source.write_text("from . import auth\n", encoding="utf-8")
    related.write_text("def login():\n    return True\n", encoding="utf-8")
    event = _event(tmp_path, str(source.relative_to(tmp_path)))

    candidates, recommended, surfaced = discover_related_candidates(
        event,
        source,
        {"imports": [{"module": "", "names": ["auth"], "is_from": True}]},
        context_kind="read_nav_map",
    )

    assert any(candidate["path"] == "src/auth.py" for candidate in candidates)
    assert "src/auth.py" in recommended
    assert "src/auth.py" in surfaced


def test_discover_related_includes_test_neighbor_when_safe(tmp_path: Any) -> None:
    source = tmp_path / "src" / "app.py"
    test_neighbor = tmp_path / "tests" / "test_app.py"
    source.parent.mkdir(parents=True)
    test_neighbor.parent.mkdir(parents=True)
    source.write_text("def main():\n    return 1\n", encoding="utf-8")
    test_neighbor.write_text("def test_main():\n    assert True\n", encoding="utf-8")
    event = _event(tmp_path, str(source.relative_to(tmp_path)))

    candidates, _, surfaced = discover_related_candidates(
        event,
        source,
        {"imports": [], "functions": [], "classes": []},
        context_kind="read_nav_map",
    )

    assert any(candidate["path"] == "tests/test_app.py" for candidate in candidates)
    assert "tests/test_app.py" in surfaced


def test_max_surfaced_caps_injected_related_files(
    tmp_path: Any, monkeypatch: Any
) -> None:
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir(parents=True)
    source.write_text("from . import mod0\n" + "x = 1\n" * 400, encoding="utf-8")
    for index in range(6):
        (source.parent / f"mod{index}.py").write_text(
            f"def f{index}():\n    return {index}\n"
        )

    imports = [
        {"module": f".mod{i}", "names": [f"f{i}"], "is_from": True} for i in range(6)
    ]

    def fake_extract(path: str, base_path: str) -> Any:
        return {"imports": imports, "functions": [], "classes": []}

    monkeypatch.setattr("code_briefcase.hooks.file_context.extract_file", fake_extract)
    result = build_read_response(_event(tmp_path, str(source.relative_to(tmp_path))))

    assert result.status == "ok"
    assert len(result.surfaced_files) == MAX_SURFACED


def test_max_candidates_limits_metadata(tmp_path: Any, monkeypatch: Any) -> None:
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir(parents=True)
    source.write_text("from . import mod0\n" + "x = 1\n" * 400, encoding="utf-8")
    for index in range(12):
        (source.parent / f"mod{index}.py").write_text(
            f"def f{index}():\n    return {index}\n"
        )

    imports = [
        {"module": f".mod{i}", "names": [f"f{i}"], "is_from": True} for i in range(12)
    ]

    def fake_extract(path: str, base_path: str) -> Any:
        return {"imports": imports, "functions": [], "classes": []}

    monkeypatch.setattr("code_briefcase.hooks.file_context.extract_file", fake_extract)
    result = build_read_response(_event(tmp_path, str(source.relative_to(tmp_path))))

    assert result.status == "ok"
    assert len(result.candidate_files) <= MAX_CANDIDATES
    assert len(result.surfaced_files) <= MAX_SURFACED


def test_no_candidates_leaves_surfaced_files_empty(
    tmp_path: Any, monkeypatch: Any
) -> None:
    source = tmp_path / "solo.py"
    source.write_text("def main():\n    return 1\n" + "x = 1\n" * 400, encoding="utf-8")

    def fake_extract(path: str, base_path: str) -> Any:
        return {"imports": [], "functions": [], "classes": []}

    monkeypatch.setattr("code_briefcase.hooks.file_context.extract_file", fake_extract)
    result = build_read_response(_event(tmp_path, "solo.py"))

    assert result.status == "ok"
    assert result.surfaced_files == []
