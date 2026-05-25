from code_briefcase.hooks.permission import check_destructive_command, build_permission_request_response
from code_briefcase.hooks.runtime import parse_hook_event
from code_briefcase.hooks.tool import build_pre_tool_response


class TestDestructiveCommandDetection:
    # --- Commands that must be blocked ---

    def test_blocks_rm_rf_root(self):
        assert check_destructive_command("rm -rf /") is not None

    def test_blocks_sudo_rm_rf_root(self):
        assert check_destructive_command("sudo rm -rf /") is not None

    def test_blocks_rm_rf_home(self):
        assert check_destructive_command("rm -rf ~") is not None

    def test_blocks_sudo_rm_rf_home(self):
        assert check_destructive_command("sudo rm -rf ~") is not None

    def test_blocks_rm_rf_dollar_home(self):
        assert check_destructive_command("rm -rf $HOME") is not None

    def test_blocks_sudo_rm_rf_dollar_home(self):
        assert check_destructive_command("sudo rm -rf $HOME") is not None

    def test_blocks_mkfs(self):
        assert check_destructive_command("mkfs.ext4 /dev/sda1") is not None

    def test_blocks_dd_to_disk(self):
        assert check_destructive_command("dd if=/dev/zero of=/dev/sda") is not None

    def test_blocks_shred_dev(self):
        assert check_destructive_command("shred /dev/sda") is not None

    def test_blocks_rm_rf_current_dir(self):
        assert check_destructive_command("rm -rf .") is not None

    # --- Commands that must NOT be blocked ---

    def test_allows_npm_install(self):
        assert check_destructive_command("npm install") is None

    def test_allows_npm_test(self):
        assert check_destructive_command("npm test") is None

    def test_allows_git_commands(self):
        for cmd in ["git status", "git add .", "git commit -m 'msg'", "git push", "git pull"]:
            assert check_destructive_command(cmd) is None

    def test_allows_pytest(self):
        assert check_destructive_command("pytest tests/") is None

    def test_allows_make(self):
        assert check_destructive_command("make build") is None

    def test_allows_cargo_build(self):
        assert check_destructive_command("cargo build") is None

    def test_allows_python_run(self):
        assert check_destructive_command("python script.py") is None

    def test_allows_pip_install(self):
        assert check_destructive_command("pip install -r requirements.txt") is None

    def test_allows_docker(self):
        assert check_destructive_command("docker build -t myapp .") is None

    def test_allows_read_commands(self):
        for cmd in ["cat file.txt", "ls -la", "head -n 10 file.py", "tail -f log.txt"]:
            assert check_destructive_command(cmd) is None

    def test_allows_find(self):
        assert check_destructive_command("find . -name '*.py'") is None

    def test_allows_grep(self):
        assert check_destructive_command("grep -r 'pattern' src/") is None

    def test_allows_curl(self):
        assert check_destructive_command("curl https://example.com") is None

    def test_allows_mkdir(self):
        assert check_destructive_command("mkdir -p new/dir") is None

    def test_allows_cp(self):
        assert check_destructive_command("cp file1.txt file2.txt") is None

    def test_allows_mv(self):
        assert check_destructive_command("mv old.txt new.txt") is None

    def test_allows_echo(self):
        assert check_destructive_command("echo 'hello'") is None

    # --- Shell-aware tokenization tests ---

    def test_allows_rm_single_file(self):
        assert check_destructive_command("rm file.txt") is None

    def test_allows_rm_recursive_specific_path(self):
        # rm -rf with a specific file path is not a high-confidence destructive command
        assert check_destructive_command("rm -rf build/") is None

    def test_blocks_project_root_when_context_available(self, tmp_path):
        assert check_destructive_command(f"rm -rf {tmp_path}", project=tmp_path) is not None
        assert check_destructive_command(f"sudo rm -rf {tmp_path}", project=tmp_path) is not None

    def test_blocks_rm_rf_with_force_first(self):
        # Different flag orderings should still be caught
        assert check_destructive_command("rm -fr /") is not None

    def test_allows_force_only_rm_root_and_home(self):
        assert check_destructive_command("rm -f /") is None
        assert check_destructive_command("rm -f ~") is None
        assert check_destructive_command("sudo rm -f /") is None

    def test_blocks_safe_prefix_then_destructive_chain(self):
        assert check_destructive_command("npm test && rm -rf /") is not None
        assert check_destructive_command("make build; sudo rm -rf ~") is not None

    def test_blocks_sudo_with_options(self):
        assert check_destructive_command("sudo -u admin rm -rf .") is not None
        assert check_destructive_command("sudo -E rm -rf /") is not None

    def test_allows_safe_sudo(self):
        assert check_destructive_command("sudo apt-get update") is None

    def test_allows_empty_command(self):
        assert check_destructive_command("") is None

    def test_allows_whitespace_only(self):
        assert check_destructive_command("   ") is None


class TestBuildPermissionRequestResponse:
    def test_blocks_destructive_command(self):
        event = parse_hook_event(
            {
                "hook_event_name": "PermissionRequest",
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf /"},
                "cwd": "/tmp",
            },
            client="codex",
        )
        result = build_permission_request_response(event)
        assert result.status == "ok"
        assert result.response.permission_decision == "deny"

    def test_noop_for_safe_command(self):
        event = parse_hook_event(
            {
                "hook_event_name": "PermissionRequest",
                "tool_name": "Bash",
                "tool_input": {"command": "npm test"},
                "cwd": "/tmp",
            },
            client="codex",
        )
        result = build_permission_request_response(event)
        assert result.is_noop()

    def test_noop_for_no_command(self):
        event = parse_hook_event(
            {
                "hook_event_name": "PermissionRequest",
                "tool_name": "Bash",
                "tool_input": {},
                "cwd": "/tmp",
            },
            client="codex",
        )
        result = build_permission_request_response(event)
        assert result.is_noop()

    def test_handles_droid_execute_tool(self):
        event = parse_hook_event(
            {
                "hook_event_name": "PermissionRequest",
                "tool_name": "Execute",
                "tool_input": {"command": "rm -rf /"},
                "cwd": "/tmp",
            },
            client="droid",
        )
        result = build_permission_request_response(event)
        assert result.status == "ok"
        assert result.response.permission_decision == "deny"

    def test_blocks_absolute_project_root(self, tmp_path):
        event = parse_hook_event(
            {
                "hook_event_name": "PermissionRequest",
                "tool_name": "Bash",
                "tool_input": {"command": f"rm -rf {tmp_path}"},
                "cwd": str(tmp_path),
            },
            client="codex",
        )

        result = build_permission_request_response(event)

        assert result.status == "ok"
        assert result.response.permission_decision == "deny"
        assert result.response.reason == "recursive forced deletion of project root"


class TestBuildPreToolResponse:
    def test_blocks_lowercase_bash_tool(self):
        event = parse_hook_event(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "bash",
                "tool_input": {"command": "rm -rf /"},
                "cwd": "/tmp",
            },
            client="opencode",
        )

        result = build_pre_tool_response(event)

        assert result.status == "ok"
        assert result.response.permission_decision == "deny"
        assert result.response.reason is not None

    def test_blocks_absolute_project_root(self, tmp_path):
        event = parse_hook_event(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "bash",
                "tool_input": {"command": f"sudo rm -rf {tmp_path}"},
                "cwd": str(tmp_path),
            },
            client="opencode",
        )

        result = build_pre_tool_response(event)

        assert result.status == "ok"
        assert result.response.permission_decision == "deny"
        assert result.response.reason == "recursive forced deletion of project root with sudo"
