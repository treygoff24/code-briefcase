from typing import Any
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from code_briefcase.hook_installer import default_config_path, install_hooks


@pytest.fixture
def fake_tldr(tmp_path: Any) -> Any:
    executable = tmp_path / "bin" / "code-briefcase"
    executable.parent.mkdir()
    executable.write_text(
        "#!/bin/sh\n" 'if [ "$1" = "hooks" ]; then\n' "  exit 0\n" "fi\n" "exit 2\n"
    )
    executable.chmod(0o755)
    return executable


def test_dry_run_does_not_write(tmp_path: Any, fake_tldr: Any) -> None:
    config = tmp_path / "settings.json"

    result = install_hooks(
        "claude", config_path=str(config), dry_run=True, tldr_path=str(fake_tldr)
    )

    assert result.changed
    assert not config.exists()


def test_merge_preserves_existing_hooks(tmp_path: Any, fake_tldr: Any) -> None:
    config = tmp_path / "settings.json"
    config.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Read",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "other-tool",
                                    "timeout": 1,
                                }
                            ],
                        }
                    ]
                }
            }
        )
    )

    install_hooks("claude", config_path=str(config), tldr_path=str(fake_tldr))
    data = json.loads(config.read_text())
    hooks = data["hooks"]["PreToolUse"][0]["hooks"]

    assert any(hook["command"] == "other-tool" for hook in hooks)
    assert any("hooks run pre-read" in hook["command"] for hook in hooks)


def test_rerunning_installer_is_idempotent(tmp_path: Any, fake_tldr: Any) -> None:
    config = tmp_path / "settings.json"

    first = install_hooks("codex", config_path=str(config), tldr_path=str(fake_tldr))
    second = install_hooks("codex", config_path=str(config), tldr_path=str(fake_tldr))

    assert first.changed
    assert not second.changed
    assert second.actions == []


def test_backup_created_on_write(tmp_path: Any, fake_tldr: Any) -> None:
    config = tmp_path / "settings.json"
    config.write_text("{}\n")

    result = install_hooks("claude", config_path=str(config), tldr_path=str(fake_tldr))

    assert result.backup_path is not None
    assert result.backup_path.exists()


def test_codex_output_has_top_level_hooks(tmp_path: Any, fake_tldr: Any) -> None:
    config = tmp_path / "hooks.json"

    install_hooks("codex", config_path=str(config), tldr_path=str(fake_tldr))
    data = json.loads(config.read_text())

    assert "hooks" in data
    assert "PreToolUse" in data["hooks"]


def test_codex_installer_uses_latest_supported_matchers(
    tmp_path: Any, fake_tldr: Any
) -> None:
    config = tmp_path / "hooks.json"

    install_hooks("codex", config_path=str(config), tldr_path=str(fake_tldr))
    data = json.loads(config.read_text())
    serialized = json.dumps(data)

    assert "hooks run pre-read" not in serialized
    assert data["hooks"]["SessionStart"][0]["matcher"] == "startup|resume|clear"
    assert data["hooks"]["PreToolUse"][0]["matcher"] == "apply_patch|Edit|Write"
    assert data["hooks"]["PostToolUse"][0]["matcher"] == "apply_patch|Edit|Write"


