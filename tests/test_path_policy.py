from __future__ import annotations

from pathlib import Path

from tldr.hooks.path_policy import (
    MAX_CANDIDATES,
    MAX_SURFACED,
    discover_related_candidates,
    should_exclude_context_path,
)
from tldr.hooks.read import build_read_response
from tldr.hooks.runtime import parse_hook_event


def _event(tmp_path: Path, file_name: str, extra: dict | None = None):
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": file_name, **(extra or {})},
        "cwd": str(tmp_path),
    }
    return parse_hook_event(payload, client="claude")


def test_non_code_extension_excluded(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text("# title\n")

    assert should_exclude_context_path(tmp_path, readme)


def test_secret_paths_excluded(tmp_path):
    secret = tmp_path / "secrets" / "api.key"
    secret.parent.mkdir(parents=True)
    secret.write_text("x")

    assert should_exclude_context_path(tmp_path, secret)


def test_node_modules_and_vendor_paths_excluded(tmp_path):
    nm = tmp_path / "node_modules" / "pkg" / "index.js"
    nm.parent.mkdir(parents=True)
    nm.write_text("export {}")
    vendor_under_nm = tmp_path / "node_modules" / "vendor" / "lib.py"
    vendor_under_nm.parent.mkdir(parents=True)
    vendor_under_nm.write_text("x = 1")

    assert should_exclude_context_path(tmp_path, nm)
    assert should_exclude_context_path(tmp_path, vendor_under_nm)


def test_tests_excluded_by_default(tmp_path):
    test_file = tmp_path / "test_app.py"
    test_file.write_text("def test_x():\n    pass\n")

    assert should_exclude_context_path(tmp_path, test_file)
    assert not should_exclude_context_path(tmp_path, test_file, include_tests=True)


def test_existing_external_project_path_excluded(tmp_path):
    external = tmp_path.parent / "external_project_path.py"
    external.write_text("def main():\n    return 1\n", encoding="utf-8")

    assert should_exclude_context_path(tmp_path, external)


def test_relative_from_import_without_module_discovers_sibling(tmp_path):
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


def test_max_surfaced_caps_injected_related_files(tmp_path, monkeypatch):
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir(parents=True)
    source.write_text("from . import mod0\n" + "x = 1\n" * 400, encoding="utf-8")
    for index in range(6):
        (source.parent / f"mod{index}.py").write_text(f"def f{index}():\n    return {index}\n")

    imports = [{"module": f".mod{i}", "names": [f"f{i}"], "is_from": True} for i in range(6)]

    def fake_extract(path: str, base_path: str):
        return {"imports": imports, "functions": [], "classes": []}

    monkeypatch.setattr("tldr.hooks.read.extract_file", fake_extract)
    result = build_read_response(_event(tmp_path, str(source.relative_to(tmp_path))))

    assert result.status == "ok"
    assert len(result.surfaced_files) == MAX_SURFACED


def test_max_candidates_limits_metadata(tmp_path, monkeypatch):
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir(parents=True)
    source.write_text("from . import mod0\n" + "x = 1\n" * 400, encoding="utf-8")
    for index in range(12):
        (source.parent / f"mod{index}.py").write_text(f"def f{index}():\n    return {index}\n")

    imports = [{"module": f".mod{i}", "names": [f"f{i}"], "is_from": True} for i in range(12)]

    def fake_extract(path: str, base_path: str):
        return {"imports": imports, "functions": [], "classes": []}

    monkeypatch.setattr("tldr.hooks.read.extract_file", fake_extract)
    result = build_read_response(_event(tmp_path, str(source.relative_to(tmp_path))))

    assert result.status == "ok"
    assert len(result.candidate_files) <= MAX_CANDIDATES
    assert len(result.surfaced_files) <= MAX_SURFACED


def test_no_candidates_leaves_surfaced_files_empty(tmp_path, monkeypatch):
    source = tmp_path / "solo.py"
    source.write_text("def main():\n    return 1\n" + "x = 1\n" * 400, encoding="utf-8")

    def fake_extract(path: str, base_path: str):
        return {"imports": [], "functions": [], "classes": []}

    monkeypatch.setattr("tldr.hooks.read.extract_file", fake_extract)
    result = build_read_response(_event(tmp_path, "solo.py"))

    assert result.status == "ok"
    assert result.surfaced_files == []
