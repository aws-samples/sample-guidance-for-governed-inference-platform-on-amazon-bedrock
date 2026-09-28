# ABOUTME: Deploy command for AWS infrastructure stacks using boto3
# ABOUTME: Handles deployment of auth, monitoring, and dashboard stacks

"""Deploy command - Deploy AWS infrastructure using boto3."""

import importlib.util
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from botocore.exceptions import ClientError
from cleo.commands.command import Command
from cleo.helpers import argument, option
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from governed_inference_platform.cli.utils.aws import get_stack_outputs
from governed_inference_platform.cli.utils.cf_exceptions import (
    CloudFormationError,
    ResourceConflictError,
    StackRollbackError,
)
from governed_inference_platform.cli.utils.cloudformation import CloudFormationManager
from governed_inference_platform.cli.utils.helpers import (
    CODEBUILD_WINDOWS_REGIONS,
    find_nearest_codebuild_region,
    get_codebuild_region,
)
from governed_inference_platform.cli.utils.proc import run_checked
from governed_inference_platform.config import WEBSEARCH_SUPPORTED_REGIONS, Config

# All deployable stack types. Used for input validation and help text.
# Keep in sync with DESTROYABLE_STACKS in destroy.py when adding new stacks.
VALID_STACKS = [
    "auth",
    "guardrails",
    "networking",
    "s3bucket",
    "monitoring",
    "dashboard",
    "cowork-dashboard",
    "analytics",
    "quota",
    "metering",
    "model-lifecycle",
    "distribution",
    "codebuild",
    "websearch",
    "memory",
    "gateway",
    "bootstrap",
    "skills",
]

