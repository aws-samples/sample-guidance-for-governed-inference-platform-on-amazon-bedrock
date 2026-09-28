# ABOUTME: Pure generators for per-harness Bedrock config files (OpenCode, Codex, Pi, Aider).
# ABOUTME: Every harness reuses the same AWS profile whose credential_process handles SSO/quota.

"""Config generators for coding harnesses other than Claude Code.

The installer writes an AWS profile into ``~/.aws/config`` whose
``credential_process`` points at the solution's credential-process binary
(see ``package.py``). Any harness that resolves credentials through the AWS
SDK default chain therefore inherits the same OIDC SSO login, quota
enforcement, session-name cost attribution, and IAM model/region guardrails
with zero harness-specific auth work. These generators only produce the
per-harness *provider* configuration (profile + region + model IDs).

Config formats verified against vendor docs (URLs cited per generator).
"""

import json
from dataclasses import dataclass

from governed_inference_platform.cli.utils.cowork_3p import WEBSEARCH_MCP_SERVER_NAME

# Harness machine names accepted by `gip package --harnesses`.
# "claude-code" is always packaged and never produces a harnesses/ entry.
CLAUDE_CODE_HARNESS = "claude-code"

# Install-time placeholder for the credential-process binary path inside
# generated MCP entries. Resolved by install.sh / install.bat to the per-OS
# absolute path (same pattern as __CREDENTIAL_PROCESS_PATH__ in settings.json).
CREDENTIAL_PROCESS_PLACEHOLDER = "__CREDENTIAL_PROCESS_PATH__"


@dataclass(frozen=True)
class McpProxyConfig:
    """Gateway MCP wiring for harnesses that need the stdio shim (R10 §4.2).

    ``credential_process_path`` is usually ``CREDENTIAL_PROCESS_PLACEHOLDER``
    so the installer can substitute the per-OS absolute binary path; an
    explicit absolute path is used verbatim. The generators stay pure: the
    caller (package.py) resolves the gateway URL and applies the gating
    (web_search_enabled + OIDC-only) before constructing this.
    """

    gateway_url: str
    credential_process_path: str = CREDENTIAL_PROCESS_PLACEHOLDER


@dataclass(frozen=True)
class HarnessConfig:
    """A generated config file plus its setup instructions for one harness."""

    harness: str  # machine name, e.g. "opencode"
    display_name: str
    filename: str  # file name written under harnesses/<harness>/
    content: str
    setup_instructions: str  # markdown, assembled into harnesses/README.md
    source_url: str


def _default_model(model_ids: dict[str, str]) -> str | None:
    """Pick the default model: sonnet tier if resolved, else any tier."""
    if "sonnet" in model_ids:
        return model_ids["sonnet"]
    for tier in ("opus", "haiku"):
        if tier in model_ids:
            return model_ids[tier]
    return next(iter(model_ids.values()), None)


