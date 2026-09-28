# ABOUTME: Tests for per-harness Bedrock config generators (OpenCode, Codex, Pi, Aider).
# ABOUTME: Verifies each generator emits parseable output with profile, region, and model IDs.

"""Tests for governed_inference_platform.cli.utils.harness_configs."""

import json

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10: stdlib tomllib is 3.11+; tomli is the same API
    import tomli as tomllib
import yaml

from governed_inference_platform.cli.utils.harness_configs import (
    CREDENTIAL_PROCESS_PLACEHOLDER,
    HARNESS_GENERATORS,
    McpProxyConfig,
    build_harnesses_readme,
    generate_aider_config,
    generate_codex_config,
    generate_harness_configs,
    generate_opencode_config,
    generate_pi_config,
    resolve_harness_selection,
)

PROFILE = "gip"
REGION = "us-east-1"
MODEL_IDS = {
    "haiku": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "sonnet": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "opus": "us.anthropic.claude-opus-4-1-20250805-v1:0",
}
GATEWAY_URL = "https://gw-abc.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
MCP_PROXY = McpProxyConfig(gateway_url=GATEWAY_URL)


class TestOpenCodeGenerator:
    def test_output_is_valid_json(self):
        cfg = generate_opencode_config(PROFILE, REGION, MODEL_IDS)
        parsed = json.loads(cfg.content)
        assert cfg.filename == "opencode.json"
        assert parsed["$schema"] == "https://opencode.ai/config.json"

    def test_provider_options_carry_profile_and_region(self):
        parsed = json.loads(generate_opencode_config(PROFILE, REGION, MODEL_IDS).content)
        options = parsed["provider"]["amazon-bedrock"]["options"]
        assert options["profile"] == PROFILE
        assert options["region"] == REGION

    def test_models_use_cris_ids_and_default_is_sonnet(self):
        parsed = json.loads(generate_opencode_config(PROFILE, REGION, MODEL_IDS).content)
        models = parsed["provider"]["amazon-bedrock"]["models"]
        for model_id in MODEL_IDS.values():
            assert model_id in models
        assert parsed["model"] == f"amazon-bedrock/{MODEL_IDS['sonnet']}"

    def test_instructions_cite_source_url(self):
        cfg = generate_opencode_config(PROFILE, REGION, MODEL_IDS)
        assert "https://opencode.ai/docs/providers/#amazon-bedrock" in cfg.setup_instructions

    def test_no_mcp_key_without_mcp_proxy(self):
        """Default output must be unchanged when web search is off (gating lives in package.py)."""
        parsed = json.loads(generate_opencode_config(PROFILE, REGION, MODEL_IDS).content)
        assert "mcp" not in parsed

    def test_mcp_proxy_emits_local_stdio_entry(self):
        """Pin R10 §3.2: type local + credential-process --mcp-proxy command, enabled."""
        parsed = json.loads(generate_opencode_config(PROFILE, REGION, MODEL_IDS, mcp_proxy=MCP_PROXY).content)
        entry = parsed["mcp"]["agentcore-websearch"]
        assert entry == {
            "type": "local",
            "command": [
                CREDENTIAL_PROCESS_PLACEHOLDER,
                "--profile",
                PROFILE,
                "--mcp-proxy",
                GATEWAY_URL,
            ],
            "enabled": True,
        }

    def test_mcp_proxy_never_emits_remote_type(self):
        """R10 risk 7: remote-type entries trigger OpenCode's browser OAuth on 401."""
        content = generate_opencode_config(PROFILE, REGION, MODEL_IDS, mcp_proxy=MCP_PROXY).content
        assert '"remote"' not in content

    def test_mcp_proxy_instructions_warn_against_remote_conversion(self):
        cfg = generate_opencode_config(PROFILE, REGION, MODEL_IDS, mcp_proxy=MCP_PROXY)
        assert "--mcp-proxy" in cfg.setup_instructions
        assert 'type:"remote"' in cfg.setup_instructions