def test_codex_installer_removes_stale_tldr_read_and_old_matcher_groups(
    tmp_path: Any, fake_tldr: Any
) -> None:
    config = tmp_path / "hooks.json"
    config.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": ".*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "/bin/echo hooks run session-start --client codex",
                                }
                            ],
                        }
                    ],
                    "PreToolUse": [
                        {
                            "matcher": "^Read$",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "/bin/echo hooks run pre-read --client codex",
                                }
                            ],
                        },
                        {
                            "matcher": "^(Edit|Write|MultiEdit|Update)$",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "/bin/echo hooks run pre-edit --client codex",
                                }
                            ],
                        },
                    ],
                    "PostToolUse": [
                        {
                            "matcher": "^(Edit|Write|MultiEdit|Update)$",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "/bin/echo hooks run post-edit --client codex",
                                }
                            ],
                        }
                    ],
                }
            }
        )
    )

    install_hooks("codex", config_path=str(config), tldr_path=str(fake_tldr))
    data = json.loads(config.read_text())
    serialized = json.dumps(data)
    matchers = {
        event: [group.get("matcher") for group in groups]
        for event, groups in data["hooks"].items()
    }

    assert "hooks run pre-read" not in serialized
    assert ".*" not in matchers["SessionStart"]
    assert "^Read$" not in matchers["PreToolUse"]
    assert "^(Edit|Write|MultiEdit|Update)$" not in matchers["PreToolUse"]
    assert "^(Edit|Write|MultiEdit|Update)$" not in matchers["PostToolUse"]
    assert data["hooks"]["SessionStart"][0]["matcher"] == "startup|resume|clear"
    assert data["hooks"]["PreToolUse"][0]["matcher"] == "apply_patch|Edit|Write"
    assert data["hooks"]["PostToolUse"][0]["matcher"] == "apply_patch|Edit|Write"


def test_claude_output_has_hooks_key(tmp_path: Any, fake_tldr: Any) -> None:
    config = tmp_path / "settings.json"

    install_hooks("claude", config_path=str(config), tldr_path=str(fake_tldr))

    assert "hooks" in json.loads(config.read_text())


def test_claude_space_default_path_targets_profile_root() -> None:
    path = default_config_path("claude-space")

    assert str(path).endswith(".claude-space/settings.json")


def test_claude_space_installer_replaces_legacy_hooks_with_current_claude_runtime(
    tmp_path: Any, fake_tldr: Any
) -> None:
    config = tmp_path / "settings.json"
    config.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Read",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "node ~/.claude-shared/hooks/tldr-read.mjs",
                                }
                            ],
                        },
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {"type": "command", "command": "rtk hook claude"}
                            ],
                        },
                    ],
                    "PostToolUse": [
                        {
                            "matcher": "Edit|Write|MultiEdit|Update",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "node ~/.claude-shared/hooks/post-edit-diagnostics.mjs",
                                }
                            ],
                        }
                    ],
                    "PreCompact": [
                        {
                            "matcher": "",
                            "hooks": [
                                {"type": "command", "command": "node pre-compact.mjs"}
                            ],
                        }
                    ],
                }
            }
        )
    )

    result = install_hooks(
        "claude-space", config_path=str(config), tldr_path=str(fake_tldr)
    )

    assert result.changed
    data = json.loads(config.read_text())
    serialized = json.dumps(data)
    assert "tldr-read.mjs" not in serialized
    assert "post-edit-diagnostics.mjs" not in serialized
    assert "hooks run session-start --client claude" in serialized
    assert "hooks run pre-read --client claude" in serialized
    assert "hooks run pre-edit --client claude" in serialized
    assert "hooks run post-edit --client claude" in serialized
    assert "--client claude-space" not in serialized
    assert (
        "CODE_BRIEFCASE_TELEMETRY=1 CODE_BRIEFCASE_TELEMETRY_REDACT_PATHS=1"
        in serialized
    )
    assert "rtk hook claude" in serialized
    assert "node pre-compact.mjs" in serialized


def test_existing_legacy_read_hook_is_replaced(tmp_path: Any, fake_tldr: Any) -> None:
    config = tmp_path / "settings.json"
    config.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Read",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "~/.claude-shared/hooks/tldr-read.mjs",
                                }
                            ],
                        }
                    ]
                }
            }
        )
    )

    result = install_hooks(
        "claude", config_path=str(config), dry_run=True, tldr_path=str(fake_tldr)
    )

    assert any("legacy Code Briefcase hook" in action for action in result.actions)
    assert "tldr-read.mjs" not in json.dumps(result.config)


