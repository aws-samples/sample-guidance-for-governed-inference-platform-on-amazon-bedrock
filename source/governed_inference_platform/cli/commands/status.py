# ABOUTME: Status command to show deployment status and usage
# ABOUTME: Displays current state, usage metrics, and health checks

"""Status command - Show deployment status."""

import json
from pathlib import Path
from typing import Any

from cleo.commands.command import Command
from cleo.helpers import option
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from governed_inference_platform.cli.utils.aws import get_stack_outputs
from governed_inference_platform.cli.utils.cloudformation import CloudFormationManager
from governed_inference_platform.cli.utils.display import display_configuration_info, get_configuration_dict
from governed_inference_platform.config import WEBSEARCH_SUPPORTED_REGIONS, Config


def _gateway_readiness_warnings(profile, endpoints: dict[str, Any]) -> list[str]:
    """Return truthful, non-blocking gateway policy/readiness warnings."""
    warnings = []

    if endpoints.get("gateway_source") == "aws-samples-upstream":
        warnings.append(
            "Claude Apps Gateway is the pinned AWS Samples implementation. Run the verified fetch, then review "
            "the upstream README and UPSTREAM.json; GIP does not reinterpret readiness or durability claims."
        )
    elif getattr(profile, "gateway_enabled", False):
        warnings.append(
            "This profile contains legacy GIP gateway fields. New deployments use the pinned AWS Samples CDK; "
            "legacy fields are retained only to identify and remove old stacks."
        )

    if getattr(profile, "web_search_enabled", False):
        missing_outputs = [
            output_name
            for endpoint_key, output_name in (
                ("agentcore_deployment_mode", "DeploymentModeValue"),
                ("agentcore_production_readiness", "ProductionReadiness"),
            )
            if endpoints.get(endpoint_key) is None
        ]
        if missing_outputs:
            warnings.append(
                "AgentCore gateway deployment readiness is unknown/unverified because authoritative stack outputs "
                f"are unavailable: {', '.join(missing_outputs)}."
            )

        deployment_mode = endpoints.get("agentcore_deployment_mode")
        if deployment_mode is not None and deployment_mode != "production":
            warnings.append(
                "AgentCore gateway is using development defaults; production authorization policy gates are not "
                "enforced."
            )

        entitlement_status = endpoints.get("agentcore_entitlement_policy_status")
        if entitlement_status is None:
            warnings.append(
                "AgentCore entitlement/authorization posture is unknown/unverified because authoritative stack "
                "output EntitlementPolicyStatus is unavailable; local profile fields are not deployment evidence."
            )
        elif entitlement_status == "NOT_CONFIGURED":
            if endpoints.get("agentcore_authorizer_type") == "CUSTOM_JWT":
                warnings.append(
                    "AgentCore gateway has no entitlement policy; any JWT accepted by the deployed authorizer may "
                    "invoke its tools."
                )
            else:
                warnings.append(
                    "AgentCore gateway reports no entitlement policy; effective caller scope is unknown/unverified."
                )
        elif entitlement_status == "LOG_ONLY":
            warnings.append("AgentCore entitlement policy is LOG_ONLY and does not block denied decisions.")
        elif entitlement_status == "ENFORCE":
            validation_status = endpoints.get("agentcore_policy_validation_status")
            if validation_status == "ACKNOWLEDGED":
                warnings.append(
                    "AgentCore entitlement is ENFORCE with operator acknowledgement; stack output does not prove a "
                    "live entitlement validation run."
                )
            elif validation_status is None:
                warnings.append(
                    "AgentCore entitlement is ENFORCE, but policy validation acknowledgement is unknown/unverified "
                    "because authoritative stack output PolicyValidationStatus is unavailable."
                )
            else:
                warnings.append(
                    "AgentCore entitlement is ENFORCE without an acknowledged observed LOG_ONLY validation run."
                )
        elif entitlement_status == "IAM_AUTHORIZATION":
            warnings.append("AgentCore uses AWS_IAM; caller policy scope cannot be verified from the gateway stack.")
        else:
            warnings.append(
                f"AgentCore reports unrecognized entitlement status {entitlement_status}; effective entitlement is "
                "unknown/unverified."
            )

    return warnings


