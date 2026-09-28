# ABOUTME: `gip models check` — compare the hardcoded model catalog against live Bedrock APIs
# ABOUTME: Admin-facing drift visibility for REVIEW.md finding #13 (hardcoded catalog)

"""Models commands — inspect the bundled model catalog and detect drift.

`gip models check` fetches live ListFoundationModels + ListInferenceProfiles
for the profile's regions (or --region) and diffs them against the catalog
hardcoded in models.py. Read-only AWS calls only.

Exit codes:
    0 — catalog in sync with live Bedrock APIs (for the checked regions)
    1 — drift detected (new live models/profiles, removed entries, or
        CRIS destination-region changes)
    2 — check could not run (no credentials, access denied, network)
"""

import concurrent.futures
import json

from cleo.commands.command import Command
from cleo.helpers import option
from rich.console import Console

from governed_inference_platform.catalog_check import (
    DriftReport,
    build_drift_report,
    build_proposals,
    extract_catalog,
    parse_foundation_model_summaries,
    parse_inference_profile_summaries,
    render_models_py_snippet,
    render_overlay_json,
)


class CatalogCheckError(Exception):
    """Raised when live data cannot be fetched; message is user-actionable."""


def _resolve_regions(region_option: str | None) -> list[str]:
    """Determine which regions to check: --region, else profile regions, else us-east-1."""
    if region_option:
        return [region_option]
    try:
        from governed_inference_platform.config import Config

        profile = Config.load().get_profile()
        if profile:
            if getattr(profile, "allowed_bedrock_regions", None):
                return sorted({r for r in profile.allowed_bedrock_regions if "gov" not in r})
            if profile.aws_region:
                return [profile.aws_region]
    except Exception:
        pass  # No config yet — fall back to the primary Bedrock region
    return ["us-east-1"]


def _fetch_region(region: str) -> tuple[str, list[dict], dict[str, dict]]:
    """Fetch and parse ListFoundationModels + ListInferenceProfiles for one region."""
    import boto3
    import botocore.config

    config = botocore.config.Config(connect_timeout=5, read_timeout=15, retries={"max_attempts": 2})
    client = boto3.client("bedrock", region_name=region, config=config)

    resp = client.list_foundation_models(byProvider="Anthropic")
    models = parse_foundation_model_summaries(resp.get("modelSummaries", []))

    profiles: dict[str, dict] = {}
    paginator = client.get_paginator("list_inference_profiles")
    for page in paginator.paginate(typeEquals="SYSTEM_DEFINED", maxResults=100):
        profiles.update(parse_inference_profile_summaries(page.get("inferenceProfileSummaries", [])))
    return region, models, profiles


def fetch_live_data(regions: list[str]) -> tuple[dict[str, list[dict]], dict[str, dict]]:
    """Fetch live Bedrock data for all regions in parallel.

    Returns (live_models_by_region, live_profiles_union).
    Raises CatalogCheckError with an actionable message on credential/access failures.
    """
    import botocore.exceptions

    live_models_by_region: dict[str, list[dict]] = {}
    live_profiles: dict[str, dict] = {}
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(regions))) as pool:
            for region, models, profiles in pool.map(_fetch_region, regions):
                live_models_by_region[region] = models
                for pid, info in profiles.items():
                    merged = live_profiles.setdefault(pid, {**info, "seen_in_regions": []})
                    # Record which queried regions returned the profile — the
                    # observable approximation of its source-region set (used
                    # by --propose; see catalog_check.build_proposals).
                    merged.setdefault("seen_in_regions", []).append(region)
    except botocore.exceptions.NoCredentialsError as e:
        raise CatalogCheckError(
            "No AWS credentials found. Run 'aws sso login' (or set AWS_PROFILE) with an account "
            "that allows bedrock:ListFoundationModels and bedrock:ListInferenceProfiles, then retry."
        ) from e
    except botocore.exceptions.ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("AccessDeniedException", "AccessDenied", "UnauthorizedOperation"):
            raise CatalogCheckError(
                "Access denied calling Bedrock. This check needs the read-only permissions "
                "bedrock:ListFoundationModels and bedrock:ListInferenceProfiles — no model "
                "invocation access is required."
            ) from e
        if code in ("ExpiredToken", "ExpiredTokenException", "InvalidClientTokenId", "UnrecognizedClientException"):
            raise CatalogCheckError("AWS credentials are expired or invalid. Refresh them and retry.") from e
        raise CatalogCheckError(f"Bedrock API error: {e}") from e
    except botocore.exceptions.EndpointConnectionError as e:
        raise CatalogCheckError(
            f"Could not reach the Bedrock endpoint ({e}). Check the region name and network connectivity."
        ) from e
    except botocore.exceptions.BotoCoreError as e:
        raise CatalogCheckError(
            f"Could not set up the AWS client: {e}. Check your AWS profile/credentials configuration."
        ) from e
    return live_models_by_region, live_profiles


