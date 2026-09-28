#!/usr/bin/env python3
# ABOUTME: CI script to validate models.py against live Bedrock APIs
# ABOUTME: Checks ListFoundationModels (regions) and ListInferenceProfiles (CRIS routing)

"""Validate that CLAUDE_MODELS in models.py matches live Bedrock APIs.

Checks:
1. Every destination_region actually has Bedrock Claude models (ListFoundationModels)
2. No new Bedrock regions with Claude models are missing from models.py
3. CRIS inference profiles match models.py profiles (ListInferenceProfiles)
4. AllowedBedrockRegions defaults in CFN templates are in sync
5. Model IDs in models.py exist in the Bedrock model catalog

The comparison logic is shared with `gip models check` — see
source/governed_inference_platform/catalog_check.py.

Usage:
    python scripts/validate_bedrock_regions.py
"""

import concurrent.futures
import sys
from pathlib import Path

try:
    import boto3
    import botocore.config
    HAS_BOTO3 = True
except ImportError:
    HAS_BOTO3 = False

# The script runs from the repo root in CI (no package install), so put
# source/ on sys.path to import the shared comparison core from the package.
_SOURCE_DIR = Path(__file__).parent.parent / "source"
if str(_SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(_SOURCE_DIR))

from governed_inference_platform.catalog_check import (  # noqa: E402
    base_model_available,
    diff_inference_profiles,
    extract_catalog,
    parse_inference_profile_summaries,
)

# Regions to probe — superset of all AWS regions that could have Bedrock
CANDIDATE_REGIONS = [
    "us-east-1", "us-east-2", "us-west-1", "us-west-2",
    "ca-central-1", "ca-west-1",
    "eu-central-1", "eu-central-2", "eu-north-1",
    "eu-south-1", "eu-south-2", "eu-west-1", "eu-west-2", "eu-west-3",
    "ap-northeast-1", "ap-northeast-2", "ap-northeast-3",
    "ap-south-1", "ap-south-2", "ap-east-1", "ap-east-2",
    "ap-southeast-1", "ap-southeast-2", "ap-southeast-3", "ap-southeast-4", "ap-southeast-5",
    "sa-east-1", "af-south-1", "me-south-1", "me-central-1", "il-central-1",
    "mx-central-1",
]

PROBE_CONFIG = botocore.config.Config(
    connect_timeout=5, read_timeout=10, retries={"max_attempts": 1},
) if HAS_BOTO3 else None


# ---------------------------------------------------------------------------
# 1. ListFoundationModels — region availability
# ---------------------------------------------------------------------------

def probe_region(region: str) -> tuple[str, list[str]]:
    """Check if a region has Claude models via ListFoundationModels."""
    try:
        client = boto3.client("bedrock", region_name=region, config=PROBE_CONFIG)
        resp = client.list_foundation_models(byProvider="Anthropic")
        models = [
            m["modelId"] for m in resp.get("modelSummaries", [])
            if "claude" in m.get("modelId", "").lower()
        ]
        return region, models
    except Exception:
        return region, []


def discover_live_regions(max_workers: int = 10, timeout: float = 30) -> dict[str, list[str]]:
    """Discover all regions with Claude models via parallel API calls."""
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(probe_region, r): r for r in CANDIDATE_REGIONS}
        done, pending = concurrent.futures.wait(futures, timeout=timeout)
        for f in done:
            region, models = f.result()
            if models:
                results[region] = models
        for f in pending:
            f.cancel()
            print(f"  ⚠ {futures[f]}: timed out (skipped)")
    return results


# ---------------------------------------------------------------------------
# 2. ListInferenceProfiles — CRIS validation
# ---------------------------------------------------------------------------

def discover_inference_profiles(region: str = "us-east-1") -> dict:
    """Fetch system-defined inference profiles and extract routing data.

    Returns: {profile_id: {"name": str, "regions": [str], "models": [str]}}
    """
    profiles = {}
    try:
        client = boto3.client("bedrock", region_name=region, config=PROBE_CONFIG)
        paginator = client.get_paginator("list_inference_profiles")
        for page in paginator.paginate(typeEquals="SYSTEM_DEFINED", maxResults=100):
            profiles.update(parse_inference_profile_summaries(page.get("inferenceProfileSummaries", [])))
    except Exception as e:
        print(f"  ⚠ ListInferenceProfiles unavailable: {e}")
    return profiles


# ---------------------------------------------------------------------------
# 3. models.py data loaders
# ---------------------------------------------------------------------------

def load_models_py():
    """Load CLAUDE_MODELS and helpers from models.py."""
    from governed_inference_platform.models import (
        CLAUDE_MODELS,
        get_all_bedrock_regions,
        get_all_model_display_names,
    )
    return CLAUDE_MODELS, set(get_all_bedrock_regions()), get_all_model_display_names()


def load_cfn_template_regions() -> dict[str, set[str]]:
    """Load AllowedBedrockRegions defaults from CloudFormation templates."""
    infra_dir = Path(__file__).parent.parent / "deployment" / "infrastructure"
    templates = {}
    for f in sorted(infra_dir.glob("bedrock-auth-*.yaml")):
        content = f.read_text()
        for line in content.split("\n"):
            if "Default:" in line and ("east" in line or "west" in line or "central" in line):
                start = line.find("'") + 1
                end = line.rfind("'")
                if start > 0 and end > start:
                    templates[f.name] = set(line[start:end].split(","))
    # Also check cognito-identity-pool.yaml
    cip = infra_dir / "cognito-identity-pool.yaml"
    if cip.exists():
        for line in cip.read_text().split("\n"):
            if "Default:" in line and ("east" in line or "west" in line or "central" in line):
                start = line.find("'") + 1
                end = line.rfind("'")
                if start > 0 and end > start:
                    templates[cip.name] = set(line[start:end].split(","))
                    break
    return templates


