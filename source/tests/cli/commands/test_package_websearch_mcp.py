# ABOUTME: Tests the web-search gateway MCP wiring in gip package (E-H1 / R10).
# ABOUTME: Covers gip-settings/mcp.json, installer registration blocks, and harness MCP gating.

"""Package-level tests for the gateway MCP endpoint wiring (R10 verdicts).

Claude Code CLI gets the native streamable-HTTP + headersHelper entry
(gip-settings/mcp.json + `claude mcp add-json -s user` in the installers);
OpenCode/Codex get stdio entries through the credential-process --mcp-proxy
shim; Pi/Aider get docs pointers only. Everything is gated identically to the
CoWork managedMcpServers injection: web_search_enabled AND auth != idc AND a
resolvable gateway URL.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10: stdlib tomllib is 3.11+; tomli is the same API
    import tomli as tomllib

from governed_inference_platform.cli.commands.package import PackageCommand
from governed_inference_platform.config import Profile

GATEWAY_URL = "https://gw-abc.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"


def _make_profile(**overrides):
    defaults = {
        "name": "gip",
        "provider_domain": "auth.example.com",
        "client_id": "client-abc",
        "credential_storage": "session",
        "aws_region": "us-east-1",
        "identity_pool_name": "test-pool",
        "auth_type": "oidc",
        "monitoring_enabled": False,
        "cross_region_profile": "us",
        "web_search_enabled": True,
        "websearch_gateway_url": GATEWAY_URL,
    }
    defaults.update(overrides)
    return Profile(**defaults)


class TestClaudeMcpConfig:
    """gip-settings/mcp.json — native headersHelper entry for Claude Code CLI."""

    def test_writes_http_entry_with_helper_placeholder(self, tmp_path):
        PackageCommand()._create_claude_mcp_config(tmp_path, _make_profile(), MagicMock())
        parsed = json.loads((tmp_path / "gip-settings" / "mcp.json").read_text())
        assert parsed == {
            "mcpServers": {
                "agentcore-websearch": {
                    "type": "http",
                    "url": GATEWAY_URL,
                    "headersHelper": "__WEBSEARCH_HEADERS_HELPER__",
                }
            }
        }

    def test_skipped_when_web_search_disabled(self, tmp_path):
        PackageCommand()._create_claude_mcp_config(tmp_path, _make_profile(web_search_enabled=False), MagicMock())
        assert not (tmp_path / "gip-settings" / "mcp.json").exists()

    def test_skipped_for_idc_auth(self, tmp_path):
        """The gateway's CUSTOM_JWT authorizer cannot validate IDC's SigV4."""
        PackageCommand()._create_claude_mcp_config(tmp_path, _make_profile(auth_type="idc"), MagicMock())
        assert not (tmp_path / "gip-settings" / "mcp.json").exists()

    def test_skipped_when_url_unresolvable(self, tmp_path):
        profile = _make_profile(websearch_gateway_url="", stack_names={})
        PackageCommand()._create_claude_mcp_config(tmp_path, profile, MagicMock())
        assert not (tmp_path / "gip-settings" / "mcp.json").exists()

    def test_appends_mcp_suffix_when_missing(self, tmp_path):
        profile = _make_profile(websearch_gateway_url=GATEWAY_URL.removesuffix("/mcp"))
        PackageCommand()._create_claude_mcp_config(tmp_path, profile, MagicMock())
        parsed = json.loads((tmp_path / "gip-settings" / "mcp.json").read_text())
        assert parsed["mcpServers"]["agentcore-websearch"]["url"] == GATEWAY_URL


class TestInstallShMcpRegistration:
    """install.sh — claude mcp add-json registration + placeholder resolution."""

    def _generate(self, profile) -> str:
        path = PackageCommand()._create_installer(Path(self._tmp), profile, [("linux", Path("/tmp/x"))])
        return path.read_text(encoding="utf-8")

    def _content(self, tmp_path, **overrides) -> str:
        self._tmp = tmp_path
        return self._generate(_make_profile(**overrides))

    def test_registers_gateway_with_claude_cli(self, tmp_path):
        content = self._content(tmp_path)
        assert "claude mcp remove agentcore-websearch -s user" in content
        assert "claude mcp add-json agentcore-websearch -s user" in content
        assert GATEWAY_URL in content

    def test_json_payload_uses_installed_helper_path(self, tmp_path):
        content = self._content(tmp_path)
        assert '\\"headersHelper\\":\\"$WS_HELPER\\"' in content

    def test_registration_guarded_on_claude_and_sudo(self, tmp_path):
        """No claude on PATH (or running under sudo) → print the exact command instead."""
        content = self._content(tmp_path)
        assert 'if [ -z "$SUDO_USER" ] && command -v claude >/dev/null 2>&1; then' in content
        assert "To enable web search in Claude Code, run:" in content

    def test_resolves_mcp_json_placeholder(self, tmp_path):
        content = self._content(tmp_path)
        assert '"gip/gip-settings/mcp.json"' in content
        assert "sed -i" not in content

    def test_resolves_harness_credential_process_placeholder(self, tmp_path):
        """OpenCode/Codex MCP entries reference the installed binary path."""
        content = self._content(tmp_path)
        assert '"gip/harnesses/$_gip_harness_rel"' in content
        assert "s|__CREDENTIAL_PROCESS_PATH__|$ACTUAL_HOME/gip/credential-process|g" in content

    def test_no_registration_when_web_search_disabled(self, tmp_path):
        content = self._content(tmp_path, web_search_enabled=False)
        assert "claude mcp add-json" not in content

    def test_no_registration_for_idc(self, tmp_path):
        content = self._content(tmp_path, auth_type="idc", web_search_enabled=True)
        assert "claude mcp add-json" not in content

    def test_no_registration_when_url_unresolvable_but_helper_still_installed(self, tmp_path):
        content = self._content(tmp_path, websearch_gateway_url="", stack_names={})
        assert "claude mcp add-json" not in content
        assert "websearch-headers" in content  # CoWork helper still installed


class TestInstallBatMcpRegistration:
    """install.bat — Windows registration with .cmd helper + forward-slash JSON paths."""

    def _content(self, tmp_path, **overrides) -> str:
        PackageCommand()._create_windows_installer(tmp_path, _make_profile(**overrides))
        return (tmp_path / "install.bat").read_text(encoding="utf-8") + (tmp_path / "gip-install.ps1").read_text(
            encoding="utf-8"
        )

    def test_registers_gateway_with_claude_cli(self, tmp_path):
        content = self._content(tmp_path)
        assert "call claude mcp remove agentcore-websearch -s user" in content
        assert "call claude mcp add-json agentcore-websearch -s user" in content
        assert GATEWAY_URL in content

    def test_helper_path_uses_forward_slashes_for_json(self, tmp_path):
        """Backslashes would need JSON escaping; %USERPROFILE:\\=/% avoids it."""
        content = self._content(tmp_path)
        assert 'set "WS_HELPER_JSON=%USERPROFILE:\\=/%/gip/websearch-headers.cmd"' in content

    def test_registration_guarded_on_claude_presence(self, tmp_path):
        content = self._content(tmp_path)
        assert "where claude >nul 2>&1" in content
        assert "To enable web search in Claude Code, run after installing Claude Code:" in content

    def test_resolves_mcp_json_placeholder(self, tmp_path):
        content = self._content(tmp_path)
        assert "__WEBSEARCH_HEADERS_HELPER__" in content
        assert "gip\\gip-settings\\mcp.json" in content
        assert "Set-Content 'gip-settings" not in content

    def test_resolves_harness_credential_process_placeholder(self, tmp_path):
        content = self._content(tmp_path)
        assert "gip\\harnesses\\" in content
        assert "__CREDENTIAL_PROCESS_PATH__" in content
        assert "Set-Content 'harnesses" not in content

    def test_no_registration_when_web_search_disabled(self, tmp_path):
        content = self._content(tmp_path, web_search_enabled=False)
        assert "claude mcp add-json" not in content

    def test_no_registration_for_idc(self, tmp_path):
        content = self._content(tmp_path, auth_type="idc", web_search_enabled=True)
        assert "claude mcp add-json" not in content


class TestHarnessMcpGating:
    """_create_harness_configs threads the shim only when websearch+OIDC+URL hold."""

    def _generate(self, tmp_path, profile):
        PackageCommand()._create_harness_configs(
            tmp_path, profile, profile.name, ["opencode", "codex", "pi", "aider"], MagicMock()
        )

    def test_opencode_and_codex_gain_mcp_entries(self, tmp_path):
        self._generate(tmp_path, _make_profile())
        opencode = json.loads((tmp_path / "harnesses" / "opencode" / "opencode.json").read_text())
        assert opencode["mcp"]["agentcore-websearch"]["command"] == [
            "__CREDENTIAL_PROCESS_PATH__",
            "--profile",
            "gip",
            "--mcp-proxy",
            GATEWAY_URL,
        ]
        codex = tomllib.loads((tmp_path / "harnesses" / "codex" / "config.toml").read_text())
        assert codex["mcp_servers"]["agentcore-websearch"]["args"] == [
            "--profile",
            "gip",
            "--mcp-proxy",
            GATEWAY_URL,
        ]

    def test_pi_and_aider_files_carry_no_gateway_url(self, tmp_path):
        self._generate(tmp_path, _make_profile())
        assert GATEWAY_URL not in (tmp_path / "harnesses" / "pi" / "pi-bedrock.env").read_text()
        assert GATEWAY_URL not in (tmp_path / "harnesses" / "aider" / ".aider.conf.yml").read_text()

    def test_disabled_web_search_emits_no_mcp(self, tmp_path):
        self._generate(tmp_path, _make_profile(web_search_enabled=False))
        opencode = json.loads((tmp_path / "harnesses" / "opencode" / "opencode.json").read_text())
        assert "mcp" not in opencode
        codex = tomllib.loads((tmp_path / "harnesses" / "codex" / "config.toml").read_text())
        assert "mcp_servers" not in codex

    def test_idc_auth_emits_no_mcp(self, tmp_path):
        self._generate(tmp_path, _make_profile(auth_type="idc"))
        opencode = json.loads((tmp_path / "harnesses" / "opencode" / "opencode.json").read_text())
        assert "mcp" not in opencode

    def test_unresolvable_url_emits_no_mcp(self, tmp_path):
        self._generate(tmp_path, _make_profile(websearch_gateway_url="", stack_names={}))
        opencode = json.loads((tmp_path / "harnesses" / "opencode" / "opencode.json").read_text())
        assert "mcp" not in opencode
