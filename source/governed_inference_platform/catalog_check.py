# ABOUTME: Pure comparison core for model-catalog drift detection against live Bedrock APIs
# ABOUTME: Shared by `gip models check` and scripts/validate_bedrock_regions.py (CI)

"""Compare the hardcoded model catalog (models.py) against live Bedrock API data.

All functions here are pure: they take pre-fetched API responses (or data
extracted from CLAUDE_MODELS) and return structured diff results. No AWS
calls are made from this module, which keeps it trivially testable with
fixture responses and reusable from both the CLI command and the CI script.

See REVIEW.md finding #13: the catalog is curated by hand, so drift against
the live Bedrock APIs is expected over time. This module makes that drift
visible; fixing it still requires a repo update.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Catalog extraction (from models.py CLAUDE_MODELS)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CatalogProfile:
    """A CRIS inference profile as declared in models.py."""

    model_key: str
    profile_key: str
    source_regions: frozenset[str]
    destination_regions: frozenset[str]


@dataclass
class Catalog:
    """Flattened view of CLAUDE_MODELS for comparison purposes."""

    profiles: dict[str, CatalogProfile]  # CRIS profile model_id -> CatalogProfile
    base_model_ids: set[str]
    base_destination_regions: dict[str, set[str]]  # base_model_id -> union of destination regions


def extract_catalog(claude_models: dict) -> Catalog:
    """Flatten CLAUDE_MODELS into profile/base-model lookup tables.

    Accepts either the dataclass-based CLAUDE_MODELS or the raw dict form
    (both support dict-style access).
    """
    profiles: dict[str, CatalogProfile] = {}
    base_ids: set[str] = set()
    base_dest: dict[str, set[str]] = {}
    for model_key, model in claude_models.items():
        base_id = model.get("base_model_id")
        if base_id:
            base_ids.add(base_id)
            base_dest.setdefault(base_id, set())
        for profile_key, profile in model.get("profiles", {}).items():
            dest = frozenset(profile.get("destination_regions", ()))
            profiles[profile["model_id"]] = CatalogProfile(
                model_key=model_key,
                profile_key=profile_key,
                source_regions=frozenset(profile.get("source_regions", ())),
                destination_regions=dest,
            )
            if base_id:
                base_dest[base_id].update(r for r in dest if not r.startswith("all-"))
    return Catalog(profiles=profiles, base_model_ids=base_ids, base_destination_regions=base_dest)


# ---------------------------------------------------------------------------
# Live API response parsing (pure — takes response dicts, not clients)
# ---------------------------------------------------------------------------


def _iso(value) -> str | None:
    """Normalize a boto3 timestamp (datetime) or string to an ISO string, None-safe."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def parse_foundation_model_summaries(summaries: Iterable[dict]) -> list[dict]:
    """Normalize ListFoundationModels modelSummaries to Anthropic Claude entries.

    Returns a list of dicts with model_id, model_name, inference_types,
    lifecycle_status, and the three lifecycle date keys (legacy_time,
    public_extended_access_time, end_of_life_time — ISO strings or None).
    Date fields only appear on the API response after a model goes LEGACY;
    public_extended_access_time is optional even then (pre-Feb-2026 policy
    models and provider discretion — R14 §1.1).
    """
    models = []
    for m in summaries or []:
        model_id = m.get("modelId", "")
        if "claude" not in model_id.lower():
            continue
        lifecycle = m.get("modelLifecycle") or {}
        models.append(
            {
                "model_id": model_id,
                "model_name": m.get("modelName", ""),
                "inference_types": list(m.get("inferenceTypesSupported", [])),
                "lifecycle_status": lifecycle.get("status", ""),
                "legacy_time": _iso(lifecycle.get("legacyTime")),
                "public_extended_access_time": _iso(lifecycle.get("publicExtendedAccessTime")),
                "end_of_life_time": _iso(lifecycle.get("endOfLifeTime")),
            }
        )
    return models


