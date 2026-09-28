# ABOUTME: Pure helpers mapping AWS regions to geographies and producing data-residency warnings
# ABOUTME: Used by `gip init` (wizard review step and --from-file summary) — advisory only, never blocking

"""Data-residency helpers for `gip init`.

Inference residency is already enforced at model-resolution time:
``models.py`` ``DATA_RESIDENCY_PREFIXES`` refuses cross-geography tier
fallback for eu/au/jp CRIS profiles. These helpers cover the *other* side
of residency — where the supporting infrastructure lives:

- monitoring/telemetry logs, quota DynamoDB records, and analytics S3
  objects (user emails + usage records) land in the infrastructure region;
- the web search gateway processes queries and stores AgentCore Memory content
  in us-east-1 only (see ``deployment/infrastructure/bedrock-agentcore-gateway.yaml``
  and ``deployment/infrastructure/memory-stack.yaml``);
- CodeBuild Windows build artifacts land in the CodeBuild region.

All warnings are advisory — deployment is never blocked.
"""

# CRIS geographies with strict data-residency requirements.
# Mirrors models.py DATA_RESIDENCY_PREFIXES.
DATA_RESIDENCY_GEOGRAPHIES = {"eu", "au", "jp", "us-gov"}

# Legacy config values from older gip versions (mirrors models.py
# PROFILE_KEY_ALIASES): profiles may store "europe"/"japan".
_CRIS_GEOGRAPHY_ALIASES = {"europe": "eu", "japan": "jp"}

# Regions whose data-residency geography is narrower than their region
# prefix ("ap-" alone would say "apac"): au = Sydney/Melbourne,
# jp = Tokyo/Osaka.
_SPECIAL_REGION_GEOGRAPHIES = {
    "ap-southeast-2": "au",
    "ap-southeast-4": "au",
    "ap-northeast-1": "jp",
    "ap-northeast-3": "jp",
}

# Prefix → geography, honest and simple. Order matters: us-gov- before us-.
_PREFIX_GEOGRAPHIES = (
    ("us-gov-", "us-gov"),
    ("us-", "us"),
    ("eu-", "eu"),
    ("ap-", "apac"),
    ("ca-", "ca"),
    ("sa-", "sa"),
    ("me-", "me"),
    ("af-", "af"),
)


def region_geography(region: str) -> str:
    """Map an AWS region to a coarse geography by its prefix.

    Returns one of 'us', 'eu', 'apac', 'au', 'jp', 'ca', 'sa', 'me', 'af',
    'us-gov'. au/jp are special-cased (ap-southeast-2/4, ap-northeast-1/3);
    everything else maps by region prefix only. Unknown prefixes fall back
    to the first hyphen-separated token (e.g. 'il-central-1' → 'il').
    """
    normalized = (region or "").strip().lower()
    if not normalized:
        return ""
    if normalized in _SPECIAL_REGION_GEOGRAPHIES:
        return _SPECIAL_REGION_GEOGRAPHIES[normalized]
    for prefix, geography in _PREFIX_GEOGRAPHIES:
        if normalized.startswith(prefix):
            return geography
    return normalized.split("-", 1)[0]


def residency_warnings(
    infra_region: str,
    cris_geography: str,
    web_search_enabled: bool,
    codebuild_region: str | None = None,
    memory_enabled: bool = False,
) -> list[str]:
    """Warnings when supporting infrastructure leaves the inference geography.

    Only applies when the CRIS profile geography has strict data-residency
    requirements (eu/au/jp/us-gov — the same set models.py refuses cross-geo
    tier fallback for). Returns human-readable warning strings; empty list means
    no residency concern detected. Advisory only — callers must not block.
    """
    geography = _CRIS_GEOGRAPHY_ALIASES.get(cris_geography, cris_geography)
    if geography not in DATA_RESIDENCY_GEOGRAPHIES:
        return []

    warnings: list[str] = []

    infra_geography = region_geography(infra_region)
    if infra_geography != geography:
        warnings.append(
            f"Infrastructure region {infra_region} ({infra_geography}) is outside the '{geography}' "
            "inference geography: monitoring telemetry, quota DynamoDB records, and analytics S3 data "
            "(including user emails and usage records) will be stored there. Consider an infrastructure "
            "region in the same geography as the inference profile."
        )

    if web_search_enabled:
        warnings.append(
            f"Web search queries are processed in us-east-1 regardless of the '{geography}' inference "
            "geography — user queries (or prompt fragments) transit to the US. See the DataResidency "
            "note in deployment/infrastructure/bedrock-agentcore-gateway.yaml and review compliance "
            "impact before enabling."
        )

    if memory_enabled:
        warnings.append(
            f"AgentCore Memory content is stored in us-east-1 regardless of the '{geography}' inference "
            "geography — conversation-derived user and organization memory leaves the geography. See "
            "the region note in deployment/infrastructure/memory-stack.yaml and review compliance impact "
            "before enabling."
        )

    if codebuild_region:
        codebuild_geography = region_geography(codebuild_region)
        if codebuild_geography != geography:
            warnings.append(
                f"CodeBuild region {codebuild_region} ({codebuild_geography}) is outside the "
                f"'{geography}' inference geography — Windows build artifacts will be stored there."
            )

    return warnings