class TestCodexGenerator:
    def test_output_is_valid_toml(self):
        cfg = generate_codex_config(PROFILE, REGION, MODEL_IDS)
        parsed = tomllib.loads(cfg.content)
        assert cfg.filename == "config.toml"
        assert parsed["model_provider"] == "amazon-bedrock"

    def test_contains_profile_and_region(self):
        content = generate_codex_config(PROFILE, REGION, MODEL_IDS).content
        assert f"AWS_PROFILE={PROFILE}" in content
        assert f"AWS_REGION={REGION}" in content

    def test_uses_openai_model_not_claude_cris(self):
        # Honest limitation: Codex's Bedrock path runs OpenAI models only —
        # Claude CRIS IDs must NOT appear as the configured model.
        parsed = tomllib.loads(generate_codex_config(PROFILE, REGION, MODEL_IDS).content)
        assert parsed["model"].startswith("openai.")
        for model_id in MODEL_IDS.values():
            assert parsed.get("model") != model_id

    def test_instructions_cite_source_url(self):
        cfg = generate_codex_config(PROFILE, REGION, MODEL_IDS)
        assert "https://developers.openai.com/codex/amazon-bedrock" in cfg.setup_instructions

    def test_no_mcp_servers_without_mcp_proxy(self):
        parsed = tomllib.loads(generate_codex_config(PROFILE, REGION, MODEL_IDS).content)
        assert "mcp_servers" not in parsed

    def test_mcp_proxy_emits_stdio_server_entry(self):
        """Pin R10 §3.3: [mcp_servers.agentcore-websearch] command + args, valid TOML."""
        parsed = tomllib.loads(generate_codex_config(PROFILE, REGION, MODEL_IDS, mcp_proxy=MCP_PROXY).content)
        entry = parsed["mcp_servers"]["agentcore-websearch"]
        assert entry == {
            "command": CREDENTIAL_PROCESS_PLACEHOLDER,
            "args": ["--profile", PROFILE, "--mcp-proxy", GATEWAY_URL],
        }

    def test_mcp_proxy_never_uses_static_bearer_options(self):
        """R10: bearer_token_env_var / http_headers are static and die at token expiry."""
        parsed = tomllib.loads(generate_codex_config(PROFILE, REGION, MODEL_IDS, mcp_proxy=MCP_PROXY).content)
        entry = parsed["mcp_servers"]["agentcore-websearch"]
        for key in ("url", "bearer_token_env_var", "http_headers", "env_http_headers"):
            assert key not in entry


class TestAiderGenerator:
    def test_output_is_valid_yaml(self):
        cfg = generate_aider_config(PROFILE, REGION, MODEL_IDS)
        parsed = yaml.safe_load(cfg.content)
        assert cfg.filename == ".aider.conf.yml"
        assert isinstance(parsed, dict)

    def test_model_uses_litellm_bedrock_prefix_with_cris_id(self):
        parsed = yaml.safe_load(generate_aider_config(PROFILE, REGION, MODEL_IDS).content)
        assert parsed["model"] == f"bedrock/{MODEL_IDS['sonnet']}"

    def test_contains_profile_and_region(self):
        content = generate_aider_config(PROFILE, REGION, MODEL_IDS).content
        assert f"AWS_PROFILE={PROFILE}" in content
        assert f"AWS_REGION={REGION}" in content

    def test_instructions_include_env_var_alternative(self):
        cfg = generate_aider_config(PROFILE, REGION, MODEL_IDS)
        assert "https://aider.chat/docs/llms/bedrock.html" in cfg.setup_instructions
        assert ".env" in cfg.setup_instructions

    def test_mcp_proxy_is_ignored_no_mcp_content(self):
        """R10 verdict: Aider has no MCP support — docs pointer only, no config change."""
        with_proxy = generate_aider_config(PROFILE, REGION, MODEL_IDS, mcp_proxy=MCP_PROXY)
        without = generate_aider_config(PROFILE, REGION, MODEL_IDS)
        assert with_proxy.content == without.content
        assert "no MCP support" in with_proxy.setup_instructions
        assert "Aider-AI/aider#2525" in with_proxy.setup_instructions