def parse_inference_profile_summaries(summaries: Iterable[dict]) -> dict[str, dict]:
    """Normalize ListInferenceProfiles summaries to Anthropic CRIS profiles.

    Destination regions are extracted from the model ARNs inside each profile.

    Returns: {profile_id: {"name": str, "regions": [str], "models": [arn]}}
    """
    profiles: dict[str, dict] = {}
    for p in summaries or []:
        pid = p.get("inferenceProfileId", "")
        if "anthropic" not in pid.lower():
            continue
        regions = set()
        model_arns = []
        for m in p.get("models", []):
            arn = m.get("modelArn", "")
            model_arns.append(arn)
            parts = arn.split(":")
            if len(parts) >= 4 and parts[3]:
                regions.add(parts[3])
        profiles[pid] = {
            "name": p.get("inferenceProfileName", ""),
            "regions": sorted(regions),
            "models": model_arns,
        }
    return profiles


# ---------------------------------------------------------------------------
# Comparison primitives
# ---------------------------------------------------------------------------


def base_model_available(base_model_id: str, live_model_ids: Iterable[str]) -> bool:
    """True if a catalog base model ID matches any live model ID.

    Live IDs may carry context-window variants (e.g. '<base>:0:200k'), so a
    live ID counts as a match when it equals the base ID or extends it with
    a ':'-separated suffix.
    """
    for live_id in live_model_ids:
        if live_id == base_model_id or live_id.startswith(base_model_id + ":"):
            return True
    return False


def _matches_any_base(live_model_id: str, base_model_ids: Iterable[str]) -> bool:
    """True if a live model ID corresponds to some catalog base model."""
    for base_id in base_model_ids:
        if live_model_id == base_id or live_model_id.startswith(base_id + ":"):
            return True
    return False


@dataclass
class ProfileDiff:
    """Result of diffing catalog CRIS profiles against live inference profiles."""

    # Live profiles with no catalog entry: [(profile_id, profile_name)]
    missing_from_catalog: list[tuple[str, str]] = field(default_factory=list)
    # Catalog profiles (within the compared set) not returned by the live API
    not_available_live: list[str] = field(default_factory=list)
    # Destination-region drift for profiles present on both sides:
    # {profile_id: {"live_only": [...], "catalog_only": [...]}} — only drifted entries
    region_drift: dict[str, dict[str, list[str]]] = field(default_factory=dict)


def diff_inference_profiles(catalog_profiles: dict[str, CatalogProfile], live_profiles: dict[str, dict]) -> ProfileDiff:
    """Diff catalog CRIS profiles against live ListInferenceProfiles data.

    Callers control scoping by pre-filtering catalog_profiles (e.g. to the
    geographies reachable from the regions actually queried).
    """
    diff = ProfileDiff()
    for pid in sorted(catalog_profiles):
        if "gov" in pid:
            continue  # GovCloud profiles are not discoverable from commercial regions
        if pid not in live_profiles:
            diff.not_available_live.append(pid)
            continue
        if any(r.startswith("all-") for r in catalog_profiles[pid].destination_regions):
            continue  # Sentinel destinations (e.g. 'all-commercial') can't be region-diffed
        live_regions = set(live_profiles[pid].get("regions", []))
        catalog_regions = {r for r in catalog_profiles[pid].destination_regions if not r.startswith("all-")}
        live_only = sorted(live_regions - catalog_regions)
        catalog_only = sorted(catalog_regions - live_regions)
        if live_only or catalog_only:
            diff.region_drift[pid] = {"live_only": live_only, "catalog_only": catalog_only}
    for pid in sorted(live_profiles):
        if pid not in catalog_profiles:
            diff.missing_from_catalog.append((pid, live_profiles[pid].get("name", "")))
    return diff


@dataclass
class FoundationModelDiff:
    """Result of diffing catalog base models against live ListFoundationModels data."""

    # Actionable: ACTIVE, CRIS-capable live models absent from the catalog
    # {model_id: {"name": str, "regions": [str]}}
    missing_from_catalog: dict[str, dict] = field(default_factory=dict)
    # Informational: live Claude models absent from the curated catalog that are
    # legacy (ON_DEMAND-only) or not ACTIVE — usually intentionally excluded
    legacy_not_in_catalog: dict[str, dict] = field(default_factory=dict)
    # Catalog base models not found live in any checked region where expected
    not_available_live: list[str] = field(default_factory=list)


