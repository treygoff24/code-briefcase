import json

from tldr.hook_installer import install_hooks


def test_dry_run_does_not_write(tmp_path):
    config = tmp_path / "settings.json"

    result = install_hooks("claude", config_path=str(config), dry_run=True, tldr_path="/bin/echo")

    assert result.changed
    assert not config.exists()


def test_merge_preserves_existing_hooks(tmp_path):
    config = tmp_path / "settings.json"
    config.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Read",
                            "hooks": [{"type": "command", "command": "other-tool", "timeout": 1}],
                        }
                    ]
                }
            }
        )
    )

    install_hooks("claude", config_path=str(config), tldr_path="/bin/echo")
    data = json.loads(config.read_text())
    hooks = data["hooks"]["PreToolUse"][0]["hooks"]

    assert any(hook["command"] == "other-tool" for hook in hooks)
    assert any("hooks run pre-read" in hook["command"] for hook in hooks)


def test_rerunning_installer_is_idempotent(tmp_path):
    config = tmp_path / "settings.json"

    first = install_hooks("codex", config_path=str(config), tldr_path="/bin/echo")
    second = install_hooks("codex", config_path=str(config), tldr_path="/bin/echo")

    assert first.changed
    assert not second.changed
    assert second.actions == []


def test_backup_created_on_write(tmp_path):
    config = tmp_path / "settings.json"
    config.write_text("{}\n")

    result = install_hooks("claude", config_path=str(config), tldr_path="/bin/echo")

    assert result.backup_path is not None
    assert result.backup_path.exists()


def test_codex_output_has_top_level_hooks(tmp_path):
    config = tmp_path / "hooks.json"

    install_hooks("codex", config_path=str(config), tldr_path="/bin/echo")
    data = json.loads(config.read_text())

    assert "hooks" in data
    assert "PreToolUse" in data["hooks"]


def test_codex_installer_uses_latest_supported_matchers(tmp_path):
    config = tmp_path / "hooks.json"

    install_hooks("codex", config_path=str(config), tldr_path="/bin/echo")
    data = json.loads(config.read_text())
    serialized = json.dumps(data)

    assert "hooks run pre-read" not in serialized
    assert data["hooks"]["SessionStart"][0]["matcher"] == "startup|resume|clear"
    assert data["hooks"]["PreToolUse"][0]["matcher"] == "apply_patch|Edit|Write"
    assert data["hooks"]["PostToolUse"][0]["matcher"] == "apply_patch|Edit|Write"


def test_codex_installer_removes_stale_tldr_read_and_old_matcher_groups(tmp_path):
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

    install_hooks("codex", config_path=str(config), tldr_path="/bin/echo")
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


def test_claude_output_has_hooks_key(tmp_path):
    config = tmp_path / "settings.json"

    install_hooks("claude", config_path=str(config), tldr_path="/bin/echo")

    assert "hooks" in json.loads(config.read_text())


def test_existing_legacy_read_hook_is_replaced(tmp_path):
    config = tmp_path / "settings.json"
    config.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Read",
                            "hooks": [{"type": "command", "command": "~/.claude-shared/hooks/tldr-read.mjs"}],
                        }
                    ]
                }
            }
        )
    )

    result = install_hooks("claude", config_path=str(config), dry_run=True, tldr_path="/bin/echo")

    assert any("legacy TLDR hook" in action for action in result.actions)
    assert "tldr-read.mjs" not in json.dumps(result.config)


def test_existing_legacy_diagnostics_hook_is_replaced(tmp_path):
    config = tmp_path / "settings.json"
    config.write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "Edit|Write|MultiEdit|Update",
                            "hooks": [{"type": "command", "command": "post-edit-diagnostics.mjs"}],
                        }
                    ]
                }
            }
        )
    )

    result = install_hooks("claude", config_path=str(config), dry_run=True, tldr_path="/bin/echo")

    assert "post-edit-diagnostics.mjs" not in json.dumps(result.config)


def test_unrelated_settings_keys_remain_unchanged(tmp_path):
    config = tmp_path / "settings.json"
    config.write_text(json.dumps({"permissions": {"allow": ["Read"]}, "statusLine": "ok"}))

    install_hooks("claude", config_path=str(config), tldr_path="/bin/echo")
    data = json.loads(config.read_text())

    assert data["permissions"] == {"allow": ["Read"]}
    assert data["statusLine"] == "ok"


def test_installed_hook_commands_use_absolute_paths(tmp_path):
    config = tmp_path / "hooks.json"
    fake = tmp_path / "bin" / "tldr"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)

    result = install_hooks("codex", config_path=str(config), tldr_path=str(fake))
    payload = json.dumps(result.config)

    assert str(fake.resolve()) in payload
