# ABOUTME: Distribute command for sharing packages via presigned URLs or landing page
# ABOUTME: Supports dual distribution platforms: presigned-s3 and landing-page

"""Distribute command - Share packages via secure presigned URLs or authenticated landing page."""

import hashlib
import json
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError
from cleo.commands.command import Command
from cleo.helpers import option
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, DownloadColumn, Progress, SpinnerColumn, TextColumn, TimeRemainingColumn

from governed_inference_platform.cli.utils.aws import get_stack_outputs
from governed_inference_platform.cli.utils.helpers import get_codebuild_region
from governed_inference_platform.config import Config


class S3UploadProgress:
    """Track S3 upload progress."""

    def __init__(self, filename, size, progress_bar):
        self._seen_so_far = 0
        self._progress_bar = progress_bar
        self._lock = threading.Lock()
        self._task_id = None

    def set_task_id(self, task_id):
        """Set the progress bar task ID."""
        self._task_id = task_id

    def __call__(self, bytes_amount):
        """Called by boto3 during upload."""
        with self._lock:
            self._seen_so_far += bytes_amount
            if self._task_id is not None:
                self._progress_bar.update(self._task_id, completed=self._seen_so_far)


class DistributeCommand(Command):
    """
    Distribute built packages via secure presigned URLs

    This command enables IT administrators to share packages
    with developers without requiring AWS credentials.
    """

    name = "distribute"
    description = "Distribute packages via secure presigned URLs"

    # High multipart threshold to avoid temp file creation on Windows.
    # Corporate security tools (Zscaler, etc.) can lock temp files during scanning,
    # causing "access denied" errors. Typical packages are ~150MB, so 256MB threshold
    # ensures single PUT upload with no temp files.
    S3_TRANSFER_CONFIG = TransferConfig(
        multipart_threshold=1024 * 1024 * 256,  # 256MB
        max_concurrency=4,
        multipart_chunksize=1024 * 1024 * 64,  # 64MB chunks if multipart is needed
        use_threads=True,
    )

    # Role-mode presigned URLs cannot outlive the assumed-role session.
    # The stack's presign role allows 12h sessions (MaxSessionDuration 43200);
    # STS further caps role-chained sessions at 1 hour.
    ROLE_PRESIGN_MAX_HOURS = 12
    ALLOWED_IPS_UNSUPPORTED = (
        "--allowed-ips is not enforced by S3 presigned URLs. "
        "Distribution stopped before creating or uploading a package."
    )
    BUILD_TIMESTAMP_FORMAT = "%Y-%m-%d-%H%M%S"
    PUBLISH_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"

    options = [
        option("expires-hours", description="URL expiration time in hours (1-168)", flag=False, default="48"),
        option("get-latest", description="Retrieve the latest distribution URL", flag=True),
        option(
            "allowed-ips",
            description="Unsupported; fails closed because presigned URLs do not enforce IPs",
            flag=False,
        ),
        option("package-path", description="Path to package directory", flag=False, default="dist"),
        option("profile", description="Configuration profile to use", flag=False),
        option("show-qr", description="Display QR code for URL (requires qrcode library)", flag=True),
        option("build-profile", description="Select build by profile name", flag=False),
        option("timestamp", description="Select build by timestamp (YYYY-MM-DD-HHMMSS)", flag=False),
        option("latest", description="Auto-select latest build without wizard", flag=True),
        option("per-os", description="Create separate packages per OS (smaller downloads)", flag=True),
    ]

    def _check_old_flat_structure(self, dist_dir: Path) -> bool:
        """Check if old flat directory structure exists."""
        if not dist_dir.exists():
            return False

        # Look for files that would be in old structure (credential-process binaries)
        old_files = [
            "credential-process-macos-arm64",
            "credential-process-macos-intel",
            "credential-process-linux-x64",
            "credential-process-linux-arm64",
            "credential-process-windows.exe",
            "config.json",
            "install.sh",
        ]

        # If any of these files exist directly in dist/, it's old structure
        for filename in old_files:
            if (dist_dir / filename).exists():
                return True

        return False

    @staticmethod
    def _parse_utc_timestamp(value: object, timestamp_format: str, label: str) -> datetime:
        """Parse a strict timestamp and attach UTC."""
        if not isinstance(value, str):
            raise ValueError(f"{label} must be a string")
        try:
            parsed = datetime.strptime(value, timestamp_format)
        except ValueError as error:
            raise ValueError(f"Malformed {label}: {value!r}") from error
        if parsed.strftime(timestamp_format) != value:
            raise ValueError(f"Malformed {label}: {value!r}")
        return parsed.replace(tzinfo=timezone.utc)

    @classmethod
    def _parse_build_timestamp(cls, value: str) -> datetime:
        """Parse a package-directory timestamp as UTC."""
        return cls._parse_utc_timestamp(value, cls.BUILD_TIMESTAMP_FORMAT, "build timestamp")

    @staticmethod
    def _parse_aware_iso_timestamp(value: object, label: str) -> datetime:
        """Parse an ISO timestamp and reject values without a timezone."""
        if not isinstance(value, str):
            raise ValueError(f"{label} must be a string")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"Malformed {label}: {value!r}") from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{label} must include a timezone: {value!r}")
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _build_distribution_record(
        cls,
        published_at: datetime,
        expires_hours: int,
        url: str,
        checksum: str,
    ) -> dict:
        """Build the latest-distribution JSON from one UTC instant."""
        if published_at.tzinfo is None or published_at.utcoffset() is None:
            raise ValueError("Publish timestamp must include a timezone")
        published_at = published_at.astimezone(timezone.utc).replace(microsecond=0)
        timestamp = published_at.strftime(cls.PUBLISH_TIMESTAMP_FORMAT)
        filename = f"gip-package-{timestamp}.zip"
        return {
            "url": url,
            "expires": (published_at + timedelta(hours=expires_hours)).isoformat(),
            "package_key": f"packages/{timestamp}/{filename}",
            "checksum": checksum,
            "filename": filename,
            "timestamp": timestamp,
            "created": published_at.isoformat(),
        }

    @classmethod
    def _validate_distribution_record(cls, distribution: dict) -> None:
        """Reject malformed or internally inconsistent latest-distribution JSON."""
        if not isinstance(distribution, dict):
            raise ValueError("Distribution record must be a JSON object")
        timestamp = distribution.get("timestamp")
        timestamp_value = cls._parse_utc_timestamp(timestamp, cls.PUBLISH_TIMESTAMP_FORMAT, "distribution timestamp")
        created = cls._parse_aware_iso_timestamp(distribution.get("created"), "distribution created timestamp")
        expires = cls._parse_aware_iso_timestamp(distribution.get("expires"), "distribution expiration timestamp")
        if created != timestamp_value:
            raise ValueError("Distribution timestamp does not match distribution JSON created timestamp")
        expected_filename = f"gip-package-{timestamp}.zip"
        expected_key = f"packages/{timestamp}/{expected_filename}"
        if distribution.get("filename") != expected_filename or distribution.get("package_key") != expected_key:
            raise ValueError("Distribution timestamp does not match package filename or key")
        if expires <= created:
            raise ValueError("Distribution expiration must be after its creation timestamp")

    def _scan_distributions(self, dist_dir: Path) -> dict:
        """Scan dist/ for organized profile/timestamp builds."""
        builds = {}

        if not dist_dir.exists():
            return builds

        # Iterate through profile directories
        for profile_dir in sorted(dist_dir.iterdir()):
            if not profile_dir.is_dir():
                continue

            profile_name = profile_dir.name
            builds[profile_name] = []

            # Iterate through timestamp directories
            for timestamp_dir in sorted(profile_dir.iterdir(), reverse=True):  # Most recent first
                if not timestamp_dir.is_dir():
                    continue

                try:
                    self._parse_build_timestamp(timestamp_dir.name)
                except ValueError:
                    continue

                # Check if this looks like a valid package directory (has config files)
                has_config = (timestamp_dir / "config.json").exists()
                has_install = (timestamp_dir / "install.sh").exists() or (timestamp_dir / "install.bat").exists()

                if not (has_config or has_install):
                    # Not a package directory, skip
                    continue

                # Detect platforms (may be empty for CodeBuild-only packages)
                platforms = self._detect_platforms(timestamp_dir)

                # Calculate size
                size = sum(f.stat().st_size for f in timestamp_dir.rglob("*") if f.is_file())

                builds[profile_name].append(
                    {
                        "timestamp": timestamp_dir.name,
                        "path": timestamp_dir,
                        "platforms": platforms,
                        "size": size,
                    }
                )

        return builds

    def _detect_platforms(self, build_dir: Path) -> list:
        """Detect which platforms are available in a build."""
        platforms = []

        platform_files = {
            "macos-arm64": "credential-process-macos-arm64",
            "macos-intel": "credential-process-macos-intel",
            "linux-x64": "credential-process-linux-x64",
            "linux-arm64": "credential-process-linux-arm64",
            "windows": "credential-process-windows.exe",
        }

        for platform, filename in platform_files.items():
            if (build_dir / filename).exists():
                platforms.append(platform)

        return platforms

    def _format_size(self, bytes_size: int) -> str:
        """Format bytes to human readable size."""
        for unit in ["B", "KB", "MB", "GB"]:
            if bytes_size < 1024.0:
                return f"{bytes_size:.1f} {unit}"
            bytes_size /= 1024.0
        return f"{bytes_size:.1f} TB"

    def _show_distribution_wizard(self, builds: dict, console: Console) -> Path:
        """Show interactive wizard to select build to distribute."""
        import questionary

        # Flatten builds into choices
        choices = []
        build_map = {}
        idx = 1

        for profile_name in sorted(builds.keys()):
            profile_builds = builds[profile_name]
            if not profile_builds:
                continue

            console.print(f"\n[bold]Profile: {profile_name}[/bold]")

            for build in profile_builds:
                timestamp = build["timestamp"]
                platforms_str = ", ".join(build["platforms"])
                size_str = self._format_size(build["size"])

                label = f"  [{idx}] {timestamp}"
                if build == profile_builds[0]:
                    label += " (Latest)"
                console.print(label)
                console.print(f"      Platforms: {platforms_str}")
                console.print(f"      Size: {size_str}")

                choice_text = f"{profile_name} - {timestamp}" + (" (Latest)" if build == profile_builds[0] else "")
                choices.append(choice_text)
                build_map[choice_text] = build["path"]
                idx += 1

        if not choices:
            return None

        # Auto-select if only one build
        if len(choices) == 1:
            console.print("\n[green]Auto-selecting only available build[/green]")
            return build_map[choices[0]]

        # Non-interactive: auto-select latest build
        import sys as _sys

        if not _sys.stdin.isatty():
            console.print("\n[dim]Non-interactive mode: selecting latest build[/dim]")
            return build_map[choices[0]]

        # Show selection
        console.print()
        selected = questionary.select(
            "Select package to distribute:",
            choices=choices,
        ).ask()

        if not selected:
            return None

        return build_map[selected]

    @staticmethod
    def _check_ssl_proxy_environment(console: Console) -> None:
        """Warn if corporate SSL inspection (Zscaler, Netskope, etc.) may interfere with S3 uploads.

        Zscaler Client Connector and similar tools run locally on the laptop,
        intercept HTTPS traffic transparently (no proxy env vars), and re-sign
        it with their own CA. Python/boto3 uses the certifi CA bundle (not the
        OS cert store), so it doesn't trust the corporate CA → SSL/access errors.
        """
        import os
        import platform as platform_mod

        ca_bundle = os.environ.get("AWS_CA_BUNDLE") or os.environ.get("REQUESTS_CA_BUNDLE")
        if ca_bundle:
            return  # User has already configured a custom CA bundle

        corporate_proxy_detected = False
        proxy_name = "corporate proxy"

        # Check for proxy environment variables
        proxy_vars = ["HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"]
        if any(os.environ.get(v) for v in proxy_vars):
            corporate_proxy_detected = True

        # On Windows, check for Zscaler/Netskope processes (common corporate SSL interceptors)
        if platform_mod.system() == "Windows" and not corporate_proxy_detected:
            try:
                import subprocess

                result = subprocess.run(  # nosec B603 — fixed argv (tasklist), no shell
                    ["tasklist", "/FI", "IMAGENAME eq ZSATunnel.exe", "/NH"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if "ZSATunnel" in result.stdout:
                    corporate_proxy_detected = True
                    proxy_name = "Zscaler"
                else:
                    result = subprocess.run(  # nosec B603 — fixed argv (tasklist), no shell
                        ["tasklist", "/FI", "IMAGENAME eq nscommon.exe", "/NH"],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if "nscommon" in result.stdout:
                        corporate_proxy_detected = True
                        proxy_name = "Netskope"
            except Exception:
                pass

        if corporate_proxy_detected:
            console.print(
                f"\n[yellow]Note: {proxy_name} detected. If S3 uploads fail with 'Access Denied' or SSL errors, "
                f"use one of these fixes:[/yellow]"
            )
            console.print(
                f"  Option 1: [cyan]pip install truststore[/cyan]  (makes Python trust the {proxy_name} CA from the OS store)"
            )
            console.print(
                f"  Option 2: [cyan]set AWS_CA_BUNDLE=C:\\path\\to\\{proxy_name}RootCA.pem[/cyan]  (ask IT for the .pem file)"
            )
            console.print()

    def handle(self) -> int:
        """Execute the distribute command."""
        console = Console()

        # Show header
        console.print(
            Panel.fit(
                "[bold cyan]Governed Inference Platform Package Distribution[/bold cyan]\n\nShare packages securely via presigned URLs",
                border_style="cyan",
                padding=(1, 2),
            )
        )

        if self.option("allowed-ips"):
            console.print(f"[red]Error: {self.ALLOWED_IPS_UNSUPPORTED}[/red]")
            console.print("[dim]Use an authenticated landing page or an enforcement layer such as CloudFront.[/dim]")
            return 1

        # Check for SSL proxy issues (Zscaler, etc.)
        self._check_ssl_proxy_environment(console)

        # Check for old flat structure and fail with clear message
        dist_dir = Path(self.option("package-path"))
        if self._check_old_flat_structure(dist_dir):
            console.print("[red]Error: Old distribution format detected![/red]")
            console.print()
            console.print("The dist/ directory contains files from an old package format.")
            console.print("Please delete the dist/ directory and run the package command again:")
            console.print()
            console.print("  [cyan]rm -rf dist/[/cyan]")
            console.print("  [cyan]poetry run gip package --profile <profile-name>[/cyan]")
            console.print()
            return 1

        # Scan for new organized structure
        console.print("\n[bold]Scanning package directory...[/bold]")
        builds = self._scan_distributions(dist_dir)

        if not builds or all(len(b) == 0 for b in builds.values()):
            console.print("[red]No packaged distributions found.[/red]")
            console.print("Run 'poetry run gip package' first to build packages.")
            return 1

        # Determine which build to use
        selected_build_path = None

        # Option 1: Explicit profile + timestamp
        build_profile = self.option("build-profile")
        timestamp = self.option("timestamp")
        if build_profile and timestamp:
            if build_profile in builds:
                for build in builds[build_profile]:
                    if build["timestamp"] == timestamp:
                        selected_build_path = build["path"]
                        break
            if not selected_build_path:
                console.print(f"[red]Build not found: {build_profile}/{timestamp}[/red]")
                return 1

        # Option 2: Latest flag (auto-select most recent)
        elif self.option("latest"):
            # Find most recent build across all profiles
            latest_build = None
            latest_timestamp = None

            for _profile_name, profile_builds in builds.items():
                if profile_builds:
                    build = profile_builds[0]  # Already sorted, first is latest
                    if latest_timestamp is None or build["timestamp"] > latest_timestamp:
                        latest_timestamp = build["timestamp"]
                        latest_build = build["path"]

            selected_build_path = latest_build
            console.print(f"[green]Auto-selected latest build: {latest_build.parent.name}/{latest_build.name}[/green]")

        # Option 3: Show wizard (default)
        else:
            selected_build_path = self._show_distribution_wizard(builds, console)
            if not selected_build_path:
                console.print("[yellow]Distribution cancelled.[/yellow]")
                return 0

        # Use selected build path for distribution
        package_path = selected_build_path
        console.print(f"\n[green]Using build: {package_path.parent.name}/{package_path.name}[/green]")

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
        if not profile and profile_name == "default":
            # Fall back to active profile
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

        # Get latest URL if requested (only if distribution is enabled).
        if self.option("get-latest"):
            if not profile.enable_distribution:
                console.print("[red]Distribution features not enabled.[/red]")
                console.print("Enable distribution in profile configuration to use this feature.")
                return 1
            return self._get_latest_url(profile, console)

        if not profile.enable_distribution:
            console.print("[yellow]Note: Distribution features not enabled.[/yellow]")
            console.print("Package will be created locally without AWS credentials or API calls.")
            return self._create_local_distribution(profile, console, package_path)

        # Cloud distribution requires a deployed distribution stack.
        dist_stack_name = profile.stack_names.get("distribution", f"{profile.identity_pool_name}-distribution")
        try:
            dist_outputs = get_stack_outputs(dist_stack_name, profile.aws_region)
            if not dist_outputs:
                console.print("[red]Distribution stack not deployed.[/red]")
                console.print("Deploy the distribution stack first:")
                console.print("  poetry run gip deploy distribution")
                return 1
        except Exception:
            console.print("[red]Distribution stack not deployed.[/red]")
            console.print("Deploy the distribution stack first:")
            console.print("  poetry run gip deploy distribution")
            return 1

        # Route to appropriate distribution method based on type
        if profile.distribution_type == "landing-page":
            # For landing page, upload platform-specific packages
            return self._upload_landing_page_packages(profile, console, package_path)
        else:
            # presigned-s3 or legacy - use existing logic
            return self._create_distribution(profile, console, package_path)

    def _get_latest_url(self, profile, console: Console) -> int:
        """Retrieve the latest distribution URL from Parameter Store."""
        try:
            ssm = boto3.client("ssm", region_name=profile.aws_region)

            # Get parameter
            response = ssm.get_parameter(
                Name=f"/gip/{profile.identity_pool_name}/distribution/latest", WithDecryption=True
            )

            # Parse the stored data
            data = json.loads(response["Parameter"]["Value"])

            # New records carry a strict timestamp. Legacy records without one
            # remain readable until their already-persisted URL expires.
            if "timestamp" in data:
                self._validate_distribution_record(data)
                expires = self._parse_aware_iso_timestamp(data["expires"], "distribution expiration timestamp")
            else:
                expires = datetime.fromisoformat(data["expires"].replace("Z", "+00:00"))
                if expires.tzinfo is None or expires.utcoffset() is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                else:
                    expires = expires.astimezone(timezone.utc)
            now = datetime.now(timezone.utc)

            if expires < now:
                console.print("[red]Latest distribution URL has expired.[/red]")
                console.print("Generate a new one with: poetry run gip distribute")
                return 1

            # Display information
            console.print("\n[bold]Latest Distribution URL[/bold]")
            console.print(f"Expires: {expires.strftime('%Y-%m-%d %H:%M:%S')}")
            console.print(f"Package: {data.get('filename', 'Unknown')}")
            console.print(f"SHA256: {data.get('checksum', 'Unknown')}")
            console.print(f"\n[cyan]{data['url']}[/cyan]")

            # Output download commands for different platforms
            console.print("\n[bold]Download and Installation Instructions:[/bold]")

            filename = data.get("filename", "gip-package.zip")

            console.print("\n[cyan]For macOS/Linux:[/cyan]")
            console.print("1. Download (copy entire line):")
            # Use regular print to avoid Rich console line wrapping
            print(f'   curl -L -o "{filename}" "{data["url"]}"')
            console.print("2. Extract and install:")
            console.print(f"   unzip {filename} && cd gip-package && chmod +x install.sh && ./install.sh")

            console.print("\n[cyan]For Windows PowerShell:[/cyan]")
            console.print("1. Download (copy entire line):")
            print(f'   Invoke-WebRequest -Uri "{data["url"]}" -OutFile "{filename}"')
            console.print("2. Extract and install:")
            console.print(f'   Expand-Archive -Path "{filename}" -DestinationPath "."')
            console.print("   cd gip-package")
            console.print("   .\\install.bat")

            console.print(f"\n[dim]Verify download with: sha256sum {filename} (or Get-FileHash on Windows)[/dim]")

            # Show QR code if requested
            if self.option("show-qr"):
                self._display_qr_code(data["url"], console)

            # Try to get download stats from S3 (optional)
            self._show_download_stats(profile, data.get("package_key"), console)

            return 0

        except ClientError as e:
            if e.response["Error"]["Code"] == "ParameterNotFound":
                console.print("[yellow]No distribution URL found.[/yellow]")
                console.print("Create one with: poetry run gip distribute")
            else:
                console.print(f"[red]Error retrieving URL: {e}[/red]")
            return 1
        except (KeyError, TypeError, ValueError) as e:
            console.print(f"[red]Invalid latest distribution record: {e}[/red]")
            return 1

    def _upload_landing_page_packages(self, profile, console: Console, package_path: Path) -> int:
        """Upload platform-specific packages to S3 for the landing page."""
        import zipfile

        import boto3

        # Validate package directory
        if not package_path.exists():
            console.print(f"[red]Package directory not found: {package_path}[/red]")
            console.print("Run 'poetry run gip package' first to build packages.")
            return 1

        try:
            build_datetime = self._parse_build_timestamp(package_path.name)
        except ValueError as e:
            console.print(f"[red]Invalid package build timestamp: {e}[/red]")
            return 1

        # Get S3 bucket from distribution stack outputs
        dist_stack_name = profile.stack_names.get("distribution", f"{profile.identity_pool_name}-distribution")
        try:
            stack_outputs = get_stack_outputs(dist_stack_name, profile.aws_region)
            bucket_name = stack_outputs.get("DistributionBucket")
            landing_url = stack_outputs.get("DistributionURL")
            if not bucket_name:
                console.print("[red]S3 bucket not found in distribution stack outputs.[/red]")
                return 1
            if not landing_url:
                console.print("[red]Landing page URL not found in distribution stack outputs.[/red]")
                return 1
        except Exception as e:
            console.print(f"[red]Error getting distribution stack outputs: {e}[/red]")
            console.print("Deploy the distribution stack first: poetry run gip deploy distribution")
            return 1

        # Check for Windows binaries and auto-download if needed
        console.print("\n[bold]Checking for Windows binaries...[/bold]")
        windows_exe = package_path / "credential-process-windows.exe"
        if not windows_exe.exists():
            # Check if Windows build is completed and download it
            try:
                project_name = f"{profile.identity_pool_name}-windows-build"
                codebuild = boto3.client("codebuild", region_name=get_codebuild_region(profile))

                # List recent builds
                response = codebuild.list_builds_for_project(projectName=project_name, sortOrder="DESCENDING")

                if response.get("ids"):
                    # Get details of recent builds
                    build_ids = response["ids"][:5]  # Check last 5 builds
                    builds_response = codebuild.batch_get_builds(ids=build_ids)

                    for build in builds_response.get("builds", []):
                        if build["buildStatus"] == "SUCCEEDED":
                            # Found a successful build, download it
                            build_time = build.get("endTime", build.get("startTime"))
                            console.print(
                                f"  [cyan]Found completed Windows build from "
                                f"{build_time.strftime('%Y-%m-%d %H:%M')}[/cyan]"
                            )
                            console.print("  [cyan]Downloading Windows artifacts...[/cyan]")

                            if self._download_windows_artifacts(profile, package_path, console):
                                console.print("  [green]✓ Downloaded Windows artifacts[/green]")
                            else:
                                console.print("  [yellow]⚠️  Failed to download Windows artifacts[/yellow]")
                            break
                        elif build["buildStatus"] == "IN_PROGRESS":
                            console.print("  [yellow]⚠️  Windows build in progress[/yellow]")
                            break
            except Exception as e:
                console.print(f"  [dim]Could not check Windows build status: {e}[/dim]")
        else:
            console.print("  [green]✓ Windows binaries found[/green]")

        # Map available binaries to platforms
        console.print("\n[bold]Scanning package directory...[/bold]")

        # Platform file mappings
        platform_files = {
            "windows": [
                ("credential-process-windows.exe", "credential-process-windows.exe"),
                ("otel-helper-windows.exe", "otel-helper-windows.exe"),
                ("otel-helper.ps1", "otel-helper.ps1"),
                ("otel-helper.cmd", "otel-helper.cmd"),
                ("otelcol-windows.exe", "otelcol-windows.exe"),
                ("collector-config.yaml", "collector-config.yaml"),
                ("install.bat", "install.bat"),
                ("gip-install.ps1", "gip-install.ps1"),
                ("config.json", "config.json"),
                ("README.md", "README.md"),
                ("cowork-3p.reg", "cowork-3p.reg"),
                ("cowork-3p-config.json", "cowork-3p-config.json"),
            ],
            "linux": [
                ("credential-process-linux-x64", "credential-process-linux-x64"),
                ("credential-process-linux-arm64", "credential-process-linux-arm64"),
                # Accept generic linux binary (maps to x64 for compat with install.sh)
                ("credential-process-linux", "credential-process-linux-x64"),
                ("otel-helper-linux-x64", "otel-helper-linux-x64"),
                ("otel-helper-linux-arm64", "otel-helper-linux-arm64"),
                ("otel-helper-linux", "otel-helper-linux-x64"),
                ("otelcol-linux-x64", "otelcol-linux-x64"),
                ("otelcol-linux-arm64", "otelcol-linux-arm64"),
                ("otel-helper.sh", "otel-helper.sh"),
                ("collector-config.yaml", "collector-config.yaml"),
                ("install.sh", "install.sh"),
                ("config.json", "config.json"),
                ("README.md", "README.md"),
                ("cowork-3p-config.json", "cowork-3p-config.json"),
            ],
            "mac": [
                ("credential-process-macos-arm64", "credential-process-macos-arm64"),
                ("credential-process-macos-intel", "credential-process-macos-intel"),
                ("otel-helper-macos-arm64", "otel-helper-macos-arm64"),
                ("otel-helper-macos-intel", "otel-helper-macos-intel"),
                ("otelcol-macos-arm64", "otelcol-macos-arm64"),
                ("otelcol-macos-intel", "otelcol-macos-intel"),
                ("otel-helper.sh", "otel-helper.sh"),
                ("collector-config.yaml", "collector-config.yaml"),
                ("install.sh", "install.sh"),
                ("config.json", "config.json"),
                ("README.md", "README.md"),
                ("cowork-3p.mobileconfig", "cowork-3p.mobileconfig"),
                ("cowork-3p-config.json", "cowork-3p-config.json"),
            ],
        }

        required_credentials = {
            "windows": ("credential-process-windows.exe",),
            "linux": ("credential-process-linux-x64", "credential-process-linux-arm64", "credential-process-linux"),
            "mac": ("credential-process-macos-arm64", "credential-process-macos-intel"),
        }

        # A helper or launcher alone does not make a platform installable.
        available_platforms = {}
        for platform, files in platform_files.items():
            if any((package_path / credential).is_file() for credential in required_credentials[platform]):
                available_platforms[platform] = files
                console.print(f"  ✓ {platform.capitalize()} platform detected")

        if not available_platforms:
            console.print("[red]No platform packages found![/red]")
            console.print("Run: [cyan]poetry run gip package[/cyan] first")
            return 1

        # Create all-platforms package (includes everything)
        all_files = []
        for files in available_platforms.values():
            all_files.extend(files)
        # Deduplicate
        all_files = list(set(all_files))
        available_platforms["all-platforms"] = all_files

        profile_name = package_path.parent.name
        build_timestamp = build_datetime.strftime(self.BUILD_TIMESTAMP_FORMAT)
        release_date = build_datetime.strftime("%Y-%m-%d")
        release_datetime = build_datetime.strftime("%Y-%m-%d %H:%M:%S UTC")

        try:
            s3 = boto3.client("s3", region_name=profile.aws_region)
        except Exception as e:
            console.print(f"[red]Failed to initialize S3 distribution client: {e}[/red]")
            return 1

        uploaded_platforms = []
        upload_failed = False
        zip_files_to_clean = []

        try:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                "[progress.percentage]{task.percentage:>3.1f}%",
                console=console,
            ) as progress:
                task = progress.add_task("Uploading packages to S3...", total=len(available_platforms))

                for platform, files in available_platforms.items():
                    zip_path = package_path / f"{platform}.zip"
                    zip_files_to_clean.append(zip_path)

                    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                        for source_file, archive_name in files:
                            source_path = package_path / source_file
                            if source_path.exists():
                                zipf.writestr(f"gip-package/{archive_name}", self._read_file_with_retry(source_path))

                        settings_dir = package_path / "gip-settings"
                        if settings_dir.exists() and settings_dir.is_dir():
                            for file in settings_dir.rglob("*"):
                                if file.is_file():
                                    rel_path = file.relative_to(package_path)
                                    zipf.writestr(
                                        f"gip-package/{rel_path.as_posix()}",
                                        self._read_file_with_retry(file),
                                    )

                        self._add_package_tree_to_zip(zipf, package_path, "harnesses")

                        extra_token = None if platform == "all-platforms" else platform
                        self._add_extra_files_to_zip(
                            zipf, package_path, getattr(profile, "extra_files", None), extra_token
                        )

                    try:
                        self._upload_file_with_retry(
                            s3,
                            str(zip_path),
                            bucket_name,
                            f"packages/{platform}/latest.zip",
                            extra_args={
                                "Metadata": {
                                    "profile": profile_name,
                                    "timestamp": build_timestamp,
                                    "release_date": release_date,
                                    "release_datetime": release_datetime,
                                }
                            },
                            config=self.S3_TRANSFER_CONFIG,
                        )
                        uploaded_platforms.append(platform)
                        progress.update(task, advance=1, description=f"Uploaded {platform} package")
                    except Exception as e:
                        upload_failed = True
                        console.print(f"[red]Failed to upload {platform} package: {e}[/red]")
                        self._print_upload_error_guidance(e, console)
        except Exception as e:
            console.print(f"[red]Failed to prepare landing-page packages: {e}[/red]")
            return 1
        finally:
            for zip_path in zip_files_to_clean:
                try:
                    zip_path.unlink()
                except OSError:
                    pass

        if upload_failed or len(uploaded_platforms) != len(available_platforms):
            console.print(
                "[red]Landing-page publish incomplete; existing absent-platform packages were preserved.[/red]"
            )
            return 1

        stale_platforms = {"windows", "linux", "mac", "all-platforms"} - set(available_platforms)
        for platform in sorted(stale_platforms):
            try:
                s3.delete_object(Bucket=bucket_name, Key=f"packages/{platform}/latest.zip")
            except Exception as e:
                console.print(f"[red]Failed to remove stale {platform} package: {e}[/red]")
                return 1

        console.print(
            f"\n[bold green]✓ Successfully uploaded {len(uploaded_platforms)} platform packages![/bold green]"
        )
        console.print(f"\n[bold]Landing Page URL:[/bold] [cyan]{landing_url}[/cyan]")
        console.print(f"[dim]Profile: {profile_name}[/dim]")
        console.print(f"[dim]Build Timestamp: {build_timestamp}[/dim]")
        console.print(f"[dim]Release Date: {release_datetime}[/dim]")
        console.print("\n[bold]Uploaded platforms:[/bold]")
        for platform in uploaded_platforms:
            console.print(f"  • {platform}")
        return 0

    def _resolve_presign_context(
        self, stack_outputs: dict, expires_hours: int, console: Console
    ) -> tuple[str, str | None, int]:
        """Read the presigning principal mode from the distribution stack outputs.

        iam-user mode (default, stacks deployed before PresignPrincipalType
        existed have no output): behavior unchanged. role mode: presigned URLs
        signed with temporary credentials stop working when the credentials
        expire, so the URL expiry is capped at the assumed-role session
        maximum (12 hours).

        Returns (principal_type, role_arn, effective_expires_hours).
        """
        principal_type = stack_outputs.get("PresignPrincipalType", "iam-user")
        role_arn = stack_outputs.get("PresignRoleArn")
        if principal_type == "role" and expires_hours > self.ROLE_PRESIGN_MAX_HOURS:
            console.print(
                f"[yellow]Role-based presigning: capping URL expiry at {self.ROLE_PRESIGN_MAX_HOURS} hours "
                f"(requested {expires_hours}).[/yellow]"
            )
            console.print(
                "[dim]Presigned URLs signed with temporary credentials expire when the assumed-role "
                "session expires (12 hours maximum). Tradeoff: shorter-lived URLs, but no static IAM "
                "user access key to leak or for SCPs to block. Re-run 'gip distribute' to mint a "
                "fresh URL, or redeploy the distribution stack with PresignPrincipalType=iam-user "
                "for URLs up to 7 days.[/dim]"
            )
            expires_hours = self.ROLE_PRESIGN_MAX_HOURS
        return principal_type, role_arn, expires_hours

    def _create_presign_client(self, profile, role_arn: str, expires_hours: int, console: Console):
        """Assume the stack's presign role and build the S3 client that signs URLs.

        The AssumeRole session duration matches the URL expiry (already capped
        at 12h by _resolve_presign_context) so the URL never outlives its
        signature. When the admin's own credentials are temporary (role
        chaining), STS rejects sessions over 1 hour — retry at 1 hour and
        shorten the URL expiry to match.

        Returns (s3_client, effective_expires_hours).
        """
        sts = boto3.client("sts", region_name=profile.aws_region)
        duration_seconds = expires_hours * 3600
        try:
            response = sts.assume_role(
                RoleArn=role_arn,
                RoleSessionName="gip-distribute-presign",
                DurationSeconds=duration_seconds,
            )
        except ClientError as e:
            if duration_seconds > 3600 and "DurationSeconds" in str(e):
                console.print(
                    "[yellow]Role chaining detected: STS caps chained sessions at 1 hour — "
                    "presigned URLs will expire in 1 hour.[/yellow]"
                )
                response = sts.assume_role(
                    RoleArn=role_arn,
                    RoleSessionName="gip-distribute-presign",
                    DurationSeconds=3600,
                )
                expires_hours = 1
            else:
                raise
        credentials = response["Credentials"]
        presign_s3 = boto3.client(
            "s3",
            region_name=profile.aws_region,
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
        )
        return presign_s3, expires_hours

    def _create_local_distribution(self, profile, console: Console, package_path: Path) -> int:
        """Create local archives without initializing or calling an AWS client."""
        if not package_path.exists():
            console.print(f"[red]Package directory not found: {package_path}[/red]")
            return 1

        if self.option("per-os"):
            return self._distribute_per_os(package_path, profile, self._detect_platforms(package_path), 0, console)

        archive_path = self._create_archive(package_path, getattr(profile, "extra_files", None))
        checksum = self._calculate_checksum(archive_path)
        published_at = datetime.now(timezone.utc).replace(microsecond=0)
        filename = f"gip-package-{published_at.strftime(self.PUBLISH_TIMESTAMP_FORMAT)}.zip"
        local_dir = Path(self.option("package-path"))
        local_dir.mkdir(parents=True, exist_ok=True)
        local_path = local_dir / filename
        try:
            shutil.copy2(archive_path, local_path)
        except OSError as e:
            console.print(f"[red]Failed to save local distribution package: {e}[/red]")
            return 1
        file_size = local_path.stat().st_size
        try:
            archive_path.unlink()
        except OSError:
            pass

        console.print("\n[bold green]Package created successfully![/bold green]")
        console.print(f"[bold]Package saved locally:[/bold] {local_path}")
        console.print(f"  SHA256: {checksum}")
        console.print(f"  Size: {self._format_size(file_size)}")
        return 0

    def _create_distribution(self, profile, console: Console, package_path: Path) -> int:
        """Create a new distribution package and generate presigned URL."""
        import json

        import boto3

        # Validate package directory
        if not package_path.exists():
            console.print(f"[red]Package directory not found: {package_path}[/red]")
            console.print("Run 'poetry run gip package' first to build packages.")
            return 1

        if not profile.enable_distribution:
            return self._create_local_distribution(profile, console, package_path)

        # Check what's in the package directory
        console.print("\n[bold]Package contents:[/bold]")
        found_platforms = []

        # Check for macOS executables
        macos_arm = package_path / "credential-process-macos-arm64"
        macos_intel = package_path / "credential-process-macos-intel"
        if macos_arm.exists():
            mod_time = datetime.fromtimestamp(macos_arm.stat().st_mtime)
            console.print(f"  ✓ macOS ARM64 executable (built: {mod_time.strftime('%Y-%m-%d %H:%M')})")
            found_platforms.append("macos-arm64")
        if macos_intel.exists():
            mod_time = datetime.fromtimestamp(macos_intel.stat().st_mtime)
            console.print(f"  ✓ macOS Intel executable (built: {mod_time.strftime('%Y-%m-%d %H:%M')})")
            found_platforms.append("macos-intel")

        # Check for Windows executables
        windows_exe = package_path / "credential-process-windows.exe"
        windows_exe_time = None
        if windows_exe.exists():
            windows_exe_time = datetime.fromtimestamp(windows_exe.stat().st_mtime, tz=timezone.utc)
            console.print(f"  ✓ Windows executable (built: {windows_exe_time.strftime('%Y-%m-%d %H:%M')})")
            found_platforms.append("windows")

            # Check if there are newer Windows builds available and download them
            try:
                # Get CodeBuild project name from profile
                project_name = f"{profile.identity_pool_name}-windows-build"
                codebuild = boto3.client("codebuild", region_name=get_codebuild_region(profile))

                # List recent builds
                response = codebuild.list_builds_for_project(projectName=project_name, sortOrder="DESCENDING")

                if response.get("ids"):
                    # Get details of recent successful builds
                    build_ids = response["ids"][:3]  # Check last 3 builds
                    builds_response = codebuild.batch_get_builds(ids=build_ids)

                    for build in builds_response.get("builds", []):
                        if build["buildStatus"] == "SUCCEEDED":
                            build_time = build.get("endTime", build.get("startTime"))
                            if build_time and build_time > windows_exe_time:
                                console.print(
                                    f"    [yellow]⚠️  Newer Windows build available "
                                    f"(completed {build_time.strftime('%Y-%m-%d %H:%M')})[/yellow]"
                                )

                                # Automatically download the newer build
                                console.print("    [cyan]Downloading newer Windows artifacts...[/cyan]")
                                if self._download_windows_artifacts(profile, package_path, console):
                                    console.print("    [green]✓ Downloaded newer Windows artifacts[/green]")
                                    # Update the timestamp
                                    windows_exe_time = datetime.fromtimestamp(
                                        windows_exe.stat().st_mtime, tz=timezone.utc
                                    )
                                else:
                                    console.print(
                                        "    [yellow]Failed to download newer artifacts, using existing[/yellow]"
                                    )
                            break
            except Exception:
                pass  # Silently ignore if we can't check
        else:
            # Check if Windows build is completed and download it
            windows_downloaded = False

            # First check for any completed builds
            try:
                project_name = f"{profile.identity_pool_name}-windows-build"
                codebuild = boto3.client("codebuild", region_name=get_codebuild_region(profile))

                # List recent builds
                response = codebuild.list_builds_for_project(projectName=project_name, sortOrder="DESCENDING")

                if response.get("ids"):
                    # Get details of recent builds
                    build_ids = response["ids"][:5]  # Check last 5 builds
                    builds_response = codebuild.batch_get_builds(ids=build_ids)

                    for build in builds_response.get("builds", []):
                        if build["buildStatus"] == "SUCCEEDED":
                            # Found a successful build, download it
                            build_time = build.get("endTime", build.get("startTime"))
                            console.print(
                                f"  ⚠️  Windows executable [yellow](found completed build from "
                                f"{build_time.strftime('%Y-%m-%d %H:%M')})[/yellow]"
                            )
                            console.print("    [cyan]Downloading Windows artifacts...[/cyan]")

                            if self._download_windows_artifacts(profile, package_path, console):
                                console.print("    [green]✓ Downloaded Windows artifacts[/green]")
                                found_platforms.append("windows")
                                windows_downloaded = True
                            else:
                                console.print("    [yellow]Failed to download Windows artifacts[/yellow]")
                            break
                        elif build["buildStatus"] == "IN_PROGRESS":
                            console.print("  ⚠️  Windows executable [yellow](build in progress)[/yellow]")
                            break
            except Exception:
                pass  # Continue to check for build info file

            # If we didn't download, check build info file
            if not windows_downloaded:
                build_info_file = Path.home() / ".gip" / "latest-build.json"
                if build_info_file.exists():
                    with open(build_info_file, encoding="utf-8") as f:
                        build_info = json.load(f)

                    # Check build status
                    try:
                        codebuild = boto3.client("codebuild", region_name=get_codebuild_region(profile))
                        response = codebuild.batch_get_builds(ids=[build_info["build_id"]])
                        if response.get("builds"):
                            build = response["builds"][0]
                            if build["buildStatus"] == "IN_PROGRESS":
                                console.print("  ⚠️  Windows executable [yellow](build in progress)[/yellow]")
                            elif build["buildStatus"] == "SUCCEEDED":
                                console.print("  ⚠️  Windows executable [yellow](build completed)[/yellow]")
                                console.print("    [cyan]Downloading Windows artifacts...[/cyan]")

                                if self._download_windows_artifacts(profile, package_path, console):
                                    console.print("    [green]✓ Downloaded Windows artifacts[/green]")
                                    found_platforms.append("windows")
                                else:
                                    console.print("    [yellow]Failed to download Windows artifacts[/yellow]")
                            else:
                                console.print("  ✗ Windows executable [red](build failed)[/red]")
                    except Exception:
                        console.print("  ✗ Windows executable [red](not found)[/red]")
                elif not windows_downloaded:
                    console.print("  ✗ Windows executable [red](not built)[/red]")

        # Check for Linux executables
        linux_x64 = package_path / "credential-process-linux-x64"
        linux_arm64 = package_path / "credential-process-linux-arm64"
        linux_generic = package_path / "credential-process-linux"  # Native Linux build

        if linux_x64.exists():
            mod_time = datetime.fromtimestamp(linux_x64.stat().st_mtime)
            found_platforms.append("linux-x64")
            console.print(f"  ✓ Linux x64 executable (built: {mod_time.strftime('%Y-%m-%d %H:%M')})")

        if linux_arm64.exists():
            mod_time = datetime.fromtimestamp(linux_arm64.stat().st_mtime)
            found_platforms.append("linux-arm64")
            console.print(f"  ✓ Linux ARM64 executable (built: {mod_time.strftime('%Y-%m-%d %H:%M')})")

        if linux_generic.exists() and not linux_x64.exists() and not linux_arm64.exists():
            # Show generic Linux build if no architecture-specific versions exist
            mod_time = datetime.fromtimestamp(linux_generic.stat().st_mtime)
            console.print(f"  ✓ Linux executable (built: {mod_time.strftime('%Y-%m-%d %H:%M')})")
            found_platforms.append("linux")

        # Check for installers and config
        if (package_path / "install.sh").exists():
            console.print("  ✓ Unix installer script")
        if (package_path / "install.bat").exists():
            console.print("  ✓ Windows installer script")
        if (package_path / "gip-install.ps1").exists():
            console.print("  ✓ Windows PowerShell installer")
        if (package_path / "config.json").exists():
            console.print("  ✓ Configuration file")

        # Warn if missing critical platforms
        if not found_platforms:
            console.print("\n[red]No platform executables found![/red]")
            console.print("Run: [cyan]poetry run gip package --target-platform all[/cyan]")
            return 1

        if "windows" not in found_platforms:
            console.print("\n[yellow]Warning: Windows support not included in this distribution[/yellow]")
            import sys as _sys

            if _sys.stdin.isatty():
                from questionary import confirm

                proceed = confirm("Continue without Windows support?", default=False).ask()
                if not proceed:
                    console.print("Distribution cancelled.")
                    return 0
            else:
                # Non-interactive: proceed with default (skip Windows)
                console.print("[dim]Non-interactive mode: proceeding without Windows support[/dim]")

        console.print(f"\n[green]Ready to distribute for: {', '.join(found_platforms)}[/green]")

        # Validate expiration hours (max 7 days for IAM user presigned URLs)
        try:
            expires_hours = int(self.option("expires-hours"))
            if not 1 <= expires_hours <= 168:
                console.print("[red]Expiration must be between 1 and 168 hours (7 days).[/red]")
                console.print(
                    "[dim]Note: Presigned URLs have a maximum lifetime of 7 days when using IAM user credentials.[/dim]"
                )
                return 1
        except ValueError:
            console.print("[red]Invalid expiration hours.[/red]")
            return 1

        # Per-OS distribution: create separate packages per platform
        if self.option("per-os"):
            return self._distribute_per_os(package_path, profile, found_platforms, expires_hours, console)

        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console
        ) as progress:
            # Create archive
            task = progress.add_task("Creating distribution archive...", total=None)
            archive_path = self._create_archive(package_path, getattr(profile, "extra_files", None))

            # Calculate checksum
            progress.update(task, description="Calculating checksum...")
            checksum = self._calculate_checksum(archive_path)

            # Prepare filename
            published_at = datetime.now(timezone.utc).replace(microsecond=0)
            timestamp = published_at.strftime(self.PUBLISH_TIMESTAMP_FORMAT)
            filename = f"gip-package-{timestamp}.zip"

            # Only do S3 operations if distribution is enabled
            presign_principal_type = "iam-user"
            presign_role_arn = None
            if profile.enable_distribution:
                # Get S3 bucket from distribution stack outputs
                progress.update(task, description="Getting S3 bucket information...")
                dist_stack_name = profile.stack_names.get("distribution", f"{profile.identity_pool_name}-distribution")
                try:
                    stack_outputs = get_stack_outputs(dist_stack_name, profile.aws_region)
                    bucket_name = stack_outputs.get("DistributionBucket")
                    if not bucket_name:
                        console.print("[red]S3 bucket not found in distribution stack outputs.[/red]")
                        return 1
                except Exception as e:
                    console.print(f"[red]Error getting distribution stack outputs: {e}[/red]")
                    console.print("Deploy the distribution stack first: poetry run gip deploy distribution")
                    return 1

                # Role-based presigning caps the URL lifetime (12h max)
                presign_principal_type, presign_role_arn, expires_hours = self._resolve_presign_context(
                    stack_outputs, expires_hours, console
                )

                # Upload to S3 with progress tracking
                progress.update(task, description="Preparing upload...")
                package_key = f"packages/{timestamp}/{filename}"

                # Get file size for progress tracking (retry for Defender locks on Windows)
                import time as _time

                for _attempt in range(5):
                    try:
                        file_size = archive_path.stat().st_size
                        break
                    except (PermissionError, OSError):
                        if _attempt < 4:
                            _time.sleep(2)
                        else:
                            raise

                config = self.S3_TRANSFER_CONFIG

                # Create S3 client
                s3 = boto3.client("s3", region_name=profile.aws_region)

                # Close the spinner progress and create a new one with upload progress
                progress.stop()

                # Create progress bar for upload
                with Progress(
                    TextColumn("[bold blue]Uploading to S3"),
                    BarColumn(),
                    "[progress.percentage]{task.percentage:>3.1f}%",
                    "•",
                    DownloadColumn(),
                    "•",
                    TimeRemainingColumn(),
                    console=console,
                ) as upload_progress:
                    upload_task = upload_progress.add_task("upload", total=file_size)

                    # Create callback
                    callback = S3UploadProgress(filename, file_size, upload_progress)
                    callback.set_task_id(upload_task)

                    try:
                        self._upload_file_with_retry(
                            s3,
                            str(archive_path),
                            bucket_name,
                            package_key,
                            extra_args={
                                "Metadata": {
                                    "checksum": checksum,
                                    "created": published_at.isoformat(),
                                    "profile": profile.name,
                                }
                            },
                            config=config,
                            callback=callback,
                        )
                    except Exception as e:
                        console.print(f"[red]Failed to upload package: {e}[/red]")
                        self._print_upload_error_guidance(e, console)
                        return 1

                # Restart the spinner progress for remaining tasks
                progress = Progress(
                    SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console
                )
                progress.start()
                task = progress.add_task("Processing...", total=None)

            # Generate presigned URL
            progress.update(task, description="Generating presigned URL...")
            allowed_ips = self.option("allowed-ips")

            # role mode signs URLs with assumed-role credentials (no static key);
            # iam-user mode keeps the ambient-credentials client unchanged.
            presign_s3 = s3
            if presign_principal_type == "role" and presign_role_arn:
                try:
                    presign_s3, expires_hours = self._create_presign_client(
                        profile, presign_role_arn, expires_hours, console
                    )
                except Exception as e:
                    console.print(f"[red]Failed to assume presigning role: {e}[/red]")
                    return 1

            try:
                if allowed_ips:
                    url = self._generate_restricted_url(
                        presign_s3, bucket_name, package_key, allowed_ips, expires_hours
                    )
                else:
                    url = presign_s3.generate_presigned_url(
                        "get_object", Params={"Bucket": bucket_name, "Key": package_key}, ExpiresIn=expires_hours * 3600
                    )
            except Exception as e:
                console.print(f"[red]Failed to generate URL: {e}[/red]")
                return 1

            # Store in Parameter Store
            progress.update(task, description="Storing in Parameter Store...")
            distribution = self._build_distribution_record(published_at, expires_hours, url, checksum)
            try:
                self._validate_distribution_record(distribution)
            except ValueError as e:
                console.print(f"[red]Invalid distribution record: {e}[/red]")
                return 1
            expiration = self._parse_aware_iso_timestamp(distribution["expires"], "distribution expiration timestamp")

            try:
                ssm = boto3.client("ssm", region_name=profile.aws_region)
                ssm.put_parameter(
                    Name=f"/gip/{profile.identity_pool_name}/distribution/latest",
                    Value=json.dumps(distribution),
                    Type="SecureString",
                    Overwrite=True,
                    Description="Latest Governed Inference Platform package distribution URL",
                )
            except Exception as e:
                console.print(f"[red]Failed to store latest distribution in Parameter Store: {e}[/red]")
                return 1

            file_size = archive_path.stat().st_size if archive_path.exists() else 0

            # Clean up temp file (retry for Defender locks on Windows)
            for _attempt in range(5):
                try:
                    archive_path.unlink()
                    break
                except (PermissionError, OSError):
                    if _attempt < 4:
                        import time as _time

                        _time.sleep(2)
                    else:
                        pass  # Ignore cleanup failure, file will be overwritten next time

            # Stop progress if it's still running
            if "progress" in locals() and hasattr(progress, "stop"):
                progress.stop()

        # Display results based on distribution mode
        if profile.enable_distribution:
            console.print("\n[bold green]✓ Distribution package created successfully![/bold green]")
            console.print(f"\n[bold]Distribution URL[/bold] (expires in {expires_hours} hours):")
        else:
            console.print("\n[bold green]✓ Package created successfully![/bold green]")
            console.print(f"\n[bold]Package saved locally:[/bold] dist/{filename}")

        if profile.enable_distribution:
            # Show distribution-specific details
            if allowed_ips:
                console.print(f"[dim]Restricted to IPs: {allowed_ips}[/dim]")

            console.print(f"\n[cyan]{url}[/cyan]")

            console.print("\n[bold]Package Details:[/bold]")
            console.print(f"  Filename: {filename}")
            console.print(f"  SHA256: {checksum}")
            console.print(f"  Expires: {expiration.strftime('%Y-%m-%d %H:%M:%S')}")
            console.print(f"  Size: {self._format_size(file_size)}")

            # Show QR code if requested
            if self.option("show-qr"):
                self._display_qr_code(url, console)

            console.print("\n[bold]Share this URL with developers to download the package.[/bold]")

            # Output download commands for different platforms
            console.print("\n[bold]Download and Installation Instructions:[/bold]")

            console.print("\n[cyan]For macOS/Linux:[/cyan]")
            console.print("1. Download (copy entire line):")
            # Use regular print to avoid Rich console line wrapping
            print(f'   curl -L -o "{filename}" "{url}"')
            console.print("2. Extract and install:")
            console.print(f"   unzip {filename} && cd gip-package && chmod +x install.sh && ./install.sh")

            console.print("\n[cyan]For Windows PowerShell:[/cyan]")
            console.print("1. Download (copy entire line):")
            print(f'   Invoke-WebRequest -Uri "{url}" -OutFile "{filename}"')
            console.print("2. Extract and install:")
            console.print(f'   Expand-Archive -Path "{filename}" -DestinationPath "."')
            console.print("   cd gip-package")
            console.print("   .\\install.bat")

            console.print(f"\n[dim]Verify download with: sha256sum {filename} (or Get-FileHash on Windows)[/dim]")
        else:
            # Show local package details
            console.print("\n[bold]Package Details:[/bold]")
            console.print(f"  Filename: {filename}")
            console.print(f"  SHA256: {checksum}")
            console.print(f"  Size: {self._format_size(file_size)}")

            console.print("\n[bold]Installation Instructions:[/bold]")
            console.print("1. Extract the package:")
            console.print(f"   unzip dist/{filename}")
            console.print("2. Install:")
            console.print("   cd gip-package")
            console.print("   chmod +x install.sh && ./install.sh  (macOS/Linux)")
            console.print("   .\\install.bat  (Windows)")

            console.print("\n[dim]To enable distribution features:[/dim]")
            console.print("  1. Run: poetry run gip init")
            console.print("  2. Enable distribution when prompted")
            console.print("  3. Run: poetry run gip deploy distribution")

        return 0

    def _distribute_per_os(
        self, package_path: Path, profile: object, found_platforms: list, expires_hours: int, console: Console
    ) -> int:
        """Create and distribute separate packages per OS platform."""
        console.print("\n[cyan]Creating per-OS distribution packages...[/cyan]\n")

        archives = self._create_per_os_archives(package_path, getattr(profile, "extra_files", None))
        if not archives:
            console.print("[red]No platform binaries found to package.[/red]")
            return 1

        published_at = datetime.now(timezone.utc).replace(microsecond=0)
        timestamp = published_at.strftime(self.PUBLISH_TIMESTAMP_FORMAT)

        # Get S3 bucket if distribution is enabled
        s3 = None
        presign_s3 = None
        bucket_name = None
        if profile.enable_distribution:
            dist_stack_name = profile.stack_names.get("distribution", f"{profile.identity_pool_name}-distribution")
            try:
                stack_outputs = get_stack_outputs(dist_stack_name, profile.aws_region)
                bucket_name = stack_outputs.get("DistributionBucket")
                if not bucket_name:
                    console.print("[red]S3 bucket not found in distribution stack outputs.[/red]")
                    return 1
                s3 = boto3.client("s3", region_name=profile.aws_region)
            except Exception as e:
                console.print(f"[red]Error getting distribution stack: {e}[/red]")
                return 1

            # Role-based presigning caps the URL lifetime (12h max) and signs
            # with assumed-role credentials; iam-user mode is unchanged.
            principal_type, role_arn, expires_hours = self._resolve_presign_context(
                stack_outputs, expires_hours, console
            )
            presign_s3 = s3
            if principal_type == "role" and role_arn:
                try:
                    presign_s3, expires_hours = self._create_presign_client(profile, role_arn, expires_hours, console)
                except Exception as e:
                    console.print(f"[red]Failed to assume presigning role: {e}[/red]")
                    return 1

        results = []
        failed = False
        for platform, label, archive_path in archives:
            try:
                size = archive_path.stat().st_size
            except (PermissionError, OSError):
                import time as _time

                _time.sleep(2)
                size = archive_path.stat().st_size
            size_mb = size / (1024 * 1024)
            filename = f"gip-{platform}-{timestamp}.zip"

            if s3 and bucket_name:
                package_key = f"packages/{timestamp}/{filename}"
                try:
                    self._upload_file_with_retry(
                        s3, str(archive_path), bucket_name, package_key, config=self.S3_TRANSFER_CONFIG
                    )
                    url = presign_s3.generate_presigned_url(
                        "get_object",
                        Params={"Bucket": bucket_name, "Key": package_key},
                        ExpiresIn=expires_hours * 3600,
                    )
                    results.append((platform, label, filename, size_mb, url))
                    console.print(f"  [green]OK[/green]  {label} ({size_mb:.1f} MB)")
                except Exception as e:
                    failed = True
                    console.print(f"  [red]FAIL[/red] {label}: {e}")
                    self._print_upload_error_guidance(e, console)
            else:
                local_dir = Path(self.option("package-path"))
                local_dir.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(archive_path, local_dir / filename)
                    results.append((platform, label, filename, size_mb, None))
                    console.print(f"  [green]OK[/green]  {label} ({size_mb:.1f} MB) -> {local_dir / filename}")
                except OSError as e:
                    failed = True
                    console.print(f"  [red]FAIL[/red] {label}: {e}")

            try:
                archive_path.unlink()
            except (PermissionError, OSError):
                pass  # Ignore cleanup failure

        if failed:
            console.print("\n[red]One or more per-OS packages failed; distribution is incomplete.[/red]")
            return 1

        if not results:
            console.print("\n[red]No packages were created.[/red]")
            return 1

        # Display results
        expiration = published_at + timedelta(hours=expires_hours)
        console.print(f"\n[bold green]{len(results)} per-OS packages created.[/bold green]")
        if s3:
            console.print(f"[dim]URLs expire: {expiration.strftime('%Y-%m-%d %H:%M')}[/dim]")

        for platform, label, filename, size_mb, url in results:
            console.print(f"\n[bold]{label}[/bold] ({size_mb:.1f} MB)")
            if url:
                if "windows" in platform:
                    console.print("  PowerShell: $ProgressPreference = 'SilentlyContinue'")
                    print(f'  Invoke-WebRequest -Uri "{url}" -OutFile "{filename}"')
                else:
                    print(f'  curl -L -o "{filename}" "{url}"')

        console.print("\n[bold]After downloading, extract and run the installer:[/bold]")
        console.print("  Windows:    Expand-Archive <file>.zip . && cd gip-package && .\\install.bat")
        console.print("  Linux/Mac:  unzip <file>.zip && cd gip-package && chmod +x install.sh && ./install.sh")

        return 0

    @staticmethod
    def _print_upload_error_guidance(error: Exception, console: Console) -> None:
        """Print actionable guidance based on the type of upload error."""
        error_str = str(error).lower()

        if "ssl" in error_str or "certificate" in error_str or "cert" in error_str:
            console.print(
                "\n[yellow]This looks like an SSL/certificate error, commonly caused by corporate "
                "security tools (Zscaler, Netskope, etc.) that intercept HTTPS traffic.[/yellow]"
            )
            console.print("[yellow]Fixes:[/yellow]")
            console.print("  1. [cyan]pip install truststore[/cyan]  (makes Python trust your corporate CA)")
            console.print(
                "  2. [cyan]set AWS_CA_BUNDLE=C:\\path\\to\\corporate-root-ca.pem[/cyan]  (ask IT for the .pem file)"
            )
        elif "access denied" in error_str or "accessdenied" in error_str or isinstance(error, PermissionError):
            console.print("\n[yellow]This may be caused by:[/yellow]")
            console.print("  1. Corporate security tool (Zscaler, Netskope) intercepting S3 traffic")
            console.print("  2. Insufficient S3 permissions")
            console.print("  3. Antivirus scanning the ZIP file")
            console.print("\n[yellow]If you use a corporate security tool, try:[/yellow]")
            console.print("  [cyan]pip install truststore[/cyan]  (makes Python trust your corporate CA)")
            console.print("  [dim]Or: set AWS_CA_BUNDLE=C:\\path\\to\\corporate-root-ca.pem[/dim]")
        elif "connect" in error_str or "timeout" in error_str or "unreachable" in error_str:
            console.print(
                "\n[yellow]Network connectivity issue. Check your proxy/VPN settings and "
                "ensure S3 endpoints are reachable.[/yellow]"
            )

    @staticmethod
    def _read_file_with_retry(file_path: Path, max_attempts: int = 5) -> bytes:
        """Read a file with retry for Windows Defender scan locks.

        On Windows, Defender scans .exe files and ZIPs containing .exe files
        immediately after creation, holding a file lock that causes PermissionError.
        """
        import time

        for attempt in range(max_attempts):
            try:
                return file_path.read_bytes()
            except (PermissionError, OSError) as e:
                if attempt < max_attempts - 1:
                    time.sleep(2)
                else:
                    raise PermissionError(
                        f"Cannot read '{file_path}' after {max_attempts} attempts. "
                        f"This may be caused by antivirus software scanning the file. "
                        f"Original error: {e}"
                    ) from e

    @staticmethod
    def _upload_file_with_retry(
        s3_client,
        file_path: str,
        bucket: str,
        key: str,
        extra_args=None,
        config=None,
        callback=None,
        max_attempts: int = 5,
    ):
        """Upload a file to S3 with retry for Windows Defender scan locks.

        boto3.upload_file internally opens the file for reading. On Windows,
        if Defender is scanning a newly created ZIP (containing .exe files),
        the open() call fails with PermissionError.
        """
        import time

        for attempt in range(max_attempts):
            try:
                kwargs = {"Filename": file_path, "Bucket": bucket, "Key": key}
                if extra_args:
                    kwargs["ExtraArgs"] = extra_args
                if config:
                    kwargs["Config"] = config
                if callback:
                    kwargs["Callback"] = callback
                s3_client.upload_file(**kwargs)
                return
            except (PermissionError, OSError) as e:
                # File system lock from antivirus scanning
                if attempt < max_attempts - 1:
                    time.sleep(2)
                    continue
                raise PermissionError(
                    f"Cannot upload '{file_path}' after {max_attempts} attempts. "
                    f"This may be caused by antivirus software scanning the file. "
                    f"Original error: {e}"
                ) from e
            except Exception as e:
                # Check if boto3 wrapped the PermissionError in another exception
                if isinstance(e.__cause__, (PermissionError, OSError)) or "denied" in str(e).lower():
                    if attempt < max_attempts - 1:
                        time.sleep(2)
                        continue
                raise

    def _add_extra_files_to_zip(self, zf, package_path: Path, entries, platform_token) -> None:
        """Add admin-defined extra files to an open zip, filtered by platform.

        Extras were already copied into ``package_path`` by ``package`` (distribute
        never re-reads the ``from`` source), so this reads plain files/dirs from the
        build folder and writes them under the ``gip-package/`` prefix using
        ``writestr()`` + ``_read_file_with_retry()`` (no temp files — SSL/AV invariant).

        ``platform_token=None`` includes every extra (the all-OS archive). Otherwise
        an entry is included only when its ``targets`` match ``platform_token`` via
        ``extra_applies_to`` (per-OS zips and landing-family zips).
        """
        from governed_inference_platform.extra_files import extra_applies_to, validate_extra_files

        if not entries or validate_extra_files(entries):
            # No extras, or an invalid config — package already fails fast on
            # invalid entries, so skip silently rather than shipping garbage.
            return

        for entry in entries:
            if platform_token is not None and not extra_applies_to(entry["targets"], platform_token):
                continue
            name = entry["name"]
            src = package_path / name
            if not src.exists():
                continue
            if src.is_dir():
                for f in src.rglob("*"):
                    if f.is_file():
                        rel = f.relative_to(package_path)
                        zf.writestr(f"gip-package/{rel.as_posix()}", self._read_file_with_retry(f))
            else:
                zf.writestr(f"gip-package/{name}", self._read_file_with_retry(src))

    def _add_package_tree_to_zip(self, zf, package_path: Path, tree_name: str) -> None:
        """Add a generated nested package tree without flattening it."""
        # F-021: Harness configs live below harnesses/<name>/ and must retain paths.
        tree = package_path / tree_name
        if not tree.is_dir():
            return
        for path in tree.rglob("*"):
            if path.is_file():
                relative = path.relative_to(package_path).as_posix()
                zf.writestr(f"gip-package/{relative}", self._read_file_with_retry(path))

    def _create_archive(self, package_path: Path, extra_files=None) -> Path:
        """Create a zip archive of the package directory.

        Builds the ZIP directly from source files using writestr() to avoid
        temp directory operations that fail on Windows with spaces in paths
        or when antivirus locks newly-written files.

        ``extra_files`` (admin-defined) are added unconditionally (all-OS archive).
        """
        import zipfile

        archive_path = package_path / "gip-package.zip"

        # Files to include in the package
        required_files = [
            # Executables for each platform
            "credential-process-macos-arm64",
            "credential-process-macos-intel",
            "credential-process-linux-x64",
            "credential-process-linux-arm64",
            "credential-process-windows.exe",
            # OTEL helpers
            "otel-helper-macos-arm64",
            "otel-helper-macos-intel",
            "otel-helper-linux-x64",
            "otel-helper-linux-arm64",
            "otel-helper-windows.exe",
            "otel-helper.sh",
            # Windows otel-helper launcher + AV-resilient fallback (required by install.bat)
            "otel-helper.ps1",
            "otel-helper.cmd",
            # OTEL Collector sidecar
            "otelcol-macos-arm64",
            "otelcol-macos-intel",
            "otelcol-linux-x64",
            "otelcol-linux-arm64",
            "otelcol-windows.exe",
            "collector-config.yaml",
            # Installation scripts
            "install.sh",
            "install.bat",
            "gip-install.ps1",
            # Configuration
            "config.json",
            "README.md",
            # CoWork 3P MDM configs (optional — only present when CoWork is enabled)
            "cowork-3p.reg",
            "cowork-3p.mobileconfig",
            "cowork-3p-config.json",
        ]
        windows_files = {
            "credential-process-windows.exe",
            "otel-helper-windows.exe",
            "otel-helper.ps1",
            "otel-helper.cmd",
            "otelcol-windows.exe",
            "install.bat",
            "gip-install.ps1",
            "cowork-3p.reg",
        }
        windows_ready = (package_path / "credential-process-windows.exe").is_file()

        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
            # Add required files directly from source
            for filename in required_files:
                if filename in windows_files and not windows_ready:
                    continue
                source_file = package_path / filename
                if source_file.exists():
                    zf.writestr(f"gip-package/{filename}", self._read_file_with_retry(source_file))

            # Include gip-settings directory if it exists
            settings_dir = package_path / "gip-settings"
            if settings_dir.exists() and settings_dir.is_dir():
                for f in settings_dir.rglob("*"):
                    if f.is_file():
                        rel_path = f.relative_to(package_path)
                        zf.writestr(f"gip-package/{rel_path.as_posix()}", self._read_file_with_retry(f))

            self._add_package_tree_to_zip(zf, package_path, "harnesses")

            # All-OS archive gets every extra file (platform_token=None).
            self._add_extra_files_to_zip(zf, package_path, extra_files, None)

        return archive_path

    # Platform-to-files mapping for per-OS packages
    PLATFORM_FILES = {
        "windows": {
            "binaries": ["credential-process-windows.exe", "otel-helper-windows.exe"],
            "installer": ["install.bat", "gip-install.ps1", "otel-helper.ps1", "otel-helper.cmd"],
            "label": "Windows",
        },
        "linux-x64": {
            "binaries": ["credential-process-linux-x64", "otel-helper-linux-x64"],
            "installer": "install.sh",
            "label": "Linux x64",
        },
        "linux-arm64": {
            "binaries": ["credential-process-linux-arm64", "otel-helper-linux-arm64"],
            "installer": "install.sh",
            "label": "Linux ARM64",
        },
        "macos-arm64": {
            "binaries": ["credential-process-macos-arm64", "otel-helper-macos-arm64"],
            "installer": "install.sh",
            "label": "macOS ARM64",
        },
        "macos-intel": {
            "binaries": ["credential-process-macos-intel", "otel-helper-macos-intel"],
            "installer": "install.sh",
            "label": "macOS Intel",
        },
    }

    def _create_per_os_archives(self, package_path: Path, extra_files=None) -> list[tuple[str, Path]]:
        """Create separate zip archives per OS platform. Returns list of (platform_label, archive_path).

        Builds ZIPs directly from source files using writestr() to avoid
        temp directory operations that fail on Windows with spaces in paths.

        ``extra_files`` are filtered per platform via the per-OS token (``macos-arm64``,
        ``windows``, ``linux-x64``, …) so a ``macos`` extra never lands in the Windows zip.
        """
        import zipfile

        archives = []
        shared_files = ["config.json", "README.md"]

        for platform, pconfig in self.PLATFORM_FILES.items():
            # Check if the primary binary exists
            primary_binary = pconfig["binaries"][0]
            if not (package_path / primary_binary).exists():
                continue

            archive_path = package_path / f"gip-{platform}.zip"

            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
                # Add platform binaries
                for binary in pconfig["binaries"]:
                    src = package_path / binary
                    if src.exists():
                        zf.writestr(f"gip-package/{binary}", self._read_file_with_retry(src))

                # Add installer(s)
                installers = pconfig["installer"] if isinstance(pconfig["installer"], list) else [pconfig["installer"]]
                for inst in installers:
                    src = package_path / inst
                    if src.exists():
                        zf.writestr(f"gip-package/{inst}", self._read_file_with_retry(src))

                # Add shared files
                for sf in shared_files:
                    src = package_path / sf
                    if src.exists():
                        zf.writestr(f"gip-package/{sf}", self._read_file_with_retry(src))

                # Add gip-settings
                settings_dir = package_path / "gip-settings"
                if settings_dir.exists() and settings_dir.is_dir():
                    for f in settings_dir.rglob("*"):
                        if f.is_file():
                            rel_path = f.relative_to(package_path)
                            zf.writestr(f"gip-package/{rel_path.as_posix()}", self._read_file_with_retry(f))

                self._add_package_tree_to_zip(zf, package_path, "harnesses")

                # Add matching extras for this per-OS token (e.g. macos-arm64).
                self._add_extra_files_to_zip(zf, package_path, extra_files, platform)

            archives.append((platform, pconfig["label"], archive_path))

        return archives

    def _calculate_checksum(self, file_path: Path) -> str:
        """Calculate SHA256 checksum of a file.

        Retries on Windows where Defender may lock newly created ZIP files
        containing executables during real-time scanning.
        """
        import time

        sha256_hash = hashlib.sha256()
        for attempt in range(5):
            try:
                with open(file_path, "rb") as f:
                    for byte_block in iter(lambda: f.read(4096), b""):
                        sha256_hash.update(byte_block)
                return sha256_hash.hexdigest()
            except (PermissionError, OSError):
                if attempt < 4:
                    time.sleep(2)
                    sha256_hash = hashlib.sha256()  # Reset for retry
                else:
                    raise

    def _generate_restricted_url(self, s3_client, bucket: str, key: str, allowed_ips: str, expires_hours: int) -> str:
        """Reject unenforced IP restrictions defensively at the URL boundary."""
        raise ValueError(self.ALLOWED_IPS_UNSUPPORTED)

    def _display_qr_code(self, url: str, console: Console):
        """Display a QR code for the URL if qrcode library is available."""
        try:
            import qrcode

            qr = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_L,
                box_size=1,
                border=1,
            )
            qr.add_data(url)
            qr.make(fit=True)

            console.print("\n[bold]QR Code for distribution URL:[/bold]")
            qr.print_ascii(invert=True)

        except ImportError:
            console.print("\n[dim]QR code display requires: pip install qrcode[/dim]")

    def _show_download_stats(self, profile, package_key: str, console: Console):
        """Show download statistics if available (requires S3 access logs)."""
        # This would require S3 access logs to be configured and queryable
        # For now, just show a placeholder
        console.print("\n[dim]Download tracking requires S3 access logs configuration.[/dim]")

    def _format_size(self, size_bytes: int) -> str:
        """Format file size in human-readable format."""
        for unit in ["B", "KB", "MB", "GB"]:
            if size_bytes < 1024.0:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024.0
        return f"{size_bytes:.1f} TB"

    def _download_windows_artifacts(self, profile, package_path: Path, console: Console) -> bool:
        """Download Windows build artifacts from S3."""
        import zipfile

        from botocore.exceptions import ClientError

        from governed_inference_platform.cli.utils.aws import get_stack_outputs

        try:
            # Windows artifacts are always in the CodeBuild bucket
            if not profile.enable_codebuild:
                console.print("[red]CodeBuild is not enabled for this profile[/red]")
                return False

            codebuild_stack_name = profile.stack_names.get("codebuild", f"{profile.identity_pool_name}-codebuild")
            codebuild_outputs = get_stack_outputs(codebuild_stack_name, get_codebuild_region(profile))

            if not codebuild_outputs:
                console.print("[red]CodeBuild stack not found[/red]")
                return False

            bucket_name = codebuild_outputs.get("BuildBucket")
            project_name = codebuild_outputs.get("ProjectName")

            if not bucket_name or not project_name:
                console.print("[red]Could not get CodeBuild bucket or project name from stack outputs[/red]")
                return False

            # Download from S3 (CodeBuild BuildBucket, in the codebuild region)
            s3 = boto3.client("s3", region_name=get_codebuild_region(profile))
            zip_path = package_path / "windows-binaries.zip"

            # CodeBuild stores artifacts at root of bucket
            artifact_key = "windows-binaries.zip"

            try:
                s3.download_file(bucket_name, artifact_key, str(zip_path))

                # Extract binaries one-by-one using read/write to avoid
                # Windows Defender file locking issues with extractall()
                with zipfile.ZipFile(zip_path, "r") as zip_ref:
                    for member in zip_ref.namelist():
                        # Skip directories
                        if member.endswith("/"):
                            continue
                        # Extract just the filename (flatten any directory structure)
                        filename = Path(member).name
                        if not filename:
                            continue
                        target_path = package_path / filename
                        data = zip_ref.read(member)
                        # Write with retry for Windows Defender scan locks
                        for attempt in range(3):
                            try:
                                target_path.write_bytes(data)
                                break
                            except PermissionError:
                                if attempt < 2:
                                    import time

                                    time.sleep(1)
                                else:
                                    raise

                # Clean up
                zip_path.unlink()
                return True

            except ClientError as e:
                console.print(f"[red]Failed to download artifacts: {e}[/red]")
                console.print(f"[dim]Tried: s3://{bucket_name}/{artifact_key}[/dim]")
                return False

        except Exception as e:
            console.print(f"[red]Error downloading Windows artifacts: {e}[/red]")
            return False