def generate_opencode_config(
    aws_profile_name: str,
    aws_region: str,
    model_ids: dict[str, str],
    mcp_proxy: McpProxyConfig | None = None,
) -> HarnessConfig:
    """Generate opencode.json for OpenCode's amazon-bedrock provider.

    Format source: https://opencode.ai/docs/providers/#amazon-bedrock
    - Provider key is "amazon-bedrock"; options.region / options.profile select
      the AWS region and named profile (resolved via the AWS SDK chain, which
      invokes our credential_process).
    - Models are declared under provider.amazon-bedrock.models keyed by model
      ID; CRIS inference-profile IDs (e.g. us.anthropic.claude-sonnet-...) are
      used directly as the model key. Top-level "model" selects the default as
      "<provider>/<model-id>".

    MCP (web search): OpenCode's ``type:"remote"`` supports only static headers
    (``{env:VAR}`` interpolation, no header command), so a bearer entry dies at
    the ~1h id_token expiry — and its automatic OAuth-on-401 opens a browser
    against a gateway that is not an OAuth AS. When ``mcp_proxy`` is set, emit
    a ``type:"local"`` (stdio) entry through the credential-process
    ``--mcp-proxy`` shim instead, which injects a fresh Authorization header
    per request (format source: https://opencode.ai/docs/mcp-servers/).
    """
    source_url = "https://opencode.ai/docs/providers/#amazon-bedrock"
    models = {
        model_id: {"name": f"Claude ({tier} tier, Bedrock cross-region inference)"}
        for tier, model_id in model_ids.items()
    }
    config: dict = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            "amazon-bedrock": {
                "options": {
                    "region": aws_region,
                    "profile": aws_profile_name,
                },
                "models": models,
            }
        },
    }
    default = _default_model(model_ids)
    if default:
        config["model"] = f"amazon-bedrock/{default}"

    mcp_section = ""
    if mcp_proxy:
        config["mcp"] = {
            WEBSEARCH_MCP_SERVER_NAME: {
                "type": "local",
                "command": [
                    mcp_proxy.credential_process_path,
                    "--profile",
                    aws_profile_name,
                    "--mcp-proxy",
                    mcp_proxy.gateway_url,
                ],
                "enabled": True,
            }
        }
        mcp_section = f"""
**Web search (MCP):** the config includes an `mcp.{WEBSEARCH_MCP_SERVER_NAME}` entry that
runs the credential-process binary in `--mcp-proxy` mode — a local stdio MCP server that
forwards to the AgentCore gateway with a fresh Authorization header per request (no ~1h
token expiry). The installer resolves the `{CREDENTIAL_PROCESS_PLACEHOLDER}` placeholder to the
installed binary path; if you copied this file before running the installer, replace it
manually (e.g. `~/gip/credential-process`). Do NOT convert the entry
to `type:"remote"`: OpenCode's remote headers are static, and on token expiry its
automatic OAuth kicks in, opens a browser against the gateway, and fails.
"""

    instructions = f"""### OpenCode

Config format: [{source_url}]({source_url})

1. Run the solution installer first (it creates the `{aws_profile_name}` AWS profile in `~/.aws/config`).
2. Copy `~/gip/harnesses/opencode/opencode.json` into your project root (or merge it into
   `~/.config/opencode/opencode.json` for a global default).
3. Launch `opencode`. The AWS SDK resolves the `{aws_profile_name}` profile, which triggers
   the credential-process binary: browser SSO on first use, silent refresh afterwards.

The `provider.amazon-bedrock.options.profile` and `options.region` keys pin the harness to
the managed profile and region. Model entries use the same Bedrock CRIS inference-profile
IDs that Claude Code receives.
{mcp_section}"""
    return HarnessConfig(
        harness="opencode",
        display_name="OpenCode",
        filename="opencode.json",
        content=json.dumps(config, indent=2) + "\n",
        setup_instructions=instructions,
        source_url=source_url,
    )