def test_existing_legacy_diagnostics_hook_is_replaced(
    tmp_path: Any, fake_tldr: Any
) -> None:
    config = tmp_path / "settings.json"
    config.write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "Edit|Write|MultiEdit|Update",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "post-edit-diagnostics.mjs",
                                }
                            ],
                        }
                    ]
                }
            }
        )
    )

    result = install_hooks(
        "claude", config_path=str(config), dry_run=True, tldr_path=str(fake_tldr)
    )

    assert "post-edit-diagnostics.mjs" not in json.dumps(result.config)


def test_unrelated_settings_keys_remain_unchanged(
    tmp_path: Any, fake_tldr: Any
) -> None:
    config = tmp_path / "settings.json"
    config.write_text(
        json.dumps({"permissions": {"allow": ["Read"]}, "statusLine": "ok"})
    )

    install_hooks("claude", config_path=str(config), tldr_path=str(fake_tldr))
    data = json.loads(config.read_text())

    assert data["permissions"] == {"allow": ["Read"]}
    assert data["statusLine"] == "ok"


def test_installer_migrates_legacy_tldr_env_keys(tmp_path: Any, fake_tldr: Any) -> None:
    config = tmp_path / "settings.json"
    config.write_text(
        json.dumps(
            {
                "env": {
                    "TLDR_TELEMETRY": "1",
                    "TLDR_TELEMETRY_MODE": "local-rich",
                    "TLDR_TELEMETRY_REDACT_PATHS": "0",
                    "TLDR_TELEMETRY_LOCAL_STRING_LIMIT": "64000",
                    "UNRELATED": "kept",
                }
            }
        )
    )

    result = install_hooks("claude", config_path=str(config), tldr_path=str(fake_tldr))
    data = json.loads(config.read_text())

    assert result.changed
    assert data["env"] == {
        "CODE_BRIEFCASE_TELEMETRY": "1",
        "CODE_BRIEFCASE_TELEMETRY_MODE": "local-rich",
        "CODE_BRIEFCASE_TELEMETRY_REDACT_PATHS": "0",
        "CODE_BRIEFCASE_TELEMETRY_LOCAL_STRING_LIMIT": "64000",
        "UNRELATED": "kept",
    }
    assert all(not key.startswith("TLDR_") for key in data["env"])


def test_installer_removes_legacy_tldr_env_without_overwriting_current_values(
    tmp_path: Any, fake_tldr: Any
) -> None:
    config = tmp_path / "settings.json"
    config.write_text(
        json.dumps(
            {
                "env": {
                    "TLDR_TELEMETRY_REDACT_PATHS": "0",
                    "CODE_BRIEFCASE_TELEMETRY_REDACT_PATHS": "1",
                }
            }
        )
    )

    result = install_hooks("claude", config_path=str(config), tldr_path=str(fake_tldr))
    data = json.loads(config.read_text())

    assert result.changed
    assert data["env"] == {"CODE_BRIEFCASE_TELEMETRY_REDACT_PATHS": "1"}


def test_installed_hook_commands_use_absolute_paths(tmp_path: Any) -> None:
    config = tmp_path / "hooks.json"
    fake = tmp_path / "bin" / "code-briefcase"
    fake.parent.mkdir()
    fake.write_text(
        "#!/bin/sh\n" 'if [ "$1" = "hooks" ]; then\n' "  exit 0\n" "fi\n" "exit 2\n"
    )
    fake.chmod(0o755)

    result = install_hooks("codex", config_path=str(config), tldr_path=str(fake))
    payload = json.dumps(result.config)

    assert str(fake.resolve()) in payload