class StatusCommand(Command):
    name = "status"
    description = "Show current deployment status and usage metrics"

    options = [
        option("profile", description="Configuration profile to check", flag=False),
        option("json", description="Output in JSON format", flag=True),
        option("detailed", description="Show detailed information", flag=True),
    ]

    def handle(self) -> int:
        """Execute the status command."""
        console = Console()

        # Load configuration
        config = Config.load()

        # Get profile name (use active profile if not specified)
        profile_name = self.option("profile")
        if not profile_name:
            profile_name = config.active_profile

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

        # Get options
        json_output = self.option("json")
        detailed = self.option("detailed")

        if json_output:
            return self._show_json_status(profile, console)
        else:
            return self._show_rich_status(profile, console, detailed)

    def _show_rich_status(self, profile, console: Console, detailed: bool) -> int:
        """Show status in rich formatted output."""
        # Header
        console.print(
            Panel.fit(
                "[bold cyan]Governed Inference Platform - Deployment Status[/bold cyan]",
                border_style="cyan",
                padding=(1, 2),
            )
        )

        # Configuration section
        console.print("\n[bold]Configuration[/bold]")

        # Get endpoints to extract identity pool ID
        endpoints = self._get_endpoints(profile)
        identity_pool_id = endpoints.get("identity_pool_id")

        # Use shared display utility
        display_configuration_info(profile, identity_pool_id, format_type="table")

        # Stack status section
        console.print("\n[bold]Stack Status[/bold]")
        stacks = self._get_stack_status(profile)

        stack_table = Table(box=box.SIMPLE)
        stack_table.add_column("Stack", style="cyan")
        stack_table.add_column("Status")
        stack_table.add_column("Last Updated")

        for stack_type, info in stacks.items():
            status_color = "green" if info["status"] == "CREATE_COMPLETE" else "yellow"
            stack_table.add_row(
                stack_type.title(),
                f"[{status_color}]{info['status']}[/{status_color}]",
                info.get("last_updated", "N/A"),
            )

        console.print(stack_table)

        # Endpoints section
        console.print("\n[bold]Endpoints[/bold]")
        endpoints = self._get_endpoints(profile)

        if endpoints.get("identity_pool_id"):
            console.print(f"• Identity Pool: [cyan]{endpoints['identity_pool_id']}[/cyan]")

        if endpoints.get("role_arn"):
            console.print(f"• Bedrock Role: [cyan]{endpoints['role_arn']}[/cyan]")

        if endpoints.get("oidc_provider"):
            console.print(f"• OIDC Provider: [cyan]{endpoints['oidc_provider']}[/cyan]")

        if profile.monitoring_enabled and endpoints.get("monitoring_endpoint"):
            console.print(f"\n• Monitoring Endpoint: [cyan]{endpoints['monitoring_endpoint']}[/cyan]")
            console.print("  Authentication: [dim]Bearer token (Cognito ID token)[/dim]")
            console.print("  Protocol: [dim]OTLP HTTP/Protobuf[/dim]")

        if endpoints.get("dashboard_url"):
            console.print(f"\n• CloudWatch Dashboard: [cyan]{endpoints['dashboard_url']}[/cyan]")

        if endpoints.get("gateway_url"):
            console.print(f"\n• Claude Apps Gateway: [cyan]{endpoints['gateway_url']}[/cyan]")
            if endpoints.get("gateway_alb_dns"):
                console.print(f"  Internal ALB (private DNS target): [dim]{endpoints['gateway_alb_dns']}[/dim]")
            if endpoints.get("gateway_service_name"):
                console.print(f"  Service: [dim]{endpoints['gateway_service_name']}[/dim]")
            if endpoints.get("gateway_inference_cell_id"):
                console.print(f"  Inference cell: [dim]{endpoints['gateway_inference_cell_id']}[/dim]")

        if endpoints.get("agentcore_gateway_url"):
            console.print(f"\n• AgentCore Gateway: [cyan]{endpoints['agentcore_gateway_url']}[/cyan]")
            if endpoints.get("agentcore_service_name"):
                console.print(f"  Service: [dim]{endpoints['agentcore_service_name']}[/dim]")
            if endpoints.get("agentcore_inference_cell_id"):
                console.print(f"  Inference cell: [dim]{endpoints['agentcore_inference_cell_id']}[/dim]")

        warnings = _gateway_readiness_warnings(profile, endpoints)
        if warnings:
            console.print("\n[bold yellow]Gateway Readiness Warnings[/bold yellow]")
            for warning in warnings:
                console.print(f"- [yellow]{warning}[/yellow]")

        # Package info
        dist_dir = Path.home() / "gip" / "dist"
        if dist_dir.exists():
            console.print("\n[bold]Distribution Package[/bold]")
            console.print(f"• Location: [cyan]{dist_dir}[/cyan]")

            # Check if settings.json exists
            settings_file = dist_dir / ".claude" / "settings.json"
            if settings_file.exists():
                console.print("• Claude Settings: [green]✓ Configured[/green]")
            else:
                console.print("• Claude Settings: [yellow]⚠ Not found[/yellow]")

        # Next steps
        if detailed:
            console.print("\n[bold]Next Steps[/bold]")
            if not dist_dir.exists():
                console.print("1. Run [cyan]poetry run gip package[/cyan] to create distribution")
            console.print("2. Distribute package to users")
            console.print("3. Users run ./install.sh")

            # Show test commands
            console.print("\n[bold]Test Commands[/bold]")
            console.print("• Test authentication: [dim]export AWS_PROFILE=gip && aws sts get-caller-identity[/dim]")
            console.print("• Get monitoring token: [dim]poetry run gip get-monitoring-token[/dim]")

        return 0

    def _show_json_status(self, profile, console: Console) -> int:
        """Show status in JSON format."""
        # Get endpoints to extract identity pool ID
        endpoints = self._get_endpoints(profile)
        identity_pool_id = endpoints.get("identity_pool_id")

        status = {
            "profile": profile.name,
            "configuration": get_configuration_dict(profile, identity_pool_id),
            "stacks": self._get_stack_status(profile),
            "endpoints": endpoints,
            "warnings": _gateway_readiness_warnings(profile, endpoints),
        }

        console.print(json.dumps(status, indent=2))
        return 0

    def _get_stack_status(self, profile) -> dict[str, Any]:
        """Get status of all stacks."""
        stacks = {}

        # Check auth stack (only when SSO is enabled)
        if getattr(profile, "sso_enabled", True):
            auth_stack = profile.stack_names.get("auth", f"{profile.identity_pool_name}-stack")
            auth_status = self._check_stack(auth_stack, profile.aws_region)
            stacks["auth"] = auth_status

        if profile.monitoring_enabled:
            monitoring_mode = getattr(profile, "monitoring_mode", "central")
            if monitoring_mode == "central":
                # Check monitoring stack (ECS)
                monitoring_stack = profile.stack_names.get("monitoring", f"{profile.identity_pool_name}-monitoring")
                stacks["monitoring"] = self._check_stack(monitoring_stack, profile.aws_region)
            else:
                # Sidecar mode: show local collector status
                stacks["monitoring (sidecar)"] = self._check_local_collector()

            # Check dashboard stack (both modes)
            dashboard_stack = profile.stack_names.get("dashboard", f"{profile.identity_pool_name}-dashboard")
            stacks["dashboard"] = self._check_stack(dashboard_stack, profile.aws_region)

        # Report legacy local stacks only so users can migrate and remove them.
        if getattr(profile, "gateway_enabled", False):
            gateway_stack = profile.stack_names.get("gateway", f"{profile.identity_pool_name}-gateway")
            legacy_gateway = self._check_stack(gateway_stack, profile.aws_region)
            if legacy_gateway["status"] != "NOT_FOUND":
                stacks["gateway (legacy GIP stack)"] = legacy_gateway

        if getattr(profile, "web_search_enabled", False):
            websearch_stack = profile.stack_names.get("websearch", f"{profile.identity_pool_name}-websearch")
            websearch_region = getattr(profile, "websearch_region", None) or WEBSEARCH_SUPPORTED_REGIONS[0]
            stacks["websearch gateway"] = self._check_stack(websearch_stack, websearch_region)

        # Check server-side metering stacks (optional, opt-in via metering_enabled;
        # one stack per allowed Bedrock region)
        if getattr(profile, "metering_enabled", False):
            metering_stack = profile.stack_names.get("metering", f"{profile.identity_pool_name}-metering")
            metering_regions = list(getattr(profile, "allowed_bedrock_regions", []) or []) or [profile.aws_region]
            for region in metering_regions:
                stacks[f"metering ({region})"] = self._check_stack(metering_stack, region)

        return stacks

    def _check_stack(self, stack_name: str, region: str) -> dict[str, Any]:
        """Check individual stack status using boto3."""
        cf_manager = CloudFormationManager(region=region)

        try:
            # Get stack details
            response = cf_manager.cf_client.describe_stacks(StackName=stack_name)
            if response["Stacks"]:
                stack = response["Stacks"][0]
                last_updated = stack.get("LastUpdatedTime") or stack.get("CreationTime")

                # Format timestamp if present
                if last_updated:
                    if hasattr(last_updated, "isoformat"):
                        last_updated = last_updated.isoformat()
                    else:
                        last_updated = str(last_updated)

                return {"status": stack["StackStatus"], "last_updated": last_updated}
        except Exception:
            pass

        return {"status": "NOT_FOUND", "last_updated": None}

    def _get_endpoints(self, profile) -> dict[str, Any]:
        """Get all relevant endpoints."""
        endpoints = {}

        # Get auth stack outputs (only when SSO is enabled)
        if getattr(profile, "sso_enabled", True):
            auth_stack = profile.stack_names.get("auth", f"{profile.identity_pool_name}-stack")
            auth_outputs = get_stack_outputs(auth_stack, profile.aws_region)

            if auth_outputs:
                endpoints["identity_pool_id"] = auth_outputs.get("IdentityPoolId")
                endpoints["role_arn"] = auth_outputs.get("FederatedRoleArn") or auth_outputs.get("BedrockRoleArn")
                endpoints["oidc_provider"] = auth_outputs.get("OIDCProviderArn")

        if profile.monitoring_enabled:
            monitoring_mode = getattr(profile, "monitoring_mode", "central")
            if monitoring_mode == "central":
                # Get monitoring endpoint from CloudFormation
                monitoring_stack = profile.stack_names.get("monitoring", f"{profile.identity_pool_name}-otel-collector")
                monitoring_outputs = get_stack_outputs(monitoring_stack, profile.aws_region)
                if monitoring_outputs:
                    endpoints["monitoring_endpoint"] = monitoring_outputs.get("CollectorEndpoint")
            else:
                endpoints["monitoring_endpoint"] = "http://localhost:4318 (sidecar)"

            # Get dashboard URL
            dashboard_stack = profile.stack_names.get("dashboard", f"{profile.identity_pool_name}-dashboard")
            dashboard_outputs = get_stack_outputs(dashboard_stack, profile.aws_region)
            if dashboard_outputs:
                endpoints["dashboard_url"] = dashboard_outputs.get("DashboardURL")

        # Legacy output discovery exists only to support migration and teardown.
        if getattr(profile, "gateway_enabled", False):
            gateway_stack = profile.stack_names.get("gateway", f"{profile.identity_pool_name}-gateway")
            gateway_outputs = get_stack_outputs(gateway_stack, profile.aws_region)
            if gateway_outputs:
                endpoints["gateway_source"] = "legacy-gip"
                gateway_output_map = {
                    "gateway_url": "GatewayUrl",
                    "gateway_alb_dns": "LoadBalancerDNSName",
                    "gateway_service_name": "GatewayServiceName",
                    "gateway_inference_cell_id": "InferenceCellId",
                    "gateway_deployment_mode": "DeploymentModeValue",
                    "gateway_production_readiness": "ProductionReadiness",
                    "gateway_image_immutability": "GatewayImageImmutability",
                    "gateway_database_multi_az": "DatabaseMultiAz",
                    "gateway_database_deletion_protection": "DatabaseDeletionProtection",
                    "gateway_database_readiness_signal": "DatabaseReadinessSignal",
                    "gateway_spend_enforcement_failure_mode": "SpendEnforcementFailureMode",
                    "gateway_inference_profile_prefix": "InferenceProfilePrefixValue",
                }
                for endpoint_key, output_key in gateway_output_map.items():
                    if gateway_outputs.get(output_key) is not None:
                        endpoints[endpoint_key] = gateway_outputs[output_key]

        if getattr(profile, "web_search_enabled", False):
            websearch_stack = profile.stack_names.get("websearch", f"{profile.identity_pool_name}-websearch")
            websearch_region = getattr(profile, "websearch_region", None) or WEBSEARCH_SUPPORTED_REGIONS[0]
            websearch_outputs = get_stack_outputs(websearch_stack, websearch_region)
            if websearch_outputs:
                agentcore_output_map = {
                    "agentcore_gateway_url": "GatewayMcpEndpoint",
                    "agentcore_service_name": "GatewayServiceName",
                    "agentcore_inference_cell_id": "InferenceCellId",
                    "agentcore_deployment_mode": "DeploymentModeValue",
                    "agentcore_entitlement_policy_status": "EntitlementPolicyStatus",
                    "agentcore_policy_validation_status": "PolicyValidationStatus",
                    "agentcore_production_readiness": "ProductionReadiness",
                    "agentcore_authorizer_type": "AuthorizerType",
                }
                for endpoint_key, output_key in agentcore_output_map.items():
                    if websearch_outputs.get(output_key) is not None:
                        endpoints[endpoint_key] = websearch_outputs[output_key]

        return endpoints

    def _check_local_collector(self) -> dict[str, Any]:
        """Check local sidecar collector status."""
        install_dir = Path.home() / "gip"
        binary = install_dir / "otelcol"
        pid_file = install_dir / "collector.pid"

        if not binary.exists():
            return {"status": "NOT_INSTALLED", "last_updated": None}

        if pid_file.exists():
            try:
                import os
                import signal

                pid = int(pid_file.read_text().strip())
                os.kill(pid, signal.SIG_DFL)  # check if process exists
                return {"status": "RUNNING", "last_updated": f"PID {pid}"}
            except (ProcessLookupError, ValueError, OSError):
                return {"status": "INSTALLED (not running)", "last_updated": None}

        return {"status": "INSTALLED (not running)", "last_updated": None}
