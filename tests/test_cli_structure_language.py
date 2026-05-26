from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def run_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "code_briefcase.cli", *args],
        capture_output=True,
        text=True,
        check=True,
    )


def test_structure_single_tsx_file_reports_typescript_language(tmp_path: Path) -> None:
    tsx = tmp_path / "widget.tsx"
    tsx.write_text(
        "export function Widget(): JSX.Element {\n  return <div />;\n}\n",
        encoding="utf-8",
    )

    result = run_cli(["structure", str(tsx), "--lang", "auto"])
    payload = json.loads(result.stdout)

    assert payload["languages"] == ["typescript"]