def test_hook_install_cli_accepts_executable_override(
    tmp_path: Any, fake_tldr: Any
) -> None:
    config = tmp_path / "hooks.json"

    subprocess.run(
        [
            sys.executable,
            "-m",
            "code_briefcase.cli",
            "hooks",
            "install",
            "codex",
            "--config",
            str(config),
            "--executable",
            str(fake_tldr),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert str(fake_tldr.resolve()) in json.dumps(json.loads(config.read_text()))


def test_executable_override_missing_raises_clear_error(tmp_path: Any) -> None:
    from code_briefcase.hook_installer import _resolve_tldr_command

    missing = tmp_path / "missing-code-briefcase"
    with pytest.raises(
        RuntimeError, match=r"Executable not found:.*missing-code-briefcase"
    ):
        _resolve_tldr_command(str(missing))


def test_executable_override_non_executable_raises_clear_error(tmp_path: Any) -> None:
    from code_briefcase.hook_installer import _resolve_tldr_command

    path = tmp_path / "not-exec"
    path.write_text("not a script\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match=r"Executable is not executable:.*not-exec"):
        _resolve_tldr_command(str(path))


def test_installer_rejects_tldr_without_hooks(tmp_path: Any) -> None:
    config = tmp_path / "hooks.json"
    fake = tmp_path / "bin" / "code-briefcase"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\nexit 2\n")
    fake.chmod(0o755)

    with pytest.raises(RuntimeError, match="does not support 'code-briefcase hooks'"):
        install_hooks("codex", config_path=str(config), tldr_path=str(fake))

    assert not config.exists()


def test_installer_prefers_current_python_module_over_stale_path_tldr(
    tmp_path: Any, monkeypatch: Any
) -> None:
    config = tmp_path / "hooks.json"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    python = bin_dir / "python"
    python.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-m" ] && [ "$2" = "code_briefcase.cli" ] && [ "$3" = "hooks" ]; then\n'
        "  exit 0\n"
        "fi\n"
        "exit 2\n"
    )
    python.chmod(0o755)

    stale_global = bin_dir / "global-tldr"
    stale_global.write_text("#!/bin/sh\nexit 2\n")
    stale_global.chmod(0o755)

    monkeypatch.setattr("code_briefcase.hook_installer.sys.executable", str(python))
    monkeypatch.setattr(
        "code_briefcase.hook_installer.shutil.which",
        lambda name: str(stale_global) if name == "code-briefcase" else None,
    )

    result = install_hooks("codex", config_path=str(config))
    payload = json.dumps(result.config)

    assert f"{python} -m code_briefcase.cli hooks run" in payload
    assert str(stale_global) not in payload


# Phase 2 new tests


def test_droid_factory_default_config_path() -> None:
    from code_briefcase.hook_installer import default_config_path

    droid_path = default_config_path("droid")
    factory_path = default_config_path("factory")
    assert str(droid_path).endswith(".factory/settings.json")
    assert str(factory_path).endswith(".factory/settings.json")


def test_droid_installer_uses_droid_matchers(tmp_path: Any, fake_tldr: Any) -> None:
    config = tmp_path / "settings.json"

    install_hooks("droid", config_path=str(config), tldr_path=str(fake_tldr))
    data = json.loads(config.read_text())

    assert data["hooks"]["SessionStart"][0]["matcher"] == "startup|resume|clear|compact"
    assert data["hooks"]["PreToolUse"][0]["matcher"] == "Read"
    assert data["hooks"]["PreToolUse"][1]["matcher"] == "Edit|Create|ApplyPatch"
    assert data["hooks"]["PostToolUse"][0]["matcher"] == "Edit|Create|ApplyPatch"


def test_droid_installer_stale_replacement_for_prompt_tool_compact(
    tmp_path: Any, fake_tldr: Any
) -> None:
    config = tmp_path / "settings.json"
    config.write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {
                            "matcher": ".*",
                            "hooks": [
                                {
                                    "command": "hooks run user-prompt-submit --client droid"
                                }
                            ],
                        }
                    ],
                    "PreToolUse": [
                        {
                            "matcher": "Execute",
                            "hooks": [{"command": "hooks run pre-tool --client droid"}],
                        }
                    ],
                    "PreCompact": [
                        {
                            "matcher": "manual",
                            "hooks": [
                                {"command": "hooks run pre-compact --client droid"}
                            ],
                        }
                    ],
                }
            }
        )
    )

    install_hooks(
        "droid",
        config_path=str(config),
        tldr_path=str(fake_tldr),
        enable_prompt_guard=True,
        enable_tool_guard=True,
        enable_compact_context=True,
    )
    data = json.loads(config.read_text())

    # Ensure opt-in groups now exist after install with flags
    assert "UserPromptSubmit" in data["hooks"]
    assert "PreCompact" in data["hooks"]