def diff_foundation_models(
    catalog: Catalog,
    live_models_by_region: dict[str, list[dict]],
    checked_regions: Iterable[str] | None = None,
) -> FoundationModelDiff:
    """Diff catalog base models against live foundation-model listings.

    Args:
        catalog: Extracted catalog data.
        live_models_by_region: {region: parse_foundation_model_summaries(...)}.
        checked_regions: Regions that were actually queried (defaults to the
            keys of live_models_by_region). A catalog model is only reported
            as unavailable if some queried region was expected to have it.
    """
    checked = set(checked_regions if checked_regions is not None else live_models_by_region.keys())
    diff = FoundationModelDiff()

    # Live -> catalog: new (or intentionally excluded) models
    seen: dict[str, dict] = {}
    for region, models in live_models_by_region.items():
        for m in models:
            entry = seen.setdefault(m["model_id"], {"info": m, "regions": set()})
            entry["regions"].add(region)
    for model_id in sorted(seen):
        if _matches_any_base(model_id, catalog.base_model_ids):
            continue
        info = seen[model_id]["info"]
        record = {"name": info.get("model_name", ""), "regions": sorted(seen[model_id]["regions"])}
        is_active = info.get("lifecycle_status", "") in ("", "ACTIVE")
        is_cris_capable = "INFERENCE_PROFILE" in info.get("inference_types", [])
        if is_active and is_cris_capable:
            diff.missing_from_catalog[model_id] = record
        else:
            diff.legacy_not_in_catalog[model_id] = record

    # Catalog -> live: removed/unavailable models
    live_ids_by_region = {region: [m["model_id"] for m in models] for region, models in live_models_by_region.items()}
    for base_id in sorted(catalog.base_model_ids):
        if "gov" in base_id:
            continue
        expected_here = catalog.base_destination_regions.get(base_id, set()) & checked
        if not expected_here:
            continue  # None of the queried regions should have this model
        if not any(base_model_available(base_id, live_ids_by_region.get(r, [])) for r in expected_here):
            diff.not_available_live.append(base_id)
    return diff


# ---------------------------------------------------------------------------
# Lifecycle warnings (legacy / premium pricing / EOL) for catalog models
# ---------------------------------------------------------------------------

# Default alert thresholds — mirror the model-lifecycle stack parameters
# (LegacyPremiumWarningDays / EolWarningDays in model-lifecycle.yaml).
PREMIUM_WARNING_DAYS = 30
EOL_CRITICAL_DAYS = 60