# ---------------------------------------------------------------------------
# Main validation
# ---------------------------------------------------------------------------

def main():
    issues = []
    warnings = []

    # Load models.py data (always works, no AWS creds needed)
    print("📖 Loading models.py data...")
    claude_models, models_py_regions, display_names = load_models_py()
    commercial_regions = {r for r in models_py_regions if "gov" not in r}
    print(f"   {len(claude_models)} models, {len(commercial_regions)} commercial regions, {len(display_names)} display names\n")

    # Flatten the catalog with the shared extraction logic
    catalog = extract_catalog(claude_models)

    # --- CFN template sync (no AWS creds needed) ---
    print("📋 Checking CloudFormation template defaults...")
    cfn_templates = load_cfn_template_regions()
    for name, regions in cfn_templates.items():
        if regions != commercial_regions:
            missing = commercial_regions - regions
            extra = regions - commercial_regions
            if missing:
                issues.append(f"CFN {name}: missing regions {sorted(missing)}")
                print(f"   ❌ {name}: missing {sorted(missing)}")
            if extra:
                warnings.append(f"CFN {name}: extra regions {sorted(extra)}")
                print(f"   ⚠️  {name}: extra {sorted(extra)}")
            if not missing and not extra:
                print(f"   ✅ {name}: in sync")
        else:
            print(f"   ✅ {name}: in sync")

    # --- Live API checks (need AWS creds) ---
    if not HAS_BOTO3:
        print("\n⚠️  boto3 not installed — skipping live API validation")
    else:
        # ListFoundationModels — region check
        print("\n🔍 Probing regions via ListFoundationModels...")
        live_regions = discover_live_regions()
        live_region_set = set(live_regions.keys())
        print(f"   Found {len(live_region_set)} regions with Claude models")

        new_regions = live_region_set - commercial_regions
        if new_regions:
            for r in sorted(new_regions):
                issues.append(f"NEW REGION: {r} has {len(live_regions[r])} Claude models, not in models.py")
                print(f"   ❌ {r}: {len(live_regions[r])} models — NOT in models.py")

        stale_regions = commercial_regions - live_region_set
        if stale_regions:
            for r in sorted(stale_regions):
                warnings.append(f"CRIS-ONLY: {r} in models.py but not discoverable (may be valid)")
                print(f"   ⚠️  {r}: in models.py but not discoverable (may be CRIS-only)")

        # Validate base model IDs exist
        print("\n🧬 Validating base model IDs...")
        us_east_models = live_regions.get("us-east-1", [])
        for base_id in sorted(catalog.base_model_ids):
            if "gov" in base_id:
                continue
            if base_model_available(base_id, us_east_models):
                print(f"   ✅ {base_id}")
            else:
                warnings.append(f"BASE MODEL: {base_id} not found in us-east-1 catalog")
                print(f"   ⚠️  {base_id}: not in us-east-1 catalog (may be new or deprecated)")

        # ListInferenceProfiles — CRIS validation
        print("\n🌐 Validating inference profiles via ListInferenceProfiles...")
        live_profiles = discover_inference_profiles()
        if live_profiles:
            print(f"   Found {len(live_profiles)} Claude inference profiles")

            diff = diff_inference_profiles(catalog.profiles, live_profiles)

            drifted = set(diff.region_drift) | set(diff.not_available_live)
            for mid in sorted(catalog.profiles):
                if "gov" in mid:
                    continue
                if mid in live_profiles and mid not in drifted:
                    print(f"   ✅ {mid}: regions match")
            for mid, drift in sorted(diff.region_drift.items()):
                if drift["catalog_only"]:
                    warnings.append(f"CRIS {mid}: models.py has regions not in live profile: {drift['catalog_only']}")
                    print(f"   ⚠️  {mid}: dest regions {drift['catalog_only']} not in live profile")
                if drift["live_only"]:
                    issues.append(f"CRIS {mid}: live profile has new regions not in models.py: {drift['live_only']}")
                    print(f"   ❌ {mid}: live profile has new regions {drift['live_only']}")
            for mid in diff.not_available_live:
                warnings.append(f"CRIS {mid}: not found in live inference profiles")
                print(f"   ⚠️  {mid}: not in live profiles (may need newer API version)")

            # Check for new live profiles not in models.py
            for pid, name in diff.missing_from_catalog:
                issues.append(f"NEW CRIS: {pid} ({name}) not in models.py")
                print(f"   ❌ {pid}: new profile not in models.py")
        else:
            print("   ⚠️  No profiles returned (API may be unavailable)")

    # --- Summary ---
    print(f"\n{'='*60}")
    if not issues:
        print(f"✅ Validation passed ({len(warnings)} warning(s))")
        for w in warnings:
            print(f"   ⚠️  {w}")
        return 0
    else:
        print(f"❌ {len(issues)} issue(s), {len(warnings)} warning(s):")
        for i in issues:
            print(f"   ❌ {i}")
        for w in warnings:
            print(f"   ⚠️  {w}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
