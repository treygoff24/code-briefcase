from __future__ import annotations

from pathlib import Path

import pytest

from code_briefcase.hooks.path_policy import _resolve_import_module
from code_briefcase.hooks.runtime import HookEvent


def _event(tmp_path: Path) -> HookEvent:
    return HookEvent(
        client="claude",
        event_name="PreToolUse",
        tool_name="Read",
        tool_input={},
        cwd=tmp_path,
        session_id="s1",
        raw={},
    )


def test_long_module_string_returns_none_without_error(tmp_path: Path) -> None:
    source = tmp_path / "app.ts"
    source.write_text("export const x = 1;\n", encoding="utf-8")
    garbage = "{" * 200
    assert _resolve_import_module(_event(tmp_path), source, garbage) is None


def test_relative_ts_import_resolves_sibling_file(tmp_path: Path) -> None:
    source = tmp_path / "council-runtime.tsx"
    sibling = tmp_path / "council-runtime.prepare.ts"
    source.write_text("export const x = 1;\n", encoding="utf-8")
    sibling.write_text("export const y = 2;\n", encoding="utf-8")

    resolved = _resolve_import_module(
        _event(tmp_path), source, "./council-runtime.prepare"
    )
    assert resolved == sibling.resolve()


@pytest.mark.parametrize("module", ["react", "@scope/foo", "@/lib/utils"])
def test_bare_ts_module_specs_return_none(tmp_path: Path, module: str) -> None:
    source = tmp_path / "app.ts"
    source.write_text(f'import x from "{module}";\n', encoding="utf-8")
    assert _resolve_import_module(_event(tmp_path), source, module) is None