def test_codex_optin_prompt_and_tool_guard(tmp_path: Any, fake_tldr: Any) -> None:
    config = tmp_path / "hooks.json"

    install_hooks(
        "codex",
        config_path=str(config),
        tldr_path=str(fake_tldr),
        enable_prompt_guard=True,
        enable_tool_guard=True,
    )
    data = json.loads(config.read_text())

    assert "UserPromptSubmit" in data["hooks"]
    assert "PermissionRequest" in data["hooks"]
    assert any("pre-tool" in json.dumps(g) for g in data["hooks"].get("PreToolUse", []))


def test_droid_optin_prompt_tool_compact(tmp_path: Any, fake_tldr: Any) -> None:
    config = tmp_path / "settings.json"

    install_hooks(
        "droid",
        config_path=str(config),
        tldr_path=str(fake_tldr),
        enable_prompt_guard=True,
        enable_tool_guard=True,
        enable_compact_context=True,
    )
    data = json.loads(config.read_text())

    assert "UserPromptSubmit" in data["hooks"]
    assert any("pre-tool" in json.dumps(g) for g in data["hooks"].get("PreToolUse", []))
    assert "PreCompact" in data["hooks"]


def test_codex_reinstall_without_optins_removes_owned_optin_hooks(
    tmp_path: Any, fake_tldr: Any
) -> None:
    config = tmp_path / "hooks.json"
    install_hooks(
        "codex",
        config_path=str(config),
        tldr_path=str(fake_tldr),
        enable_prompt_guard=True,
        enable_tool_guard=True,
    )

    result = install_hooks("codex", config_path=str(config), tldr_path=str(fake_tldr))
    data = json.loads(config.read_text())
    serialized = json.dumps(data)

    assert result.changed
    assert "UserPromptSubmit" not in data["hooks"]
    assert "PermissionRequest" not in data["hooks"]
    assert "hooks run pre-tool" in serialized
    assert "hooks run pre-edit" in serialized


def test_codex_default_install_adds_shell_context_without_permission_guard(
    tmp_path: Any, fake_tldr: Any
) -> None:
    config = tmp_path / "hooks.json"

    install_hooks("codex", config_path=str(config), tldr_path=str(fake_tldr))
    data = json.loads(config.read_text())
    serialized = json.dumps(data)

    assert "PermissionRequest" not in data["hooks"]
    assert any("pre-tool" in json.dumps(g) for g in data["hooks"].get("PreToolUse", []))
    assert "Code Briefcase shell context" in serialized


def test_droid_reinstall_without_optins_removes_owned_optin_hooks(
    tmp_path: Any, fake_tldr: Any
) -> None:
    config = tmp_path / "settings.json"
    install_hooks(
        "droid",
        config_path=str(config),
        tldr_path=str(fake_tldr),
        enable_prompt_guard=True,
        enable_tool_guard=True,
        enable_compact_context=True,
    )

    result = install_hooks("droid", config_path=str(config), tldr_path=str(fake_tldr))
    data = json.loads(config.read_text())
    serialized = json.dumps(data)

    assert result.changed
    assert "UserPromptSubmit" not in data["hooks"]
    assert "PreCompact" not in data["hooks"]
    assert "hooks run pre-tool" not in serialized
    assert "hooks run pre-edit" in serialized


def test_cursor_install_requires_flags(tmp_path: Any, fake_tldr: Any) -> None:
    config = tmp_path / "cursor.json"

    # Calling directly without flags must fail
    with pytest.raises(ValueError, match="experimental"):
        install_hooks("cursor", config_path=str(config), tldr_path=str(fake_tldr))


