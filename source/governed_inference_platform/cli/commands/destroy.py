# ABOUTME: Destroy command for cleaning up AWS resources
# ABOUTME: Safely removes deployed stacks and configurations

"""Destroy command - Remove deployed infrastructure."""

import os

from cleo.commands.command import Command
from cleo.helpers import argument, option
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm

from governed_inference_platform.cli.utils.cloudformation import CloudFormationManager
from governed_inference_platform.cli.utils.helpers import clear_cached_credentials, get_codebuild_region
from governed_inference_platform.config import WEBSEARCH_SUPPORTED_REGIONS, Config

# All destroyable stacks in reverse dependency order (destroy-all uses this sequence).
# Leaf stacks (no dependents) go first; foundational stacks (auth) go last.
# New stacks: add before 'codebuild' if they have no cross-stack dependencies.
# Keep in sync with VALID_STACKS in deploy.py.
DESTROYABLE_STACKS = [
    "skills",  # Leaf: skills registry (S3 + Agent Registry custom resource), no dependents
    "bootstrap",  # Legacy GIP stack cleanup only; new deployments use the upstream CDK add-on
    "memory",  # Leaf: AgentCore memory target on the websearch gateway — must go before websearch; cross-region
    "websearch",  # Leaf: AgentCore gateway, no dependents, may be cross-region
    "gateway",  # Legacy GIP stack cleanup only; upstream ClaudeGatewayStack uses its own CDK lifecycle
    "guardrails",  # Leaf: regional account-level Bedrock Guardrails enforcement
    "model-lifecycle",  # Leaf: lifecycle alerts (Lambda + SSM + optional topic); may reference quota's topic, so destroy before quota
    "codebuild",  # Leaf: build pipeline, may be cross-region
    "metering",  # Leaf: per-Bedrock-region metering stacks; must go before quota (writes to its table)
    "analytics",
    "quota",
    "cowork-dashboard",
    "dashboard",
    "monitoring",
    "distribution",
    "networking",
    "s3bucket",
    "auth",  # Foundation: destroy last (other stacks reference its outputs)
]


def _matches_legacy_stack_template(stack_type: str, template_body) -> bool:
    """Return whether a live template has the retired GIP stack signature."""
    if isinstance(template_body, str):
        if stack_type == "gateway":
            return all(
                marker in template_body for marker in ("GatewayImage:", "SpendEnforcementPosture:", "JwtSecret:")
            )
        if stack_type == "bootstrap":
            device_code = all(marker in template_body for marker in ("DeviceCodeTable:", "BootstrapHandlerFunction:"))
            bearer = all(marker in template_body for marker in ("DefaultInferenceModels:", "BootstrapFunction:"))
            return device_code or bearer
        return False

    if not isinstance(template_body, dict):
        return False
    parameters = set(template_body.get("Parameters", {}))
    resources = set(template_body.get("Resources", {}))
    if stack_type == "gateway":
        return {"GatewayImage", "SpendEnforcementPosture"} <= parameters and {"Database", "LoadBalancer"} <= resources
    if stack_type == "bootstrap":
        device_code = {"OidcClientSecretArn", "OidcTokenEndpoint"} <= parameters and {
            "DeviceCodeTable",
            "BootstrapHandlerFunction",
        } <= resources
        bearer = {"DefaultInferenceModels", "OtlpIdentityMode"} <= parameters and {
            "BootstrapFunction",
            "BootstrapApi",
        } <= resources
        return device_code or bearer
    return False


def _partition_family(region: str) -> str:
    if region.startswith("us-gov"):
        return "gov"
    if region.startswith("cn-"):
        return "china"
    return "commercial"


def _guardrails_regions(profile) -> list[str]:
    from governed_inference_platform.models import expand_region_sentinels, get_all_bedrock_regions

    regions = expand_region_sentinels(list(getattr(profile, "allowed_bedrock_regions", []) or []))
    if not regions:
        regions = get_all_bedrock_regions()
    family = _partition_family(profile.aws_region)
    filtered = [r for r in regions if _partition_family(r) == family]
    return filtered or [profile.aws_region]


