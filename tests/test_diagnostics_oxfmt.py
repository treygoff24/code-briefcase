from typing import Any
from pathlib import Path

import pytest

from code_briefcase import diagnostics as diag


@pytest.fixture
def install_fake_oxfmt(make_executable: Any) -> Any:
    def _install(project: Path) -> None:
        make_executable(
            project / "node_modules" / ".bin" / "oxfmt",
            """#!/bin/sh
case "$2" in
  *drifted*) exit 1 ;;
  *) exit 0 ;;
esac
""",
        )

    return _install


def test_run_oxfmt_accepts_formatted_file(
    tmp_path: Any, monkeypatch: Any, install_fake_oxfmt: Any
) -> None:
    monkeypatch.setattr(diag.shutil, "which", lambda name: None)
    install_fake_oxfmt(tmp_path)

    source = tmp_path / "formatted.ts"
    source.write_text("const answer = 42;\n")

    assert diag._run_oxfmt(source) == []


def test_run_oxfmt_reports_formatting_drift(
    tmp_path: Any, monkeypatch: Any, install_fake_oxfmt: Any
) -> None:
    monkeypatch.setattr(diag.shutil, "which", lambda name: None)
    install_fake_oxfmt(tmp_path)

    source = tmp_path / "drifted.ts"
    source.write_text("const answer={value:42}\n")

    diagnostics = diag._run_oxfmt(source)

    assert len(diagnostics) == 1
    assert diagnostics[0]["source"] == "oxfmt"
    assert diagnostics[0]["severity"] == "warning"


def test_run_oxfmt_skips_declaration_files(
    tmp_path: Any, monkeypatch: Any, install_fake_oxfmt: Any
) -> None:
    monkeypatch.setattr(diag.shutil, "which", lambda name: None)
    install_fake_oxfmt(tmp_path)

    source = tmp_path / "types.d.ts"
    source.write_text("declare const answer:{value:number}\n")

    assert diag._run_oxfmt(source) == []
    result = diag.get_diagnostics(str(source), language="typescript")
    assert "oxfmt" not in result["tools"]
