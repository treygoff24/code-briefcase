import json
import subprocess
import sys

from tldr.diagnostics import DIAGNOSTIC_RUNNERS, LANG_TOOLS, TOOL_SLOTS


def tool_names(language: str, slot: str) -> list[str]:
    return [tool["name"] for tool in LANG_TOOLS[language][slot]]


def test_diagnostic_runners_match_lang_tools():
    assert set(DIAGNOSTIC_RUNNERS) == set(LANG_TOOLS)


def test_javascript_typescript_tool_config_snapshot():
    assert tool_names("typescript", "type_checkers") == ["tsc"]
    assert tool_names("typescript", "linters") == ["oxlint"]
    assert tool_names("typescript", "formatters") == ["oxfmt"]

    assert tool_names("javascript", "type_checkers") == ["tsc"]
    assert tool_names("javascript", "linters") == ["oxlint"]
    assert tool_names("javascript", "formatters") == ["oxfmt"]


def test_doctor_json_is_derived_from_lang_tools():
    result = subprocess.run(
        [sys.executable, "-m", "tldr.cli", "doctor", "--json"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    doctor = json.loads(result.stdout)
    assert set(doctor) == set(LANG_TOOLS)

    for language, config in LANG_TOOLS.items():
        for slot in TOOL_SLOTS:
            expected = [tool["name"] for tool in config.get(slot, [])]
            actual = [tool["name"] for tool in doctor[language][slot]]
            assert actual == expected