def _parse_ts(value: str | None) -> datetime | None:
    """Parse an ISO timestamp (tolerating a trailing 'Z') into an aware datetime."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _date_str(value: str | None) -> str:
    """Date part of an ISO timestamp for display."""
    parsed = _parse_ts(value)
    return parsed.date().isoformat() if parsed else "unknown"


def collect_lifecycle_warnings(
    catalog: Catalog,
    live_models_by_region: dict[str, list[dict]],
    now: datetime | None = None,
) -> list[dict]:
    """Lifecycle warnings for LEGACY live models that the catalog actually ships.

    R14 §1.7: `gip models check` previously surfaced lifecycle only for
    *uncatalogued* models — models the platform packages (catalog entries)
    never warned about legacy/premium/EOL. This joins live modelLifecycle
    data (status + dates) onto catalog base models.

    Returns a list of dicts (one per LEGACY catalog model) with keys:
    model_id, catalog_keys, status, legacy_time, public_extended_access_time,
    end_of_life_time, severity ('warning'|'critical'), messages (list[str]).
    """
    now = now or datetime.now(timezone.utc)
    seen: dict[str, dict] = {}
    for models in live_models_by_region.values():
        for m in models:
            if m.get("lifecycle_status") != "LEGACY":
                continue
            seen.setdefault(m["model_id"], m)

    warnings = []
    for live_id in sorted(seen):
        matched_bases = [b for b in catalog.base_model_ids if live_id == b or live_id.startswith(b + ":")]
        if not matched_bases:
            continue  # Uncatalogued legacy models stay in legacy_not_in_catalog notes
        info = seen[live_id]
        legacy_time = info.get("legacy_time")
        premium_time = info.get("public_extended_access_time")
        eol_time = info.get("end_of_life_time")

        messages = [f"entered Legacy on {_date_str(legacy_time)}" if legacy_time else "is LEGACY"]
        severity = "warning"

        premium_dt = _parse_ts(premium_time)
        if premium_dt:
            if now >= premium_dt:
                messages.append(
                    f"premium pricing ACTIVE since {_date_str(premium_time)} "
                    "(provider-set premium — check the AWS Health Legacy notification)"
                )
            elif now >= premium_dt - timedelta(days=PREMIUM_WARNING_DAYS):
                messages.append(f"premium pricing begins {_date_str(premium_time)} (provider-set premium)")

        eol_dt = _parse_ts(eol_time)
        if eol_dt:
            if now >= eol_dt - timedelta(days=EOL_CRITICAL_DAYS):
                severity = "critical"
                messages.append(f"inference FAILS after {_date_str(eol_time)} (end of life)")
            else:
                messages.append(f"end of life {_date_str(eol_time)}")

        warnings.append(
            {
                "model_id": live_id,
                "catalog_keys": sorted(matched_bases),
                "status": "LEGACY",
                "legacy_time": legacy_time,
                "public_extended_access_time": premium_time,
                "end_of_life_time": eol_time,
                "severity": severity,
                "messages": messages,
            }
        )
    return warnings


# ---------------------------------------------------------------------------
# Proposal synthesis for `gip models check --propose`
# ---------------------------------------------------------------------------

_TODO_NOTES = (
    "TODO(review): tier placement in MODEL_TIER_PREFERENCES (proposals never self-place)",
    "TODO(review): rate limits — MODEL_RATE_LIMITS is family-keyed, usually no change needed",
    "TODO(review): data-residency semantics and display naming conventions",
)


def _suggest_model_key(base_model_id: str, existing_keys: Iterable[str]) -> str:
    """Derive a catalog key from a base model ID (anthropic.claude-sonnet-4-20250514-v1:0 → sonnet-4)."""
    key = base_model_id.split(".")[-1]
    key = key.removeprefix("claude-")
    parts = []
    for part in key.split("-"):
        if len(part) == 8 and part.isdigit():
            break  # date stamp — everything after is version noise
        if part.startswith("v") and part[1:].split(":")[0].isdigit():
            break
        parts.append(part.split(":")[0])
    suggested = "-".join(parts) or key
    if suggested in set(existing_keys):
        suggested = f"{suggested}-new"
    return suggested


def _split_profile_id(profile_id: str) -> tuple[str | None, str | None]:
    """Split a CRIS profile ID into (geo_prefix, base_model_id)."""
    if ".anthropic." not in profile_id:
        return None, None
    geo, rest = profile_id.split(".anthropic.", 1)
    return geo, f"anthropic.{rest}"


def build_proposals(
    catalog: Catalog,
    report: "DriftReport",
    live_profiles: dict[str, dict],
    checked_regions: Iterable[str],
) -> list[dict]:
    """Synthesize catalog-entry proposals from drift results (pure, no AWS calls).

    Two proposal kinds:
    - ``new_model``: an ACTIVE, CRIS-capable live model absent from the catalog,
      with every discovered CRIS profile attached.
    - ``new_profile``: a live CRIS profile for a base model the catalog already
      has, missing from that entry's profiles.

    Per R14 §2.1, source_regions are derived from the regions that returned the
    profile (``seen_in_regions``, recorded by fetch_live_data). When that data
    is unavailable or the check covered a single geography, coverage is partial
    and flagged via ``source_regions_partial``.
    """
    checked = sorted(set(checked_regions))
    profiles_by_base: dict[str, dict[str, dict]] = {}
    for pid, info in live_profiles.items():
        geo, base_id = _split_profile_id(pid)
        if not geo:
            continue
        seen_in = sorted(info.get("seen_in_regions", []))
        profiles_by_base.setdefault(base_id, {})[geo] = {
            "model_id": pid,
            "description": f"{geo.upper()} CRIS - proposed by gip models check --propose",
            # Observable coverage is bounded by the regions actually queried:
            # a single-geography check cannot see another geography's sources.
            "source_regions": seen_in or checked,
            "source_regions_partial": not seen_in,
            "destination_regions": sorted(info.get("regions", [])),
        }

    proposals = []
    existing_keys = {p.model_key for p in catalog.profiles.values()}

    for base_id, info in sorted(report.foundation_models.missing_from_catalog.items()):
        matching_profiles = {}
        for profile_base, geos in profiles_by_base.items():
            if (
                profile_base == base_id
                or profile_base.startswith(base_id + ":")
                or base_id.startswith(profile_base + ":")
            ):
                matching_profiles.update(geos)
        proposals.append(
            {
                "kind": "new_model",
                "model_key": _suggest_model_key(base_id, existing_keys),
                "name": info.get("name", "") or base_id,
                "base_model_id": base_id,
                "profiles": matching_profiles,
            }
        )

    catalog_profile_ids = set(catalog.profiles)
    for pid, name in report.profiles.missing_from_catalog:
        geo, base_id = _split_profile_id(pid)
        if not geo or pid in catalog_profile_ids:
            continue
        owner_bases = [b for b in catalog.base_model_ids if base_id == b or base_id.startswith(b + ":")]
        if not owner_bases:
            continue  # Covered by a new_model proposal (or a non-catalog model)
        owner_key = None
        for cat_pid, cat_prof in catalog.profiles.items():
            _g, cat_base = _split_profile_id(cat_pid)
            if cat_base and any(cat_base == b or cat_base.startswith(b + ":") for b in owner_bases):
                owner_key = cat_prof.model_key
                break
        proposals.append(
            {
                "kind": "new_profile",
                "model_key": owner_key or owner_bases[0],
                "name": name or pid,
                "base_model_id": owner_bases[0],
                "profiles": {
                    geo: profiles_by_base.get(base_id, {}).get(geo)
                    or {
                        "model_id": pid,
                        "description": f"{geo.upper()} CRIS - proposed by gip models check --propose",
                        "source_regions": checked,
                        "source_regions_partial": True,
                        "destination_regions": sorted(live_profiles.get(pid, {}).get("regions", [])),
                    }
                },
            }
        )
    return proposals


def _render_region_list(regions: list[str], indent: str) -> str:
    inner = "".join(f'{indent}    "{r}",\n' for r in regions)
    return "[\n" + inner + indent + "]"


def render_models_py_snippet(proposals: list[dict]) -> str:
    """Render proposals as ready-to-paste _CLAUDE_MODELS_RAW entry blocks."""
    if not proposals:
        return "# No proposals — catalog already covers every live model.\n"
    lines = [
        "# Generated by `gip models check --propose` — paste into",
        "# source/governed_inference_platform/models.py (_CLAUDE_MODELS_RAW).",
        "# Codegen is the system of record (ADR-0018); review before merging:",
    ]
    lines += [f"#   {note}" for note in _TODO_NOTES]
    lines.append("")
    for prop in proposals:
        if prop["kind"] == "new_profile":
            lines.append(f'# Add to _CLAUDE_MODELS_RAW["{prop["model_key"]}"]["profiles"]:')
            for geo, profile in sorted(prop["profiles"].items()):
                lines.append(_render_profile_block(geo, profile, base_indent="    "))
            lines.append("")
            continue
        lines.append(f'    "{prop["model_key"]}": {{')
        lines.append(f'        "name": "{prop["name"]}",')
        lines.append(f'        "base_model_id": "{prop["base_model_id"]}",')
        lines.append('        "profiles": {')
        if not prop["profiles"]:
            lines.append("            # TODO: no CRIS profiles discovered in the checked regions")
        for geo, profile in sorted(prop["profiles"].items()):
            lines.append(_render_profile_block(geo, profile, base_indent="            "))
        lines.append("        },")
        lines.append("    },")
        lines.append("")
    return "\n".join(lines) + "\n"


def _render_profile_block(geo: str, profile: dict, base_indent: str) -> str:
    partial = (
        "  # TODO: partial coverage — re-run with --region in each geography"
        if profile.get("source_regions_partial")
        else ""
    )
    lines = [
        f'{base_indent}"{geo}": {{',
        f'{base_indent}    "model_id": "{profile["model_id"]}",',
        f'{base_indent}    "description": "{profile["description"]}",',
        f'{base_indent}    "source_regions": {_render_region_list(profile["source_regions"], base_indent + "    ")},{partial}',
        f'{base_indent}    "destination_regions": {_render_region_list(profile["destination_regions"], base_indent + "    ")},',
        f"{base_indent}}},",
    ]
    return "\n".join(lines)


def render_overlay_json(proposals: list[dict]) -> dict:
    """Render proposals as an ``extra_models`` profile-overlay block (dict form).

    Only ``new_model`` proposals are included: the overlay is additive-only, so
    a new profile on an existing catalog entry cannot be expressed (it would
    override the catalog entry — rejected by validate_extra_models).
    """
    overlay = {}
    for prop in proposals:
        if prop["kind"] != "new_model":
            continue
        overlay[prop["model_key"]] = {
            "name": prop["name"],
            "base_model_id": prop["base_model_id"],
            "profiles": {
                geo: {
                    "model_id": p["model_id"],
                    "description": p["description"],
                    "source_regions": p["source_regions"],
                    "destination_regions": p["destination_regions"],
                }
                for geo, p in sorted(prop["profiles"].items())
            },
        }
    return {"extra_models": overlay}


# ---------------------------------------------------------------------------
# Full drift report (used by `gip models check`)
# ---------------------------------------------------------------------------


@dataclass
class DriftReport:
    """Aggregate drift between the hardcoded catalog and live Bedrock APIs."""

    regions_checked: list[str]
    foundation_models: FoundationModelDiff
    profiles: ProfileDiff
    notes: list[str] = field(default_factory=list)
    # Lifecycle warnings for catalog models observed LEGACY live (R14):
    # informational — they do not affect in_sync / the exit code.
    lifecycle_warnings: list[dict] = field(default_factory=list)

    @property
    def in_sync(self) -> bool:
        return not (
            self.foundation_models.missing_from_catalog
            or self.foundation_models.not_available_live
            or self.profiles.missing_from_catalog
            or self.profiles.not_available_live
            or self.profiles.region_drift
        )

    def to_dict(self) -> dict:
        """Machine-readable form for --json output."""
        return {
            "regions_checked": list(self.regions_checked),
            "in_sync": self.in_sync,
            "missing_from_catalog": {
                "models": [
                    {"model_id": mid, **info}
                    for mid, info in sorted(self.foundation_models.missing_from_catalog.items())
                ],
                "profiles": [{"profile_id": pid, "name": name} for pid, name in self.profiles.missing_from_catalog],
            },
            "removed_from_live": {
                "models": list(self.foundation_models.not_available_live),
                "profiles": list(self.profiles.not_available_live),
            },
            "region_drift": [
                {
                    "profile_id": pid,
                    "live_only_regions": drift["live_only"],
                    "catalog_only_regions": drift["catalog_only"],
                }
                for pid, drift in sorted(self.profiles.region_drift.items())
            ],
            "lifecycle_warnings": list(self.lifecycle_warnings),
            "notes": list(self.notes),
        }


def build_drift_report(
    catalog: Catalog,
    live_models_by_region: dict[str, list[dict]],
    live_profiles: dict[str, dict],
    regions_checked: list[str],
    now: datetime | None = None,
) -> DriftReport:
    """Build the full drift report from pre-fetched, pre-parsed live data.

    Catalog CRIS profiles are scoped to the geographies reachable from the
    checked regions (ListInferenceProfiles is regional: querying only
    us-east-1 cannot see EU/APAC profiles, so those are not reported as
    missing).
    """
    checked = set(regions_checked)
    scoped_profiles = {pid: p for pid, p in catalog.profiles.items() if p.source_regions & checked}
    profile_diff = diff_inference_profiles(scoped_profiles, live_profiles)
    fm_diff = diff_foundation_models(catalog, live_models_by_region, checked)

    notes = []
    skipped = len(catalog.profiles) - len(scoped_profiles)
    if skipped:
        notes.append(
            f"{skipped} catalog profile(s) not reachable from the checked regions were skipped "
            "(run with --region in each geography for full coverage)"
        )
    for mid, info in sorted(fm_diff.legacy_not_in_catalog.items()):
        notes.append(f"live model {mid} ({info['name']}) is not in the catalog but looks legacy/on-demand — ignored")
    return DriftReport(
        regions_checked=sorted(checked),
        foundation_models=fm_diff,
        profiles=profile_diff,
        notes=notes,
        lifecycle_warnings=collect_lifecycle_warnings(catalog, live_models_by_region, now=now),
    )