def generate_codex_config(
    aws_profile_name: str,
    aws_region: str,
    model_ids: dict[str, str],
    mcp_proxy: McpProxyConfig | None = None,
) -> HarnessConfig:
    """Generate ~/.codex/config.toml snippet for OpenAI Codex CLI on Bedrock.

    Format source: https://developers.openai.com/codex/amazon-bedrock
    - `model_provider = "amazon-bedrock"` selects the built-in Bedrock (Mantle)
      provider; model selection is optional and limited to OpenAI model IDs
      (e.g. openai.gpt-5.5) — Codex's Bedrock path does NOT run Claude models,
      so the Claude CRIS IDs used by other harnesses do not apply here.
    - AWS auth uses the SDK credential chain; the docs explicitly support
      "Federated identity configured with credential_process" via a named
      profile (AWS_PROFILE), which is exactly what this solution installs.

    MCP (web search): Codex's native streamable-HTTP auth
    (``bearer_token_env_var`` / ``http_headers`` / ``env_http_headers``) is
    static for the process lifetime, so sessions longer than the ~1h id_token
    TTL lose the server. When ``mcp_proxy`` is set, emit a stdio
    ``[mcp_servers.<id>]`` entry through the credential-process ``--mcp-proxy``
    shim instead (format source: https://developers.openai.com/codex/config-reference).
    """
    source_url = "https://developers.openai.com/codex/amazon-bedrock"
    # model_ids (Claude tiers) intentionally unused: Codex on Bedrock supports
    # OpenAI models only (see source_url, "Supported models").
    _ = model_ids
    content = f"""# OpenAI Codex CLI on Amazon Bedrock (Mantle path).
# Merge into ~/.codex/config.toml
# Format source: {source_url}
#
# Authentication: Codex resolves AWS SDK credentials from the named profile.
# Set in your shell (or in ~/.codex/.env for the desktop app / IDE extension):
#   export AWS_PROFILE={aws_profile_name}
#   export AWS_REGION={aws_region}
# The "{aws_profile_name}" profile's credential_process handles OIDC SSO login,
# quota enforcement, and per-user session-name attribution.

model_provider = "amazon-bedrock"

# NOTE: Codex on Bedrock supports OpenAI models only (not Claude).
# Supplying a model is optional; supported IDs per the docs include:
model = "openai.gpt-5.5"
"""
    mcp_instructions = ""
    if mcp_proxy:
        args = json.dumps(["--profile", aws_profile_name, "--mcp-proxy", mcp_proxy.gateway_url])
        content += f"""
# Web search via the AgentCore gateway (MCP, stdio shim). The credential-process
# binary proxies stdio<->streamable-HTTP and injects a fresh Authorization
# header per request — Codex's own header options (bearer_token_env_var,
# http_headers) are static for the process lifetime and die at token expiry.
# The installer resolves {CREDENTIAL_PROCESS_PLACEHOLDER} to the installed
# binary path; replace it manually if you copied this file first.
[mcp_servers.{WEBSEARCH_MCP_SERVER_NAME}]
command = "{mcp_proxy.credential_process_path}"
args = {args}
"""
        mcp_instructions = f"""
**Web search (MCP):** the snippet registers `{WEBSEARCH_MCP_SERVER_NAME}` as a stdio MCP
server through the credential-process `--mcp-proxy` shim, so the gateway bearer token is
refreshed per request (Codex's native `bearer_token_env_var`/`http_headers` are static and
break after the ~1h token TTL). Requires the solution installer to have resolved
`{CREDENTIAL_PROCESS_PLACEHOLDER}` in this file.
"""
    instructions = f"""### OpenAI Codex CLI

Config format: [{source_url}]({source_url})

1. Run the solution installer first (it creates the `{aws_profile_name}` AWS profile).
2. Merge `~/gip/harnesses/codex/config.toml` into `~/.codex/config.toml`.
3. Export the profile and region (or add them to `~/.codex/.env` for the desktop
   app / VS Code extension, which do not inherit shell variables):

   ```bash
   export AWS_PROFILE={aws_profile_name}
   export AWS_REGION={aws_region}
   ```

**Model caveat (honest limitation):** Codex's Bedrock integration runs *OpenAI* models
(`openai.gpt-5.5`, `openai.gpt-5.4`) via Bedrock's OpenAI-compatible Responses API. It does
not run Claude models, so the Claude CRIS model IDs used by the other harnesses do not
apply. Ensure the OpenAI model is available in `{aws_region}` and permitted by your IAM
policy (the default `RestrictToAnthropicModels=true` deployment blocks non-Anthropic
models — set it to `false` if Codex should work). Credential flow (SSO, quota, session-name
attribution) is identical: Codex's docs explicitly support `credential_process` federation.
{mcp_instructions}"""
    return HarnessConfig(
        harness="codex",
        display_name="OpenAI Codex CLI",
        filename="config.toml",
        content=content,
        setup_instructions=instructions,
        source_url=source_url,
    )


def generate_aider_config(
    aws_profile_name: str,
    aws_region: str,
    model_ids: dict[str, str],
    mcp_proxy: McpProxyConfig | None = None,
) -> HarnessConfig:
    """Generate .aider.conf.yml for Aider on Bedrock.

    Format source: https://aider.chat/docs/llms/bedrock.html
    - Models use LiteLLM's "bedrock/<model-id>" naming; CRIS inference-profile
      IDs are passed as bedrock/us.anthropic.claude-... (the docs call out
      that inference-profile-only models REQUIRE the Inference Profile ID).
    - AWS auth via environment: AWS_PROFILE + AWS_REGION (also supported in a
      .env file per the same page). boto3 resolves the profile, which invokes
      our credential_process.

    MCP (web search): skipped — Aider has no MCP client support (nothing in
    the docs/options reference; upstream request Aider-AI/aider#2525 open
    since 2024-12). ``mcp_proxy`` is accepted for registry-signature parity
    and ignored.
    """
    _ = mcp_proxy
    source_url = "https://aider.chat/docs/llms/bedrock.html"
    default = _default_model(model_ids)
    model_line = f"model: bedrock/{default}" if default else "# model: bedrock/<cris-inference-profile-id>"
    content = f"""# Aider on Amazon Bedrock via LiteLLM "bedrock/<model-id>" naming.
# Place as .aider.conf.yml in your project root or home directory.
# Format source: {source_url}
#
# Authentication comes from the AWS SDK (boto3) credential chain. Export:
#   export AWS_PROFILE={aws_profile_name}
#   export AWS_REGION={aws_region}
# or put the same two lines (without 'export') in an .env file next to
# this config. The "{aws_profile_name}" profile's credential_process handles
# OIDC SSO login, quota enforcement, and session-name cost attribution.

{model_line}
"""
    tier_lines = "\n".join(
        f"# aider --model bedrock/{model_id}   # {tier} tier" for tier, model_id in model_ids.items()
    )
    if tier_lines:
        content += f"""
# Other tiers (pass via --model to override):
{tier_lines}
"""
    instructions = f"""### Aider

Config format: [{source_url}]({source_url})

1. Run the solution installer first (it creates the `{aws_profile_name}` AWS profile).
2. Copy `~/gip/harnesses/aider/.aider.conf.yml` into your project root (or `~/`).
3. Point boto3 at the managed profile — either shell exports:

   ```bash
   export AWS_PROFILE={aws_profile_name}
   export AWS_REGION={aws_region}
   aider
   ```

   or the env-var alternative, a `.env` file in the project root:

   ```
   AWS_PROFILE={aws_profile_name}
   AWS_REGION={aws_region}
   ```

Models use LiteLLM `bedrock/<model-id>` naming with the same CRIS inference-profile IDs as
Claude Code (e.g. `bedrock/{default or "us.anthropic.claude-..."}`). Aider may prompt you to
`pip install boto3` on first Bedrock use.

**Web search (MCP):** not available — Aider has no MCP support (upstream request
Aider-AI/aider#2525 is still open), so the AgentCore web-search gateway cannot be wired here.
"""
    return HarnessConfig(
        harness="aider",
        display_name="Aider",
        filename=".aider.conf.yml",
        content=content,
        setup_instructions=instructions,
        source_url=source_url,
    )


