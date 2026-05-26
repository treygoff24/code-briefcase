from typing import Any
import json
import subprocess
import sys


def test_extract_accepts_format_json(tmp_path: Any) -> None:
    source = tmp_path / "sample.py"
    source.write_text("def hello():\n    return 'world'\n")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "code_briefcase.cli",
            "extract",
            str(source),
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