def run_check_with_proposals(regions: list[str]) -> tuple[DriftReport, list[dict]]:
    """Fetch live data once, build the drift report plus --propose artifacts."""
    from governed_inference_platform.models import CLAUDE_MODELS

    catalog = extract_catalog(CLAUDE_MODELS)
    live_models_by_region, live_profiles = fetch_live_data(regions)
    report = build_drift_report(catalog, live_models_by_region, live_profiles, regions)
    proposals = build_proposals(catalog, report, live_profiles, regions)
    return report, proposals


def _print_report(report: DriftReport, console: Console) -> None:
    """Human-readable drift report."""
    console.print(f"[bold]Model catalog drift check[/bold] — regions: {', '.join(report.regions_checked)}\n")

    fm = report.foundation_models
    prof = report.profiles

    if fm.missing_from_catalog:
        console.print("[red]New Anthropic models live on Bedrock but missing from the catalog:[/red]")
        for mid, info in sorted(fm.missing_from_catalog.items()):
            name = f" ({info['name']})" if info.get("name") else ""
            console.print(f"  [red]✗[/red] {mid}{name} — live in {', '.join(info['regions'])}")
        console.print("  [dim]→ Update models.py (or upgrade gip) to make these available to users.[/dim]\n")

    if prof.missing_from_catalog:
        console.print("[red]New CRIS inference profiles not in the catalog:[/red]")
        for pid, name in prof.missing_from_catalog:
            suffix = f" ({name})" if name else ""
            console.print(f"  [red]✗[/red] {pid}{suffix}")
        console.print("")

    if fm.not_available_live or prof.not_available_live:
        console.print("[yellow]Catalog entries no longer available live:[/yellow]")
        for mid in fm.not_available_live:
            console.print(f"  [yellow]⚠[/yellow] model {mid}")
        for pid in prof.not_available_live:
            console.print(f"  [yellow]⚠[/yellow] profile {pid}")
        console.print("")

    if prof.region_drift:
        console.print("[yellow]CRIS destination-region drift:[/yellow]")
        for pid, drift in sorted(prof.region_drift.items()):
            if drift["live_only"]:
                console.print(f"  [yellow]⚠[/yellow] {pid}: live routes to {drift['live_only']} (not in catalog)")
            if drift["catalog_only"]:
                console.print(f"  [yellow]⚠[/yellow] {pid}: catalog lists {drift['catalog_only']} (not live)")
        console.print("")

    if report.lifecycle_warnings:
        console.print("[yellow]Model lifecycle (catalog models observed LEGACY live):[/yellow]")
        for warning in report.lifecycle_warnings:
            color = "red" if warning["severity"] == "critical" else "yellow"
            marker = "✗" if warning["severity"] == "critical" else "⚠"
            console.print(f"  [{color}]{marker}[/{color}] {warning['model_id']}: {'; '.join(warning['messages'])}")
        console.print(
            "  [dim]→ Premium magnitude is provider-set and not published in the API — "
            "see the AWS Health Legacy notification. Rotation runbook: assets/docs/RUNBOOKS.md "
            "'Model rotation'.[/dim]\n"
        )

    for note in report.notes:
        console.print(f"[dim]note: {note}[/dim]")

    if report.in_sync:
        console.print("\n[green]✓ Catalog is in sync with live Bedrock APIs for the checked regions.[/green]")
    else:
        console.print(
            "\n[red]✗ Drift detected.[/red] The model catalog is curated in the repo — see "
            "'Keeping the model catalog current' in assets/docs/CLI_REFERENCE.md."
        )