class TestPiGenerator:
    def test_contains_profile_region_and_cris_model(self):
        cfg = generate_pi_config(PROFILE, REGION, MODEL_IDS)
        assert cfg.filename == "pi-bedrock.env"
        assert f"export AWS_PROFILE={PROFILE}" in cfg.content
        assert f"export AWS_REGION={REGION}" in cfg.content
        assert f"--provider amazon-bedrock --model {MODEL_IDS['sonnet']}" in cfg.content

    def test_instructions_cite_source_url(self):
        cfg = generate_pi_config(PROFILE, REGION, MODEL_IDS)
        assert "https://pi.dev/docs/latest/providers#amazon-bedrock" in cfg.setup_instructions

    def test_mcp_proxy_is_ignored_no_mcp_content(self):
        """R10 verdict: Pi has no MCP by design — docs pointer only, no config change."""
        with_proxy = generate_pi_config(PROFILE, REGION, MODEL_IDS, mcp_proxy=MCP_PROXY)
        without = generate_pi_config(PROFILE, REGION, MODEL_IDS)
        assert with_proxy.content == without.content
        assert "no MCP support by design" in with_proxy.setup_instructions


class TestResolveHarnessSelection:
    def test_default_claude_code_only_returns_empty(self):
        assert resolve_harness_selection("claude-code") == []

    def test_none_and_empty_return_empty(self):
        assert resolve_harness_selection(None) == []
        assert resolve_harness_selection("") == []

    def test_all_expands_to_every_known_harness(self):
        assert resolve_harness_selection("all") == list(HARNESS_GENERATORS)

    def test_comma_list_filters_claude_code_and_dedupes(self):
        assert resolve_harness_selection("claude-code,opencode,opencode") == ["opencode"]

    def test_unknown_harness_raises(self):
        with pytest.raises(ValueError, match="Unknown harness 'cursor'"):
            resolve_harness_selection("cursor")


class TestGenerateHarnessConfigs:
    def test_generates_selected_harnesses_in_registry_order(self):
        configs = generate_harness_configs(["aider", "opencode"], PROFILE, REGION, MODEL_IDS)
        assert [c.harness for c in configs] == ["opencode", "aider"]

    def test_all_harnesses_generate(self):
        configs = generate_harness_configs(list(HARNESS_GENERATORS), PROFILE, REGION, MODEL_IDS)
        assert {c.harness for c in configs} == set(HARNESS_GENERATORS)

    def test_mcp_proxy_threads_to_opencode_and_codex_only(self):
        configs = generate_harness_configs(list(HARNESS_GENERATORS), PROFILE, REGION, MODEL_IDS, mcp_proxy=MCP_PROXY)
        by_name = {c.harness: c for c in configs}
        assert "mcp" in json.loads(by_name["opencode"].content)
        assert "mcp_servers" in tomllib.loads(by_name["codex"].content)
        assert GATEWAY_URL not in by_name["pi"].content
        assert GATEWAY_URL not in by_name["aider"].content

    def test_aider_and_pi_use_selected_compatibility_model(self):
        selected = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
        current_models = {**MODEL_IDS, "sonnet": "us.anthropic.claude-sonnet-5"}
        configs = generate_harness_configs(
            ["opencode", "pi", "aider"],
            PROFILE,
            REGION,
            current_models,
            compatibility_model_id=selected,
        )
        by_name = {config.harness: config for config in configs}
        assert yaml.safe_load(by_name["aider"].content)["model"] == f"bedrock/{selected}"
        assert f"--provider amazon-bedrock --model {selected}" in by_name["pi"].content
        assert json.loads(by_name["opencode"].content)["model"].endswith("claude-sonnet-5")


class TestHarnessesReadme:
    def test_readme_includes_guarantees_models_and_all_sections(self):
        configs = generate_harness_configs(list(HARNESS_GENERATORS), PROFILE, REGION, MODEL_IDS)
        readme = build_harnesses_readme(configs, PROFILE, REGION, MODEL_IDS)
        assert PROFILE in readme
        assert REGION in readme
        for model_id in MODEL_IDS.values():
            assert model_id in readme
        for keyword in ("SSO login", "Quota enforcement", "cost attribution", "guardrails"):
            assert keyword in readme
        for cfg in configs:
            assert cfg.setup_instructions.strip() in readme