def generate_pi_config(
    aws_profile_name: str,
    aws_region: str,
    model_ids: dict[str, str],
    mcp_proxy: McpProxyConfig | None = None,
) -> HarnessConfig:
    """Generate an env/launcher snippet for Pi on Bedrock.

    Format source: https://pi.dev/docs/latest/providers#amazon-bedrock
    - Pi has no dedicated Bedrock config file: auth is "Option 1: AWS Profile"
      via AWS_PROFILE (plus optional AWS_REGION, default us-east-1), and the
      provider/model are selected on the command line:
        pi --provider amazon-bedrock --model us.anthropic.claude-...-v1:0
    - CRIS (system-defined) inference-profile IDs are passed directly as the
      --model value; prompt caching is auto-enabled for recognizable Claude IDs.

    MCP (web search): skipped — Pi has no MCP by design (official README: "No
    MCP. Build CLI tools with READMEs (see Skills), or build an extension that
    adds MCP support."). ``mcp_proxy`` is accepted for registry-signature
    parity and ignored.
    """
    _ = mcp_proxy
    source_url = "https://pi.dev/docs/latest/providers#amazon-bedrock"
    default = _default_model(model_ids)
    launch_lines = "\n".join(
        f"#   pi --provider amazon-bedrock --model {model_id}   # {tier} tier" for tier, model_id in model_ids.items()
    )
    content = f"""# Pi on Amazon Bedrock — environment + launch reference.
# Source this file (or add the exports to your shell profile).
# Format source: {source_url}
#
# Pi has no Bedrock config file: credentials come from the AWS SDK chain
# ("Option 1: AWS Profile" in the docs) and the model is a CLI flag.
# The "{aws_profile_name}" profile's credential_process handles OIDC SSO login,
# quota enforcement, and session-name cost attribution.

export AWS_PROFILE={aws_profile_name}
export AWS_REGION={aws_region}

# Launch with a Bedrock CRIS inference-profile ID:
{launch_lines or "#   pi --provider amazon-bedrock --model <cris-inference-profile-id>"}
"""
    instructions = f"""### Pi

Config format: [{source_url}]({source_url})

1. Run the solution installer first (it creates the `{aws_profile_name}` AWS profile).
2. Source `~/gip/harnesses/pi/pi-bedrock.env` (or add its exports to your shell profile).
3. Launch Pi with the provider and model flags:

   ```bash
   pi --provider amazon-bedrock --model {default or "<cris-inference-profile-id>"}
   ```

Pi has no dedicated Bedrock config file — it reads `AWS_PROFILE`/`AWS_REGION` from the
environment ("Option 1: AWS Profile" in its docs) and takes the model as a CLI flag. CRIS
inference-profile IDs are passed directly; Pi auto-enables prompt caching for Claude model
IDs it recognizes.

**Web search (MCP):** not available — Pi has no MCP support by design (its README points to
extensions instead); users of the community `pi-mcp-adapter` extension can reuse the stdio
shim entry documented in `assets/docs/HARNESSES.md` (Platform tools section).
"""
    return HarnessConfig(
        harness="pi",
        display_name="Pi",
        filename="pi-bedrock.env",
        content=content,
        setup_instructions=instructions,
        source_url=source_url,
    )