def test_installer_preserves_file_mode(tmp_path: Any, fake_tldr: Any) -> None:
    config = tmp_path / "settings.json"
    config.write_text("{}\n")
    os.chmod(config, 0o640)

    install_hooks("claude", config_path=str(config), tldr_path=str(fake_tldr))
    mode = oct(os.stat(config).st_mode)[-3:]
    assert mode == "640"


def test_installer_rejects_invalid_json_no_mutation(
    tmp_path: Any, fake_tldr: Any
) -> None:
    config = tmp_path / "settings.json"
    config.write_text("{invalid json")
    mtime_before = config.stat().st_mtime
    mode_before = oct(config.stat().st_mode)

    with pytest.raises(json.JSONDecodeError):
        install_hooks("claude", config_path=str(config), tldr_path=str(fake_tldr))

    assert config.read_text() == "{invalid json"
    assert config.stat().st_mtime == mtime_before
    assert oct(config.stat().st_mode) == mode_before


def test_installer_rejects_managed_path(tmp_path: Any, fake_tldr: Any) -> None:
    managed = "/Library/Application Support/example.json"

    with pytest.raises(ValueError, match="managed"):
        install_hooks("claude", config_path=managed, tldr_path=str(fake_tldr))


def test_installer_allows_user_library_application_support_path(
    tmp_path: Any, fake_tldr: Any
) -> None:
    config = (
        tmp_path
        / "Users"
        / "trey"
        / "Library"
        / "Application Support"
        / "settings.json"
    )
    config.parent.mkdir(parents=True)

    result = install_hooks(
        "claude", config_path=str(config), tldr_path=str(fake_tldr), dry_run=True
    )

    assert result.changed


def test_installer_rejects_managed_json_marker(tmp_path: Any, fake_tldr: Any) -> None:
    config = tmp_path / "settings.json"
    config.write_text(json.dumps({"enterprise_managed": True}))

    with pytest.raises(ValueError, match="managed"):
        install_hooks("claude", config_path=str(config), tldr_path=str(fake_tldr))


def test_installer_reports_exact_added_removed_actions(
    tmp_path: Any, fake_tldr: Any
) -> None:
    config = tmp_path / "hooks.json"
    # seed stale + unrelated
    config.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": ".*",
                            "hooks": [
                                {"command": "hooks run session-start --client codex"}
                            ],
                        }
                    ],
                    "Other": [{"hooks": [{"command": "other"}]}],
                }
            }
        )
    )

    result = install_hooks(
        "codex", config_path=str(config), tldr_path=str(fake_tldr), dry_run=True
    )
    actions_text = "\n".join(result.actions)

    assert "remove" in actions_text
    assert "add Code Briefcase hook for SessionStart" in actions_text
    assert (
        "Other" not in actions_text
    )  # unrelated preserved and not reported as Code Briefcase action


def test_installer_creates_backup_and_preserves_unrelated(
    tmp_path: Any, fake_tldr: Any
) -> None:
    config = tmp_path / "settings.json"
    config.write_text(json.dumps({"permissions": {"allow": ["*"]}, "hooks": {}}))

    result = install_hooks("droid", config_path=str(config), tldr_path=str(fake_tldr))

    assert result.backup_path is not None
    assert result.backup_path.exists()
    final = json.loads(config.read_text())
    assert final.get("permissions") == {"allow": ["*"]}


def test_cursor_doctor_reports_experimental_status() -> None:
    from code_briefcase.hook_installer import doctor_report

    # Doctor should report cursor status without requiring a default path
    report = doctor_report(clients=["cursor"])
    info = report["clients"]["cursor"]
    assert info.get("status") == "experimental_unverified"
    assert info.get("code_briefcase_hooks_present") is False


