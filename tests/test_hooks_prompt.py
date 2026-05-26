from code_briefcase.hooks.prompt import (
    check_prompt_for_secrets,
    build_user_prompt_submit_response,
)
from code_briefcase.hooks.runtime import parse_hook_event


# --- High-confidence detector tests ---


class TestOpenAIKeyDetection:
    def test_blocks_openai_api_key(self) -> None:
        prompt = "Here is my key: sk-" + "A" * 48 + " please use it"
        assert check_prompt_for_secrets(prompt) == "possible OpenAI API key"

    def test_does_not_block_short_sk(self) -> None:
        prompt = "sk-abc short prefix value"
        assert check_prompt_for_secrets(prompt) is None

    def test_does_not_block_placeholder(self) -> None:
        prompt = "sk-YOUR_API_KEY_HERE replace with real key"
        assert check_prompt_for_secrets(prompt) is None


class TestAnthropicKeyDetection:
    def test_blocks_anthropic_api_key(self) -> None:
        prompt = "My key is sk-ant-" + "B" * 24 + " for Claude"
        assert check_prompt_for_secrets(prompt) == "possible Anthropic API key"

    def test_long_anthropic_key_not_misclassified_as_openai(self) -> None:
        prompt = "My key is sk-ant-" + "B" * 56
        assert check_prompt_for_secrets(prompt) == "possible Anthropic API key"

    def test_does_not_block_short_anthropic_prefix(self) -> None:
        prompt = "sk-ant-abc too short"
        assert check_prompt_for_secrets(prompt) is None


class TestGitHubTokenDetection:
    def test_blocks_github_token(self) -> None:
        prompt = "export GITHUB_TOKEN=ghp_" + "A" * 36
        assert check_prompt_for_secrets(prompt) == "possible GitHub token"

    def test_does_not_block_short_ghp(self) -> None:
        prompt = "ghp_abc short"
        assert check_prompt_for_secrets(prompt) is None


class TestSlackTokenDetection:
    def test_blocks_slack_token(self) -> None:
        prompt = "SLACK_TOKEN=xoxb-" + "1234567890" * 3
        assert check_prompt_for_secrets(prompt) == "possible Slack token"

    def test_does_not_block_short_slack(self) -> None:
        prompt = "xoxb-abc"
        assert check_prompt_for_secrets(prompt) is None


class TestAWSKeyDetection:
    def test_blocks_aws_access_key(self) -> None:
        prompt = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
        assert check_prompt_for_secrets(prompt) == "possible AWS access key"

    def test_does_not_block_non_akia(self) -> None:
        prompt = "AKIASHORT"
        assert check_prompt_for_secrets(prompt) is None


class TestPEMPrivateKeyDetection:
    def test_blocks_rsa_private_key(self) -> None:
        prompt = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA..."
        assert check_prompt_for_secrets(prompt) == "possible PEM private key"

    def test_blocks_generic_private_key(self) -> None:
        prompt = "-----BEGIN PRIVATE KEY-----\nMIIEpAIBAAKCAQEA..."
        assert check_prompt_for_secrets(prompt) == "possible PEM private key"

    def test_does_not_block_public_key(self) -> None:
        prompt = "-----BEGIN PUBLIC KEY-----\nMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8A..."
        assert check_prompt_for_secrets(prompt) is None

    def test_does_not_block_certificate(self) -> None:
        prompt = "-----BEGIN CERTIFICATE-----\nMIIDXTCCAkWgAwIBAgIJAKL..."
        assert check_prompt_for_secrets(prompt) is None


class TestEnvCredentialDetection:
    def test_blocks_env_style_api_key(self) -> None:
        prompt = "API_KEY=aB3dE7fG9hJ2kL5mN8oP1qR4sT6uV9"
        assert check_prompt_for_secrets(prompt) == "possible .env credential"

    def test_blocks_provider_prefixed_env_style_api_key(self) -> None:
        prompt = "OPENAI_API_KEY=aB3dE7fG9hJ2kL5mN8oP1qR4sT6uV9"
        assert check_prompt_for_secrets(prompt) == "possible .env credential"

    def test_blocks_env_style_secret(self) -> None:
        prompt = "SECRET=Xy7zW9aB3cD5eF6gH8jK0lM2"
        assert check_prompt_for_secrets(prompt) == "possible .env credential"

    def test_does_not_block_generic_password_word(self) -> None:
        prompt = "Please enter your password to continue"
        assert check_prompt_for_secrets(prompt) is None

    def test_does_not_block_placeholder_value(self) -> None:
        prompt = "API_KEY=TODO_replace"
        assert check_prompt_for_secrets(prompt) is None

    def test_does_not_block_short_value(self) -> None:
        prompt = "TOKEN=abc123"
        assert check_prompt_for_secrets(prompt) is None

    def test_does_not_block_low_entropy_value(self) -> None:
        prompt = "SECRET=aaaaaaaaaaaaaaaa"
        assert check_prompt_for_secrets(prompt) is None

    def test_does_not_block_docs_snippet(self) -> None:
        prompt = "Set your API_KEY environment variable to your key value."
        assert check_prompt_for_secrets(prompt) is None


class TestNoSecretEcho:
    """Blocking messages must name only the class, never echo the secret."""

    def test_reason_does_not_echo_key(self) -> None:
        fake_key = "sk-" + "X" * 48
        prompt = f"Here is my key: {fake_key}"
        reason = check_prompt_for_secrets(prompt)
        assert reason is not None
        assert fake_key not in reason
        # The key itself should not appear in the reason
        assert "XXX" not in reason  # no placeholder echo

    def test_reason_does_not_echo_pem_content(self) -> None:
        prompt = "-----BEGIN PRIVATE KEY-----\nMIIEpAIBAAKCAQEA0Z3VS5JJcds3xfn/yg"
        reason = check_prompt_for_secrets(prompt)
        assert reason is not None
        assert "MIIEpA" not in reason


# --- build_user_prompt_submit_response integration tests ---


class TestBuildUserPromptSubmitResponse:
    def test_noop_for_clean_prompt(self) -> None:
        event = parse_hook_event(
            {
                "hook_event_name": "UserPromptSubmit",
                "prompt": "Hello world",
                "cwd": "/tmp",
            },
            client="codex",
        )
        result = build_user_prompt_submit_response(event)
        assert result.is_noop()

    def test_blocks_prompt_with_secret(self) -> None:
        event = parse_hook_event(
            {
                "hook_event_name": "UserPromptSubmit",
                "prompt": "sk-" + "A" * 48,
                "cwd": "/tmp",
            },
            client="codex",
        )
        result = build_user_prompt_submit_response(event)
        assert result.status == "ok"
        assert result.response.decision == "block"
        assert result.response.reason is not None

    def test_noop_for_empty_prompt(self) -> None:
        event = parse_hook_event(
            {"hook_event_name": "UserPromptSubmit", "cwd": "/tmp"},
            client="codex",
        )
        result = build_user_prompt_submit_response(event)
        assert result.is_noop()