def _guardrails_cleanup_regions(profile, enabled_regions: list[str] | None = None) -> list[str]:
    current_regions = _guardrails_regions(profile)
    explicit = [r.strip() for r in os.getenv("GIP_GUARDRAILS_CLEANUP_REGIONS", "").split(",") if r.strip()]
    if enabled_regions is None:
        try:
            manager = CloudFormationManager(region=profile.aws_region)
            response = manager.session.client("ec2").describe_regions(AllRegions=False)
            enabled_regions = [r["RegionName"] for r in response.get("Regions", []) if r.get("RegionName")]
        except Exception:
            enabled_regions = []
    family = _partition_family(profile.aws_region)
    enabled = [r for r in enabled_regions if _partition_family(r) == family]
    return list(dict.fromkeys([*current_regions, *enabled, *explicit]))


def _artifact_regions(profile) -> list[str]:
    regions = [profile.aws_region]
    if getattr(profile, "metering_enabled", False):
        regions.extend(list(getattr(profile, "allowed_bedrock_regions", []) or []))
    return list(dict.fromkeys(regions))


class DestroyCommand(Command):
    name = "destroy"
    description = "Remove deployed AWS infrastructure"

    arguments = [
        argument(
            "stack",
            description=f"Specific stack to destroy ({'/'.join(DESTROYABLE_STACKS)})",
            optional=True,
        )
    ]

    options = [
        option("profile", description="Configuration profile to use", flag=False),
        option("force", description="Skip confirmation prompts", flag=True),
    ]

    def handle(self) -> int:
        """Execute the destroy command."""
        console = Console()

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

        # Determine which stacks to destroy
        stack_arg = self.argument("stack")
        force = self.option("force")

        stacks_to_destroy = []
        if stack_arg:
            if stack_arg in DESTROYABLE_STACKS:
                stacks_to_destroy.append(stack_arg)
            else:
                console.print(f"[red]Unknown stack: {stack_arg}[/red]")
                console.print(f"Valid stacks: {', '.join(DESTROYABLE_STACKS)}")
                return 1
        else:
            # Destroy all stacks in reverse dependency order
            stacks_to_destroy = list(DESTROYABLE_STACKS)

        # Show what will be destroyed
        console.print(
            Panel.fit(
                "[bold red]⚠️  Infrastructure Destruction Warning[/bold red]\n\n"
                "This will permanently delete the following AWS resources:",
                border_style="red",
                padding=(1, 2),
            )
        )

        for stack in stacks_to_destroy:
            stack_name = profile.stack_names.get(stack, f"{profile.identity_pool_name}-{stack}")
            console.print(f"• {stack.capitalize()} stack: [cyan]{stack_name}[/cyan]")

        console.print("\n[yellow]Note: Some resources may require manual cleanup:[/yellow]")
        console.print("• CloudWatch LogGroups (/ecs/otel-collector, /aws/gip/metrics)")
        console.print("• S3 Buckets and Athena resources created by analytics stack")
        console.print("• Any custom resources created outside of CloudFormation")

        # Confirm destruction
        if not force:
            if not Confirm.ask("\n[bold red]Are you sure you want to destroy these resources?[/bold red]"):
                console.print("\n[yellow]Destruction cancelled.[/yellow]")
                return 0

        # Destroy stacks
        console.print("\n[bold]Destroying stacks...[/bold]\n")

        all_failed_resources = []  # Collect failed resources from all stacks
        all_retained_resources = []  # Collect intentionally retained resources
        stacks_with_failures = []

        for stack in stacks_to_destroy:
            if stack == "monitoring" and not profile.monitoring_enabled:
                continue
            if stack == "dashboard" and not profile.monitoring_enabled:
                continue
            if stack == "networking" and not profile.monitoring_enabled:
                continue
            if stack == "analytics" and not profile.monitoring_enabled:
                continue
            if (
                stack == "s3bucket"
                and not profile.monitoring_enabled
                and not getattr(profile, "metering_enabled", False)
            ):
                continue
            # Skip ECS-related stacks in sidecar mode
            monitoring_mode = getattr(profile, "monitoring_mode", "central")
            if monitoring_mode == "sidecar" and stack in ("networking", "monitoring", "analytics", "s3bucket"):
                continue
            if stack == "quota" and not getattr(profile, "quota_monitoring_enabled", False):
                continue
            if stack == "metering" and not getattr(profile, "metering_enabled", False):
                continue
            if stack == "model-lifecycle" and not getattr(profile, "model_lifecycle_enabled", False):
                continue
            if stack == "distribution" and not getattr(profile, "enable_distribution", False):
                continue
            if stack == "codebuild" and not getattr(profile, "enable_codebuild", False):
                continue
            if stack == "websearch" and not getattr(profile, "web_search_enabled", False):
                continue
            if stack == "memory" and not getattr(profile, "memory_enabled", False):
                continue
            if stack == "gateway" and not stack_arg and not getattr(profile, "gateway_enabled", False):
                continue
            if stack == "guardrails" and not stack_arg and getattr(profile, "guardrails_enabled", False) is not True:
                continue

            stack_name = profile.stack_names.get(stack, f"{profile.identity_pool_name}-{stack}")
            if stack in ("gateway", "bootstrap"):
                console.print(
                    f"[yellow]Legacy cleanup only:[/yellow] deleting old GIP {stack} stack '{stack_name}'. "
                    "AWS Samples CDK stacks and build artifacts are managed with the vendored upstream instructions."
                )
                if stack == "gateway":
                    console.print(
                        "[yellow]The legacy gateway database uses snapshot-on-delete. Record stack outputs and "
                        "confirm the resulting RDS snapshot before removing migration rollback state.[/yellow]"
                    )

            # Server-side metering deploys one stack per allowed Bedrock region;
            # destroy each regional copy where it lives (cross-region like
            # codebuild/websearch, but N regions instead of one).
            if stack == "metering":
                metering_regions = list(getattr(profile, "allowed_bedrock_regions", []) or []) or [profile.aws_region]
                for metering_region in metering_regions:
                    console.print(f"Destroying metering stack in {metering_region}: [cyan]{stack_name}[/cyan]")
                    result = self._delete_stack(stack_name, metering_region, console)
                    if result != 0:
                        stacks_with_failures.append(f"{stack_name} ({metering_region})")
                        failed = self._get_failed_resources(stack_name, metering_region)
                        if failed:
                            all_failed_resources.extend(failed)
                        console.print(f"[yellow]⚠ Metering stack in {metering_region} needs manual cleanup[/yellow]\n")
                    else:
                        console.print(f"[green]✓ Metering stack in {metering_region} destroyed[/green]\n")
                continue

            if stack == "guardrails":
                current_guardrails_regions = set(_guardrails_regions(profile))
                for guardrails_region in _guardrails_cleanup_regions(profile):
                    console.print(f"Destroying guardrails stack in {guardrails_region}: [cyan]{stack_name}[/cyan]")
                    try:
                        result = self._delete_stack(stack_name, guardrails_region, console)
                    except Exception as e:
                        console.print(f"[yellow]Could not check guardrails stack in {guardrails_region}: {e}[/yellow]")
                        if guardrails_region in current_guardrails_regions:
                            stacks_with_failures.append(f"{stack_name} ({guardrails_region})")
                        continue
                    if result != 0:
                        stacks_with_failures.append(f"{stack_name} ({guardrails_region})")
                        failed = self._get_failed_resources(stack_name, guardrails_region)
                        if failed:
                            all_failed_resources.extend(failed)
                        console.print(
                            f"[yellow]⚠ Guardrails stack in {guardrails_region} needs manual cleanup[/yellow]\n"
                        )
                    else:
                        console.print(f"[green]✓ Guardrails stack in {guardrails_region} destroyed[/green]\n")
                continue

            if stack == "s3bucket":
                # F-030: Metering can create artifact stacks outside the home region.
                for artifact_region in _artifact_regions(profile):
                    console.print(f"Destroying artifacts stack in {artifact_region}: [cyan]{stack_name}[/cyan]")
                    result = self._delete_stack(stack_name, artifact_region, console)
                    if result != 0:
                        stacks_with_failures.append(f"{stack_name} ({artifact_region})")
                        failed = self._get_failed_resources(stack_name, artifact_region)
                        if failed:
                            all_failed_resources.extend(failed)
                        console.print(f"[yellow]⚠ Artifacts stack in {artifact_region} needs manual cleanup[/yellow]\n")
                    else:
                        console.print(f"[green]✓ Artifacts stack in {artifact_region} destroyed[/green]\n")
                continue

            # CodeBuild may have been deployed cross-region (Windows container fleet
            # isn't in every region); delete it where it actually lives, or it's
            # silently orphaned in the build region while the destroy reports success.
            # Web search likewise deploys into us-east-1 (managed connector region);
            # memory attaches to that gateway and lives in the same region.
            if stack == "codebuild":
                stack_region = get_codebuild_region(profile)
            elif stack in ("websearch", "memory"):
                stack_region = getattr(profile, "websearch_region", None) or WEBSEARCH_SUPPORTED_REGIONS[0]
            else:
                stack_region = profile.aws_region
            console.print(f"Destroying {stack} stack: [cyan]{stack_name}[/cyan]")

            self._legacy_stack_type = stack if stack in ("gateway", "bootstrap") else None
            try:
                result = self._delete_stack(stack_name, stack_region, console)
            finally:
                self._legacy_stack_type = None
            if result != 0:
                # Don't break - record the failure and continue with remaining stacks.
                # Always track the stack: a non-zero result means it did not delete cleanly.
                # Enumerable DELETE_FAILED resources may be empty (e.g. a real delete error,
                # or a client-side timeout while resources are still DELETE_IN_PROGRESS), and
                # the summary must not report overall success in that case.
                stacks_with_failures.append(stack_name)
                failed = self._get_failed_resources(stack_name, stack_region)
                if failed:
                    all_failed_resources.extend(failed)
                    console.print(f"[yellow]⚠ {stack.capitalize()} stack — failed resources:[/yellow]")
                    for r in failed:
                        console.print(f"    • {r['logical_id']} ({r['resource_type']}): {r['physical_id']}")
                else:
                    console.print(
                        f"[yellow]⚠ {stack.capitalize()} stack has resources requiring manual cleanup[/yellow]"
                    )
                console.print()
            else:
                # Check for silently retained resources (DeletionPolicy: Retain)
                retained = self._get_retained_resources(stack_name, stack_region)
                if retained:
                    all_retained_resources.extend(retained)
                    console.print(f"[yellow]ℹ {stack.capitalize()} stack — retained resources (by policy):[/yellow]")
                    for r in retained:
                        console.print(f"    • {r['logical_id']} ({r['resource_type']}): {r['physical_id']}")
                    console.print()
                else:
                    console.print(f"[green]✓ {stack.capitalize()} stack destroyed[/green]\n")

        # Clean up CodeBuild stacks left in regions the build region was moved away
        # from (cross-region was reconfigured via re-init). Without this they're
        # orphaned: the loop above only deletes in the *current* codebuild region.
        # NOT gated on enable_codebuild — disabling CodeBuild (init "Skip") is
        # exactly when an old cross-region stack needs cleaning, and the main loop
        # skips codebuild when it's disabled. prior_regions is only populated when
        # there's an orphan to clean, so iterating it unconditionally is safe.
        current_cb_region = get_codebuild_region(profile)
        cb_stack_name = profile.stack_names.get("codebuild", f"{profile.identity_pool_name}-codebuild")
        for prior in getattr(profile, "codebuild_prior_regions", []) or []:
            if prior == current_cb_region:
                continue  # already handled by the main loop (if enabled)
            console.print(f"Destroying orphaned CodeBuild stack in [cyan]{prior}[/cyan]: {cb_stack_name}")
            if self._delete_stack(cb_stack_name, prior, console) == 0:
                console.print(f"[green]✓ CodeBuild stack in {prior} destroyed[/green]\n")
            else:
                failed = self._get_failed_resources(cb_stack_name, prior)
                if failed:
                    all_failed_resources.extend(failed)
                    stacks_with_failures.append(f"{cb_stack_name} ({prior})")
                console.print(f"[yellow]⚠ CodeBuild stack in {prior} needs manual cleanup[/yellow]\n")

        # Clean up cached credentials for this profile
        if clear_cached_credentials(profile_name):
            console.print(f"[green]✓ Cleared cached credentials for profile '{profile_name}'[/green]")

        # Show cleanup summary at the end
        self._show_cleanup_summary(all_failed_resources, all_retained_resources, stacks_with_failures, profile, console)

        # Exit non-zero when any stack didn't delete cleanly so scripts/CI can
        # fail fast on a broken teardown. Matches deploy/package, which already
        # return 1 on failure. The summary above still surfaces what to clean up.
        return 1 if stacks_with_failures else 0

    def _delete_stack(self, stack_name: str, region: str, console: Console) -> int:
        """Delete a CloudFormation stack using boto3.

        Returns:
            0: Success (stack deleted or doesn't exist)
            1: Partial success (DELETE_FAILED - some resources need manual cleanup)
            2: Actual error (permissions, network, etc.)
        """
        cf_manager = CloudFormationManager(region=region)

        # Check if stack exists
        status = cf_manager.get_stack_status(stack_name)
        if not status:
            console.print(f"[yellow]Stack {stack_name} not found or already deleted[/yellow]")
            return 0

        legacy_stack_type = getattr(self, "_legacy_stack_type", None)
        if legacy_stack_type:
            try:
                template_body = cf_manager.cf_client.get_template(StackName=stack_name)["TemplateBody"]
            except Exception as error:
                console.print(f"[red]Refusing legacy cleanup: could not verify stack origin: {error}[/red]")
                return 2
            if not _matches_legacy_stack_template(legacy_stack_type, template_body):
                console.print(
                    "[red]Refusing cleanup: the live stack does not match a retired GIP template. "
                    "Use the owning deployment's teardown procedure.[/red]"
                )
                return 2

        # If already in DELETE_FAILED, pre-clean and retry
        if status == "DELETE_FAILED":
            console.print(f"[yellow]Stack {stack_name} is in DELETE_FAILED state, retrying after cleanup...[/yellow]")

        # Pre-clean resources that block deletion (non-empty S3 buckets, Athena workgroups)
        cf_manager.pre_cleanup_stack(
            stack_name,
            on_event=lambda msg: console.print(f"  [dim]{msg}[/dim]"),
        )

        # Use progress indicator
        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console
        ) as progress:
            task = progress.add_task(f"Deleting stack {stack_name}...", total=None)

            # Delete the stack with event tracking
            result = cf_manager.delete_stack(
                stack_name=stack_name,
                force=True,
                on_event=lambda e: progress.update(
                    task, description=f"Deleting {e.get('LogicalResourceId', stack_name)}..."
                ),
                timeout=300,
            )

            progress.update(task, completed=True)

            if result.success:
                return 0

            # Check if it ended up in DELETE_FAILED (some resources retained)
            new_status = cf_manager.get_stack_status(stack_name)
            if new_status == "DELETE_FAILED":
                return 1  # Not an error, just needs manual cleanup

            # Actual error
            console.print(f"[red]Error deleting stack: {result.error}[/red]")
            return 2

    def _get_failed_resources(self, stack_name: str, region: str) -> list[dict]:
        """Get list of resources that failed to delete from a stack."""
        cf_manager = CloudFormationManager(region=region)
        return cf_manager.get_failed_resources(stack_name)

    def _get_retained_resources(self, stack_name: str, region: str) -> list[dict]:
        """Get resources silently retained (DeletionPolicy: Retain) during deletion."""
        cf_manager = CloudFormationManager(region=region)
        return cf_manager.get_retained_resources(stack_name)

    def _show_cleanup_summary(
        self,
        failed_resources: list[dict],
        retained_resources: list[dict],
        stacks: list[str],
        profile,
        console: Console,
    ) -> None:
        """Show cleanup instructions for failed and retained resources."""
        if not failed_resources and not retained_resources and not stacks:
            console.print("\n[green]✓ All stacks destroyed successfully![/green]")
            return

        if retained_resources:
            console.print("\n[bold]Retained resources (DeletionPolicy: Retain):[/bold]")
            console.print("[dim]These were kept intentionally. Delete manually if no longer needed:[/dim]\n")
            for r in retained_resources:
                console.print(f"  • {r['logical_id']} ({r['resource_type']}): {r['physical_id']}")
            console.print()

        if not failed_resources:
            # Stacks failed to delete but no DELETE_FAILED resources are enumerable
            # (real delete error, or a client-side timeout mid-delete). Don't claim
            # success. Point the user at the affected stacks to re-run / verify.
            if stacks:
                region = profile.aws_region
                console.print("\n[yellow]⚠ The following stacks did not delete cleanly:[/yellow]")
                for stack in stacks:
                    console.print(f"  • {stack}")
                    console.print(
                        f"    [cyan]aws cloudformation delete-stack --stack-name {stack} --region {region}[/cyan]"
                    )
                console.print(
                    "\n[dim]A delete may still be in progress - re-run "
                    "[cyan]gip destroy[/cyan] or check the CloudFormation console to confirm.[/dim]"
                )
            return

        console.print("\n[yellow]⚠ Manual cleanup required for the following resources:[/yellow]\n")

        # Group by resource type for organized output
        by_type: dict[str, list[dict]] = {}
        for r in failed_resources:
            rtype = r["resource_type"]
            if rtype not in by_type:
                by_type[rtype] = []
            by_type[rtype].append(r)

        region = profile.aws_region

        # S3 Buckets
        if "AWS::S3::Bucket" in by_type:
            console.print("[bold]S3 Buckets (must be emptied first):[/bold]")
            for r in by_type["AWS::S3::Bucket"]:
                bucket = r["physical_id"]
                console.print(f"  • {bucket}")
                console.print(f"    [cyan]aws s3 rm s3://{bucket} --recursive[/cyan]")
                console.print(f"    [cyan]aws s3 rb s3://{bucket}[/cyan]")
            console.print()

        # CloudWatch Log Groups
        if "AWS::Logs::LogGroup" in by_type:
            console.print("[bold]CloudWatch Log Groups:[/bold]")
            for r in by_type["AWS::Logs::LogGroup"]:
                log_group = r["physical_id"]
                console.print(f"  • {log_group}")
                console.print(
                    f"    [cyan]aws logs delete-log-group --log-group-name {log_group} --region {region}[/cyan]"
                )
            console.print()

        # DynamoDB Tables
        if "AWS::DynamoDB::Table" in by_type:
            console.print("[bold]DynamoDB Tables:[/bold]")
            for r in by_type["AWS::DynamoDB::Table"]:
                table = r["physical_id"]
                console.print(f"  • {table}")
                console.print(f"    [cyan]aws dynamodb delete-table --table-name {table} --region {region}[/cyan]")
            console.print()

        # ECR Repositories
        if "AWS::ECR::Repository" in by_type:
            console.print("[bold]ECR Repositories (must delete images first):[/bold]")
            for r in by_type["AWS::ECR::Repository"]:
                repo = r["physical_id"]
                console.print(f"  • {repo}")
                console.print(
                    f"    [cyan]aws ecr delete-repository --repository-name {repo} --force --region {region}[/cyan]"
                )
            console.print()

        # Other resources
        known_types = ["AWS::S3::Bucket", "AWS::Logs::LogGroup", "AWS::DynamoDB::Table", "AWS::ECR::Repository"]
        other_types = [t for t in by_type if t not in known_types]
        if other_types:
            console.print("[bold]Other Resources:[/bold]")
            for rtype in other_types:
                for r in by_type[rtype]:
                    console.print(f"  • {r['logical_id']} ({rtype}): {r['physical_id']}")
                    console.print(f"    Reason: {r['status_reason']}")
            console.print()

        # Final instructions
        if stacks:
            console.print("[yellow]After manual cleanup, delete the failed stacks:[/yellow]")
            for stack in stacks:
                console.print(f"  [cyan]aws cloudformation delete-stack --stack-name {stack} --region {region}[/cyan]")
            console.print()

        console.print("For more information, see: assets/docs/TROUBLESHOOTING.md")