class ModelsCommand(Command):
    """Bare namespace command — shows available models subcommands."""

    name = "models"
    description = "Inspect the bundled model catalog (run 'gip models check')"

    def handle(self) -> int:
        """Show available models subcommands."""
        self.line("")
        self.line("<info>Usage:</info>")
        self.line("  models <subcommand> [options]")
        self.line("")
        self.line("<info>Available subcommands:</info>")
        self.line("  <comment>models check</comment>  Compare the bundled model catalog against live Bedrock APIs")
        self.line("")
        self.line("Run <comment>gip models check --help</comment> for details.")
        return 0


class ModelsCheckCommand(Command):
    name = "models check"
    description = "Detect drift between the bundled model catalog and live Bedrock APIs"
    help = """Compare the model catalog hardcoded in this release against the live
Bedrock ListFoundationModels and ListInferenceProfiles APIs.

Reports:
  - New Anthropic models live on Bedrock but missing from the catalog
  - Catalog models/profiles no longer available live
  - CRIS destination-region drift per geography
  - Lifecycle warnings for catalog models that went LEGACY live
    (legacy date, provider-set premium-pricing window, end-of-life date)

Regions default to the active profile's allowed Bedrock regions.

With <info>--propose</info>, missing models are synthesized into ready-to-paste
artifacts: a models.py entry block (the codegen path — system of record,
ADR-0018) and an `extra_models` profile-overlay JSON block (the additive
escape hatch). Printed to stdout; use <info>--output</info> to also write them
to a proposals file. Proposals never modify any file on their own.

Exit codes: 0 = in sync, 1 = drift detected, 2 = check failed to run.
Suitable for cron/CI:
  <info>gip models check --json</info>

Only read-only AWS calls are made (bedrock:ListFoundationModels,
bedrock:ListInferenceProfiles).
"""

    options = [
        option("region", description="Check a specific region instead of the profile's regions", flag=False),
        option("json", description="Output machine-readable JSON (for automation)"),
        option("propose", description="Emit ready-to-paste catalog entries for models missing from the catalog"),
        option("output", description="Write the --propose artifacts to this file (in addition to stdout)", flag=False),
    ]

    def handle(self) -> int:
        console = Console()
        regions = _resolve_regions(self.option("region"))
        propose = self.option("propose")
        output_path = self.option("output")
        if output_path and not propose:
            console.print("[red]Error:[/red] --output requires --propose.")
            return 2

        try:
            report, proposals = run_check_with_proposals(regions)
        except CatalogCheckError as e:
            if self.option("json"):
                self.line(json.dumps({"error": str(e), "regions_checked": regions}, indent=2))
            else:
                console.print(f"[red]Error:[/red] {e}")
            return 2

        proposal_text = None
        if propose:
            proposal_text = render_models_py_snippet(proposals)
            overlay = render_overlay_json(proposals)
            proposal_text += (
                "\n# --- extra_models profile overlay (additive escape hatch — see "
                "assets/docs/CLI_REFERENCE.md) ---\n" + json.dumps(overlay, indent=2) + "\n"
            )

        if self.option("json"):
            payload = report.to_dict()
            if propose:
                payload["proposals"] = proposals
                payload["proposal_text"] = proposal_text
            self.line(json.dumps(payload, indent=2))
        else:
            _print_report(report, console)
            if propose:
                if proposals:
                    console.print("\n[bold]Proposals (--propose):[/bold]")
                    self.line(proposal_text)
                else:
                    console.print("\n[green]No missing models — nothing to propose.[/green]")

        if propose and output_path:
            from pathlib import Path

            try:
                Path(output_path).write_text(proposal_text, encoding="utf-8")
                if not self.option("json"):
                    console.print(f"[dim]Proposals written to {output_path}[/dim]")
            except OSError as e:
                console.print(f"[red]Error:[/red] could not write proposals file: {e}")
                return 2

        return 0 if report.in_sync else 1