# Registry: machine name -> generator. Order defines output order.
HARNESS_GENERATORS = {
    "opencode": generate_opencode_config,
    "codex": generate_codex_config,
    "pi": generate_pi_config,
    "aider": generate_aider_config,
}


def resolve_harness_selection(selection: str | None) -> list[str]:
    """Parse a --harnesses value into the list of EXTRA harnesses to generate.

    "claude-code" is always implicit and never returned (it is the default
    package output). "all" expands to every known extra harness. Unknown
    names raise ValueError.
    """
    if not selection:
        return []
    names: list[str] = []
    for raw in selection.split(","):
        name = raw.strip().lower()
        if not name or name == CLAUDE_CODE_HARNESS:
            continue
        if name == "all":
            for known in HARNESS_GENERATORS:
                if known not in names:
                    names.append(known)
            continue
        if name not in HARNESS_GENERATORS:
            valid = ", ".join([CLAUDE_CODE_HARNESS, *HARNESS_GENERATORS, "all"])
            raise ValueError(f"Unknown harness '{name}'. Valid values: {valid}")
        if name not in names:
            names.append(name)
    return names


def generate_harness_configs(
    harnesses: list[str],
    aws_profile_name: str,
    aws_region: str,
    model_ids: dict[str, str],
    mcp_proxy: McpProxyConfig | None = None,
    compatibility_model_id: str | None = None,
) -> list[HarnessConfig]:
    """Generate configs for the given extra harnesses (registry order).

    ``mcp_proxy`` (already gated by the caller: web search enabled, OIDC auth,
    URL resolved) threads the gateway MCP wiring into the generators that
    support it (OpenCode/Codex); Pi/Aider ignore it (no MCP support upstream).
    ``compatibility_model_id`` overrides the Sonnet model for Aider and Pi.
    """
    configs = []
    for name, generator in HARNESS_GENERATORS.items():
        if name not in harnesses:
            continue
        harness_models = model_ids
        # F-022/F-025: Current Aider and Pi releases need a compatible fallback.
        if name in {"aider", "pi"} and compatibility_model_id:
            harness_models = {**model_ids, "sonnet": compatibility_model_id}
        configs.append(
            generator(
                aws_profile_name,
                aws_region,
                harness_models,
                mcp_proxy=mcp_proxy,
            )
        )
    return configs


def build_harnesses_readme(
    configs: list[HarnessConfig], aws_profile_name: str, aws_region: str, model_ids: dict[str, str]
) -> str:
    """Assemble harnesses/README.md from per-harness setup instructions."""
    model_rows = "\n".join(f"| {tier} | `{model_id}` |" for tier, model_id in model_ids.items())
    sections = "\n---\n\n".join(cfg.setup_instructions.strip() + "\n" for cfg in configs)
    return f"""# Using Other Coding Harnesses with This Deployment

Every config in this folder points its harness at the **`{aws_profile_name}`** AWS profile
(region `{aws_region}`) that the installer writes into `~/.aws/config`. That profile's
`credential_process` is this solution's credential-process binary, so ANY harness that
resolves credentials through the AWS SDK default chain gets, with no harness-specific
auth work:

The installer places resolved, customer-usable copies under `~/gip/harnesses`
(`%USERPROFILE%\\gip\\harnesses` on Windows). Use those installed copies rather than
the package-source templates.

| Guarantee | How |
|---|---|
| **SSO login** | Same OIDC browser flow + cached/refreshed tokens as Claude Code |
| **Quota enforcement** | Quota is checked inside credential-process *before* credentials are vended; blocked users get no credentials at all |
| **Per-user cost attribution** | STS session name embeds the user identity; costs appear per user in CUR (`line_item_iam_principal`) |
| **Model/region guardrails** | IAM policy on the assumed role (Anthropic-model allow-list, region conditions) applies to every Bedrock call |

Model IDs are the same Bedrock cross-region inference (CRIS) profile IDs that the
packaged Claude Code settings use:

| Tier | Model ID |
|---|---|
{model_rows}

**What does NOT carry over:** Claude Code's OTEL telemetry dashboards, managed-settings
model locks, and the Claude apps gateway are Claude-Code-only. See
`assets/docs/HARNESSES.md` in the solution repository for the full capability matrix.

---

{sections}"""
