import json
import subprocess
import sys
from typing import Literal, cast

from code_briefcase.diagnostics import DIAGNOSTIC_RUNNERS, LANG_TOOLS, TOOL_SLOTS

ToolSlot = Literal["type_checkers", "linters", "formatters"]


def tool_names(language: str, slot: ToolSlot) -> list[str]:
    tools = cast(list[dict[str, str]], LANG_TOOLS[language].get(slot, []))
    return [tool["name"] for tool in tools]


def test_diagnostic_runners_match_lang_tools() -> None:
    assert set(DIAGNOSTIC_RUNNERS) == set(LANG_TOOLS)


def test_javascript_typescript_tool_config_snapshot() -> None:
    assert tool_names("typescript", "type_checkers") == ["tsc"]
    assert tool_names("typescript", "linters") == ["oxlint"]
    assert tool_names("typescript", "formatters") == ["oxfmt"]

    assert tool_names("javascript", "type_checkers") == ["tsc"]
    assert tool_names("javascript", "linters") == ["oxlint"]
    assert tool_names("javascript", "formatters") == ["oxfmt"]


def test_doctor_json_is_derived_from_lang_tools() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "code_briefcase.cli", "doctor", "--json"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    doctor = cast(dict[str, dict[str, list[dict[str, str]]]], json.loads(result.stdout))
    assert set(doctor) == set(LANG_TOOLS)

    for language, config in LANG_TOOLS.items():
        for slot in TOOL_SLOTS:
            typed_slot = cast(ToolSlot, slot)
            expected_tools = cast(list[dict[str, str]], config.get(typed_slot, []))
            expected = [tool["name"] for tool in expected_tools]
            actual = [tool["name"] for tool in doctor[language][slot]]
            assert actual == expected