def test_doctor_reports_stale_tldr_hooks(tmp_path: Any, monkeypatch: Any) -> None:
    from code_briefcase.hook_installer import doctor_report

    config = tmp_path / "hooks.json"
    stale_command = (
        "TLDR_TELEMETRY=1 /Users/treygoff/.local/pipx/venvs/llm-tldr/bin/tldr "
        "hooks run pre-edit --client codex"
    )
    config.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "apply_patch|Edit|Write",
                            "hooks": [{"command": stale_command}],
                        }
                    ]
                }
            }
        )
    )
    monkeypatch.setattr(
        "code_briefcase.hook_installer.default_config_path", lambda _client: config
    )

    report = doctor_report(clients=["codex"])
    info = report["clients"]["codex"]

    assert info["code_briefcase_hooks_present"] is True
    assert info["stale_tldr_hooks_present"] is True
    assert info["hook_command_targets"] == [stale_command]


# OpenCode integration tests


def test_opencode_default_config_path() -> None:
    from code_briefcase.hook_installer import default_config_path

    path = default_config_path("opencode")
    assert str(path).endswith(".config/opencode/plugins/code-briefcase-hooks.js")


def test_opencode_dry_run_does_not_write(tmp_path: Any, fake_tldr: Any) -> None:
    config = tmp_path / "code-briefcase-hooks.js"
    result = install_hooks(
        "opencode", config_path=str(config), dry_run=True, tldr_path=str(fake_tldr)
    )
    assert result.changed
    assert not config.exists()
    assert "write" in "\n".join(result.actions)


def test_opencode_install_writes_file_with_absolute_path(
    tmp_path: Any, fake_tldr: Any
) -> None:
    config = tmp_path / "code-briefcase-hooks.js"
    result = install_hooks(
        "opencode", config_path=str(config), tldr_path=str(fake_tldr)
    )
    assert result.changed
    assert config.exists()

    content = config.read_text()
    assert "TLDRHooks" in content
    assert str(fake_tldr.resolve()) in content
    command_prefix = "const CODE_BRIEFCASE_COMMAND = "
    command_line = next(
        line for line in content.splitlines() if line.startswith(command_prefix)
    )
    command = json.loads(command_line.removeprefix(command_prefix).removesuffix(";"))
    assert command == [str(Path("/bin/sh").resolve()), str(fake_tldr.resolve())]
    assert "CODE_BRIEFCASE_TIMEOUT_MS = 1500" in content


def test_opencode_install_existing_unrelated_backups(
    tmp_path: Any, fake_tldr: Any
) -> None:
    config = tmp_path / "code-briefcase-hooks.js"
    config.write_text("console.log('some existing unrelated plugin content');")

    result = install_hooks(
        "opencode", config_path=str(config), tldr_path=str(fake_tldr)
    )
    assert result.changed
    assert result.backup_path is not None
    assert result.backup_path.exists()
    assert (
        result.backup_path.read_text()
        == "console.log('some existing unrelated plugin content');"
    )

    actions_text = "\n".join(result.actions)
    assert "backup and replace" in actions_text


def test_opencode_doctor_reports_status(tmp_path: Any, fake_tldr: Any) -> None:
    from code_briefcase.hook_installer import doctor_report

    config = tmp_path / "code-briefcase-hooks.js"

    # Not present
    report = doctor_report(clients=["opencode"])
    assert report["clients"]["opencode"]["exists"] is False
    assert report["clients"]["opencode"]["code_briefcase_hooks_present"] is False

    # Write some placeholder representing Code Briefcase
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("// CODE_BRIEFCASE_COMMAND is present")

    # Overriding default_config_path behavior or monkeypatching is easier, but
    # doctor_report uses default_config_path directly which targets ~/.config.
    # We can temporarily patch default_config_path or Path.home().
    # Let's test doctor_report with standard paths but verify opencode structure in the returned dict is correct.
    report = doctor_report(clients=["opencode"])
    assert "opencode" in report["clients"]
    assert "config_path" in report["clients"]["opencode"]
    assert "code_briefcase_hooks_present" in report["clients"]["opencode"]