# Azure tenant ID GUID pattern — matches UUIDs in various URL formats:
#   login.microsoftonline.com/{tenant-id}/v2.0
#   https://login.microsoftonline.com/{tenant-id}
#   {tenant-id} (bare GUID)
_AZURE_GUID_PATTERN = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _stage_skills_registry_template(project_root: Path, build_dir: Path) -> Path:
    """Stage the skills template with the GA-capable AWS SDK bundled."""
    infrastructure = project_root / "deployment" / "infrastructure"
    source_dir = infrastructure / "lambda-functions" / "skills_registry"
    staged_dir = build_dir / "lambda-functions" / "skills_registry"
    staged_dir.parent.mkdir(parents=True)
    shutil.copytree(source_dir, staged_dir, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    template = build_dir / "skills-registry.yaml"
    shutil.copy2(infrastructure / "skills-registry.yaml", template)
    # F-007: uv-created environments may not include pip as a Python module.
    if importlib.util.find_spec("pip") is not None:
        installer = [sys.executable, "-m", "pip", "install"]
    elif uv := shutil.which("uv"):
        installer = [uv, "pip", "install", "--python", sys.executable]
    else:
        raise RuntimeError(
            "Failed to bundle skills registry Lambda dependencies: this Python environment has no pip. "
            "Install pip or uv, then retry."
        )

    result = run_checked(  # nosec B603 — trusted installer argv, no shell
        [
            *installer,
            "--disable-pip-version-check",
            "--no-compile",
            "--requirement",
            str(source_dir / "requirements.txt"),
            "--target",
            str(staged_dir),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to bundle skills registry Lambda dependencies: {result.stderr.strip()}")
    return template


def _extract_azure_tenant_id(domain: str) -> str:
    """Extract Azure AD tenant GUID from provider domain or URL.

    Supports: full URLs, domain/tenant/v2.0, or bare GUIDs.
    Returns the bare GUID, or the original input if no GUID found.
    """
    match = _AZURE_GUID_PATTERN.search(domain)
    return match.group(0) if match else domain


class OidcThumbprintLookupError(CloudFormationError):
    """Raised when the current IAM OIDC provider thumbprints of an existing auth stack cannot be read."""


def _existing_oidc_provider_thumbprints(cf_manager: CloudFormationManager, stack_name: str) -> list[str]:
    """Return the thumbprints IAM currently holds for an existing auth stack's OIDC provider(s).

    IAM rejects removing ThumbprintList from an existing AWS::IAM::OIDCProvider
    (the CloudFormation handler calls UpdateOpenIDConnectProviderThumbprint with
    null -> 400 "Member must not be null"; verified live). A stack whose provider
    was created with thumbprints must therefore keep passing them on every update.
    No hash is computed here: we only re-send what IAM already holds, read back via
    GetOpenIDConnectProvider. Returns [] for a new stack, a stack without an OIDC
    provider (cognito-pool in cognito mode), or a provider that no longer exists in
    IAM (e.g. the stack rolled back and deploy_stack() is about to delete and
    re-create it): there is nothing to preserve, so the create path applies.

    Raises:
        OidcThumbprintLookupError: when CloudFormation or IAM cannot be read (access
            denied, throttling, network), so the caller stops before running into the
            known-failing update instead of silently proceeding.
    """
    import boto3

    try:
        provider_arns = list(cf_manager.get_stack_resource_physical_ids(stack_name, "AWS::IAM::OIDCProvider"))
    except ClientError as e:
        raise OidcThumbprintLookupError(
            f"Could not list the resources of existing stack {stack_name} to preserve its IAM OIDC provider "
            f"thumbprints (cloudformation:DescribeStackResources): {e}"
        ) from e

    thumbprints: list[str] = []
    if not provider_arns:
        return thumbprints

    # IAM is partition-global; the region only selects the partition endpoint.
    iam_client = boto3.client("iam", region_name=cf_manager.region)
    for arn in provider_arns:
        try:
            response = iam_client.get_open_id_connect_provider(OpenIDConnectProviderArn=arn)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "NoSuchEntity":
                # Provider already gone (rolled-back stack, manual deletion): nothing to
                # re-send, the update-with-null failure cannot happen for it.
                continue
            raise OidcThumbprintLookupError(
                f"Could not read the current thumbprints of IAM OIDC provider {arn} "
                f"(iam:GetOpenIDConnectProvider): {e}. IAM rejects removing existing thumbprints during a stack "
                "update, so they must be re-sent; grant the permission or put oidc_thumbprint in the profile."
            ) from e
        for thumbprint in response.get("ThumbprintList") or []:
            if thumbprint and thumbprint not in thumbprints:
                thumbprints.append(thumbprint)
    return thumbprints


def _discover_oidc_endpoints(profile) -> dict:
    """Fetch OIDC discovery document and extract endpoints.

    Builds the issuer URL from profile.provider_type / provider_domain,
    then fetches /.well-known/openid-configuration. Falls back to manual
    endpoint construction per provider type when discovery fails.
    """
    import json
    import urllib.request

    # Build issuer URL from provider type
    provider_type = profile.provider_type or ""
    provider_domain = profile.provider_domain or ""

    tid = None  # needed for azure fallback
    if provider_type == "okta":
        issuer = f"https://{provider_domain}/oauth2/default"
    elif provider_type == "azure":
        tid = _extract_azure_tenant_id(provider_domain)
        issuer = f"https://login.microsoftonline.com/{tid}/v2.0"
    elif provider_type == "google":
        issuer = "https://accounts.google.com"
    elif provider_type == "auth0":
        issuer = f"https://{provider_domain}/"
    else:
        issuer = f"https://{provider_domain}"

    # Try fetching discovery document
    discovery_url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    try:
        req = urllib.request.Request(discovery_url)
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310  # nosec B310 — https URL built from provider type above
            doc = json.loads(resp.read())
        return {
            "issuer": doc.get("issuer", issuer),
            "authorization_endpoint": doc.get("authorization_endpoint", ""),
            "token_endpoint": doc.get("token_endpoint", ""),
            "jwks_uri": doc.get("jwks_uri", ""),
        }
    except Exception:
        # Fallback: construct manually based on provider type
        if provider_type == "okta":
            return {
                "issuer": issuer,
                "authorization_endpoint": f"{issuer}/v1/authorize",
                "token_endpoint": f"{issuer}/v1/token",
                "jwks_uri": f"{issuer}/v1/keys",
            }
        elif provider_type == "azure":
            return {
                "issuer": issuer,
                "authorization_endpoint": f"https://login.microsoftonline.com/{tid}/oauth2/v2.0/authorize",
                "token_endpoint": f"https://login.microsoftonline.com/{tid}/oauth2/v2.0/token",
                "jwks_uri": f"https://login.microsoftonline.com/{tid}/discovery/v2.0/keys",
            }
        else:
            return {
                "issuer": issuer,
                "authorization_endpoint": "",
                "token_endpoint": "",
                "jwks_uri": "",
            }


# Provider types supported by the AgentCore Web Search gateway. The CUSTOM_JWT
# authorizer is provider-agnostic (validates any OIDC id_token), so all OIDC
# providers work. Non-OIDC (idc, none) have no id_token to validate.
WEBSEARCH_SUPPORTED_PROVIDERS = ("cognito", "azure", "okta", "auth0", "google", "generic")


def compute_tracked_models(profile) -> list[str]:
    """Model IDs this deployment references, for the model-lifecycle stack.

    Seeded into the /gip/<profile>/tracked-models SSM parameter at deploy time:
    the admin-selected model, the resolved tier defaults for the profile's CRIS
    geography (what packaged DEFAULT_*_MODEL env vars point at), and any
    extra_models overlay entries. Order-preserving, deduplicated.
    """
    from governed_inference_platform.models import (
        MODEL_TIER_PREFERENCES,
        get_effective_models,
        resolve_model_for_tier,
    )

    tracked: list[str] = []
    if getattr(profile, "selected_model", None):
        tracked.append(profile.selected_model)
    geography = getattr(profile, "cross_region_profile", None) or "us"
    for tier in MODEL_TIER_PREFERENCES:
        resolved = resolve_model_for_tier(tier, geography)
        if resolved:
            tracked.append(resolved)
    extra = getattr(profile, "extra_models", {}) or {}
    if extra:
        try:
            merged = get_effective_models(extra)
            for key in extra:
                for overlay_profile in merged[key]["profiles"].values():
                    tracked.append(overlay_profile["model_id"])
        except ValueError:
            pass  # Invalid overlay is surfaced by init/package validation; don't block deploy
    deduped: list[str] = []
    for model_id in tracked:
        if model_id not in deduped:
            deduped.append(model_id)
    return deduped


def lifecycle_check_regions(profile) -> list[str]:
    """Regions the lifecycle Lambda polls: the profile's allowed Bedrock regions.

    GovCloud endpoints are only reachable from the GovCloud partition (R14
    §1.2), so regions from the other partition are filtered out based on where
    the stack actually runs (profile.aws_region).
    """
    from governed_inference_platform.models import expand_region_sentinels

    regions = expand_region_sentinels(list(getattr(profile, "allowed_bedrock_regions", []) or []))
    if not regions:
        return [profile.aws_region]
    gov_deployment = profile.aws_region.startswith("us-gov")
    filtered = [r for r in regions if r.startswith("us-gov") == gov_deployment]
    return filtered or [profile.aws_region]


def _partition_family(region: str) -> str:
    if region.startswith("us-gov"):
        return "gov"
    if region.startswith("cn-"):
        return "china"
    return "commercial"


def guardrails_regions(profile) -> list[str]:
    """Regions that receive account-level Bedrock guardrail enforcement.

    Enforced guardrail configuration is regional. Deploy only to regions in the
    same partition family as the infrastructure region; cross-partition regions
    are unreachable from the current AWS credentials/partition.
    """
    from governed_inference_platform.models import expand_region_sentinels, get_all_bedrock_regions

    regions = expand_region_sentinels(list(getattr(profile, "allowed_bedrock_regions", []) or []))
    if not regions:
        regions = get_all_bedrock_regions()
    family = _partition_family(profile.aws_region)
    filtered = [r for r in regions if _partition_family(r) == family]
    return filtered or [profile.aws_region]


def guardrails_cleanup_regions(profile) -> list[str]:
    """Return configured and explicitly requested guardrail cleanup regions."""
    current_regions = guardrails_regions(profile)
    family = _partition_family(profile.aws_region)
    explicit = os.environ.get("GIP_GUARDRAILS_CLEANUP_REGIONS", "").split(",")
    candidates = [r.strip() for r in explicit if r.strip() and _partition_family(r.strip()) == family]
    return list(dict.fromkeys([*current_regions, *candidates]))


def _quota_token_limit(profile) -> int:
    """Return the configured token cap; zero means no token cap."""
    return getattr(profile, "monthly_token_limit", 225000000)


def guardrails_name(profile) -> str:
    """Return a Bedrock-valid guardrail name (<=50 chars, alnum/_/-)."""
    raw = getattr(profile, "guardrails_name", "") or f"{profile.identity_pool_name}-guardrail"
    safe = re.sub(r"[^0-9A-Za-z-_]", "-", raw)[:50].strip("-_")
    return safe or "gip-guardrail"


def guardrails_preflight(profile) -> tuple[bool, str | None]:
    strength = getattr(profile, "guardrails_content_filter_strength", "MEDIUM") or "MEDIUM"
    if strength not in ("LOW", "MEDIUM", "HIGH"):
        return False, "guardrails_content_filter_strength must be one of LOW, MEDIUM, HIGH."
    models = getattr(profile, "guardrails_model_include_list", None) or []
    if not isinstance(models, list) or any(not isinstance(m, str) or not m.strip() or "," in m for m in models):
        return False, "guardrails_model_include_list must be a list of non-empty model IDs without commas."
    kms_key_arn = getattr(profile, "guardrails_kms_key_arn", None)
    if kms_key_arn:
        regions = guardrails_regions(profile)
        if len(regions) != 1:
            return False, "guardrails_kms_key_arn can only be used when guardrails deploys to exactly one region."
        if f":{regions[0]}:" not in kms_key_arn:
            return False, f"guardrails_kms_key_arn must be in the guardrails region {regions[0]}."
    return True, None


def build_guardrails_params(profile) -> list[str]:
    """Build CloudFormation parameters for the regional guardrails enforcement stack."""
    params = [
        f"GuardrailName={guardrails_name(profile)}",
        f"ContentFilterStrength={getattr(profile, 'guardrails_content_filter_strength', None) or 'MEDIUM'}",
    ]
    models = [m.strip() for m in (getattr(profile, "guardrails_model_include_list", None) or []) if m.strip()]
    if models:
        params.append(f"ModelIncludeList={','.join(models)}")
    kms_key_arn = getattr(profile, "guardrails_kms_key_arn", None)
    if kms_key_arn:
        params.append(f"KmsKeyArn={kms_key_arn}")
    return params


def get_websearch_region(profile) -> str:
    """Region the web search gateway stack deploys into (defaults to us-east-1)."""
    return getattr(profile, "websearch_region", None) or WEBSEARCH_SUPPORTED_REGIONS[0]


def _normalized_websearch_entitled_groups(profile) -> list[str]:
    """Return trimmed entitlement groups, rejecting entries that normalize empty."""
    groups = getattr(profile, "websearch_entitled_groups", None)
    if groups is None:
        return []
    if not isinstance(groups, (list, tuple)):
        raise ValueError("websearch_entitled_groups must be a list of non-empty group names.")

    normalized = []
    for group in groups:
        if not isinstance(group, str) or not group.strip():
            raise ValueError("websearch_entitled_groups entries must be non-empty after trimming whitespace.")
        normalized.append(group.strip())
    return normalized


def websearch_preflight(profile) -> tuple[bool, str | None]:
    """Validate that web search can be deployed for this profile.

    Returns (ok, error_message). When ok is False, the caller must NOT deploy
    the gateway stack and should surface error_message. Mirrors the per-provider
    guards used elsewhere in deploy (e.g. quota requires OIDC/IDC).
    """
    provider = getattr(profile, "provider_type", None)
    if provider not in WEBSEARCH_SUPPORTED_PROVIDERS:
        return False, (
            f"Web search requires an OIDC provider (one of: "
            f"{', '.join(WEBSEARCH_SUPPORTED_PROVIDERS)}). "
            f"Current provider_type: '{provider}'."
        )

    region = get_websearch_region(profile)
    if region not in WEBSEARCH_SUPPORTED_REGIONS:
        return False, (
            f"Web search region '{region}' is not supported. "
            f"Supported regions: {', '.join(WEBSEARCH_SUPPORTED_REGIONS)}."
        )

    if provider == "cognito" and not (
        getattr(profile, "cognito_user_pool_id", None) and getattr(profile, "client_id", None)
    ):
        return False, "Cognito web search requires cognito_user_pool_id and client_id in the profile."

    if provider == "azure":
        has_issuer = getattr(profile, "oidc_issuer_url", None) or getattr(profile, "provider_domain", None)
        if not (has_issuer and getattr(profile, "client_id", None)):
            return False, (
                "Azure (Entra ID) web search requires the Entra issuer and client_id in the profile. "
                "Re-run 'gip init' to configure SSO."
            )
        # websearch_jwt_audience is optional: the gateway validates the id_token
        # 'aud' claim, which defaults to client_id. Only set it when the Entra
        # app is configured with a custom API audience (e.g. api://<app-id>).

    if provider == "generic":
        if not getattr(profile, "oidc_issuer_url", None):
            return False, (
                "Generic OIDC provider requires oidc_issuer_url to derive the "
                "web-search gateway discovery URL. Re-run 'gip init'."
            )

    try:
        entitled_groups = _normalized_websearch_entitled_groups(profile)
    except ValueError as error:
        return False, str(error)

    deployment_mode = getattr(profile, "websearch_deployment_mode", None) or "development"
    if deployment_mode not in ("development", "production"):
        return False, "websearch_deployment_mode must be 'development' or 'production'."
    if deployment_mode == "production":
        if not entitled_groups:
            return False, "Production web search requires at least one websearch_entitled_groups value."
        if (getattr(profile, "websearch_policy_mode", None) or "LOG_ONLY") != "ENFORCE":
            return False, "Production web search requires websearch_policy_mode=ENFORCE."
        if str(getattr(profile, "websearch_policy_validation_complete", False)).lower() != "true":
            return False, (
                "Production web search requires websearch_policy_validation_complete=true only after real "
                "JWT group decisions have been observed in LOG_ONLY."
            )

    return True, None


def validate_websearch_readiness(profile) -> list[dict]:
    """Validate websearch configuration readiness for a profile.

    Returns a list of diagnostic issues (each a dict with 'level' and 'message').
    Empty list = healthy. Designed for `gip doctor` integration — call this
    to surface websearch misconfigurations without reimplementing the logic.

    Levels: 'error' (broken), 'warning' (degraded), 'info' (informational).
    """
    issues = []
    enabled = getattr(profile, "web_search_enabled", False)

    if not enabled:
        return []  # Websearch not enabled — nothing to validate

    # Check provider compatibility
    ok, msg = websearch_preflight(profile)
    if not ok:
        issues.append({"level": "error", "message": msg})
        return issues  # No point checking further if preflight fails

    # Check gateway URL is populated (set after successful deploy)
    gateway_url = getattr(profile, "websearch_gateway_url", None)
    if not gateway_url:
        issues.append(
            {
                "level": "warning",
                "message": (
                    "web_search_enabled=True but websearch_gateway_url is not set. "
                    "Run 'gip deploy websearch' to deploy the gateway stack."
                ),
            }
        )

    # Check region is supported
    region = get_websearch_region(profile)
    if region not in WEBSEARCH_SUPPORTED_REGIONS:
        issues.append(
            {
                "level": "error",
                "message": f"websearch_region '{region}' is not in supported regions: {', '.join(WEBSEARCH_SUPPORTED_REGIONS)}",
            }
        )

    return issues


def _websearch_discovery_url(profile) -> str:
    """Build the OIDC discovery URL for the gateway CUSTOM_JWT authorizer.

    The gateway's expected issuer must match the id_token's actual `iss` claim,
    so this derives the issuer per-provider and appends the well-known suffix.
    """
    provider = profile.provider_type
    provider_domain = (getattr(profile, "provider_domain", "") or "").rstrip("/")

    if provider == "cognito":
        pool_id = profile.cognito_user_pool_id
        pool_region = pool_id.split("_")[0] if pool_id and "_" in pool_id else profile.aws_region
        return f"https://cognito-idp.{pool_region}.amazonaws.com/{pool_id}/.well-known/openid-configuration"
    elif provider == "azure":
        tenant_id = _extract_azure_tenant_id(getattr(profile, "oidc_issuer_url", None) or provider_domain or "")
        return f"https://login.microsoftonline.com/{tenant_id}/v2.0/.well-known/openid-configuration"
    elif provider == "okta":
        return f"https://{provider_domain}/oauth2/default/.well-known/openid-configuration"
    elif provider == "auth0":
        return f"https://{provider_domain}/.well-known/openid-configuration"
    elif provider == "google":
        return "https://accounts.google.com/.well-known/openid-configuration"
    elif provider == "generic":
        issuer = (getattr(profile, "oidc_issuer_url", "") or "").rstrip("/")
        if not issuer.startswith(("http://", "https://")):
            issuer = f"https://{issuer}"
        return f"{issuer}/.well-known/openid-configuration"
    else:
        raise ValueError(f"Unsupported provider_type '{provider}' for web search.")


def build_websearch_params(profile) -> list[str]:
    """Build CloudFormation parameter overrides for the web search gateway stack.

    Matches the merged gateway template parameters (DiscoveryUrl, ClientId,
    DomainExcludeList). The CUSTOM_JWT authorizer validates the inbound
    id_token's 'aud' claim against ClientId; for an OIDC id_token aud == client_id.
    The stack is deployed into a Web Search supported region via
    ``get_websearch_region`` (region is no longer a template parameter).
    """
    discovery_url = _websearch_discovery_url(profile)
    # The merged gateway template (PR #607) validates the inbound id_token's
    # 'aud' claim against a single ClientId parameter (AllowedAudience: [ClientId]).
    # For an OIDC id_token aud == client_id, so client_id works for every
    # provider (Cognito, Okta, Auth0, Google, generic). Entra ID may override
    # with a custom API audience (api://<app-id>) when the app is configured
    # with one; otherwise the default aud (client_id) is used.
    expected_audience = profile.client_id
    if profile.provider_type == "azure" and getattr(profile, "websearch_jwt_audience", None):
        expected_audience = profile.websearch_jwt_audience
    params = [
        f"DiscoveryUrl={discovery_url}",
        f"ClientId={expected_audience}",
        f"DeploymentMode={getattr(profile, 'websearch_deployment_mode', None) or 'development'}",
        f"PolicyValidationComplete={str(getattr(profile, 'websearch_policy_validation_complete', False)).lower()}",
    ]
    denylist = getattr(profile, "websearch_domain_denylist", None) or []
    if denylist:
        params.append(f"DomainExcludeList={','.join(denylist)}")
    # Per-user entitlement (REVIEW #5): when entitled groups are configured,
    # the template attaches a Cedar policy engine (OIDC only). PolicyMode is
    # only meaningful alongside EntitledGroups; the template default (LOG_ONLY)
    # applies when the profile does not set websearch_policy_mode.
    entitled_groups = _normalized_websearch_entitled_groups(profile)
    if entitled_groups:
        params.append(f"EntitledGroups={','.join(entitled_groups)}")
        params.append(f"PolicyMode={getattr(profile, 'websearch_policy_mode', None) or 'LOG_ONLY'}")
    return params


UPSTREAM_GATEWAY_REPOSITORY = "https://github.com/aws-samples/anthropic-on-aws"
UPSTREAM_GATEWAY_VENDOR_PATH = "vendor/aws-samples/anthropic-on-aws"
UPSTREAM_GATEWAY_FETCH_SCRIPT = "scripts/fetch-claude-apps-gateway.sh"


def _pinned_upstream_gateway_commit() -> str | None:
    """Best effort: read the pinned upstream commit when run from a repository checkout."""
    pin = Path.cwd() / UPSTREAM_GATEWAY_VENDOR_PATH / "UPSTREAM.json"
    try:
        match = re.search(r'"commit":\s*"([0-9a-f]{40})"', pin.read_text(encoding="utf-8"))
    except OSError:
        return None
    return match.group(1) if match else None


def upstream_gateway_guidance(component: str = "gateway") -> str:
    """Return the authoritative upstream path for gateway or Desktop bootstrap (ADR-0036)."""
    subtree = "claude-apps-gateway-bootstrap" if component == "bootstrap" else "claude-apps-gateway"
    ref = _pinned_upstream_gateway_commit() or "main"
    return (
        f"GIP no longer maintains or deploys a separate {component} implementation. "
        f"Use the unmodified AWS Samples source: run {UPSTREAM_GATEWAY_FETCH_SCRIPT} to materialize the "
        f"upstream commit pinned in {UPSTREAM_GATEWAY_VENDOR_PATH}/UPSTREAM.json (Git tree identities "
        f"verify paths, types, executable modes, symlink targets, and bytes), then cd "
        f"{UPSTREAM_GATEWAY_VENDOR_PATH}/{subtree} "
        f"(canonical source: {UPSTREAM_GATEWAY_REPOSITORY}/tree/{ref}/{subtree})."
    )


# --- AgentCore Memory stack (opt-in, ADR-0016) --------------------------------
# The memory stack attaches a second GatewayTarget to the EXISTING websearch
# gateway (separate stack, plain-string GatewayIdentifier — R9 F17), so it
# deploys into the websearch region and requires the websearch stack outputs.

MEMORY_MODES = ("extracted-only", "full")
MEMORY_DEPLOY_GATES = ("log-only", "active")


def memory_preflight(profile) -> tuple[bool, str | None]:
    """Validate that the memory stack can be deployed for this profile.

    Returns (ok, error_message). The memory tools ride on the websearch
    gateway, so web search must be enabled and pass its own preflight; at
    least one memory scope (user/org) must be enabled; mode/retention/gate
    values must be in range (ADR-0012 D4).
    """
    if not getattr(profile, "web_search_enabled", False):
        return False, (
            "Memory requires the web search gateway (the memory tools attach to it as a "
            "second target). Enable web search via 'gip init' first."
        )
    ok, msg = websearch_preflight(profile)
    if not ok:
        return False, f"Memory rides on the web search gateway, which is not deployable: {msg}"

    if not (getattr(profile, "memory_user_enabled", False) or getattr(profile, "memory_org_enabled", False)):
        return False, (
            "Memory is enabled but neither memory_user_enabled nor memory_org_enabled is set. "
            "Enable at least one scope via 'gip init'."
        )

    mode = getattr(profile, "memory_mode", "extracted-only")
    if mode not in MEMORY_MODES:
        return False, f"memory_mode '{mode}' is invalid (one of: {', '.join(MEMORY_MODES)})."

    retention = getattr(profile, "memory_raw_event_retention_days", 30)
    if mode == "full" and not (isinstance(retention, int) and 3 <= retention <= 365):
        return False, f"memory_raw_event_retention_days must be 3-365 in full mode (got {retention})."

    gate = getattr(profile, "memory_deploy_gate", "log-only")
    if gate not in MEMORY_DEPLOY_GATES:
        return False, f"memory_deploy_gate '{gate}' is invalid (one of: {', '.join(MEMORY_DEPLOY_GATES)})."

    return True, None


def build_memory_params(profile, gateway_id: str, gateway_role_arn: str) -> list[str]:
    """Build CloudFormation parameter overrides for the memory stack.

    ``gateway_id`` / ``gateway_role_arn`` come from the deployed websearch
    stack outputs (GatewayId / GatewayExecutionRoleArn) — the memory template
    takes them as plain parameters so the websearch template stays untouched
    (axiom A1). IdentityPoolName seeds the Memory resource name, which is
    replacement-on-update and must stay stable.
    """
    params = [
        f"IdentityPoolName={profile.identity_pool_name}",
        f"GatewayIdentifier={gateway_id}",
        f"GatewayExecutionRoleArn={gateway_role_arn}",
        f"EnableUserMemory={str(getattr(profile, 'memory_user_enabled', False)).lower()}",
        f"EnableOrgMemory={str(getattr(profile, 'memory_org_enabled', False)).lower()}",
        f"MemoryMode={getattr(profile, 'memory_mode', None) or 'extracted-only'}",
        f"RawEventRetentionDays={getattr(profile, 'memory_raw_event_retention_days', None) or 30}",
        f"DeployGate={getattr(profile, 'memory_deploy_gate', None) or 'log-only'}",
    ]
    if getattr(profile, "memory_kms_key_arn", None):
        params.append(f"KmsKeyArn={profile.memory_kms_key_arn}")
    org_groups = getattr(profile, "memory_org_write_groups", None) or []
    if org_groups:
        params.append(f"OrgMemoryWriteGroups={','.join(org_groups)}")
    return params


def build_skills_params(profile) -> list[str]:
    """Build CloudFormation parameter overrides for the skills registry stack.

    Maps profile skills_* fields onto deployment/infrastructure/skills-registry.yaml
    parameters. The retired Desktop bootstrap plugin-index parameter is not
    emitted; Desktop plugin delivery follows the mirrored upstream contract.
    """
    params = [f"RegistryName={getattr(profile, 'skills_registry_name', None) or 'gip-skills'}"]
    org_id = getattr(profile, "skills_organization_id", "") or ""
    if org_id:
        params.append(f"OrganizationId={org_id}")
    publisher_groups = getattr(profile, "skills_publisher_groups", None) or []
    if publisher_groups:
        params.append(f"PublisherGroups={','.join(publisher_groups)}")
    curator_groups = getattr(profile, "skills_curator_groups", None) or []
    if curator_groups:
        params.append(f"CuratorGroups={','.join(curator_groups)}")
    return params


def _poll_websearch_target_ready(
    gateway_id: str, region: str, console, timeout: int = 600, interval: int = 15, session=None
) -> bool:
    """Poll the gateway's connector target until READY.

    CFN CREATE_COMPLETE precedes the target reaching READY — the connector
    provisions asynchronously. Returns True on READY, False on FAILED or timeout.
    Uses the provided boto3 session to respect proxy/CA/endpoint config.
    """
    import time

    try:
        import boto3

        if session is None:
            session = boto3.Session(region_name=region)
        client = session.client("bedrock-agentcore-control", region_name=region)
    except Exception as e:
        console.print(f"[yellow]\u26a0 Could not create AgentCore client to poll target: {e}[/yellow]")
        return False

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = client.list_gateway_targets(gatewayIdentifier=gateway_id, maxResults=1)
            # ListGatewayTargets returns the targets under "items" (per the
            # bedrock-agentcore-control API). Reading any other key yields an
            # empty list, so the poll would never observe READY and would spin
            # silently until the timeout.
            targets = resp.get("items", [])
            if targets:
                status = targets[0].get("status", "")
                if status == "READY":
                    console.print("[green]\u2713 Web search connector target is READY[/green]")
                    return True
                elif status == "FAILED":
                    reason = targets[0].get("statusReason", "unknown")
                    console.print(f"[red]\u2717 Connector target FAILED: {reason}[/red]")
                    return False
                console.print(f"[dim]  Connector status: {status}, waiting...[/dim]")
        except Exception as e:
            console.print(f"[yellow]  Poll error (retrying): {e}[/yellow]")
        time.sleep(interval)

    console.print(f"[yellow]\u26a0 Connector target did not reach READY within {timeout}s[/yellow]")
    return False


class DeployCommand(Command):
    name = "deploy"
    description = "Deploy AWS infrastructure (auth, monitoring, dashboards)"

    arguments = [
        argument(
            "stack",
            description="Specific stack to deploy (auth/networking/monitoring/dashboard/analytics/quota)",
            optional=True,
        )
    ]

    options = [
        option(
            "profile", description="Configuration profile to use (defaults to active profile)", flag=False, default=None
        ),
        option("dry-run", description="Show what would be deployed without executing", flag=True),
        option("show-commands", description="Show AWS CLI commands instead of executing", flag=True),
    ]

    def handle(self) -> int:
        """Execute the deploy command."""
        console = Console()

        # Welcome
        console.print(
            Panel.fit(
                "[bold cyan]Governed Inference Platform Infrastructure Deployment[/bold cyan]\n\n"
                "Deploy or update CloudFormation stacks",
                border_style="cyan",
                padding=(1, 2),
            )
        )

        # Load configuration
        config = Config.load()

        # Get profile name (use active profile if not specified)
        profile_name = self.option("profile")
        if not profile_name:
            profile_name = config.active_profile
            console.print(f"[dim]Using active profile: {profile_name}[/dim]\n")
        else:
            console.print(f"[dim]Using profile: {profile_name}[/dim]\n")

        profile = config.get_profile(profile_name)

        if not profile:
            if profile_name:
                console.print(f"[red]Profile '{profile_name}' not found. Run 'poetry run gip init' first.[/red]")
            else:
                console.print(
                    "[red]No active profile set. Run 'poetry run gip init' or "
                    "'poetry run gip context use <profile>' first.[/red]"
                )
            return 1

        # Get deployment options
        stack_arg = self.argument("stack")
        dry_run = self.option("dry-run")
        show_commands = self.option("show-commands")

        # Determine which stacks to deploy
        stacks_to_deploy = []

        if stack_arg:
            # Deploy specific stack
            if stack_arg == "auth":
                if profile.effective_auth_type == "none":
                    console.print("[yellow]Authentication stack is disabled for 'none' auth type.[/yellow]")
                    console.print("Enable authentication by running: [cyan]poetry run gip init[/cyan]")
                    return 1
                stacks_to_deploy.append(("auth", "Authentication Stack (Cognito + IAM)"))
            elif stack_arg == "networking":
                if not profile.monitoring_enabled:
                    console.print("[yellow]Monitoring is not enabled in your configuration.[/yellow]")
                    return 1
                if getattr(profile, "monitoring_mode", "central") == "sidecar":
                    console.print(
                        "[yellow]Networking stack is not used in sidecar monitoring mode "
                        "(the local OTEL collector needs no VPC/subnets).[/yellow]"
                    )
                    return 1
                stacks_to_deploy.append(("networking", "VPC Networking for OTEL Collector"))
            elif stack_arg == "s3bucket":
                # Recovery path: quota/monitoring error messages instruct users to
                # run `gip deploy s3bucket` when the artifacts bucket is missing.
                # No prerequisite gate — it is a leaf stack consumed by monitoring
                # (central), quota Lambda packaging (sidecar), and bootstrap plugins.
                stacks_to_deploy.append(("s3bucket", "S3 Bucket"))
            elif stack_arg == "monitoring":
                if not profile.monitoring_enabled:
                    console.print("[yellow]Monitoring is not enabled in your configuration.[/yellow]")
                    return 1
                if getattr(profile, "monitoring_mode", "central") == "sidecar":
                    console.print(
                        "[yellow]The central OTEL collector stack is not used in sidecar mode "
                        "(telemetry is sent to CloudWatch by the local collector).[/yellow]"
                    )
                    return 1
                stacks_to_deploy.append(("monitoring", "OpenTelemetry Collector"))
            elif stack_arg == "dashboard":
                if profile.monitoring_enabled:
                    stacks_to_deploy.append(("dashboard", "CloudWatch Dashboard"))
                else:
                    console.print("[yellow]Monitoring is not enabled in your configuration.[/yellow]")
                    return 1
            elif stack_arg == "cowork-dashboard":
                if not profile.monitoring_enabled:
                    console.print("[yellow]Monitoring is not enabled in your configuration.[/yellow]")
                    return 1
                if getattr(profile, "monitoring_mode", "central") == "sidecar":
                    console.print(
                        "[yellow]CoWork dashboard requires central monitoring mode: its metric filters "
                        "read log groups created by the central collector stack, which sidecar mode "
                        "does not deploy. (CoWork telemetry itself works in sidecar mode via the local "
                        "OTEL proxy.)[/yellow]"
                    )
                    return 1
                stacks_to_deploy.append(("cowork-dashboard", "CoWork CloudWatch Dashboard"))
            elif stack_arg == "analytics":
                if profile.monitoring_enabled:
                    stacks_to_deploy.append(("analytics", "Analytics Pipeline (Kinesis Firehose + Athena)"))
                else:
                    console.print("[yellow]Analytics requires monitoring to be enabled in your configuration.[/yellow]")
                    return 1
            elif stack_arg == "quota":
                if profile.effective_auth_type not in ("oidc", "idc"):
                    console.print(
                        "[yellow]Quota monitoring requires user authentication "
                        "(OIDC or IAM Identity Center) and cannot be deployed without it.[/yellow]"
                    )
                    console.print(
                        "[dim]See issue #454. Enable OIDC or IDC authentication to deploy quota monitoring.[/dim]"
                    )
                    return 1
                if profile.monitoring_enabled:
                    if getattr(profile, "quota_monitoring_enabled", False):
                        stacks_to_deploy.append(("quota", "Quota Monitoring (Per-User Token Limits)"))
                    else:
                        console.print("[yellow]Quota monitoring is not enabled in your configuration.[/yellow]")
                        return 1
                else:
                    console.print(
                        "[yellow]Quota monitoring requires monitoring to be enabled in your configuration.[/yellow]"
                    )
                    return 1
            elif stack_arg == "metering":
                if not getattr(profile, "metering_enabled", False):
                    console.print("[yellow]Server-side metering is not enabled in your configuration.[/yellow]")
                    console.print(
                        "[dim]Set metering_enabled=true in the profile JSON (see "
                        "assets/docs/QUOTA_MONITORING.md 'Server-Side Metering').[/dim]"
                    )
                    return 1
                if not getattr(profile, "quota_monitoring_enabled", False):
                    console.print(
                        "[yellow]Server-side metering requires quota monitoring to be enabled "
                        "(it accrues into the quota stack's UserQuotaMetrics table).[/yellow]"
                    )
                    return 1
                stacks_to_deploy.append(("metering", "Server-Side Metering (Bedrock invocation logs)"))
            elif stack_arg == "model-lifecycle":
                if not getattr(profile, "model_lifecycle_enabled", False):
                    console.print("[yellow]Model lifecycle alerts are not enabled in your configuration.[/yellow]")
                    console.print("Run 'poetry run gip init' and enable model lifecycle alerts.")
                    return 1
                stacks_to_deploy.append(("model-lifecycle", "Model Lifecycle Alerts (Bedrock legacy/EOL monitoring)"))
            elif stack_arg == "guardrails":
                if not getattr(profile, "guardrails_enabled", False):
                    console.print(
                        "[yellow]Bedrock Guardrails enforcement is not enabled in your configuration.[/yellow]"
                    )
                    console.print(
                        "Set guardrails_enabled=true in the profile JSON or enable guardrails via 'gip init'."
                    )
                    return 1
                ok, msg = guardrails_preflight(profile)
                if not ok:
                    console.print(f"[yellow]{msg}[/yellow]")
                    return 1
                stacks_to_deploy.append(("guardrails", "Bedrock Guardrails account-level enforcement"))
            elif stack_arg == "distribution":
                if profile.enable_distribution:
                    stacks_to_deploy.append(("distribution", "Distribution infrastructure (S3 + IAM)"))
                else:
                    console.print("[yellow]Distribution features not enabled in profile.[/yellow]")
                    console.print("Run 'poetry run gip init' and enable distribution features.")
                    return 1
            elif stack_arg == "codebuild":
                if profile.enable_codebuild:
                    stacks_to_deploy.append(("codebuild", "CodeBuild for Windows binary builds"))
                else:
                    console.print("[yellow]CodeBuild is not enabled in your configuration.[/yellow]")
                    return 1
            elif stack_arg == "bootstrap":
                console.print(f"[yellow]{upstream_gateway_guidance('bootstrap')}[/yellow]")
                return 1
            elif stack_arg == "websearch":
                if not getattr(profile, "web_search_enabled", False):
                    console.print("[yellow]Web search is not enabled in your configuration.[/yellow]")
                    console.print("Run 'poetry run gip init' and enable web search.")
                    return 1
                ok, msg = websearch_preflight(profile)
                if not ok:
                    console.print(f"[yellow]{msg}[/yellow]")
                    return 1
                stacks_to_deploy.append(("websearch", "AgentCore Gateway + Web Search connector"))
            elif stack_arg == "memory":
                if not getattr(profile, "memory_enabled", False):
                    console.print("[yellow]Memory is not enabled in your configuration.[/yellow]")
                    console.print("Run 'poetry run gip init' and enable memory (requires web search).")
                    return 1
                ok, msg = memory_preflight(profile)
                if not ok:
                    console.print(f"[yellow]{msg}[/yellow]")
                    return 1
                stacks_to_deploy.append(("memory", "AgentCore Memory (user/org memory tools)"))
            elif stack_arg == "gateway":
                console.print(f"[yellow]{upstream_gateway_guidance()}[/yellow]")
                return 1
            elif stack_arg == "skills":
                if not getattr(profile, "skills_registry_enabled", False):
                    console.print("[yellow]The skills registry is not enabled in your configuration.[/yellow]")
                    console.print(
                        "Run 'poetry run gip init' and enable the skills registry, or set "
                        "skills_registry_enabled=true in the profile JSON (see assets/docs/SKILLS_REGISTRY.md)."
                    )
                    return 1
                stacks_to_deploy.append(("skills", "Skills Registry (Agent Registry + artifact bucket)"))
            else:
                console.print(f"[red]Unknown stack: {stack_arg}[/red]")
                console.print(f"Valid stacks: {', '.join(VALID_STACKS)}\n")

                console.print("[dim]Tip: Use 'gip deploy' without arguments to deploy all enabled stacks.[/dim]")
                console.print("[dim]Use 'gip deploy quota' for quota-specific updates or late enablement.[/dim]")
                return 1
        else:
            # Deploy all configured stacks in dependency order.
            stacks_to_deploy = self._select_full_deploy_stacks(profile, console)

            # Web search gateway — independent stack, only needs the IdP from auth.
            # Deployed cross-region (us-east-1) where the connector is available.
            if getattr(profile, "web_search_enabled", False):
                ok, msg = websearch_preflight(profile)
                if ok:
                    stacks_to_deploy.append(("websearch", "AgentCore Gateway + Web Search connector"))
                else:
                    console.print(f"[yellow]⚠ Skipping web search gateway: {msg}[/yellow]")

            # AgentCore Memory — rides on the websearch gateway (second target,
            # separate stack), so it schedules right after it (ADR-0016).
            if getattr(profile, "memory_enabled", False):
                ok, msg = memory_preflight(profile)
                if ok:
                    stacks_to_deploy.append(("memory", "AgentCore Memory (user/org memory tools)"))
                else:
                    console.print(f"[yellow]⚠ Skipping memory stack: {msg}[/yellow]")

            if getattr(profile, "gateway_enabled", False):
                console.print(f"[yellow]⚠ Skipping legacy gateway settings. {upstream_gateway_guidance()}[/yellow]")

            cowork_mode = getattr(profile, "cowork_config_delivery", "static")
            if cowork_mode in ("bootstrap-device-code", "bootstrap-oidc-bearer"):
                console.print(
                    f"[yellow]⚠ Skipping legacy bootstrap settings. {upstream_gateway_guidance('bootstrap')}[/yellow]"
                )

            # Skills registry — independent optional stack (S3 + Agent Registry
            # custom resource). Follows bootstrap so the plugins bucket exists.
            if getattr(profile, "skills_registry_enabled", False):
                if not any(stack == "s3bucket" for stack, _desc in stacks_to_deploy):
                    stacks_to_deploy.append(("s3bucket", "S3 Bucket"))
                stacks_to_deploy.append(("skills", "Skills Registry (Agent Registry + artifact bucket)"))

        # Initialize CloudFormation manager
        cf_manager = CloudFormationManager(region=profile.aws_region)

        # Show deployment plan
        console.print("\n[bold]Deployment Plan:[/bold]")
        table = Table(box=box.SIMPLE)
        table.add_column("Stack", style="cyan")
        table.add_column("Description")
        table.add_column("Status")

        for stack_type, description in stacks_to_deploy:
            stack_name = profile.stack_names.get(stack_type, f"{profile.identity_pool_name}-{stack_type}")
            # CodeBuild may live in a different region than the main infrastructure.
            status_manager = cf_manager
            if stack_type == "codebuild":
                cb_region = get_codebuild_region(profile)
                if cb_region != profile.aws_region:
                    status_manager = CloudFormationManager(region=cb_region)
            elif stack_type in ("websearch", "memory"):
                ws_region = get_websearch_region(profile)
                if ws_region != profile.aws_region:
                    status_manager = CloudFormationManager(region=ws_region)
            status = status_manager.get_stack_status(stack_name)
            if status and status in ["CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"]:
                status_display = "[green]Update[/green]"
            else:
                status_display = "[yellow]Create[/yellow]"
            table.add_row(stack_type, description, status_display)

        console.print(table)

        # Check for orphaned stacks (exist but disabled in config)
        # Only check when deploying ALL stacks, not when deploying a specific stack
        orphaned_stacks = []
        if not stack_arg:  # Only check for orphaned stacks when deploying all stacks
            orphaned_stacks = self._check_orphaned_stacks(stacks_to_deploy, profile, cf_manager, console)

        if orphaned_stacks and not dry_run and not show_commands:
            import questionary

            console.print("\n[yellow]⚠️  Found stacks that exist but are disabled in your configuration:[/yellow]")
            for orphan in orphaned_stacks:
                stack_type, stack_name, status = orphan[:3]
                orphan_region = orphan[3] if len(orphan) > 3 else None
                region_label = f" in {orphan_region}" if orphan_region else ""
                console.print(f"  • {stack_type}{region_label}: {stack_name} ({status})")

            should_delete = questionary.confirm("Would you like to delete these orphaned stacks?", default=False).ask()

            if should_delete:
                console.print("\n[bold]Cleaning up orphaned stacks...[/bold]\n")
                # Delete in reverse deployment order (dependents first)
                for orphan in reversed(orphaned_stacks):
                    stack_type, stack_name, _status = orphan[:3]
                    orphan_region = orphan[3] if len(orphan) > 3 else None
                    try:
                        console.print(f"[yellow]Deleting {stack_type} stack: {stack_name}...[/yellow]")
                        # Some stacks live outside the profile region; delete them where they were deployed.
                        del_mgr = cf_manager
                        if stack_type == "guardrails":
                            regions = [orphan_region] if orphan_region else guardrails_cleanup_regions(profile)
                            for guardrails_region in regions:
                                regional_mgr = (
                                    cf_manager
                                    if guardrails_region == profile.aws_region
                                    else CloudFormationManager(region=guardrails_region)
                                )
                                regional_mgr.delete_stack(stack_name)
                            console.print(f"[green]✓ {stack_type} stack deletion initiated[/green]")
                            continue
                        if stack_type == "codebuild":
                            cb_region = get_codebuild_region(profile)
                            if cb_region != profile.aws_region:
                                del_mgr = CloudFormationManager(region=cb_region)
                        elif stack_type in ("websearch", "memory"):
                            ws_region = get_websearch_region(profile)
                            if ws_region != profile.aws_region:
                                del_mgr = CloudFormationManager(region=ws_region)
                        del_mgr.delete_stack(stack_name)
                        console.print(f"[green]✓ {stack_type} stack deletion initiated[/green]")
                    except Exception as e:
                        console.print(f"[red]✗ Failed to delete {stack_type} stack: {e}[/red]")
                console.print("")

        if dry_run:
            console.print("\n[yellow]Dry run mode - no changes will be made[/yellow]")
            return 0

        if show_commands:
            # Just show the commands that would be executed
            self._show_all_deployment_commands(stacks_to_deploy, profile, console)
            return 0

        # Deploy stacks
        console.print("\n[bold]Deploying stacks...[/bold]\n")

        failed = False
        for stack_type, description in stacks_to_deploy:
            console.print(f"[bold]{description}[/bold]")

            result = self._deploy_stack(stack_type, profile, console, cf_manager)
            if result != 0:
                if stack_arg:
                    failed = True
                    console.print(f"[red]Failed to deploy {stack_type} stack[/red]")
                    break
                if stack_type == "websearch":
                    # Web search is an optional add-on. A failure here must not mark
                    # the whole platform deploy as failed or abort stacks that follow;
                    # surface it clearly with remediation and continue. (Running
                    # 'gip deploy websearch' explicitly still returns non-zero.)
                    console.print(
                        "[yellow]⚠ Web search gateway deploy failed — this is optional and does not "
                        "block the rest of the deployment.[/yellow]\n"
                        "[dim]  Re-run 'gip deploy websearch' to retry, or "
                        "'gip destroy websearch' to clean up.[/dim]"
                    )
                    console.print("")
                    continue
                if stack_type == "memory":
                    # The memory stack is an optional add-on riding on the
                    # websearch gateway; a failure must not abort the stacks
                    # that follow. ('gip deploy memory' explicitly still
                    # returns non-zero.)
                    console.print(
                        "[yellow]⚠ Memory stack deploy failed — this is optional and does not "
                        "block the rest of the deployment.[/yellow]\n"
                        "[dim]  Re-run 'gip deploy memory' to retry, or "
                        "'gip destroy memory' to clean up.[/dim]"
                    )
                    console.print("")
                    continue
                if stack_type == "metering":
                    # Server-side metering is an optional add-on (shadow mode by
                    # default); a per-region failure must not abort what follows.
                    # ('gip deploy metering' explicitly still returns non-zero.)
                    console.print(
                        "[yellow]⚠ Server-side metering deploy failed — this is optional and does not "
                        "block the rest of the deployment.[/yellow]\n"
                        "[dim]  Re-run 'gip deploy metering' to retry, or "
                        "'gip destroy metering' to clean up.[/dim]"
                    )
                    console.print("")
                    continue
                if stack_type == "skills":
                    # The skills registry is an optional add-on; a failure must
                    # not abort the stacks that follow. ('gip deploy skills'
                    # explicitly still returns non-zero.)
                    console.print(
                        "[yellow]⚠ Skills registry deploy failed — this is optional and does not "
                        "block the rest of the deployment.[/yellow]\n"
                        "[dim]  Re-run 'gip deploy skills' to retry, or "
                        "'gip destroy skills' to clean up.[/dim]"
                    )
                    console.print("")
                    continue
                if stack_type == "model-lifecycle":
                    # Lifecycle alerts are an optional add-on; a failure must not
                    # abort the stacks that follow in a full deploy.
                    # ('gip deploy model-lifecycle' explicitly still returns non-zero.)
                    console.print(
                        "[yellow]⚠ Model lifecycle stack deploy failed — this is optional and does not "
                        "block the rest of the deployment.[/yellow]\n"
                        "[dim]  Re-run 'gip deploy model-lifecycle' to retry, or "
                        "'gip destroy model-lifecycle' to clean up.[/dim]"
                    )
                    console.print("")
                    continue
                failed = True
                console.print(f"[red]Failed to deploy {stack_type} stack[/red]")
                break
            console.print("")

        if failed:
            console.print("\n[red]Deployment failed. Check the errors above.[/red]")
            return 1

        # Show summary
        console.print("\n[bold green]Deployment complete![/bold green]")

        console.print("\n[bold]Stack Outputs:[/bold]")
        self._show_stack_outputs(profile, console, config)

        return 0

    def _select_full_deploy_stacks(self, profile, console: Console) -> list:
        """Return the ordered ``(stack_type, description)`` list for a full ``gip deploy``.

        Pure selection logic with no AWS calls so it can be unit-tested. The only
        side effect is a warning printed via ``console`` when quota is enabled but
        the auth type cannot support it.

        Ordering constraints:
        - auth always comes first (produces the IAM role + OIDC provider every
          other stack may reference). Skipped when auth_type == "none".
        - networking must precede any stack that reads its VPC/subnet outputs:
          central monitoring (OTel ECS ALB) and landing-page distribution.
        - distribution follows networking for the landing-page variant; the
          presigned-s3 variant doesn't need networking but the order is harmless.
        - dashboard / analytics / quota all follow monitoring.
        - codebuild is independent and can trail.

        Monitoring mode is the key gate: sidecar runs a local OTEL collector that
        ships metrics straight to CloudWatch, so it needs no VPC/ECS/ALB and no
        Athena pipeline — only the CloudWatch dashboard. The #338 Go-rewrite
        refactor dropped this gate, so sidecar profiles were deploying the entire
        central stack (VPC + ECS + ALB + Athena), which fails in accounts that
        disallow new VPCs.
        """
        stacks_to_deploy = []

        if profile.effective_auth_type != "none":
            stacks_to_deploy.append(("auth", "Authentication Stack (Cognito + IAM)"))

        monitoring_mode = getattr(profile, "monitoring_mode", "central")
        central_monitoring = profile.monitoring_enabled and monitoring_mode == "central"

        # Networking first so any downstream stack can read its outputs.
        need_networking = central_monitoring or profile.enable_distribution
        if need_networking:
            vpc_config = profile.monitoring_config or {}
            if vpc_config.get("create_vpc", True):
                stacks_to_deploy.append(("networking", "VPC Networking for OTEL Collector"))

        # Distribution (landing-page reads networking outputs; presigned-s3
        # doesn't, but the scheduling order is a no-op either way).
        if profile.enable_distribution:
            stacks_to_deploy.append(("distribution", "Distribution infrastructure (S3 + IAM)"))

        # Monitoring and its dependents.
        if profile.monitoring_enabled:
            if central_monitoring:
                stacks_to_deploy.append(("s3bucket", "S3 Bucket"))
                stacks_to_deploy.append(("monitoring", "OpenTelemetry Collector"))
                stacks_to_deploy.append(("dashboard", "CloudWatch Dashboard"))
                stacks_to_deploy.append(("cowork-dashboard", "CoWork CloudWatch Dashboard"))
                # Analytics defaults to True for backward compatibility.
                if getattr(profile, "analytics_enabled", True):
                    stacks_to_deploy.append(("analytics", "Analytics Pipeline (Kinesis Firehose + Athena)"))
            else:
                # Sidecar mode: metrics reach CloudWatch via the local collector,
                # so the only server-side stack is the CloudWatch dashboard
                # (PromQL). No networking/ECS, no Athena pipeline, and CoWork
                # cannot export telemetry in this mode.
                stacks_to_deploy.append(("dashboard", "CloudWatch Dashboard"))

            # Quota enforcement works in both monitoring modes. It needs the
            # s3bucket stack for Lambda packaging; central scheduled it above, so
            # add it here for sidecar before the quota stack. Quota requires OIDC
            # or IDC (per-user identity); skip with a warning rather than letting
            # CloudFormation fail mid-deploy (issue #454).
            if getattr(profile, "quota_monitoring_enabled", False):
                if profile.effective_auth_type in ("oidc", "idc"):
                    if not central_monitoring:
                        stacks_to_deploy.append(("s3bucket", "S3 Bucket"))
                    stacks_to_deploy.append(("quota", "Quota Monitoring (Per-User Token Limits)"))
                    # Server-side metering rides on the quota stack (table +
                    # alert topic), so it only ever follows quota.
                    if getattr(profile, "metering_enabled", False):
                        stacks_to_deploy.append(("metering", "Server-Side Metering (Bedrock invocation logs)"))
                else:
                    console.print(
                        "[yellow]⚠ Skipping quota monitoring stack: quota enforcement requires "
                        "user authentication (OIDC or IAM Identity Center).[/yellow]"
                    )
                    console.print(
                        "[dim]Re-run 'gip init' with OIDC or IDC authentication to deploy "
                        "quota monitoring. See issue #454.[/dim]"
                    )

        if getattr(profile, "enable_codebuild", False):
            stacks_to_deploy.append(("codebuild", "CodeBuild for Windows binary builds"))

        # Bedrock Guardrails enforcement is security policy, not an add-on data
        # sink: if enabled, a deployment failure must fail the whole deploy.
        if getattr(profile, "guardrails_enabled", False):
            stacks_to_deploy.append(("guardrails", "Bedrock Guardrails account-level enforcement"))

        # Model lifecycle alerts — independent optional stack (works with every
        # auth type and monitoring mode; reuses the quota alert topic when one
        # exists). Needs the s3bucket stack for Lambda packaging.
        if getattr(profile, "model_lifecycle_enabled", False):
            if not any(stack == "s3bucket" for stack, _desc in stacks_to_deploy):
                stacks_to_deploy.append(("s3bucket", "S3 Bucket"))
            stacks_to_deploy.append(("model-lifecycle", "Model Lifecycle Alerts (Bedrock legacy/EOL monitoring)"))

        return stacks_to_deploy

    def _convert_params_to_boto3(self, params: list) -> list:
        """Convert CLI parameter format to boto3 format.

        From: ["Key1=Value1", "Key2=Value2"]
        To: [{"ParameterKey": "Key1", "ParameterValue": "Value1"}, ...]
        """
        result = []
        for param in params:
            if "=" in param:
                key, value = param.split("=", 1)
                result.append({"ParameterKey": key, "ParameterValue": value})
        return result

    def _ensure_regional_artifacts_bucket(self, profile, region: str, cf, console: Console) -> str | None:
        """Return a CFN-artifacts S3 bucket in ``region``, deploying s3bucket.yaml if needed.

        Lambda code must be staged in an S3 bucket in the same region as the
        function, and the metering stack deploys once per allowed Bedrock
        region — so each region needs its own artifacts bucket. Reuses the
        existing s3bucket stack in the primary region; elsewhere deploys the
        same template (bucket names are CloudFormation-generated, so there are
        no global name collisions). Returns None on failure.
        """
        project_root = Path(__file__).parents[4]
        s3_stack = profile.stack_names.get("s3", f"{profile.identity_pool_name}-s3bucket")

        outputs = get_stack_outputs(s3_stack, region)
        if outputs and outputs.get("CfnArtifactsBucket"):
            return outputs["CfnArtifactsBucket"]

        if region == profile.aws_region:
            # Primary region bucket is owned by the main deploy flow; don't
            # create it as a side effect here.
            console.print(f"[red]Could not get S3 bucket from s3bucket stack {s3_stack} in {region}[/red]")
            console.print("Run: [cyan]gip deploy s3bucket[/cyan]")
            return None

        console.print(f"[dim]Deploying artifacts bucket stack {s3_stack} in {region}...[/dim]")
        try:
            result = cf.deploy_stack(
                stack_name=s3_stack,
                template_path=project_root / "deployment" / "infrastructure" / "s3bucket.yaml",
                parameters=None,
                capabilities=["CAPABILITY_NAMED_IAM"],
                tags=profile.tags if profile.tags else None,
            )
        except Exception as e:
            console.print(f"[red]Failed to deploy artifacts bucket in {region}: {e}[/red]")
            return None
        if not result.success:
            console.print(f"[red]Failed to deploy artifacts bucket in {region}: {result.error}[/red]")
            return None
        outputs = get_stack_outputs(s3_stack, region)
        return (outputs or {}).get("CfnArtifactsBucket")

    def _deploy_stack(self, stack_type: str, profile, console: Console, cf_manager: CloudFormationManager) -> int:
        """Deploy a CloudFormation stack using boto3."""
        project_root = Path(__file__).parents[4]

        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console
        ) as progress:
            # Common deployment function
            def deploy_with_cf(
                template_path, stack_name, params, capabilities=None, task_description="Deploying stack...", cf=None
            ):
                """Helper function to deploy a stack with CloudFormation manager.

                ``cf`` overrides the shared region-bound manager (used for the
                CodeBuild stack when it deploys to a different region).
                """
                manager = cf or cf_manager
                task = progress.add_task(task_description, total=None)

                try:
                    # Convert parameters to boto3 format
                    boto3_params = self._convert_params_to_boto3(params) if params else None

                    # Deploy stack
                    # F-003: CloudFormation rejects inline templates over 51,200 bytes.
                    artifacts_bucket = None
                    if Path(template_path).stat().st_size > 51200:
                        s3_stack = profile.stack_names.get("s3", f"{profile.identity_pool_name}-s3bucket")
                        s3_outputs = get_stack_outputs(s3_stack, manager.region)
                        artifacts_bucket = (s3_outputs or {}).get("CfnArtifactsBucket")
                        if not artifacts_bucket:
                            console.print(f"[red]Could not get S3 bucket from s3bucket stack {s3_stack}[/red]")
                            console.print("Run: [cyan]gip deploy s3bucket[/cyan]")
                            return 1

                    result = manager.deploy_stack(
                        stack_name=stack_name,
                        template_path=template_path,
                        parameters=boto3_params,
                        capabilities=capabilities or ["CAPABILITY_NAMED_IAM"],
                        tags=profile.tags if profile.tags else None,
                        on_event=lambda e: progress.update(
                            task,
                            description=f"{e.get('LogicalResourceId', 'Stack')} - {e.get('ResourceStatus', '')}"
                            if isinstance(e, dict)
                            else str(e),
                        ),
                        artifacts_bucket=artifacts_bucket,
                    )

                    progress.update(task, completed=True)

                    if result.success:
                        console.print(f"[green]✓ {stack_type} stack deployed successfully[/green]")
                        return 0
                    else:
                        console.print(f"[red]✗ Failed to deploy {stack_type} stack: {result.error}[/red]")
                        return 1

                except ResourceConflictError as e:
                    progress.update(task, completed=True)
                    console.print(f"[yellow]Resource conflict: {e.message}[/yellow]")
                    if e.get_cleanup_command():
                        console.print(f"Run: [cyan]{e.get_cleanup_command()}[/cyan]")
                    return 1

                except StackRollbackError as e:
                    progress.update(task, completed=True)
                    console.print(f"[yellow]Stack rollback: {e.message}[/yellow]")
                    console.print(f"Recovery: {e.recovery_action}")
                    return 1

                except CloudFormationError as e:
                    progress.update(task, completed=True)
                    console.print(f"[red]CloudFormation error: {e.message}[/red]")
                    return 1

                except Exception as e:
                    progress.update(task, completed=True)
                    console.print(f"[red]Unexpected error: {str(e)}[/red]")
                    return 1

            # Deploy based on stack type
            if stack_type == "auth":
                # IAM Identity Center uses a dedicated template
                if profile.effective_auth_type == "idc":
                    template = project_root / "deployment" / "infrastructure" / "bedrock-auth-idc.yaml"
                    stack_name = profile.stack_names.get("auth", f"{profile.identity_pool_name}-stack")

                    from governed_inference_platform.models import get_all_bedrock_regions

                    bedrock_regions = profile.allowed_bedrock_regions
                    if not bedrock_regions:
                        bedrock_regions = [r for r in get_all_bedrock_regions() if "gov" not in r]

                    idc_permission_set = getattr(profile, "idc_permission_set_name", None) or "BedrockDeveloperAccess"
                    existing_role_name = cf_manager.get_stack_parameters(stack_name).get("FederatedRoleName")
                    federated_role_name = existing_role_name or "BedrockIDCFederatedRole"
                    params = [
                        f"FederatedRoleName={federated_role_name}",
                        f"AllowedPermissionSetName={idc_permission_set}",
                        f"IdentityPoolName={profile.identity_pool_name}",
                        f"AllowedBedrockRegions={','.join(bedrock_regions)}",
                        f"RestrictToAnthropicModels={str(getattr(profile, 'restrict_to_anthropic_models', True)).lower()}",
                        f"EnableMonitoring={str(profile.monitoring_enabled).lower()}",
                    ]
                    return deploy_with_cf(
                        template,
                        stack_name,
                        params,
                        ["CAPABILITY_NAMED_IAM"],
                        task_description="Deploying IAM Identity Center auth stack...",
                    )

                # Select template based on provider type (OIDC)
                provider_type = profile.provider_type or "okta"
                template_map = {
                    "okta": "bedrock-auth-okta.yaml",
                    "auth0": "bedrock-auth-auth0.yaml",
                    "azure": "bedrock-auth-azure.yaml",
                    "cognito": "bedrock-auth-cognito-pool.yaml",
                    "google": "bedrock-auth-google.yaml",
                    "generic": "bedrock-auth-generic.yaml",
                }

                template_file = template_map.get(provider_type, "bedrock-auth-okta.yaml")
                template = project_root / "deployment" / "infrastructure" / template_file

                # Verify template exists
                if not template.exists():
                    console.print(f"[red]Error: Template not found: {template_file}[/red]")
                    console.print(f"[yellow]Supported provider types: {', '.join(template_map.keys())}[/yellow]")
                    return 1

                stack_name = profile.stack_names.get("auth", f"{profile.identity_pool_name}-stack")

                # Build parameters
                params = []
                params.append(f"FederationType={profile.federation_type}")
                params.append(
                    f"RestrictToAnthropicModels={str(getattr(profile, 'restrict_to_anthropic_models', True)).lower()}"
                )

                # Opt-in sts:RoleSessionName trust-policy binding (ADR-0015).
                # Default "none" renders today's trust policy unchanged.
                session_binding = getattr(profile, "session_name_binding", "none") or "none"
                if provider_type == "auth0" and session_binding == "sub":
                    console.print(
                        "[red]SessionNameBinding=sub is not supported for Auth0: Auth0 'sub' "
                        "claims contain '|', which STS forbids in session names. "
                        "Set session_name_binding to 'email' or 'none' and re-deploy.[/red]"
                    )
                    return 1
                params.append(f"SessionNameBinding={session_binding}")

                if provider_type == "okta":
                    params.extend(
                        [
                            f"OktaDomain={profile.provider_domain}",
                            f"OktaClientId={profile.client_id}",
                        ]
                    )
                elif provider_type == "auth0":
                    params.extend(
                        [
                            f"Auth0Domain={profile.provider_domain}",
                            f"Auth0ClientId={profile.client_id}",
                        ]
                    )
                elif provider_type == "azure":
                    # Azure uses tenant ID (GUID) — extract from provider_domain URL
                    tenant_id = _extract_azure_tenant_id(profile.provider_domain)

                    params.extend(
                        [
                            f"AzureTenantId={tenant_id}",
                            f"AzureClientId={profile.client_id}",
                        ]
                    )
                elif provider_type == "cognito":
                    # Extract domain prefix from full domain
                    # e.g., "us-east-1example01" from "us-east-1example01.auth.us-east-1.amazoncognito.com"
                    cognito_domain = (
                        profile.provider_domain.split(".")[0]
                        if "." in profile.provider_domain
                        else profile.provider_domain
                    )
                    params.extend(
                        [
                            f"CognitoUserPoolId={profile.cognito_user_pool_id}",
                            f"CognitoUserPoolClientId={profile.client_id}",
                            f"CognitoUserPoolDomain={cognito_domain}",
                        ]
                    )
                elif provider_type == "google":
                    params.extend(
                        [
                            f"GoogleDomain={profile.provider_domain}",
                            f"GoogleClientId={profile.client_id}",
                        ]
                    )
                elif provider_type == "generic":
                    if not profile.oidc_issuer_url:
                        console.print(
                            "[red]Generic OIDC provider requires oidc_issuer_url."
                            " Re-run `gip init` to configure it.[/red]"
                        )
                        return 1
                    params.extend(
                        [
                            f"OidcIssuerUrl={profile.oidc_issuer_url}",
                            f"OidcClientId={profile.client_id}",
                        ]
                    )

                # OidcThumbprintList (every OIDC auth template, Default: ''). New stacks
                # pass nothing and IAM retrieves the CA thumbprint itself. A generic
                # profile that carries its own oidc_thumbprint (private-CA JWKS host, or
                # an older profile) keeps passing it. Otherwise, when the stack already
                # exists, re-send the thumbprints IAM currently holds: IAM rejects
                # removing ThumbprintList from an existing provider (verified live), so
                # omitting it would fail every upgrade of a stack created with thumbprints.
                if provider_type == "generic" and profile.oidc_thumbprint:
                    params.append(f"OidcThumbprintList={profile.oidc_thumbprint}")
                else:
                    try:
                        existing_thumbprints = _existing_oidc_provider_thumbprints(cf_manager, stack_name)
                    except OidcThumbprintLookupError as e:
                        console.print(f"[red]{e}[/red]")
                        return 1
                    if existing_thumbprints:
                        console.print(
                            f"[dim]Preserving {len(existing_thumbprints)} existing IAM OIDC provider "
                            "thumbprint(s) on update[/dim]"
                        )
                        params.append(f"OidcThumbprintList={','.join(existing_thumbprints)}")

                # Use profile regions, or fall back to all known Bedrock regions
                bedrock_regions = profile.allowed_bedrock_regions
                if not bedrock_regions:
                    from governed_inference_platform.models import get_all_bedrock_regions

                    bedrock_regions = [r for r in get_all_bedrock_regions() if "gov" not in r]

                params.extend(
                    [
                        f"IdentityPoolName={profile.identity_pool_name}",
                        f"AllowedBedrockRegions={','.join(bedrock_regions)}",
                        f"EnableMonitoring={str(profile.monitoring_enabled).lower()}",
                    ]
                )

                return deploy_with_cf(
                    template,
                    stack_name,
                    params,
                    ["CAPABILITY_NAMED_IAM"],
                    task_description="Deploying authentication stack...",
                )

            elif stack_type == "distribution":
                stack_name = profile.stack_names.get("distribution", f"{profile.identity_pool_name}-distribution")

                # Select template based on distribution type
                if profile.distribution_type == "landing-page":
                    template = project_root / "deployment" / "infrastructure" / "landing-page-distribution.yaml"

                    # Get VPC outputs from networking stack
                    networking_stack_name = profile.stack_names.get(
                        "networking", f"{profile.identity_pool_name}-networking"
                    )
                    networking_outputs = get_stack_outputs(networking_stack_name, profile.aws_region)

                    if not networking_outputs:
                        console.print(
                            "[red]Error: Networking stack outputs not found. Deploy networking stack first.[/red]"
                        )
                        return 1

                    vpc_id = networking_outputs.get("VpcId", "")
                    # Networking stack only has public subnets (SubnetIds), use for both ALB and Lambda
                    subnet_ids = networking_outputs.get("SubnetIds", "")

                    if not vpc_id or not subnet_ids:
                        console.print("[red]Error: Missing required VPC/subnet outputs from networking stack.[/red]")
                        console.print("[yellow]Expected: VpcId, SubnetIds[/yellow]")
                        console.print(f"[yellow]Got: {list(networking_outputs.keys())}[/yellow]")
                        return 1

                    # Use same subnets for both public (ALB) and private (Lambda)
                    public_subnets = subnet_ids
                    private_subnets = subnet_ids

                    # Build parameters for landing page
                    params = [
                        f"IdentityPoolName={profile.identity_pool_name}",
                        f"VpcId={vpc_id}",
                        f"PublicSubnetIds={public_subnets}",
                        f"PrivateSubnetIds={private_subnets}",
                        f"IdPProvider={profile.distribution_idp_provider}",
                    ]

                    # Add IdP-specific parameters
                    if profile.distribution_idp_provider == "okta":
                        params.extend(
                            [
                                f"OktaDomain={profile.distribution_idp_domain}",
                                f"OktaClientId={profile.distribution_idp_client_id}",
                                f"OktaClientSecretArn={profile.distribution_idp_client_secret_arn}",
                            ]
                        )
                    elif profile.distribution_idp_provider == "azure":
                        # Extract tenant ID from domain or use full domain
                        params.extend(
                            [
                                f"AzureTenantId={_extract_azure_tenant_id(profile.distribution_idp_domain or '')}",
                                f"AzureClientId={profile.distribution_idp_client_id}",
                                f"AzureClientSecretArn={profile.distribution_idp_client_secret_arn}",
                            ]
                        )
                    elif profile.distribution_idp_provider == "auth0":
                        params.extend(
                            [
                                f"Auth0Domain={profile.distribution_idp_domain}",
                                f"Auth0ClientId={profile.distribution_idp_client_id}",
                                f"Auth0ClientSecretArn={profile.distribution_idp_client_secret_arn}",
                            ]
                        )
                    elif profile.distribution_idp_provider == "cognito":
                        # Split domain to get user pool ID and domain prefix
                        params.extend(
                            [
                                f"CognitoUserPoolId={profile.cognito_user_pool_id or ''}",
                                f"CognitoUserPoolDomain={profile.distribution_idp_domain}",
                                f"CognitoClientId={profile.distribution_idp_client_id}",
                                f"CognitoClientSecretArn={profile.distribution_idp_client_secret_arn}",
                            ]
                        )
                    elif profile.distribution_idp_provider == "generic":
                        # Generic OIDC (PingFederate, Keycloak, etc.): endpoints can't be derived
                        # from a domain, so pass each explicitly. Client ID + secret reuse the
                        # shared distribution fields.
                        params.extend(
                            [
                                f"GenericIssuer={profile.distribution_idp_issuer or ''}",
                                f"GenericAuthorizationEndpoint={profile.distribution_idp_authorization_endpoint or ''}",
                                f"GenericTokenEndpoint={profile.distribution_idp_token_endpoint or ''}",
                                f"GenericUserInfoEndpoint={profile.distribution_idp_userinfo_endpoint or ''}",
                                f"GenericClientId={profile.distribution_idp_client_id}",
                                f"GenericClientSecretArn={profile.distribution_idp_client_secret_arn}",
                            ]
                        )

                    # Add optional custom domain parameters
                    if profile.distribution_custom_domain:
                        params.append(f"CustomDomainName={profile.distribution_custom_domain}")
                    if profile.distribution_hosted_zone_id:
                        params.append(f"HostedZoneId={profile.distribution_hosted_zone_id}")

                    # Add deployment timestamp to force custom resource re-execution
                    from datetime import datetime, timezone

                    deployment_timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
                    params.append(f"DeploymentTimestamp={deployment_timestamp}")

                    result = deploy_with_cf(
                        template,
                        stack_name,
                        params,
                        ["CAPABILITY_NAMED_IAM"],
                        task_description="Deploying landing page distribution stack...",
                    )

                    # Display outputs for landing page
                    if result == 0:
                        outputs = get_stack_outputs(stack_name, profile.aws_region)
                        console.print("\n[bold green]✓ Landing page deployed successfully![/bold green]")
                        console.print(f"\n[bold]Distribution URL:[/bold] {outputs.get('DistributionURL', 'N/A')}")
                        console.print("\n[bold yellow]⚠️  Configure your IdP web application:[/bold yellow]")
                        console.print(f"   [cyan]Redirect URI:[/cyan] {outputs.get('IdPRedirectURI', 'N/A')}")
                        console.print(
                            "\n   Add this redirect URI to your IdP web application settings "
                            "before users can authenticate."
                        )

                    return result

                else:  # presigned-s3 or legacy
                    template = project_root / "deployment" / "infrastructure" / "presigned-s3-distribution.yaml"
                    presign_principal = getattr(profile, "distribution_presign_principal_type", None) or "iam-user"
                    params = [
                        f"IdentityPoolName={profile.identity_pool_name}",
                        f"PresignPrincipalType={presign_principal}",
                    ]
                    return deploy_with_cf(
                        template,
                        stack_name,
                        params,
                        ["CAPABILITY_NAMED_IAM"],
                        task_description="Deploying presigned S3 distribution stack...",
                    )

            elif stack_type == "networking":
                template = project_root / "deployment" / "infrastructure" / "networking.yaml"
                stack_name = profile.stack_names.get("networking", f"{profile.identity_pool_name}-networking")
                vpc_config = profile.monitoring_config or {}

                params = [
                    f"VpcCidr={vpc_config.get('vpc_cidr', '10.0.0.0/16')}",
                    f"PublicSubnet1Cidr={vpc_config.get('subnet1_cidr', '10.0.1.0/24')}",
                    f"PublicSubnet2Cidr={vpc_config.get('subnet2_cidr', '10.0.2.0/24')}",
                ]
                return deploy_with_cf(
                    template, stack_name, params, task_description="Deploying networking infrastructure..."
                )

            elif stack_type == "s3bucket":
                template = project_root / "deployment" / "infrastructure" / "s3bucket.yaml"
                stack_name = profile.stack_names.get("s3", f"{profile.identity_pool_name}-s3bucket")
                params = []
                return deploy_with_cf(template, stack_name, params, task_description="Deploying S3 Bucket...")
            elif stack_type == "monitoring":
                # Ensure ECS service linked role exists before deploying
                self._ensure_ecs_service_linked_role(console)

                template = project_root / "deployment" / "infrastructure" / "otel-collector.yaml"
                stack_name = profile.stack_names.get("monitoring", f"{profile.identity_pool_name}-otel-collector")
                params = []
                vpc_config = profile.monitoring_config or {}

                if not vpc_config.get("create_vpc", True):
                    params.append(f"VpcId={vpc_config.get('vpc_id', '')}")
                    subnet_ids = ",".join(vpc_config.get("subnet_ids", []))
                    params.append(f"SubnetIds={subnet_ids}")
                else:
                    # Get VPC outputs from networking stack
                    networking_stack_name = profile.stack_names.get(
                        "networking", f"{profile.identity_pool_name}-networking"
                    )
                    networking_outputs = get_stack_outputs(networking_stack_name, profile.aws_region)

                    if networking_outputs:
                        vpc_id = networking_outputs.get("VpcId", "")
                        subnet_ids = networking_outputs.get("SubnetIds", "")
                        if vpc_id:
                            params.append(f"VpcId={vpc_id}")
                        if subnet_ids:
                            params.append(f"SubnetIds={subnet_ids}")

                # Add HTTPS domain parameters if configured
                monitoring_config = getattr(profile, "monitoring_config", {})
                has_monitoring_https = False
                has_monitoring_jwt_auth = False
                if monitoring_config.get("custom_domain"):
                    domain = (
                        monitoring_config["custom_domain"].replace("https://", "").replace("http://", "").rstrip("/")
                    )
                    params.append(f"CustomDomainName={domain}")
                    if monitoring_config.get("hosted_zone_id"):
                        params.append(f"HostedZoneId={monitoring_config['hosted_zone_id']}")
                    if monitoring_config.get("certificate_arn"):
                        params.append(f"CertificateArn={monitoring_config['certificate_arn']}")
                    has_monitoring_https = bool(
                        monitoring_config.get("hosted_zone_id") or monitoring_config.get("certificate_arn")
                    )
                    # Add OIDC JWT validation parameters for ALB (all IdP types)
                    provider_type = profile.provider_type or ""
                    provider_domain = profile.provider_domain
                    if provider_type:
                        oidc_issuer = ""
                        oidc_jwks = ""
                        if provider_type == "azure":
                            uuid_pat = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
                            tenant_match = re.search(uuid_pat, provider_domain or "")
                            if tenant_match:
                                tid = tenant_match.group(0)
                                oidc_issuer = f"https://login.microsoftonline.com/{tid}/v2.0"
                                oidc_jwks = f"https://login.microsoftonline.com/{tid}/discovery/v2.0/keys"
                        elif provider_type == "okta" and provider_domain:
                            # provider_domain is e.g. "company.okta.com"
                            domain = provider_domain.rstrip("/")
                            oidc_issuer = f"https://{domain}/oauth2/default"
                            oidc_jwks = f"https://{domain}/oauth2/default/v1/keys"
                        elif provider_type == "auth0" and provider_domain:
                            domain = provider_domain.rstrip("/")
                            oidc_issuer = f"https://{domain}/"
                            oidc_jwks = f"https://{domain}/.well-known/jwks.json"
                        elif provider_type == "google":
                            oidc_issuer = "https://accounts.google.com"
                            oidc_jwks = "https://www.googleapis.com/oauth2/v3/certs"
                        elif provider_type == "cognito":
                            # Cognito issuer uses cognito-idp endpoint, not the hosted UI domain
                            pool_id = getattr(profile, "cognito_user_pool_id", "")
                            if pool_id:
                                # Extract region from pool ID (format: us-east-1_AbCdEfGhI)
                                pool_region = pool_id.split("_")[0] if "_" in pool_id else profile.aws_region
                                oidc_issuer = f"https://cognito-idp.{pool_region}.amazonaws.com/{pool_id}"
                                oidc_jwks = (
                                    f"https://cognito-idp.{pool_region}.amazonaws.com/{pool_id}/.well-known/jwks.json"
                                )
                        elif provider_type == "generic":
                            oidc_issuer = (getattr(profile, "oidc_issuer_url", "") or "").rstrip("/")
                            oidc_jwks = getattr(profile, "oidc_jwks_uri", "") or ""
                        if oidc_issuer and oidc_jwks:
                            params.append(f"OidcIssuerUrl={oidc_issuer}")
                            params.append(f"OidcJwksEndpoint={oidc_jwks}")
                            params.append(f"OidcClientId={profile.client_id}")
                            has_monitoring_jwt_auth = True

                # Pass CoWork service token for ALB auth bypass (if configured)
                cowork_token = getattr(profile, "cowork_service_token", "") or ""
                if cowork_token:
                    params.append(f"CoWorkServiceToken={cowork_token}")

                # Pass analytics flag to control dual-export (OTLP + EMF)
                analytics_enabled = "true" if getattr(profile, "analytics_enabled", True) else "false"
                params.append(f"EnableAnalytics={analytics_enabled}")

                # Pass ALB scheme. Internet-facing telemetry must have HTTPS plus
                # an auth path; otherwise any network caller can inject metadata.
                alb_scheme = monitoring_config.get("alb_scheme", "internal")
                allow_insecure_http = monitoring_config.get("allow_insecure_http_ingress") is True
                if not has_monitoring_https:
                    if monitoring_config.get("custom_domain"):
                        console.print(
                            "[red]Monitoring TLS configuration is incomplete: custom_domain requires "
                            "hosted_zone_id or certificate_arn.[/red]"
                        )
                        return 1
                    if not allow_insecure_http:
                        console.print(
                            "[red]Central monitoring requires HTTPS. Configure monitoring_config.custom_domain "
                            "with hosted_zone_id or certificate_arn. For internal local development only, set "
                            "monitoring_config.allow_insecure_http_ingress=true.[/red]"
                        )
                        return 1
                    if alb_scheme != "internal":
                        console.print(
                            "[red]Insecure HTTP collector ingress is forbidden for internet-facing load balancers.[/red]"
                        )
                        return 1
                    params.append("AllowInsecureHttpIngress=true")
                    console.print(
                        "[bold red]DANGER: deploying plaintext collector ingress for internal local development only. "
                        "Bearer tokens and telemetry will not be encrypted.[/bold red]"
                    )
                if alb_scheme == "internet-facing":
                    if not monitoring_config.get("custom_domain") or not has_monitoring_https:
                        console.print(
                            "[red]Internet-facing monitoring requires monitoring_config.custom_domain plus "
                            "hosted_zone_id or certificate_arn so HTTPS is enabled.[/red]"
                        )
                        return 1
                    if not has_monitoring_jwt_auth:
                        console.print(
                            "[red]Internet-facing monitoring requires a supported OIDC provider with resolved "
                            "issuer, JWKS endpoint, and client ID for ALB JWT validation.[/red]"
                        )
                        return 1
                params.append(f"ALBScheme={alb_scheme}")

                console.print(f"[dim]Using parameters: {params}[/dim]")
                result = deploy_with_cf(
                    template, stack_name, params, task_description="Deploying monitoring collector..."
                )

                # Force ECS service redeploy so the collector picks up the new config.
                # The collector config is stored in SSM and resolved at container start;
                # a CFN update alone won't restart the running task.
                if result == 0:
                    try:
                        import boto3

                        ecs_client = boto3.client("ecs", region_name=profile.aws_region)
                        cluster = "gip-otel-cluster"
                        services = ecs_client.list_services(cluster=cluster)["serviceArns"]
                        if services:
                            ecs_client.update_service(
                                cluster=cluster,
                                service=services[0],
                                forceNewDeployment=True,
                            )
                            console.print("[dim]Forced ECS service redeploy to load new collector config[/dim]")
                        else:
                            console.print(
                                "[dim]No ECS service found in cluster (first deploy — service starting)[/dim]"
                            )
                    except Exception as e:
                        # Non-fatal: stack deployed fine, just couldn't force redeploy
                        console.print(
                            f"[yellow]⚠ Stack deployed but could not force ECS redeploy: {e}[/yellow]\n"
                            "[dim]  Run: aws ecs list-services --cluster gip-otel-cluster "
                            "to find the service name, then force redeploy[/dim]"
                        )

                # Save OTel collector endpoint to profile immediately after deploy
                if result == 0:
                    monitoring_outputs = get_stack_outputs(stack_name, profile.aws_region)
                    if monitoring_outputs:
                        endpoint = monitoring_outputs.get("CollectorEndpoint")
                        verified_endpoint = monitoring_outputs.get("VerifiedCollectorEndpoint")
                        if endpoint and endpoint != "N/A":
                            profile.otel_collector_endpoint = endpoint
                            profile.otel_verified_collector_endpoint = (
                                verified_endpoint if verified_endpoint == endpoint else None
                            )
                            try:
                                Config.load().save_profile(profile)
                                console.print(f"[dim]Saved OTel endpoint to profile: {endpoint}[/dim]")
                            except Exception as e:
                                console.print(
                                    f"[yellow]⚠ Stack deployed but could not save OTel endpoint to profile: {e}[/yellow]\n"
                                    f"[dim]  Set otel_collector_endpoint manually to: {endpoint}[/dim]"
                                )

                return result

            elif stack_type == "dashboard":
                template = project_root / "deployment" / "infrastructure" / "claude-code-dashboard.yaml"
                stack_name = profile.stack_names.get("dashboard", f"{profile.identity_pool_name}-dashboard")
                params = [f"MetricsRegion={profile.aws_region}"]
                return deploy_with_cf(
                    template, stack_name, params, task_description="Deploying monitoring dashboard..."
                )

            elif stack_type == "cowork-dashboard":
                template = project_root / "deployment" / "infrastructure" / "cowork-dashboard.yaml"
                stack_name = profile.stack_names.get(
                    "cowork-dashboard", f"{profile.identity_pool_name}-cowork-dashboard"
                )
                params = [
                    f"MetricsRegion={profile.aws_region}",
                ]
                return deploy_with_cf(template, stack_name, params, task_description="Deploying CoWork dashboard...")

            elif stack_type == "analytics":
                template = project_root / "deployment" / "infrastructure" / "analytics-pipeline.yaml"
                stack_name = profile.stack_names.get("analytics", f"{profile.identity_pool_name}-analytics")
                params = [
                    f"MetricsLogGroup={profile.metrics_log_group}",
                    f"DataRetentionDays={profile.data_retention_days}",
                    f"FirehoseBufferInterval={profile.firehose_buffer_interval}",
                    f"DebugMode={str(profile.analytics_debug_mode).lower()}",
                ]
                return deploy_with_cf(template, stack_name, params, task_description="Deploying analytics pipeline...")

            elif stack_type == "quota":
                template = project_root / "deployment" / "infrastructure" / "quota-monitoring.yaml"
                stack_name = profile.stack_names.get("quota", f"{profile.identity_pool_name}-quota")

                # Get S3 bucket from s3bucket stack for packaging
                s3_stack = profile.stack_names.get("s3", f"{profile.identity_pool_name}-s3bucket")
                s3_outputs = get_stack_outputs(s3_stack, profile.aws_region)

                if not s3_outputs or not s3_outputs.get("CfnArtifactsBucket"):
                    console.print(f"[red]Could not get S3 bucket from s3bucket stack {s3_stack}[/red]")
                    console.print("[yellow]The s3bucket stack must be deployed first.[/yellow]")
                    console.print("Run: [cyan]gip deploy s3bucket[/cyan]")
                    return 1

                s3_bucket = s3_outputs["CfnArtifactsBucket"]

                # Build parameters
                monthly_limit = _quota_token_limit(profile)
                daily_limit = getattr(profile, "daily_token_limit", None)
                daily_enforcement = getattr(profile, "daily_enforcement_mode", "alert")
                monthly_enforcement = getattr(profile, "monthly_enforcement_mode", "block")
                warning_80 = getattr(profile, "warning_threshold_80", int(monthly_limit * 0.8))
                warning_90 = getattr(profile, "warning_threshold_90", int(monthly_limit * 0.9))

                # Get OIDC configuration for JWT authentication (only when SSO is enabled)
                oidc_issuer_url, oidc_client_id = self._resolve_oidc_config(profile)

                # Pass explicitly so the profile is the source of truth; the CF template
                # default is 'false' to match the opt-in intent of this field.
                enable_finegrained_quotas = profile.enable_finegrained_quotas

                # Sidecar bypass detection: opt-in detective control (default off).
                enable_bypass_detection = getattr(profile, "enable_bypass_detection", False)

                params = [
                    f"MonthlyTokenLimit={monthly_limit}",
                    f"WarningThreshold80={warning_80}",
                    f"WarningThreshold90={warning_90}",
                    f"DailyTokenLimit={daily_limit or 0}",
                    f"DailyEnforcementMode={daily_enforcement}",
                    f"MonthlyEnforcementMode={monthly_enforcement}",
                    f"QuotaMode={getattr(profile, 'quota_limit_type', 'token')}",
                    f"MonthlyCostLimitUsd={getattr(profile, 'monthly_cost_limit', 0)}",
                    f"DailyCostLimitUsd={getattr(profile, 'daily_cost_limit', 0)}",
                    f"OidcIssuerUrl={oidc_issuer_url}",
                    f"OidcClientId={oidc_client_id}",
                    f"EnableFinegrainedQuotas={str(enable_finegrained_quotas).lower()}",
                    f"EnableBypassDetection={str(enable_bypass_detection).lower()}",
                    f"MeteringMode={getattr(profile, 'metering_mode', 'shadow') if getattr(profile, 'metering_enabled', False) else 'shadow'}",
                ]

                # Package the template using AWS CLI
                task = progress.add_task("Packaging quota monitoring Lambda functions...", total=None)

                packaged_template_path: str | None = None
                try:
                    # Create temp file for packaged template
                    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
                        packaged_template_path = f.name

                    # Run AWS CLI package command
                    cmd = [
                        "aws",
                        "cloudformation",
                        "package",
                        "--template-file",
                        str(template),
                        "--s3-bucket",
                        s3_bucket,
                        "--s3-prefix",
                        "gip/quota",
                        "--output-template-file",
                        packaged_template_path,
                        "--region",
                        profile.aws_region,
                    ]

                    result_pkg = run_checked(cmd, capture_output=True, text=True)  # nosec B603 — fixed argv, no shell

                    if result_pkg.returncode != 0:
                        console.print(f"[red]Failed to package template: {result_pkg.stderr}[/red]")
                        return 1

                    progress.update(
                        task, description="Quota monitoring Lambda functions packaged successfully", completed=True
                    )

                    # Deploy the packaged template
                    result = deploy_with_cf(
                        packaged_template_path, stack_name, params, task_description="Deploying quota monitoring..."
                    )

                    # Seed default quota policy on successful deploy
                    if result == 0:
                        self._create_default_quota_policy(profile, stack_name, console)

                        # If using IAM auth (IDC/non-OIDC), remind about execute-api:Invoke permission
                        if profile.effective_auth_type == "idc":
                            quota_outputs = get_stack_outputs(stack_name, profile.aws_region)
                            policy_arn = (quota_outputs or {}).get("QuotaApiInvokePolicyArn", "")
                            if policy_arn:
                                console.print(
                                    f"\n[yellow]\u26a0 IAM Auth Mode: Attach this policy to your IDC permission set "
                                    f"(or the IAM role used by Claude Code users):[/yellow]\n"
                                    f"[bold]{policy_arn}[/bold]\n"
                                    f"[dim]This grants execute-api:Invoke on the quota API. "
                                    f"Without it, quota checks will return 403.[/dim]"
                                )

                    return result

                finally:
                    # Clean up temp file
                    if packaged_template_path is not None:
                        try:
                            os.unlink(packaged_template_path)
                        except Exception:
                            pass  # nosec B110

            elif stack_type == "metering":
                template = project_root / "deployment" / "infrastructure" / "quota-metering.yaml"
                stack_name = profile.stack_names.get("metering", f"{profile.identity_pool_name}-metering")

                # Metering accrues into the quota stack's UserQuotaMetrics table
                # and reuses its alert topic — skip with a warning when the quota
                # stack isn't deployed yet.
                quota_stack = profile.stack_names.get("quota", f"{profile.identity_pool_name}-quota")
                quota_outputs = get_stack_outputs(quota_stack, profile.aws_region)
                if not quota_outputs or not quota_outputs.get("QuotaTableName"):
                    console.print(
                        f"[yellow]⚠ Skipping server-side metering: quota stack '{quota_stack}' "
                        f"is not deployed in {profile.aws_region}.[/yellow]"
                    )
                    console.print("Run: [cyan]gip deploy quota[/cyan] first, then retry 'gip deploy metering'.")
                    return 1

                quota_table_name = quota_outputs["QuotaTableName"]
                alert_topic_arn = quota_outputs.get("QuotaAlertTopicArn", "")
                metering_mode = getattr(profile, "metering_mode", "shadow")

                # One stack per Bedrock source region: with CRIS, invocation logs
                # stay in the source region, and users can only invoke in
                # AllowedBedrockRegions (the auth stack's aws:RequestedRegion
                # condition), so those are exactly the regions to instrument.
                regions = list(getattr(profile, "allowed_bedrock_regions", []) or [])
                if not regions:
                    console.print(
                        "[yellow]⚠ Profile has no allowed_bedrock_regions — metering only the "
                        f"primary region ({profile.aws_region}). Narrow AllowedBedrockRegions "
                        "to get full coverage.[/yellow]"
                    )
                    regions = [profile.aws_region]

                failed_regions = []
                for region in regions:
                    region_cf = cf_manager if region == profile.aws_region else CloudFormationManager(region=region)

                    s3_bucket = self._ensure_regional_artifacts_bucket(profile, region, region_cf, console)
                    if not s3_bucket:
                        failed_regions.append(region)
                        continue

                    # Package the Lambda code into the regional bucket (Lambda
                    # code must live in an S3 bucket in the function's region).
                    task = progress.add_task(f"Packaging metering Lambdas for {region}...", total=None)
                    packaged_template_path = None
                    try:
                        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
                            packaged_template_path = f.name
                        cmd = [
                            "aws",
                            "cloudformation",
                            "package",
                            "--template-file",
                            str(template),
                            "--s3-bucket",
                            s3_bucket,
                            "--s3-prefix",
                            "gip/metering",
                            "--output-template-file",
                            packaged_template_path,
                            "--region",
                            region,
                        ]
                        result_pkg = run_checked(cmd, capture_output=True, text=True)  # nosec B603 — fixed argv, no shell
                        if result_pkg.returncode != 0:
                            progress.update(task, completed=True)
                            console.print(
                                f"[red]Failed to package metering template ({region}): {result_pkg.stderr}[/red]"
                            )
                            failed_regions.append(region)
                            continue
                        progress.update(task, description=f"Metering Lambdas packaged for {region}", completed=True)

                        params = [
                            f"QuotaTableName={quota_table_name}",
                            f"QuotaTableRegion={profile.aws_region}",
                            f"MeteringMode={metering_mode}",
                        ]
                        # CloudWatch alarm actions can't target cross-region SNS
                        # topics — only wire the quota alert topic in its region.
                        if region == profile.aws_region and alert_topic_arn:
                            params.append(f"AlarmTopicArn={alert_topic_arn}")

                        result = deploy_with_cf(
                            packaged_template_path,
                            stack_name,
                            params,
                            ["CAPABILITY_NAMED_IAM"],
                            task_description=f"Deploying metering stack in {region} ({metering_mode} mode)...",
                            cf=region_cf,
                        )
                        if result != 0:
                            failed_regions.append(region)
                    finally:
                        if packaged_template_path:
                            try:
                                os.unlink(packaged_template_path)
                            except Exception:
                                pass  # nosec B110

                if failed_regions:
                    console.print(f"[red]Metering deploy failed in region(s): {', '.join(failed_regions)}[/red]")
                    return 1
                console.print(
                    f"[green]✓ Server-side metering deployed to {len(regions)} region(s) "
                    f"in '{metering_mode}' mode[/green]"
                )
                return 0

            elif stack_type == "guardrails":
                ok, msg = guardrails_preflight(profile)
                if not ok:
                    console.print(f"[red]{msg}[/red]")
                    return 1

                template = project_root / "deployment" / "infrastructure" / "guardrails-enforcement.yaml"
                stack_name = profile.stack_names.get("guardrails", f"{profile.identity_pool_name}-guardrails")
                params = build_guardrails_params(profile)
                regions = guardrails_regions(profile)
                failed_regions = []
                for guardrails_region in regions:
                    region_cf = (
                        cf_manager
                        if guardrails_region == profile.aws_region
                        else CloudFormationManager(region=guardrails_region)
                    )
                    result = deploy_with_cf(
                        template,
                        stack_name,
                        params,
                        ["CAPABILITY_IAM"],
                        task_description=f"Deploying Bedrock Guardrails enforcement in {guardrails_region}...",
                        cf=region_cf,
                    )
                    if result != 0:
                        failed_regions.append(guardrails_region)

                if failed_regions:
                    console.print(f"[red]Guardrails deploy failed in region(s): {', '.join(failed_regions)}[/red]")
                    return 1
                console.print(f"[green]✓ Bedrock Guardrails enforcement deployed to {len(regions)} region(s)[/green]")
                return 0

            elif stack_type == "model-lifecycle":
                template = project_root / "deployment" / "infrastructure" / "model-lifecycle.yaml"
                stack_name = profile.stack_names.get("model-lifecycle", f"{profile.identity_pool_name}-model-lifecycle")

                # Lambda code needs a same-region artifacts bucket (like quota).
                s3_stack = profile.stack_names.get("s3", f"{profile.identity_pool_name}-s3bucket")
                s3_outputs = get_stack_outputs(s3_stack, profile.aws_region)
                if not s3_outputs or not s3_outputs.get("CfnArtifactsBucket"):
                    console.print(f"[red]Could not get S3 bucket from s3bucket stack {s3_stack}[/red]")
                    console.print("[yellow]The s3bucket stack must be deployed first.[/yellow]")
                    console.print("Run: [cyan]gip deploy s3bucket[/cyan]")
                    return 1
                s3_bucket = s3_outputs["CfnArtifactsBucket"]

                # Reuse the quota stack's alert topic when it exists (R14 §2.3);
                # otherwise the template creates a dedicated topic. The reused
                # topic is KMS-encrypted, so its key ARN is passed too — the
                # check Lambda needs kms:GenerateDataKey/kms:Decrypt on it to
                # publish (AlertTopicKmsKeyArn; absent when the quota stack
                # itself reuses an external, unencrypted topic).
                alert_topic_arn = ""
                alert_topic_kms_key_arn = ""
                if getattr(profile, "quota_monitoring_enabled", False):
                    quota_stack = profile.stack_names.get("quota", f"{profile.identity_pool_name}-quota")
                    quota_outputs = get_stack_outputs(quota_stack, profile.aws_region)
                    alert_topic_arn = (quota_outputs or {}).get("QuotaAlertTopicArn", "")
                    alert_topic_kms_key_arn = (quota_outputs or {}).get("QuotaAlertTopicKmsKeyArn", "")

                params = [
                    f"ProfileName={profile.name}",
                    f"CheckRegions={','.join(lifecycle_check_regions(profile))}",
                ]
                if alert_topic_arn:
                    params.append(f"AlertTopicArn={alert_topic_arn}")
                    if alert_topic_kms_key_arn:
                        params.append(f"AlertTopicKmsKeyArn={alert_topic_kms_key_arn}")

                task = progress.add_task("Packaging model lifecycle Lambda...", total=None)
                packaged_template_path = None
                try:
                    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
                        packaged_template_path = f.name
                    cmd = [
                        "aws",
                        "cloudformation",
                        "package",
                        "--template-file",
                        str(template),
                        "--s3-bucket",
                        s3_bucket,
                        "--s3-prefix",
                        "gip/model-lifecycle",
                        "--output-template-file",
                        packaged_template_path,
                        "--region",
                        profile.aws_region,
                    ]
                    result_pkg = run_checked(cmd, capture_output=True, text=True)  # nosec B603 — fixed argv, no shell
                    if result_pkg.returncode != 0:
                        console.print(f"[red]Failed to package template: {result_pkg.stderr}[/red]")
                        return 1
                    progress.update(task, description="Model lifecycle Lambda packaged", completed=True)

                    result = deploy_with_cf(
                        packaged_template_path,
                        stack_name,
                        params,
                        task_description="Deploying model lifecycle stack...",
                    )
                    if result != 0:
                        return result

                    # Seed the tracked-models SSM parameter from the profile
                    # (out-of-band put so a later rotation is a put-parameter,
                    # not a stack update — R14 §2.3).
                    import json as json_module

                    import boto3

                    tracked = compute_tracked_models(profile)
                    try:
                        ssm = boto3.client("ssm", region_name=profile.aws_region)
                        ssm.put_parameter(
                            Name=f"/gip/{profile.name}/tracked-models",
                            Value=json_module.dumps(tracked),
                            Type="String",
                            Overwrite=True,
                        )
                        console.print(
                            f"[green]✓ Tracked models seeded ({len(tracked)}):[/green] "
                            + ", ".join(tracked[:4])
                            + (" …" if len(tracked) > 4 else "")
                        )
                    except Exception as e:
                        # A5: the stack is up but unseeded — the Lambda will log
                        # a warning daily until this is fixed. Surface it loudly.
                        console.print(
                            f"[red]✗ Could not seed tracked-models SSM parameter: {e}[/red]\n"
                            "[yellow]Re-run 'gip deploy model-lifecycle' — the lifecycle check "
                            "is a no-op until the parameter is seeded.[/yellow]"
                        )
                        return 1

                    outputs = get_stack_outputs(stack_name, profile.aws_region)
                    topic = (outputs or {}).get("LifecycleAlertTopicArn", "")
                    if topic:
                        console.print(
                            f"[dim]Subscribe to lifecycle alerts: aws sns subscribe --topic-arn {topic} "
                            "--protocol email --notification-endpoint you@example.com[/dim]"
                        )
                    return 0
                finally:
                    if packaged_template_path:
                        try:
                            os.unlink(packaged_template_path)
                        except Exception:
                            pass  # nosec B110

            elif stack_type == "codebuild":
                # CodeBuild region is chosen in `gip init` (the Windows container
                # fleet only exists in some regions). Deploy just executes that
                # choice. If a legacy profile still resolves to an unsupported
                # region — e.g. it predates the init region picker — skip with a
                # pointer to init rather than failing the whole deploy.
                codebuild_region = get_codebuild_region(profile)
                if codebuild_region not in CODEBUILD_WINDOWS_REGIONS:
                    nearest = find_nearest_codebuild_region(profile.aws_region)
                    console.print(
                        f"[yellow]⚠ Skipping CodeBuild: Windows containers aren't available in "
                        f"{codebuild_region}.[/yellow]"
                    )
                    console.print(
                        f"[dim]Re-run [cyan]gip init[/cyan] to pick a supported CodeBuild region "
                        f"(nearest: {nearest}), then deploy again.[/dim]"
                    )
                    return 0

                # Deploy to the (possibly cross-region) CodeBuild region. Build a
                # dedicated manager when it differs from the main region.
                cf = (
                    cf_manager
                    if codebuild_region == profile.aws_region
                    else CloudFormationManager(region=codebuild_region)
                )
                template = project_root / "deployment" / "infrastructure" / "codebuild-windows.yaml"
                stack_name = profile.stack_names.get("codebuild", f"{profile.identity_pool_name}-codebuild")
                params = [f"ProjectNamePrefix={profile.identity_pool_name}"]
                return deploy_with_cf(
                    template,
                    stack_name,
                    params,
                    task_description=f"Deploying CodeBuild for Windows builds in {codebuild_region}...",
                    cf=cf,
                )

            elif stack_type == "bootstrap":
                console.print(f"[red]{upstream_gateway_guidance('bootstrap')}[/red]")
                return 1

            elif stack_type == "skills":
                stack_name = profile.stack_names.get("skills", f"{profile.identity_pool_name}-skills")

                # Lambda code (registry provisioner + distributor) must be
                # packaged into a regional S3 bucket — same flow as quota.
                s3_stack = profile.stack_names.get("s3", f"{profile.identity_pool_name}-s3bucket")
                s3_outputs = get_stack_outputs(s3_stack, profile.aws_region)
                if not s3_outputs or not s3_outputs.get("CfnArtifactsBucket"):
                    console.print(f"[red]Could not get S3 bucket from s3bucket stack {s3_stack}[/red]")
                    console.print("[yellow]The s3bucket stack must be deployed first.[/yellow]")
                    console.print("Run: [cyan]gip deploy s3bucket[/cyan]")
                    return 1
                s3_bucket = s3_outputs["CfnArtifactsBucket"]

                params = build_skills_params(profile)

                task = progress.add_task("Packaging skills registry Lambda functions...", total=None)
                packaged_template_path = None
                try:
                    with tempfile.TemporaryDirectory() as build_dir:
                        template = _stage_skills_registry_template(project_root, Path(build_dir))
                        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
                            packaged_template_path = f.name
                        cmd = [
                            "aws",
                            "cloudformation",
                            "package",
                            "--template-file",
                            str(template),
                            "--s3-bucket",
                            s3_bucket,
                            "--s3-prefix",
                            "gip/skills",
                            "--output-template-file",
                            packaged_template_path,
                            "--region",
                            profile.aws_region,
                        ]
                        result_pkg = run_checked(cmd, capture_output=True, text=True)  # nosec B603 — fixed argv, no shell
                        if result_pkg.returncode != 0:
                            console.print(f"[red]Failed to package template: {result_pkg.stderr}[/red]")
                            return 1
                    progress.update(task, description="Skills registry Lambdas packaged", completed=True)

                    result = deploy_with_cf(
                        packaged_template_path,
                        stack_name,
                        params,
                        ["CAPABILITY_NAMED_IAM"],
                        task_description="Deploying skills registry...",
                    )

                    if result == 0:
                        outputs = get_stack_outputs(stack_name, profile.aws_region) or {}
                        profile.skills_registry_id = outputs.get("RegistryId", "")
                        profile.skills_artifact_bucket = outputs.get("ArtifactBucket", "")
                        profile.skills_distributor_function = outputs.get("DistributorFunctionArn", "")
                        config = Config.load()
                        config.save_profile(profile)
                        console.print("\n[bold green]✓ Skills registry deployed![/bold green]")
                        console.print(f"[bold]Registry ID:[/bold] {profile.skills_registry_id}")
                        console.print(f"[bold]Artifact bucket:[/bold] {profile.skills_artifact_bucket}")
                        console.print(
                            "[dim]Publish a skill: gip skills publish <dir> — see assets/docs/SKILLS_REGISTRY.md[/dim]"
                        )
                    return result
                finally:
                    if packaged_template_path:
                        try:
                            os.unlink(packaged_template_path)
                        except Exception:
                            pass  # nosec B110

            elif stack_type == "websearch":
                ok, msg = websearch_preflight(profile)
                if not ok:
                    console.print(f"[red]{msg}[/red]")
                    return 1
                ws_region = get_websearch_region(profile)
                cf = cf_manager if ws_region == profile.aws_region else CloudFormationManager(region=ws_region)
                template = project_root / "deployment" / "infrastructure" / "bedrock-agentcore-gateway.yaml"
                stack_name = profile.stack_names.get("websearch", f"{profile.identity_pool_name}-websearch")
                params = build_websearch_params(profile)
                result = deploy_with_cf(
                    template,
                    stack_name,
                    params,
                    task_description=f"Deploying AgentCore web search gateway in {ws_region}...",
                    cf=cf,
                )
                if result == 0:
                    outputs = cf.get_stack_outputs(stack_name)
                    gateway_url = outputs.get("GatewayMcpEndpoint")
                    if gateway_url:
                        profile.websearch_gateway_url = gateway_url
                        config = Config.load()
                        config.save_profile(profile)
                        console.print(f"[green]✓ Gateway URL saved: {gateway_url}[/green]")
                    outputs = cf.get_stack_outputs(stack_name)
                    gateway_id = outputs.get("GatewayId")
                    if gateway_id:
                        ready = _poll_websearch_target_ready(gateway_id, ws_region, console, session=cf.session)
                        if not ready:
                            console.print(
                                "[yellow]⚠ Connector target not yet READY. "
                                "Re-run gip deploy websearch to re-check, or "
                                "gip destroy websearch to tear down.[/yellow]"
                            )
                return result

            elif stack_type == "memory":
                ok, msg = memory_preflight(profile)
                if not ok:
                    console.print(f"[red]{msg}[/red]")
                    return 1
                # The memory target attaches to the websearch gateway, so it
                # deploys into the websearch region and needs that stack's
                # GatewayId + GatewayExecutionRoleArn outputs (R9 F17 —
                # plain-string GatewayIdentifier, websearch template untouched).
                ws_region = get_websearch_region(profile)
                cf = cf_manager if ws_region == profile.aws_region else CloudFormationManager(region=ws_region)
                ws_stack = profile.stack_names.get("websearch", f"{profile.identity_pool_name}-websearch")
                ws_outputs = cf.get_stack_outputs(ws_stack) or {}
                gateway_id = ws_outputs.get("GatewayId")
                gateway_role_arn = ws_outputs.get("GatewayExecutionRoleArn")
                if not gateway_id or not gateway_role_arn:
                    console.print(
                        f"[red]Could not read GatewayId/GatewayExecutionRoleArn from the websearch stack "
                        f"'{ws_stack}' in {ws_region}.[/red]"
                    )
                    console.print("Run: [cyan]gip deploy websearch[/cyan] first, then retry 'gip deploy memory'.")
                    return 1

                # Lambda code must be staged in an S3 bucket in the stack's
                # region (same pattern as metering).
                s3_bucket = self._ensure_regional_artifacts_bucket(profile, ws_region, cf, console)
                if not s3_bucket:
                    return 1

                template = project_root / "deployment" / "infrastructure" / "memory-stack.yaml"
                stack_name = profile.stack_names.get("memory", f"{profile.identity_pool_name}-memory")
                params = build_memory_params(profile, gateway_id, gateway_role_arn)

                task = progress.add_task("Packaging memory Lambda functions...", total=None)
                packaged_template_path = None
                try:
                    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
                        packaged_template_path = f.name
                    cmd = [
                        "aws",
                        "cloudformation",
                        "package",
                        "--template-file",
                        str(template),
                        "--s3-bucket",
                        s3_bucket,
                        "--s3-prefix",
                        "gip/memory",
                        "--output-template-file",
                        packaged_template_path,
                        "--region",
                        ws_region,
                    ]
                    result_pkg = run_checked(cmd, capture_output=True, text=True)  # nosec B603 — fixed argv, no shell
                    if result_pkg.returncode != 0:
                        progress.update(task, completed=True)
                        console.print(f"[red]Failed to package memory template: {result_pkg.stderr}[/red]")
                        return 1
                    progress.update(task, description="Memory Lambda functions packaged", completed=True)

                    result = deploy_with_cf(
                        packaged_template_path,
                        stack_name,
                        params,
                        ["CAPABILITY_IAM"],
                        task_description=f"Deploying AgentCore Memory stack in {ws_region}...",
                        cf=cf,
                    )
                    if result == 0:
                        outputs = cf.get_stack_outputs(stack_name) or {}
                        memory_id = outputs.get("MemoryId")
                        if memory_id:
                            profile.memory_id = memory_id
                            config = Config.load()
                            config.save_profile(profile)
                            console.print(f"[green]✓ Memory ID saved to profile: {memory_id}[/green]")
                        gate = outputs.get("DeployGateValue", "log-only")
                        if gate != "active":
                            console.print(
                                "[yellow]⚠ DeployGate is 'log-only': identity-dependent memory tools "
                                "answer with a gated response until the actorId-binding checklist "
                                "(ADR-0016) is verified against this deployment. Set "
                                "memory_deploy_gate='active' in the profile JSON and redeploy to "
                                "enable them.[/yellow]"
                            )
                    return result
                finally:
                    if packaged_template_path:
                        try:
                            os.unlink(packaged_template_path)
                        except Exception:
                            pass  # nosec B110

            elif stack_type == "gateway":
                console.print(f"[red]{upstream_gateway_guidance()}[/red]")
                return 1

            else:
                console.print(f"[red]Unknown stack type: {stack_type}[/red]")
                return 1

    def _show_all_deployment_commands(self, stacks_to_deploy, profile, console):
        """Show AWS CLI commands that would be executed."""
        console.print("\n[bold]AWS CLI Commands:[/bold]")
        for stack_type, description in stacks_to_deploy:
            console.print(f"\n[dim]# {description}[/dim]")
            self._show_deployment_commands(stack_type, profile, console)

    def _show_deployment_commands(self, stack_type: str, profile, console: Console) -> None:
        """Show AWS CLI commands for manual deployment."""
        project_root = Path(__file__).parents[4]
        # CodeBuild may deploy to a different region than the main infrastructure;
        # print the command for the region it actually deploys to.
        region = get_codebuild_region(profile) if stack_type == "codebuild" else profile.aws_region

        def print_deploy_cmd(template, stack_name, params, capabilities=None):
            caps_str = " ".join(capabilities or ["CAPABILITY_NAMED_IAM"])
            lines = [
                "aws cloudformation deploy \\",
                f"    --template-file {template} \\",
                f"    --stack-name {stack_name} \\",
            ]
            if params:
                param_str = " \\\n    ".join(params)
                lines.append(f"    --parameter-overrides {param_str} \\")
            lines.append(f"    --capabilities {caps_str} \\")
            lines.append(f"    --region {region}")
            console.print("\n[cyan]" + "\n".join(lines) + "[/cyan]", soft_wrap=True)

        if stack_type == "auth":
            bedrock_regions = profile.allowed_bedrock_regions
            if not bedrock_regions:
                from governed_inference_platform.models import get_all_bedrock_regions

                bedrock_regions = [r for r in get_all_bedrock_regions() if "gov" not in r]

            stack_name = profile.stack_names.get("auth", f"{profile.identity_pool_name}-stack")
            auth_type = profile.effective_auth_type

            if auth_type == "idc":
                template = project_root / "deployment" / "infrastructure" / "bedrock-auth-idc.yaml"
                idc_permission_set = getattr(profile, "idc_permission_set_name", None) or "BedrockDeveloperAccess"
                params = [
                    f"AllowedPermissionSetName={idc_permission_set}",
                    f"IdentityPoolName={profile.identity_pool_name}",
                    f"AllowedBedrockRegions={','.join(bedrock_regions)}",
                    f"RestrictToAnthropicModels={str(getattr(profile, 'restrict_to_anthropic_models', True)).lower()}",
                    f"EnableMonitoring={str(profile.monitoring_enabled).lower()}",
                ]
                print_deploy_cmd(template, stack_name, params, ["CAPABILITY_NAMED_IAM"])
            else:
                provider_type = profile.provider_type or "okta"
                template_map = {
                    "okta": "bedrock-auth-okta.yaml",
                    "auth0": "bedrock-auth-auth0.yaml",
                    "azure": "bedrock-auth-azure.yaml",
                    "cognito": "bedrock-auth-cognito-pool.yaml",
                    "google": "bedrock-auth-google.yaml",
                    "generic": "bedrock-auth-generic.yaml",
                }
                template_file = template_map.get(provider_type, "bedrock-auth-okta.yaml")
                template = project_root / "deployment" / "infrastructure" / template_file
                params = [
                    f"FederationType={profile.federation_type}",
                    f"RestrictToAnthropicModels={str(getattr(profile, 'restrict_to_anthropic_models', True)).lower()}",
                    f"SessionNameBinding={getattr(profile, 'session_name_binding', 'none') or 'none'}",
                ]
                if provider_type == "okta":
                    params.extend([f"OktaDomain={profile.provider_domain}", f"OktaClientId={profile.client_id}"])
                elif provider_type == "auth0":
                    params.extend([f"Auth0Domain={profile.provider_domain}", f"Auth0ClientId={profile.client_id}"])
                elif provider_type == "azure":
                    tenant_id = _extract_azure_tenant_id(profile.provider_domain)
                    params.extend([f"AzureTenantId={tenant_id}", f"AzureClientId={profile.client_id}"])
                elif provider_type == "cognito":
                    cognito_domain = (
                        profile.provider_domain.split(".")[0]
                        if "." in profile.provider_domain
                        else profile.provider_domain
                    )
                    params.extend(
                        [
                            f"CognitoUserPoolId={profile.cognito_user_pool_id}",
                            f"CognitoUserPoolClientId={profile.client_id}",
                            f"CognitoUserPoolDomain={cognito_domain}",
                        ]
                    )
                params.extend(
                    [
                        f"IdentityPoolName={profile.identity_pool_name}",
                        f"AllowedBedrockRegions={','.join(bedrock_regions)}",
                        f"EnableMonitoring={str(profile.monitoring_enabled).lower()}",
                    ]
                )
                print_deploy_cmd(template, stack_name, params, ["CAPABILITY_NAMED_IAM"])

        elif stack_type == "networking":
            template = project_root / "deployment" / "infrastructure" / "networking.yaml"
            stack_name = profile.stack_names.get("networking", f"{profile.identity_pool_name}-networking")
            vpc_config = profile.monitoring_config or {}
            params = [
                f"VpcCidr={vpc_config.get('vpc_cidr', '10.0.0.0/16')}",
                f"PublicSubnet1Cidr={vpc_config.get('subnet1_cidr', '10.0.1.0/24')}",
                f"PublicSubnet2Cidr={vpc_config.get('subnet2_cidr', '10.0.2.0/24')}",
            ]
            print_deploy_cmd(template, stack_name, params)

        elif stack_type == "s3bucket":
            template = project_root / "deployment" / "infrastructure" / "s3bucket.yaml"
            stack_name = profile.stack_names.get("s3", f"{profile.identity_pool_name}-s3bucket")
            print_deploy_cmd(template, stack_name, [])

        elif stack_type == "monitoring":
            template = project_root / "deployment" / "infrastructure" / "otel-collector.yaml"
            stack_name = profile.stack_names.get("monitoring", f"{profile.identity_pool_name}-otel-collector")
            console.print("[dim]  Note: VpcId/SubnetIds are resolved from the networking stack at deploy time[/dim]")
            params = ["VpcId=<from-networking-stack>", "SubnetIds=<from-networking-stack>"]
            monitoring_config = getattr(profile, "monitoring_config", {})
            if monitoring_config.get("custom_domain"):
                params.append(f"CustomDomainName={monitoring_config['custom_domain']}")
                params.append(f"HostedZoneId={monitoring_config.get('hosted_zone_id', '<hosted-zone-id>')}")
            if monitoring_config.get("allow_insecure_http_ingress") is True:
                params.append("AllowInsecureHttpIngress=true")
            params.append(f"ALBScheme={monitoring_config.get('alb_scheme', 'internal')}")
            print_deploy_cmd(template, stack_name, params)

        elif stack_type == "dashboard":
            template = project_root / "deployment" / "infrastructure" / "claude-code-dashboard.yaml"
            stack_name = profile.stack_names.get("dashboard", f"{profile.identity_pool_name}-dashboard")
            s3_stack = profile.stack_names.get("s3", f"{profile.identity_pool_name}-s3bucket")
            console.print(
                f"\n[cyan]# Step 1: Package Lambda functions\n"
                f"aws cloudformation package \\\n"
                f"    --template-file {template} \\\n"
                f"    --s3-bucket <CfnArtifactsBucket from {s3_stack}> \\\n"
                f"    --s3-prefix gip/dashboard \\\n"
                f"    --output-template-file /tmp/claude-code-dashboard-packaged.yaml \\\n"
                f"    --region {region}[/cyan]"
            )
            console.print("\n[dim]# Step 2: Deploy packaged template[/dim]")
            print_deploy_cmd(
                "/tmp/claude-code-dashboard-packaged.yaml",
                stack_name,
                [f"MetricsRegion={region}"],
            )

        elif stack_type == "cowork-dashboard":
            template = project_root / "deployment" / "infrastructure" / "cowork-dashboard.yaml"
            stack_name = profile.stack_names.get("cowork-dashboard", f"{profile.identity_pool_name}-cowork-dashboard")
            params = [
                f"MetricsRegion={region}",
            ]
            print_deploy_cmd(template, stack_name, params)

        elif stack_type == "analytics":
            template = project_root / "deployment" / "infrastructure" / "analytics-pipeline.yaml"
            stack_name = profile.stack_names.get("analytics", f"{profile.identity_pool_name}-analytics")
            params = [
                f"MetricsLogGroup={profile.metrics_log_group}",
                f"DataRetentionDays={profile.data_retention_days}",
                f"FirehoseBufferInterval={profile.firehose_buffer_interval}",
                f"DebugMode={str(profile.analytics_debug_mode).lower()}",
            ]
            print_deploy_cmd(template, stack_name, params)

        elif stack_type == "quota":
            template = project_root / "deployment" / "infrastructure" / "quota-monitoring.yaml"
            stack_name = profile.stack_names.get("quota", f"{profile.identity_pool_name}-quota")
            profile.stack_names.get("dashboard", f"{profile.identity_pool_name}-dashboard")
            s3_stack = profile.stack_names.get("s3", f"{profile.identity_pool_name}-s3bucket")
            console.print(
                f"\n[cyan]# Step 1: Package Lambda functions\n"
                f"aws cloudformation package \\\n"
                f"    --template-file {template} \\\n"
                f"    --s3-bucket <CfnArtifactsBucket from {s3_stack}> \\\n"
                f"    --s3-prefix gip/quota \\\n"
                f"    --output-template-file /tmp/quota-monitoring-packaged.yaml \\\n"
                f"    --region {region}[/cyan]"
            )
            console.print("\n[dim]# Step 2: Deploy packaged template[/dim]")
            monthly_limit = _quota_token_limit(profile)
            daily_limit = getattr(profile, "daily_token_limit", None)
            params = [
                f"MonthlyTokenLimit={monthly_limit}",
                f"WarningThreshold80={getattr(profile, 'warning_threshold_80', int(monthly_limit * 0.8))}",
                f"WarningThreshold90={getattr(profile, 'warning_threshold_90', int(monthly_limit * 0.9))}",
                f"DailyTokenLimit={daily_limit or 0}",
                f"DailyEnforcementMode={getattr(profile, 'daily_enforcement_mode', 'alert')}",
                f"MonthlyEnforcementMode={getattr(profile, 'monthly_enforcement_mode', 'block')}",
                f"QuotaMode={getattr(profile, 'quota_limit_type', 'token')}",
                f"MonthlyCostLimitUsd={getattr(profile, 'monthly_cost_limit', 0)}",
                f"DailyCostLimitUsd={getattr(profile, 'daily_cost_limit', 0)}",
                f"OidcIssuerUrl={profile.provider_domain}",
                f"OidcClientId={profile.client_id}",
                f"EnableFinegrainedQuotas={str(profile.enable_finegrained_quotas).lower()}",
                f"EnableBypassDetection={str(getattr(profile, 'enable_bypass_detection', False)).lower()}",
            ]
            print_deploy_cmd("/tmp/quota-monitoring-packaged.yaml", stack_name, params)

        elif stack_type == "guardrails":
            template = project_root / "deployment" / "infrastructure" / "guardrails-enforcement.yaml"
            stack_name = profile.stack_names.get("guardrails", f"{profile.identity_pool_name}-guardrails")
            params = build_guardrails_params(profile)
            for guardrails_region in guardrails_regions(profile):
                region = guardrails_region
                print_deploy_cmd(template, stack_name, params, ["CAPABILITY_IAM"])

        elif stack_type == "model-lifecycle":
            template = project_root / "deployment" / "infrastructure" / "model-lifecycle.yaml"
            stack_name = profile.stack_names.get("model-lifecycle", f"{profile.identity_pool_name}-model-lifecycle")
            s3_stack = profile.stack_names.get("s3", f"{profile.identity_pool_name}-s3bucket")
            console.print(
                f"\n[cyan]# Step 1: Package Lambda function\n"
                f"aws cloudformation package \\\n"
                f"    --template-file {template} \\\n"
                f"    --s3-bucket <CfnArtifactsBucket from {s3_stack}> \\\n"
                f"    --s3-prefix gip/model-lifecycle \\\n"
                f"    --output-template-file /tmp/model-lifecycle-packaged.yaml \\\n"
                f"    --region {region}[/cyan]",
                soft_wrap=True,
            )
            console.print("\n[dim]# Step 2: Deploy packaged template[/dim]")
            params = [
                f"ProfileName={profile.name}",
                f"CheckRegions={','.join(lifecycle_check_regions(profile))}",
            ]
            print_deploy_cmd("/tmp/model-lifecycle-packaged.yaml", stack_name, params)
            console.print("\n[dim]# Step 3: Seed the tracked-models parameter[/dim]")
            import json as json_module

            tracked_json = json_module.dumps(compute_tracked_models(profile))
            console.print(
                f"[cyan]aws ssm put-parameter --name /gip/{profile.name}/tracked-models \\\n"
                f"    --type String --overwrite --value '{tracked_json}' --region {region}[/cyan]"
            )

        elif stack_type == "codebuild":
            template = project_root / "deployment" / "infrastructure" / "codebuild-windows.yaml"
            stack_name = profile.stack_names.get("codebuild", f"{profile.identity_pool_name}-codebuild")
            params = [f"ProjectNamePrefix={profile.identity_pool_name}"]
            print_deploy_cmd(template, stack_name, params)

        elif stack_type == "distribution":
            stack_name = profile.stack_names.get("distribution", f"{profile.identity_pool_name}-distribution")
            if profile.distribution_type == "landing-page":
                template = project_root / "deployment" / "infrastructure" / "landing-page-distribution.yaml"
                networking_stack = profile.stack_names.get("networking", f"{profile.identity_pool_name}-networking")
                params = [
                    f"IdentityPoolName={profile.identity_pool_name}",
                    f"VpcId=<VpcId from {networking_stack}>",
                    f"PublicSubnetIds=<SubnetIds from {networking_stack}>",
                    f"PrivateSubnetIds=<SubnetIds from {networking_stack}>",
                    f"IdPProvider={profile.distribution_idp_provider}",
                ]
            else:
                template = project_root / "deployment" / "infrastructure" / "presigned-s3-distribution.yaml"
                presign_principal = getattr(profile, "distribution_presign_principal_type", None) or "iam-user"
                params = [
                    f"IdentityPoolName={profile.identity_pool_name}",
                    f"PresignPrincipalType={presign_principal}",
                ]
            print_deploy_cmd(template, stack_name, params, ["CAPABILITY_NAMED_IAM"])

        elif stack_type == "bootstrap":
            console.print(f"[yellow]{upstream_gateway_guidance('bootstrap')}[/yellow]")

        elif stack_type == "gateway":
            console.print(f"[yellow]{upstream_gateway_guidance()}[/yellow]")

        else:
            console.print(f"[yellow]  No command template available for stack type: {stack_type}[/yellow]")

    def _show_stack_outputs(self, profile, console: Console, config: Config) -> None:
        """Show outputs from deployed stacks."""
        # Get auth stack outputs
        auth_stack = profile.stack_names.get("auth", f"{profile.identity_pool_name}-stack")
        outputs = get_stack_outputs(auth_stack, profile.aws_region)

        if outputs:
            console.print("\n[bold]Authentication Stack:[/bold]")
            console.print(f"• Federation Type: [cyan]{outputs.get('FederationType', 'cognito')}[/cyan]")
            if outputs.get("FederationType") == "direct" or outputs.get("DirectSTSRoleArn", "").startswith("arn:"):
                console.print(f"• Direct STS Role ARN: [cyan]{outputs.get('DirectSTSRoleArn', 'N/A')}[/cyan]")
            if outputs.get("IdentityPoolId"):
                console.print(f"• Identity Pool ID: [cyan]{outputs.get('IdentityPoolId', 'N/A')}[/cyan]")
            # FederatedRoleArn is the new output name from split templates
            role_arn = outputs.get("FederatedRoleArn") or outputs.get("BedrockRoleArn", "N/A")
            console.print(f"• Role ARN: [cyan]{role_arn}[/cyan]")
            console.print(f"• OIDC Provider: [cyan]{outputs.get('OIDCProviderArn', 'N/A')}[/cyan]")

            # Save federated_role_arn to profile for direct STS federation
            direct_sts_role = outputs.get("DirectSTSRoleArn")
            if direct_sts_role and direct_sts_role != "N/A" and direct_sts_role.startswith("arn:"):
                profile.federated_role_arn = direct_sts_role
                config.save_profile(profile)

        # Get networking outputs if enabled
        if profile.monitoring_enabled:
            networking_stack = profile.stack_names.get("networking", f"{profile.identity_pool_name}-networking")
            networking_outputs = get_stack_outputs(networking_stack, profile.aws_region)

            if networking_outputs:
                console.print("\n[bold]Networking Stack:[/bold]")
                vpc_id = networking_outputs.get("VpcId", "N/A")
                subnet_ids = networking_outputs.get("SubnetIds", "N/A")
                console.print(f"• VPC ID: [cyan]{vpc_id}[/cyan]")
                console.print(f"• Subnet IDs: [cyan]{subnet_ids}[/cyan]")

            # Get monitoring stack endpoint
            monitoring_stack = profile.stack_names.get("monitoring", f"{profile.identity_pool_name}-otel-collector")
            monitoring_outputs = get_stack_outputs(monitoring_stack, profile.aws_region)

            if monitoring_outputs:
                console.print("\n[bold]Monitoring Stack:[/bold]")
                endpoint = monitoring_outputs.get("CollectorEndpoint", "N/A")
                verified_endpoint = monitoring_outputs.get("VerifiedCollectorEndpoint")
                console.print(f"• OTLP Endpoint: [cyan]{endpoint}[/cyan]")

                # Save endpoint to profile so gip package doesn't need to read CF outputs
                if endpoint and endpoint != "N/A":
                    profile.otel_collector_endpoint = endpoint
                    profile.otel_verified_collector_endpoint = (
                        verified_endpoint if verified_endpoint == endpoint else None
                    )
                    config.save_profile(profile)
                    console.print("[dim]  Saved to profile for package generation[/dim]")

            dashboard_stack = profile.stack_names.get("dashboard", f"{profile.identity_pool_name}-dashboard")
            dashboard_outputs = get_stack_outputs(dashboard_stack, profile.aws_region)

            if dashboard_outputs:
                console.print("\n[bold]Dashboard Stack:[/bold]")
                dashboard_url = dashboard_outputs.get("DashboardURL", "")
                if dashboard_url:
                    console.print(f"• Dashboard URL: [cyan][link={dashboard_url}]{dashboard_url}[/link][/cyan]")

            # Get quota monitoring stack outputs if enabled
            if profile.quota_monitoring_enabled:
                quota_stack = profile.stack_names.get("quota", f"{profile.identity_pool_name}-quota")
                quota_outputs = get_stack_outputs(quota_stack, profile.aws_region)

                if quota_outputs:
                    console.print("\n[bold]Quota Monitoring Stack:[/bold]")
                    quota_endpoint = quota_outputs.get("QuotaCheckApiEndpoint")
                    console.print(f"• Quota API Endpoint: [cyan]{quota_endpoint or 'N/A'}[/cyan]")
                    console.print(f"• Alert Topic ARN: [cyan]{quota_outputs.get('QuotaAlertTopicArn', 'N/A')}[/cyan]")
                    console.print(f"• User Metrics Table: [cyan]{quota_outputs.get('QuotaTableName', 'N/A')}[/cyan]")
                    console.print(f"• Policies Table: [cyan]{quota_outputs.get('PoliciesTableName', 'N/A')}[/cyan]")

                    # Show configured limits
                    monthly_limit = getattr(profile, "monthly_token_limit", 225000000)
                    monthly_mode = getattr(profile, "monthly_enforcement_mode", "block")
                    daily_limit = getattr(profile, "daily_token_limit", None)
                    daily_mode = getattr(profile, "daily_enforcement_mode", "alert")

                    console.print(f"• Monthly Limit: [cyan]{monthly_limit:,}[/cyan] tokens ({monthly_mode})")
                    if daily_limit:
                        console.print(f"• Daily Limit: [cyan]{daily_limit:,}[/cyan] tokens ({daily_mode})")

                    # Save quota outputs to profile for test command and credential provider
                    if quota_endpoint and quota_endpoint != "N/A":
                        profile.quota_api_endpoint = quota_endpoint
                    if quota_outputs.get("PoliciesTableName"):
                        profile.quota_policies_table = quota_outputs["PoliciesTableName"]
                    if quota_outputs.get("QuotaTableName"):
                        profile.user_quota_metrics_table = quota_outputs["QuotaTableName"]
                    config.save_profile(profile)

    def _create_default_quota_policy(self, profile, quota_stack_name: str, console: Console) -> None:
        """Auto-create default quota policy in DynamoDB after quota stack deployment."""
        try:
            from governed_inference_platform.models import (
                EnforcementMode,
                PolicyType,
                QuotaMode,
            )
            from governed_inference_platform.quota_policies import (
                PolicyAlreadyExistsError,
                QuotaPolicyManager,
            )

            # Get the policies table name from stack outputs
            quota_outputs = get_stack_outputs(quota_stack_name, profile.aws_region)
            if not quota_outputs or not quota_outputs.get("PoliciesTableName"):
                console.print("[yellow]Warning: Could not get policies table name from stack outputs[/yellow]")
                return

            table_name = quota_outputs["PoliciesTableName"]
            manager = QuotaPolicyManager(table_name, profile.aws_region)

            monthly_limit = _quota_token_limit(profile)
            daily_limit = getattr(profile, "daily_token_limit", None)
            monthly_enforcement = getattr(profile, "monthly_enforcement_mode", "block")

            enforcement_mode = EnforcementMode.BLOCK if monthly_enforcement == "block" else EnforcementMode.ALERT

            try:
                manager.create_policy(
                    policy_type=PolicyType.DEFAULT,
                    identifier="default",
                    monthly_token_limit=monthly_limit,
                    quota_mode=QuotaMode(getattr(profile, "quota_limit_type", "token")),
                    daily_token_limit=daily_limit,
                    enforcement_mode=enforcement_mode,
                    monthly_cost_limit=(
                        getattr(profile, "monthly_cost_limit", 0)
                        if getattr(profile, "quota_limit_type", "token") == "cost"
                        else None
                    ),
                    daily_cost_limit=(
                        getattr(profile, "daily_cost_limit", 0)
                        if getattr(profile, "quota_limit_type", "token") == "cost"
                        else None
                    ),
                )
                if getattr(profile, "quota_limit_type", "token") == "cost":
                    console.print(
                        f"[green]Created default quota policy "
                        f"(monthly: ${getattr(profile, 'monthly_cost_limit', 0):.2f}, "
                        f"daily: ${getattr(profile, 'daily_cost_limit', 0):.2f}, "
                        f"enforcement: {monthly_enforcement})[/green]"
                    )
                else:
                    console.print(
                        f"[green]Created default quota policy "
                        f"(monthly: {monthly_limit:,} tokens, enforcement: {monthly_enforcement})[/green]"
                    )
            except PolicyAlreadyExistsError:
                console.print("[dim]Default quota policy already exists (skipping)[/dim]")

        except Exception as e:
            console.print(f"[yellow]Warning: Could not create default quota policy: {str(e)}[/yellow]")
            console.print("[dim]Run 'gip quota set-default' manually to configure quota limits[/dim]")

    def _check_orphaned_stacks(self, stacks_to_deploy, profile, cf_manager, console: Console) -> list:
        """Check for stacks that exist but are disabled in config.

        Returns:
            List of (stack_type, stack_name, status[, region]) tuples for orphaned stacks.
        """
        # All possible stack types
        all_stack_types = {
            "auth": "Authentication Stack",
            "distribution": "Distribution infrastructure",
            "networking": "VPC Networking",
            "monitoring": "OpenTelemetry Collector",
            "dashboard": "CloudWatch Dashboard",
            "cowork-dashboard": "CoWork CloudWatch Dashboard",
            "analytics": "Analytics Pipeline",
            "quota": "Quota Monitoring",
            "codebuild": "CodeBuild",
            "bootstrap": "Bootstrap Server",
            "websearch": "AgentCore Gateway + Web Search connector",
            "memory": "AgentCore Memory (user/org memory tools)",
            "gateway": "Claude Apps Gateway (ECS Fargate + internal ALB)",
            "guardrails": "Bedrock Guardrails enforcement",
        }

        # Stack types that are being deployed
        deploying_types = {stack_type for stack_type, _ in stacks_to_deploy}

        # Check for orphaned stacks
        orphaned = []
        for stack_type in all_stack_types:
            if stack_type == "guardrails":
                stack_name = profile.stack_names.get(stack_type, f"{profile.identity_pool_name}-{stack_type}")
                candidate_regions = guardrails_cleanup_regions(profile)
                # F-002: Probe only enabled regions unless cleanup is explicit.
                explicit_regions = {
                    r.strip() for r in os.environ.get("GIP_GUARDRAILS_CLEANUP_REGIONS", "").split(",") if r.strip()
                }
                try:
                    enabled = cf_manager.session.client("ec2").describe_regions(AllRegions=False).get("Regions", [])
                    enabled_regions = [r["RegionName"] for r in enabled if r.get("RegionName")]
                    enabled_set = set(enabled_regions)
                    requested = [r for r in candidate_regions if r in enabled_set or r in explicit_regions]
                    candidate_regions = list(dict.fromkeys([*requested, *enabled_regions]))
                except Exception as e:
                    candidate_regions = [
                        r for r in candidate_regions if r == profile.aws_region or r in explicit_regions
                    ]
                    if console:
                        console.print(
                            f"[yellow]Could not discover enabled regions for guardrails cleanup: {e}[/yellow]"
                        )
                if stack_type in deploying_types:
                    deploying_guardrails_regions = set(guardrails_regions(profile))
                    candidate_regions = [r for r in candidate_regions if r not in deploying_guardrails_regions]
                for guardrails_region in candidate_regions:
                    mgr = (
                        cf_manager
                        if guardrails_region == profile.aws_region
                        else CloudFormationManager(region=guardrails_region)
                    )
                    try:
                        status = mgr.get_stack_status(stack_name)
                    except Exception as e:
                        if console:
                            console.print(
                                f"[yellow]Could not check guardrails stack in {guardrails_region}: {e}[/yellow]"
                            )
                        continue
                    if status and status not in ["DELETE_COMPLETE", "DELETE_IN_PROGRESS"]:
                        orphaned.append((stack_type, stack_name, status, guardrails_region))
                continue
            if stack_type not in deploying_types:
                # This stack type is not being deployed - check if it exists.
                # CodeBuild and websearch may live in a different region, so
                # check them there or a cross-region orphan is never detected.
                stack_name = profile.stack_names.get(stack_type, f"{profile.identity_pool_name}-{stack_type}")
                mgr = cf_manager
                if stack_type == "codebuild":
                    cb_region = get_codebuild_region(profile)
                    if cb_region != profile.aws_region:
                        mgr = CloudFormationManager(region=cb_region)
                elif stack_type in ("websearch", "memory"):
                    ws_region = get_websearch_region(profile)
                    if ws_region != profile.aws_region:
                        mgr = CloudFormationManager(region=ws_region)
                status = mgr.get_stack_status(stack_name)

                if status and status not in ["DELETE_COMPLETE", "DELETE_IN_PROGRESS"]:
                    orphaned.append((stack_type, stack_name, status))

        return orphaned

    def _ensure_ecs_service_linked_role(self, console: Console) -> None:
        """Ensure ECS service linked role exists, create if needed."""
        try:
            import boto3

            iam_client = boto3.client("iam")

            # Check if role exists
            try:
                iam_client.get_role(RoleName="AWSServiceRoleForECS")
                console.print("[dim]✓ ECS service linked role exists[/dim]")
            except iam_client.exceptions.NoSuchEntityException:
                # Role doesn't exist, create it
                console.print("[yellow]Creating ECS service linked role...[/yellow]")
                try:
                    iam_client.create_service_linked_role(AWSServiceName="ecs.amazonaws.com")
                    console.print("[green]✓ ECS service linked role created[/green]")
                    # Wait for IAM propagation before proceeding with ECS cluster creation
                    import time

                    console.print("[dim]Waiting for IAM role propagation...[/dim]")
                    time.sleep(10)
                except iam_client.exceptions.InvalidInputException as e:
                    # Role might already exist (race condition)
                    if "has been taken in this account" in str(e):
                        console.print("[dim]✓ ECS service linked role already exists[/dim]")
                    else:
                        raise

        except Exception as e:
            console.print(f"[yellow]Warning: Could not verify ECS service linked role: {str(e)}[/yellow]")
            console.print("[dim]If deployment fails, manually create the role with:[/dim]")
            console.print("[dim]aws iam create-service-linked-role --aws-service-name ecs.amazonaws.com[/dim]")

    def _resolve_oidc_config(self, profile) -> tuple:
        """Resolve OIDC issuer URL and client ID for quota JWT authentication.

        Returns ("", "") when SSO is disabled — the CF template's HasJwtAuth
        condition will disable the JWT authorizer and use an open route instead.
        """
        # For real Profile objects, use the new auth_type system
        # For mocks and legacy code, fall back to sso_enabled
        from governed_inference_platform.config import Profile

        if isinstance(profile, Profile) and profile.effective_auth_type != "oidc":
            return "", ""
        elif not isinstance(profile, Profile) and not getattr(profile, "sso_enabled", True):
            return "", ""

        if profile.provider_type == "cognito":
            pool_id = getattr(profile, "cognito_user_pool_id", "")
            if not pool_id:
                raise ValueError(
                    "Cognito User Pool ID is required for quota monitoring JWT authentication. "
                    "Please set cognito_user_pool_id in your profile configuration."
                )
            pool_region = pool_id.split("_")[0] if "_" in pool_id else profile.aws_region
            issuer_url = f"https://cognito-idp.{pool_region}.amazonaws.com/{pool_id}"
        else:
            issuer_url = profile.provider_domain
            if issuer_url and not issuer_url.startswith(("http://", "https://")):
                issuer_url = f"https://{issuer_url}"

        # Okta authenticates via its default custom authorization server, so issued
        # tokens carry iss=https://<domain>/oauth2/default. The quota JWT authorizer
        # must match that exact issuer or every /check request 401s (and, with
        # fail-open, silently disables enforcement).
        if profile.provider_type == "okta" and issuer_url and not issuer_url.rstrip("/").endswith("/oauth2/default"):
            issuer_url = f"{issuer_url.rstrip('/')}/oauth2/default"

        # Auth0 tokens include trailing slash in iss claim, so authorizer must match
        if profile.provider_type == "auth0" and issuer_url and not issuer_url.endswith("/"):
            issuer_url += "/"

        return issuer_url, profile.client_id
