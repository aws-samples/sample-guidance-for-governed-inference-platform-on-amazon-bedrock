# ABOUTME: Package command for building distribution packages
# ABOUTME: Creates ready-to-distribute packages with embedded configuration

"""Package command - Build distribution packages."""

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import questionary
from cleo.commands.command import Command
from cleo.helpers import option
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

from governed_inference_platform.cli.utils.aws import get_stack_outputs
from governed_inference_platform.cli.utils.display import display_configuration_info
from governed_inference_platform.cli.utils.helpers import get_codebuild_region
from governed_inference_platform.cli.utils.proc import run_checked
from governed_inference_platform.cli.validators import validate_profile_for_packaging
from governed_inference_platform.config import Config
from governed_inference_platform.models import (
    get_source_region_for_profile,
)


def _is_interactive() -> bool:
    """Return True when stdin is a TTY and prompts can be displayed."""
    return sys.stdin.isatty()


# Runtime packages bundled into the credential provider binary.
_CREDENTIAL_PROVIDER_RUNTIME_DEPS = ["boto3", "requests", "PyJWT", "keyring", "cryptography"]
_OTEL_HELPER_RUNTIME_DEPS: list[str] = []  # otel_helper uses only stdlib
_PYINSTALLER_PIN = "pyinstaller==6.*"

# Single source of truth for Go cross-compilation targets, shared by the auth-binary
# build (_build_go_binaries) and the collector sidecar build (_build_otelcol). Keeping
# this in one place prevents the two paths from drifting (e.g. one learning about a new
# arch the other doesn't). Maps a package platform key → (GOOS, GOARCH).
_GO_PLATFORM_MAP: dict[str, tuple[str, str]] = {
    "macos-arm64": ("darwin", "arm64"),
    "macos-intel": ("darwin", "amd64"),
    "macos": ("darwin", "arm64"),  # generic macos defaults to arm64
    "linux-x64": ("linux", "amd64"),
    "linux-arm64": ("linux", "arm64"),
    "linux": ("linux", "amd64"),  # generic linux defaults to amd64
    "windows": ("windows", "amd64"),
}

PLATFORM_CANONICAL = {
    "linux": "linux-x64",
    "macos": "macos-arm64",
}


def _default_go_platforms(host_system: str | None = None) -> list[str]:
    platforms = ["linux-x64", "linux-arm64", "windows"]
    if (host_system or platform.system()) == "Darwin":
        return ["macos-arm64", "macos-intel", *platforms]
    return platforms


def _resolve_platforms_to_build(
    target_platform: str | list[str],
    *,
    use_go: bool,
    host_system: str,
    host_machine: str,
    universal2_available: bool,
    docker_available: bool,
    codebuild_enabled: bool,
) -> list[str]:
    """Resolve requested platforms without probing the host or mutating state."""
    requested = target_platform if isinstance(target_platform, list) else [target_platform]
    if use_go:
        requested = [PLATFORM_CANONICAL.get(platform_name, platform_name) for platform_name in requested]

    if use_go and "all" in requested:
        return ["macos-arm64", "macos-intel", "linux-x64", "linux-arm64", "windows"]
    if "all" not in requested:
        return list(dict.fromkeys(requested))

    resolved: list[str] = []
    if host_system == "darwin":
        host_platform = "macos-arm64" if host_machine == "arm64" else "macos-intel"
        resolved.append(host_platform)
        cross_platform = "macos-intel" if host_platform == "macos-arm64" else "macos-arm64"
        if universal2_available:
            resolved.append(cross_platform)
        if docker_available:
            resolved.extend(["linux-x64", "linux-arm64"])
    elif host_system == "linux":
        resolved.append("linux")
    elif host_system == "windows":
        resolved.append("windows")

    if host_system != "windows" and codebuild_enabled:
        resolved.append("windows")

    resolved.extend(platform_name for platform_name in requested if platform_name != "all")
    return list(dict.fromkeys(resolved))


def _copy_windows_otel_fallbacks(output_dir: Path, source_dir: Path | None = None) -> list[Path]:
    source_dir = source_dir or Path(__file__).resolve().parent.parent.parent.parent / "otel_helper"
    sources = [source_dir / script_name for script_name in ("otel-helper.ps1", "otel-helper.cmd")]
    missing = next((path for path in sources if not path.is_file()), None)
    if missing:
        raise FileNotFoundError(f"Required Windows monitoring fallback missing: {missing}")

    copied = []
    for script_src in sources:
        destination = output_dir / script_src.name
        shutil.copy2(script_src, destination)
        copied.append(destination)
    return copied


def _go_ldflags(goos: str) -> str:
    """Return the ldflags for a Go build targeting goos.

    Windows binaries must NOT be stripped: Defender cloud ML (Wacatac.B!ml) flags
    stripped Go binaries in subprocess/non-interactive contexts. Everywhere else we
    strip (-s -w) for size. This rule applies identically to credential-process,
    otel-helper, and the otelcol sidecar — hence one shared helper.

    Always injects version and commit via -X flags so --version and --explain
    report the build origin (critical for beta vs release troubleshooting).
    """
    import subprocess

    # Resolve version from git tags
    try:
        ver = subprocess.check_output(  # nosec B603 — fixed argv (git), no shell
            ["git", "describe", "--tags", "--always", "--dirty"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        ver = "dev"

    # Resolve commit SHA
    try:
        commit = subprocess.check_output(  # nosec B603 — fixed argv (git), no shell
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        commit = "unknown"

    version_flags = f"-X gip-go/internal/version.Version={ver} -X gip-go/internal/version.Commit={commit}"
    strip_flags = "" if goos == "windows" else "-s -w"
    return f"{strip_flags} {version_flags}".strip()


def _find_universal2_python() -> Path | None:
    """Return the first universal2 Python ≥3.10 found in the standard python.org install location, or None."""
    import glob

    candidates = sorted(
        glob.glob("/Library/Frameworks/Python.framework/Versions/*/bin/python3*"),
        reverse=True,  # prefer higher versions
    )
    for candidate in candidates:
        p = Path(candidate)
        if not p.is_file() or not p.stat().st_size:
            continue
        result = subprocess.run(["/usr/bin/lipo", "-info", str(p)], capture_output=True, text=True)  # nosec B603 B607
        if result.returncode != 0:
            continue
        out = result.stdout.strip()
        # universal2 shows "are: x86_64 arm64" or "are: arm64 x86_64"
        if "are:" in out and "x86_64" in out and "arm64" in out:
            # Verify version ≥3.10
            ver = run_checked([str(p), "--version"], capture_output=True, text=True)  # nosec B603 B607
            if ver.returncode == 0:
                try:
                    parts = ver.stdout.strip().split()[1].split(".")
                    if int(parts[0]) >= 3 and int(parts[1]) >= 10:
                        return p
                except (IndexError, ValueError):
                    continue
    return None


def _ensure_cross_arch_venv(arch: str, universal2_python: Path, runtime_packages: list[str], console: Console) -> Path:
    """Create (or reuse) ~/.gip/build-venvs/<arch>/ seeded from a universal2 Python.

    The venv is created under `arch -<arch>` so pip pulls arch-matched wheels for every
    native extension (cffi, cryptography, etc.). Reused on subsequent runs unless stale.
    """
    venv_dir = Path.home() / ".gip" / "build-venvs" / arch
    pyinstaller_bin = venv_dir / "bin" / "pyinstaller"
    python_bin = venv_dir / "bin" / "python3"

    if pyinstaller_bin.exists() and python_bin.exists():
        # Validate the venv's Python is actually the right arch
        result = subprocess.run(["/usr/bin/lipo", "-info", str(python_bin)], capture_output=True, text=True)  # nosec B603 B607
        if result.returncode == 0 and arch in result.stdout:
            return venv_dir
        # Wrong arch — rebuild
        import shutil

        console.print(f"[yellow]Rebuilding {arch} build venv (wrong architecture detected)[/yellow]")
        shutil.rmtree(venv_dir, ignore_errors=True)

    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    console.print(f"[cyan]Preparing {arch} build venv at {venv_dir} (first run, ~30s)...[/cyan]")

    create = subprocess.run(  # nosec B603 B607
        ["/usr/bin/arch", f"-{arch}", str(universal2_python), "-m", "venv", str(venv_dir)],
        capture_output=True,
        text=True,
    )
    if create.returncode != 0:
        raise RuntimeError(f"Failed to create {arch} build venv: {create.stderr}")

    pip = venv_dir / "bin" / "pip"
    install = subprocess.run(  # nosec B603 B607
        ["/usr/bin/arch", f"-{arch}", str(pip), "install", "--quiet", _PYINSTALLER_PIN, *runtime_packages],
        capture_output=True,
        text=True,
    )
    if install.returncode != 0:
        raise RuntimeError(f"Failed to install deps into {arch} build venv: {install.stderr or install.stdout}")

    console.print(f"[green]✓ {arch} build venv ready[/green]")
    return venv_dir


def _assert_host_os_can_build_macos() -> None:
    """Refuse to produce macOS binaries from a non-macOS host.

    PyInstaller is not a cross-OS compiler: run on Linux, it emits Linux ELF
    binaries regardless of --target-arch. Without this guard, a Linux-host
    build would write ELF content into a macOS-named output file, and the
    resulting package would fail on end-user Macs with "exec format error"
    (the macOS kernel cannot load ELF binaries and Rosetta only translates
    Mach-O, not ELF).

    macOS binaries must be built on macOS — this is a property of Apple's
    platform, not a limitation of this tool.
    """
    host = platform.system().lower()
    if host == "darwin":
        return
    raise RuntimeError(
        f"Cannot build macOS binaries on {platform.system()}.\n"
        f"\n"
        f"PyInstaller cannot cross-compile across operating systems. On "
        f"{platform.system()}, it produces {platform.system()}-native binaries "
        f"regardless of --target-arch.\n"
        f"\n"
        f"Options for producing macOS binaries:\n"
        f"  1. Run `gip package` on a macOS workstation\n"
        f"  2. Use a CI macOS runner (GitHub Actions `macos-latest`, AWS\n"
        f"     CodeBuild macOS project, or a self-hosted Mac runner) and\n"
        f"     collect the artifacts from there\n"
        f"  3. Drop macOS targets from this build — run with\n"
        f"     --target-platform=linux-x64,linux-arm64,windows\n"
        f"     to build only the platforms your current host supports"
    )


class PackageCommand(Command):
    """
    Build distribution packages for your organization

    package
        {--target-platform=all : Target platform(s), comma-separated (macos-arm64, linux-x64, windows, all)}
    """

    name = "package"
    description = "Build distribution packages with embedded configuration"

    options = [
        option(
            "target-platform",
            description="Target platform(s): macos-arm64, macos-intel, linux-x64, linux-arm64, windows, all. Comma-separated for multiple.",
            flag=False,
            default=None,
        ),
        option(
            "profile", description="Configuration profile to use (defaults to active profile)", flag=False, default=None
        ),
        option(
            "status",
            description="[DEPRECATED] Use 'gip builds' instead. Check build status by ID or 'latest'",
            flag=False,
            default=None,
        ),
        option("build-local", description="[DEPRECATED no-op] Go builds are always local", flag=True),
        option("no-cache", description="[DEPRECATED no-op] No pre-built binary cache is used", flag=True),
        option("build-verbose", description="Enable verbose logging for build processes", flag=True),
        option(
            "regenerate-installers",
            description="Regenerate installer scripts using existing binaries from latest dist",
            flag=True,
        ),
        option(
            "go",
            description="Build using Go (default; kept for backwards compatibility)",
            flag=True,
        ),
        option(
            "legacy",
            description="Use legacy PyInstaller/Nuitka build instead of Go (deprecated)",
            flag=True,
        ),
        option(
            "skip-validation",
            description="Skip configuration validation checks",
            flag=True,
        ),
        option(
            "prepare-offline",
            description="Prepare an offline bundle (OCB binary + Go module cache) for air-gapped builds",
            flag=True,
        ),
        option(
            "harnesses",
            description=(
                "Coding harnesses to generate configs for: claude-code (default), "
                "opencode, codex, pi, aider, or 'all'. Comma-separated."
            ),
            flag=False,
            default=None,
        ),
    ]

    def handle(self) -> int:
        """Execute the package command."""
        import platform
        import subprocess

        console = Console()

        if self.option("build-local"):
            console.print("[yellow]--build-local is deprecated and has no effect; Go builds are always local.[/yellow]")
        if self.option("no-cache"):
            console.print(
                "[yellow]--no-cache is deprecated and has no effect; no pre-built binary cache is used.[/yellow]"
            )

        # Check if this is a status check (deprecated - moved to builds command)
        if self.option("status") is not None:
            console.print("[yellow]⚠️  DEPRECATED: Status check has moved to the builds command[/yellow]")
            console.print("\nUse one of these commands instead:")
            console.print("  • [cyan]poetry run gip builds[/cyan]                    (list all recent builds)")
            console.print("  • [cyan]poetry run gip builds --status <build-id>[/cyan] (check specific build)")
            console.print("  • [cyan]poetry run gip builds --status latest[/cyan]    (check latest build)")
            console.print("\nRedirecting to builds command...\n")
            return self._check_build_status(self.option("status"), console)

        # Prepare offline bundle for air-gapped environments
        if self.option("prepare-offline"):
            return self._prepare_offline_bundle(console)

        # Load configuration first (needed to check CodeBuild status)
        config = Config.load()
        # Use specified profile or default to active profile, or fall back to "gip"
        profile_name = self.option("profile") or config.active_profile or "gip"
        profile = config.get_profile(profile_name)

        if not profile:
            console.print("[red]No deployment found. Run 'poetry run gip init' first.[/red]")
            return 1

        # Run configuration validation (unless skipped)
        if not self.option("skip-validation"):
            validation_errors = validate_profile_for_packaging(profile)
            if validation_errors:
                has_errors = False
                for err in validation_errors:
                    if err.severity == "error":
                        console.print(f"[red]✗ [{err.field}] {err.message}[/red]")
                        has_errors = True
                    else:
                        console.print(f"[yellow]⚠ [{err.field}] {err.message}[/yellow]")
                if has_errors:
                    console.print(
                        "\n[red]Configuration validation failed. "
                        "Fix the errors above or re-run with --skip-validation to bypass.[/red]"
                    )
                    return 1
                console.print()  # blank line after warnings

        # Regenerate installers from existing binaries (no rebuild needed)
        if self.option("regenerate-installers"):
            return self._regenerate_installers(profile, profile_name, console)

        # Resolve extra harness selection early (fail fast before any build).
        # CLI flag wins; otherwise the profile's packaging-side `harnesses`
        # field applies. Default is claude-code only -> no extra output.
        from governed_inference_platform.cli.utils.harness_configs import resolve_harness_selection

        harness_selection = self.option("harnesses") or ",".join(getattr(profile, "harnesses", None) or ["claude-code"])
        try:
            extra_harnesses = resolve_harness_selection(harness_selection)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            return 1

        # Go build mode: default unless --legacy is explicitly passed
        use_legacy = self.option("legacy")
        use_go = not use_legacy  # Go is now the default

        if self.option("go") and use_legacy:
            console.print("[yellow]Both --go and --legacy passed; using --legacy.[/yellow]")

        # Check Go availability and version when using Go path
        if use_go:
            try:
                go_result = subprocess.run(["go", "version"], capture_output=True, text=True, check=True)  # nosec B603 — fixed argv (go), no shell
                version_str = go_result.stdout.strip().split()[2].lstrip("go")  # e.g. "1.24.2"
                major_minor = tuple(int(x) for x in version_str.split(".")[:2])
                if major_minor < (1, 24):
                    console.print(
                        f"[yellow]Go {version_str} found but >= 1.24 required. "
                        f"Falling back to legacy build mode.[/yellow]"
                    )
                    console.print("[dim]Update Go: https://go.dev/dl/[/dim]")
                    use_go = False
            except (FileNotFoundError, subprocess.CalledProcessError):
                console.print("[yellow]Go not found. Falling back to legacy build mode.[/yellow]")
                console.print("[dim]Install Go from https://go.dev/dl/ for faster, more reliable builds.[/dim]")
                use_go = False
            except (IndexError, ValueError):
                pass  # Could not parse version — proceed anyway

        # Interactive prompts if not provided via CLI
        target_platform = self.option("target-platform")

        if target_platform is None:
            if use_go:
                target_platform = _default_go_platforms()
            else:
                target_platform = "all"

        # Support comma-separated platforms: --target-platform=linux-x64,macos-arm64,windows
        if target_platform and target_platform != "all" and "," in target_platform:
            target_platform = [p.strip() for p in target_platform.split(",")]
        elif target_platform == "all" and use_go:
            # With Go, "all" builds all 5 platforms without prompting
            target_platform = ["macos-arm64", "macos-intel", "linux-x64", "linux-arm64", "windows"]
        elif target_platform == "all":
            # Build list of available platform choices
            # Note: "macos" is omitted because it's just a smart alias for the current architecture
            # Users should explicitly choose macos-arm64 or macos-intel for clarity
            platform_choices = [
                "macos-arm64",
                "macos-intel",
                "linux-x64",
                "linux-arm64",
            ]

            # With Go, Windows is always available
            if use_go:
                platform_choices.append("windows")
            elif hasattr(profile, "enable_codebuild") and profile.enable_codebuild:
                platform_choices.append("windows")

            # Use checkbox for multiple selection (require at least one)
            if _is_interactive():
                selected_platforms = questionary.checkbox(
                    "Which platform(s) do you want to build for? (Use space to select, enter to confirm)",
                    choices=platform_choices,
                    validate=lambda x: len(x) > 0 or "You must select at least one platform",
                ).ask()
            else:
                # Non-interactive: build all available platforms
                selected_platforms = platform_choices
                console.print(
                    f"[dim]Non-interactive mode: building all platforms ({', '.join(selected_platforms)})[/dim]"
                )

            # Use the selected platforms (guaranteed to have at least one due to validation)
            target_platform = selected_platforms if len(selected_platforms) > 1 else selected_platforms[0]

        # Prompt for co-authorship preference (default to No - opt-in approach)
        if _is_interactive():
            include_coauthored_by = questionary.confirm(
                "Include 'Co-Authored-By: Claude' in git commits?",
                default=False,
            ).ask()
        else:
            include_coauthored_by = False

        # Prompt for custom OTel resource attributes (only when monitoring is enabled)
        otel_resource_attributes = None
        if profile.monitoring_enabled:
            if _is_interactive():
                customize_otel = questionary.confirm(
                    "Customize telemetry resource attributes? (department, team, cost center)",
                    default=False,
                ).ask()
            else:
                customize_otel = False

            if customize_otel:
                console.print(
                    "[dim]Example: department=platform, team.id=infra-core, "
                    "cost_center=CC-4521, organization=acme-corp[/dim]"
                )
                department = questionary.text("Department:", default="engineering").ask()
                team_id = questionary.text("Team ID:", default="default").ask()
                cost_center = questionary.text("Cost center:", default="default").ask()
                organization = questionary.text("Organization:", default="default").ask()
                otel_resource_attributes = (
                    f"department={department},team.id={team_id},cost_center={cost_center},organization={organization}"
                )

        # Validate platform
        valid_platforms = ["macos", "macos-arm64", "macos-intel", "linux", "linux-x64", "linux-arm64", "windows", "all"]
        if isinstance(target_platform, list):
            for platform_name in target_platform:
                if platform_name not in valid_platforms:
                    console.print(
                        f"[red]Invalid platform: {platform_name}. Valid options: {', '.join(valid_platforms)}[/red]"
                    )
                    return 1
        elif target_platform not in valid_platforms:
            console.print(
                f"[red]Invalid platform: {target_platform}. Valid options: {', '.join(valid_platforms)}[/red]"
            )
            return 1

        # Resolve federation identifier (role ARN for direct STS, Identity Pool ID
        # for Cognito). See _resolve_federation: Cognito ALWAYS reads the pool ID
        # from CloudFormation stack outputs because identity_pool_name is only a
        # name, not the "<region>:<uuid>" pool ID.
        federation_type, identity_pool_id, federated_role_arn = self._resolve_federation(profile, console)
        if getattr(profile, "sso_enabled", True) and not (identity_pool_id or federated_role_arn):
            return 1

        # Welcome
        console.print(
            Panel.fit(
                "[bold cyan]Package Builder[/bold cyan]\n\n"
                f"Creating distribution package for {profile.provider_domain}",
                border_style="cyan",
                padding=(1, 2),
            )
        )

        # Create timestamped output directory under profile name.
        # Resolve to absolute immediately: subsequent Go / CodeBuild builds run
        # subprocesses with cwd=<elsewhere>, and a relative path here would be
        # interpreted relative to THAT cwd (binaries would land in source/go/
        # dist/... while the config files stay in source/dist/...). Absolute
        # path keeps binaries and config co-located.
        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        output_dir = (Path.cwd() / "dist" / profile_name / timestamp).resolve()

        # Create output directory
        output_dir.mkdir(parents=True, exist_ok=True)

        # Show what will be packaged using shared display utility
        display_configuration_info(profile, identity_pool_id or federated_role_arn, format_type="simple")

        # Determine if this is a zero-binary IDC deployment
        # IDC users authenticate via 'aws sso login' (no credential-process) and
        # get static identity baked into the collector config (no otel-helper).
        # Exception: if quota enforcement is configured, credential-process IS needed.
        _is_idc_auth = getattr(profile, "effective_auth_type", profile.auth_type) == "idc"
        _has_quota = bool(getattr(profile, "quota_api_endpoint", None))
        is_idc_zero_binary = _is_idc_auth and not _has_quota
        idc_user_email = None
        if _is_idc_auth and _has_quota:
            console.print(
                "\n[dim]IDC auth + quota enforcement detected — credential-process binary will be included[/dim]"
            )
        elif is_idc_zero_binary:
            console.print("\n[bold]IDC zero-binary package mode:[/bold]")
            console.print("  ✅ Authentication: aws sso login (no binary needed)")
            console.print("  ✅ Monitoring: static identity in collector config")
            console.print("  ⚠️  Quota enforcement: disabled (requires credential-process binary)")
            console.print("[dim]To enable quota enforcement, run: gip init → enable quota monitoring[/dim]")
            console.print()

            # Auto-detect user email from STS caller identity (IDC ARN session name = email)
            try:
                import boto3

                sts = boto3.client("sts", region_name=profile.aws_region)
                identity = sts.get_caller_identity()
                arn = identity.get("Arn", "")
                # IDC ARN format: arn:aws:sts::ACCOUNT:assumed-role/RoleName/user@company.com
                session_name = arn.rsplit("/", 1)[-1] if "/" in arn else ""
                if "@" in session_name:
                    idc_user_email = session_name
                    console.print(f"[dim]Detected user email from IDC: {idc_user_email}[/dim]")
                elif _is_interactive():
                    # Prompt if we can't auto-detect
                    idc_user_email = questionary.text(
                        "User email for OTEL attribution (IDC session name):",
                        default=session_name or "",
                    ).ask()
                else:
                    idc_user_email = session_name or ""
            except Exception:
                if _is_interactive():
                    idc_user_email = questionary.text(
                        "User email for OTEL attribution:",
                        default="",
                    ).ask()
                else:
                    idc_user_email = ""

            if not idc_user_email:
                console.print("[yellow]Warning: No user email — OTEL attribution will be anonymous[/yellow]")

        # Build package
        console.print("\n[bold]Building package...[/bold]")

        # Cross-arch macOS builds auto-create per-arch venvs from a universal2 Python.
        # Detect once here so both the platforms_to_build assembly and _build_macos_pyinstaller share the same result.
        host_system = platform.system().lower()
        host_machine = platform.machine().lower()
        _universal2_python = _find_universal2_python() if host_system == "darwin" else None
        all_requested = target_platform == "all" or (isinstance(target_platform, list) and "all" in target_platform)
        docker_available = False
        if not use_go and all_requested and host_system == "darwin":
            try:
                docker_available = subprocess.run(["docker", "--version"], capture_output=True).returncode == 0  # nosec B603 — fixed argv (docker), no shell
            except FileNotFoundError:
                pass
        if not use_go and all_requested and host_system == "darwin" and not _universal2_python:
            cross_platform = "macos-intel" if host_machine == "arm64" else "macos-arm64"
            console.print(
                f"[dim]Note: {cross_platform} skipped — install Python universal2 from python.org to enable.[/dim]"
            )

        platforms_to_build = _resolve_platforms_to_build(
            target_platform,
            use_go=use_go,
            host_system=host_system,
            host_machine=host_machine,
            universal2_available=bool(_universal2_python),
            docker_available=docker_available,
            codebuild_enabled=bool(profile and profile.enable_codebuild),
        )

        built_executables = []
        built_otel_helpers = []
        legacy_build_errors = []
        windows_codebuild_pending = False

        console.print()

        if is_idc_zero_binary:
            # IDC path: no binaries needed — skip all Go/Python builds
            console.print("[dim]Skipping binary builds (IDC zero-binary mode)[/dim]")
        elif use_go:
            # Go cross-compilation: build all selected platforms at once
            console.print("[cyan]Building Go binaries (cross-compilation)...[/cyan]")
            try:
                go_results = self._build_go_binaries(output_dir, platforms_to_build, profile.monitoring_enabled)
                built_executables = go_results["executables"]
                built_otel_helpers = go_results["otel_helpers"]

            except Exception as e:
                console.print(f"[red]Go build failed: {e}[/red]")
                return 1
        else:
            for platform_name in platforms_to_build:
                # Initialize so the `executable_path is None` checks below are safe even if
                # _build_executable() raises before assigning (UnboundLocalError, PR #320 bug 1).
                executable_path = None
                # Build credential process
                console.print(f"[cyan]Building credential process for {platform_name}...[/cyan]")
                try:
                    executable_path = self._build_executable(output_dir, platform_name)
                    # Check if this was an async Windows build
                    if executable_path is None:
                        if platform_name != "windows":
                            console.print(f"[red]Builder produced no executable for {platform_name}.[/red]")
                            legacy_build_errors.append((platform_name, "builder produced no executable"))
                            continue
                        console.print("[dim]Windows binaries will be built in CodeBuild[/dim]")
                        windows_codebuild_pending = True
                    else:
                        built_executables.append((platform_name, executable_path))
                except Exception as e:
                    console.print(f"[red]Failed to build credential process for {platform_name}: {e}[/red]")
                    legacy_build_errors.append((platform_name, str(e)))
                    continue

                # Build OTEL helper if monitoring is enabled
                if profile.monitoring_enabled:
                    # Skip OTEL helper for Windows if being built in CodeBuild
                    if platform_name == "windows" and executable_path is None:
                        console.print("[dim]Windows OTEL helper will be built in CodeBuild[/dim]")
                    else:
                        console.print(f"[cyan]Building OTEL helper for {platform_name}...[/cyan]")
                        try:
                            otel_helper_path = self._build_otel_helper(output_dir, platform_name)
                            if otel_helper_path is None:
                                legacy_build_errors.append(
                                    (platform_name, "OTEL helper builder produced no executable")
                                )
                            else:
                                built_otel_helpers.append((platform_name, otel_helper_path))
                        except Exception as e:
                            console.print(f"[red]Failed to build OTEL helper for {platform_name}: {e}[/red]")
                            legacy_build_errors.append((platform_name, str(e)))

        if legacy_build_errors:
            console.print("\n[red]Packaging stopped because one or more requested binaries failed.[/red]")
            try:
                shutil.rmtree(output_dir)
            except OSError as exc:
                console.print(f"[red]Failed to remove incomplete package directory {output_dir}: {exc}[/red]")
            return 1

        windows_monitoring_requested = profile.monitoring_enabled and (
            windows_codebuild_pending or any(plat == "windows" for plat, _ in built_otel_helpers)
        )
        if windows_monitoring_requested:
            try:
                for fallback in _copy_windows_otel_fallbacks(output_dir):
                    console.print(f"[dim]  Included {fallback.name} (AV-safe fallback)[/dim]")
            except OSError as exc:
                console.print(f"[red]Failed to include required Windows monitoring fallback: {exc}[/red]")
                try:
                    shutil.rmtree(output_dir)
                except OSError as cleanup_exc:
                    console.print(
                        f"[red]Failed to remove incomplete package directory {output_dir}: {cleanup_exc}[/red]"
                    )
                return 1

        # Sidecar mode ships a local OTEL Collector (otelcol-{os}-{arch}) for ALL target
        # platforms. The collector is always OCB/Go cross-compiled regardless of how the
        # auth binaries were built (--go or PyInstaller), so it runs once here for both
        # paths — keeping macOS, Linux, and Windows at parity. Without it, a generated
        # collector-config.yaml points at a collector that doesn't exist. Degrade gracefully:
        # a failed/skipped collector build (e.g. no Go on the admin machine) must not fail
        # packaging — only local telemetry forwarding is affected, and the installer warns.
        #
        # IDC zero-binary mode is intentionally EXCLUDED: its contract is no build tools on
        # the admin machine, and OCB needs Go. IDC monitoring routes to the central collector
        # instead of a local sidecar.
        if (
            not is_idc_zero_binary
            and profile.monitoring_enabled
            and getattr(profile, "monitoring_mode", "central") == "sidecar"
        ):
            console.print("[cyan]Building OTEL Collector sidecar (OCB)...[/cyan]")
            try:
                self._build_otelcol(output_dir, platforms_to_build)
            except Exception as e:
                console.print(f"[yellow]Warning: Could not build OTEL Collector sidecar: {e}[/yellow]")
                console.print("[dim]Sidecar telemetry will not work until the collector is built.[/dim]")

        # A Windows build runs asynchronously in CodeBuild and produces no local
        # binary now (_build_executable returns None), so built_executables can be
        # empty even though the build was submitted successfully. Treat that as a
        # success and still generate the config/installer for distribution.
        # Check if any binaries were built (or are pending in CodeBuild)
        # IDC zero-binary mode intentionally skips all binary builds.
        build_failed = False
        if not built_executables and not windows_codebuild_pending and not is_idc_zero_binary:
            console.print("\n[yellow]Warning: No binaries were successfully built.[/yellow]")
            console.print("Configuration files will still be generated.")
            console.print("Fix the issue above and re-run [cyan]gip package[/cyan].\n")
            build_failed = True

        if windows_codebuild_pending and not built_executables:
            console.print("\n[bold cyan]Windows binaries are building in AWS CodeBuild[/bold cyan]")
            console.print("Local configuration files will be generated now for distribution.")
            console.print("\nTo check build status:")
            console.print("  [cyan]poetry run gip builds[/cyan]")
            console.print("\nOnce complete, retrieve binaries with:")
            console.print("  [cyan]poetry run gip distribute[/cyan]\n")

        # Create configuration
        console.print("\n[cyan]Creating configuration...[/cyan]")
        # Pass the appropriate identifier based on federation type
        federation_identifier = federated_role_arn if federation_type == "direct" else identity_pool_id
        self._create_config(output_dir, profile, federation_identifier, federation_type, profile_name, console)

        # Generate IDC-specific collector config with static identity
        _is_sidecar = getattr(profile, "monitoring_mode", "central") == "sidecar"
        _is_idc_auth = getattr(profile, "effective_auth_type", profile.auth_type) == "idc"
        _is_oidc_auth = not _is_idc_auth

        if profile.monitoring_enabled and _is_sidecar:
            if _is_idc_auth:
                # IDC sidecar: bake static identity into collector config (no otel-helper at runtime).
                # Applies to both zero-binary (no quota) and IDC+quota paths.
                self._generate_collector_config(
                    output_dir=output_dir,
                    template_name="collector-config-idc.yaml",
                    region=profile.aws_region or "us-east-1",
                    idc_user_email=idc_user_email,
                    otel_resource_attributes=otel_resource_attributes,
                )
            else:
                # OIDC sidecar: otelHeadersHelper injects user identity at runtime via HTTP headers,
                # so no identity is baked in — substitute only ${REGION}.
                self._generate_collector_config(
                    output_dir=output_dir,
                    template_name="collector-config.yaml",
                    region=profile.aws_region or "us-east-1",
                )

        # Create installer
        console.print("[cyan]Creating installer script...[/cyan]")
        self._create_installer(output_dir, profile, built_executables, built_otel_helpers)

        # Create documentation
        console.print("[cyan]Creating documentation...[/cyan]")
        self._create_documentation(output_dir, profile, timestamp)

        # Always create Claude Code settings (required for Bedrock configuration)
        console.print("[cyan]Creating Claude Code settings...[/cyan]")
        self._create_claude_settings(
            output_dir,
            profile,
            include_coauthored_by,
            profile_name,
            otel_resource_attributes,
            is_idc_zero_binary=is_idc_zero_binary,
            settings_version=timestamp,
        )

        # Claude Code MCP config for the web search gateway (gip-settings/mcp.json,
        # registered by the installers via `claude mcp add-json -s user`). No-op unless
        # web search is enabled with OIDC auth.
        self._create_claude_mcp_config(output_dir, profile, console)

        # Generate CoWork 3P MDM configuration if enabled
        if profile.cowork_3p_enabled:
            console.print("\n[cyan]Generating CoWork 3P MDM configuration...[/cyan]")
            self._generate_cowork_3p_mdm_config(output_dir, profile, profile_name)

        # Generate configs for extra coding harnesses (opencode/codex/pi/aider).
        # They all reuse the same AWS profile + credential_process as Claude Code;
        # only provider config differs. Default (claude-code only) writes nothing.
        if extra_harnesses:
            console.print("\n[cyan]Generating harness configurations...[/cyan]")
            self._create_harness_configs(output_dir, profile, profile_name, extra_harnesses, console)

        # Copy admin-defined extra files into the build folder, filtered to the
        # platforms actually being built (distribute filters again per-OS at zip
        # time). Fail fast on a missing source or a validation error — a listed
        # cert the admin expects shipped must not be silently skipped.
        copied_extra_files = self._copy_extra_files(profile, output_dir, console, platforms_to_build)
        if copied_extra_files is None:
            return 1

        # Summary
        console.print("\n[green]✓ Package created successfully![/green]")
        console.print(f"\nOutput directory: [cyan]{output_dir}[/cyan]")
        console.print("\nPackage contents:")

        # Show which binaries were built
        for platform_name, executable_path in built_executables:
            binary_name = executable_path.name
            console.print(f"  • {binary_name} - Authentication executable for {platform_name}")

        console.print("  • config.json - Configuration")
        console.print("  • install.sh - Installation script for macOS/Linux")
        # Check if Windows installer exists (created when Windows binaries are present)
        if (output_dir / "install.bat").exists():
            console.print("  • install.bat - Installation script for Windows")
            console.print("  • gip-install.ps1 - PowerShell installer (called by install.bat)")
        console.print("  • README.md - Installation instructions")
        if profile.monitoring_enabled and (output_dir / "gip-settings" / "settings.json").exists():
            console.print("  • gip-settings/settings.json - Claude Code telemetry settings")
            for platform_name, otel_helper_path in built_otel_helpers:
                console.print(f"  • {otel_helper_path.name} - OTEL helper executable for {platform_name}")
        if (output_dir / "gip-settings" / "managed-settings.json").exists():
            console.print("  • gip-settings/managed-settings.json - Organization-wide enforcement settings")
        if profile.cowork_3p_enabled:
            if (output_dir / "cowork-3p-config.json").exists():
                console.print("  • cowork-3p-config.json - CoWork 3P MDM configuration (JSON)")
            if (output_dir / "cowork-3p.mobileconfig").exists():
                console.print("  • cowork-3p.mobileconfig - CoWork 3P MDM profile (macOS)")
            if (output_dir / "cowork-3p.reg").exists():
                console.print("  • cowork-3p.reg - CoWork 3P registry file (Windows)")
        if extra_harnesses:
            console.print(f"  • harnesses/ - Config files for {', '.join(extra_harnesses)} (see harnesses/README.md)")
        for name, targets in copied_extra_files:
            console.print(f"  • {name} - Extra file (targets: {targets})")

        # Next steps
        console.print("\n[bold]Distribution steps:[/bold]")
        console.print("1. Send users the entire dist folder")
        console.print("2. Users run: chmod +x install.sh && ./install.sh")
        console.print("3. Authentication is configured automatically")

        console.print("\n[bold]To test locally:[/bold]")
        console.print(f"cd {output_dir}")
        console.print("chmod +x install.sh && ./install.sh")

        # Show next steps
        console.print("\n[bold]Next steps:[/bold]")
        console.print("After installation, verify with: [cyan]poetry run gip doctor[/cyan]")

        # Only show distribute command if distribution is enabled
        if profile.enable_distribution:
            console.print("To create a distribution package: [cyan]poetry run gip distribute[/cyan]")
        else:
            console.print("Share the dist folder with your users for installation")

        if build_failed:
            console.print("\n[yellow]⚠ Package generated without binaries. Fix the build issue and re-run.[/yellow]")
            return 1

        return 0

    def _copy_extra_files(
        self, profile, output_dir: Path, console: Console, platforms_to_build: list[str] | None = None
    ) -> list[tuple[str, str]] | None:
        """Copy admin-defined extra files into the build folder.

        Copies only the entries whose ``targets`` apply to at least one platform
        in ``platforms_to_build`` (``None`` disables filtering and copies every
        entry). Distribute filters again per-OS at zip time. Returns a list of
        ``(name, targets)`` tuples for the package summary, or ``None`` to signal
        a fatal error (missing source or validation failure) — the caller must
        return a non-zero exit code.
        """
        import shutil

        from governed_inference_platform.extra_files import extra_applies_to_any, validate_extra_files

        entries = getattr(profile, "extra_files", []) or []
        if not entries:
            return []

        errors = validate_extra_files(entries)
        if errors:
            console.print("[red]Invalid extra_files configuration:[/red]")
            for err in errors:
                console.print(f"  [red]• {err}[/red]")
            return None

        console.print("\n[cyan]Copying extra files...[/cyan]")
        copied: list[tuple[str, str]] = []
        for entry in entries:
            name = entry["name"]
            if platforms_to_build is not None and not extra_applies_to_any(entry["targets"], platforms_to_build):
                console.print(f"  [dim]– {name} skipped (not targeted for this build)[/dim]")
                continue
            src = Path(entry["from"]).expanduser()
            if not src.exists():
                console.print(f"[red]Extra file source not found for '{name}': {src}[/red]")
                console.print("[red]Fix the 'from' path or remove the entry, then re-run.[/red]")
                return None

            dst = output_dir / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)

            targets = entry["targets"]
            targets_label = ", ".join(targets) if isinstance(targets, list) else str(targets)
            copied.append((name, targets_label))
            console.print(f"  [green]✓[/green] {name} ← {src} (targets: {targets_label})")

        return copied

    def _create_harness_configs(
        self, output_dir: Path, profile, profile_name: str, harnesses: list[str], console: Console
    ) -> None:
        """Write per-harness config files under harnesses/<name>/ plus a README.

        Every harness reuses the AWS profile written by the installer
        ([profile <profile_name>] with credential_process in ~/.aws/config), so
        SSO login, quota enforcement, session-name cost attribution, and IAM
        model/region guardrails apply identically. Model IDs are the SAME
        resolved CRIS inference-profile IDs the Claude Code settings get
        (resolve_model_for_tier with the profile's CRIS prefix).

        Web search (gateway MCP endpoint): when enabled (OIDC only — the
        gateway's CUSTOM_JWT authorizer cannot validate IDC's SigV4), the
        OpenCode/Codex configs additionally get a stdio MCP entry through the
        credential-process --mcp-proxy shim. Pi/Aider have no MCP support
        upstream and only get a docs pointer.
        """
        from governed_inference_platform.cli.utils.cowork_3p import resolve_websearch_gateway_url
        from governed_inference_platform.cli.utils.harness_configs import (
            McpProxyConfig,
            build_harnesses_readme,
            generate_harness_configs,
        )
        from governed_inference_platform.models import resolve_model_for_tier

        cris_prefix = getattr(profile, "cross_region_profile", None) or "us"
        model_ids: dict[str, str] = {}
        for tier in ("haiku", "sonnet", "opus"):
            model_id = resolve_model_for_tier(tier, cris_prefix)
            if model_id:
                model_ids[tier] = model_id

        # Same gating as the CoWork managedMcpServers injection (cowork_3p.py):
        # feature enabled, non-IDC auth, endpoint resolvable.
        mcp_proxy = None
        if getattr(profile, "web_search_enabled", False):
            auth_type = getattr(profile, "effective_auth_type", getattr(profile, "auth_type", "oidc"))
            if auth_type == "idc":
                console.print(
                    "[dim]Web search: skipping harness MCP config (IDC uses IAM/SigV4 auth, "
                    "which harness MCP clients do not support)[/dim]"
                )
            else:
                gateway_url = resolve_websearch_gateway_url(profile)
                if gateway_url:
                    mcp_proxy = McpProxyConfig(gateway_url=gateway_url)
                else:
                    console.print(
                        "[yellow]⚠ Web search is enabled but the gateway endpoint could not be resolved; "
                        "generating harness configs without the web search MCP entry.[/yellow]\n"
                        "[dim]  Run 'gip deploy websearch' first.[/dim]"
                    )

        aws_region = profile.aws_region or "us-east-1"
        # F-022/F-025: Prefer the selected compatibility model for affected harnesses.
        configs = generate_harness_configs(
            harnesses,
            profile_name,
            aws_region,
            model_ids,
            mcp_proxy=mcp_proxy,
            compatibility_model_id=getattr(profile, "selected_model", None),
        )

        harness_root = output_dir / "harnesses"
        for cfg in configs:
            harness_dir = harness_root / cfg.harness
            harness_dir.mkdir(parents=True, exist_ok=True)
            (harness_dir / cfg.filename).write_text(cfg.content, encoding="utf-8")
            console.print(f"  [green]✓[/green] harnesses/{cfg.harness}/{cfg.filename} ({cfg.display_name})")

        readme = build_harnesses_readme(configs, profile_name, aws_region, model_ids)
        (harness_root / "README.md").write_text(readme, encoding="utf-8")
        console.print("  [green]✓[/green] harnesses/README.md (setup instructions)")

    def _create_claude_mcp_config(self, output_dir: Path, profile, console: Console) -> None:
        """Write gip-settings/mcp.json — the gateway MCP server for Claude Code CLI.

        Claude Code carries MCP servers in .mcp.json / ~/.claude.json (NOT the
        settings.json that _create_claude_settings writes), configured natively
        with a streamable-HTTP entry plus ``headersHelper`` — the same wrapper
        script the installer already drops for CoWork. Claude Code re-runs the
        helper on every connection and once on a 401/403 (v2.1.193+), so the
        ~1h id_token TTL needs no shim and no TTL knob.

        Two consumption paths for this artifact:
        1. install.sh / install.bat register it directly via
           ``claude mcp add-json <name> -s user`` (user scope — applies across
           projects, no per-repo approval prompt);
        2. admins can distribute the file as a project ``.mcp.json`` (triggers
           the per-project approval prompt) after resolving the helper-path
           placeholder — the installers substitute it on the local copy.

        Gating mirrors add_websearch_mcp_config (cowork_3p.py): web search
        enabled, non-IDC auth (the gateway's CUSTOM_JWT authorizer cannot
        validate SigV4), endpoint resolvable.
        """
        from governed_inference_platform.cli.utils.cowork_3p import (
            WEBSEARCH_HEADERS_HELPER_PLACEHOLDER,
            WEBSEARCH_MCP_SERVER_NAME,
            resolve_websearch_gateway_url,
        )

        if not getattr(profile, "web_search_enabled", False):
            return
        auth_type = getattr(profile, "effective_auth_type", getattr(profile, "auth_type", "oidc"))
        if auth_type == "idc":
            console.print(
                "[dim]Web search: skipping Claude Code MCP config (IDC uses IAM/SigV4 auth, "
                "not supported by Claude Code's MCP client)[/dim]"
            )
            return
        gateway_url = resolve_websearch_gateway_url(profile)
        if not gateway_url:
            console.print(
                "[yellow]⚠ Web search is enabled but the gateway endpoint could not be resolved; "
                "skipping gip-settings/mcp.json.[/yellow]\n"
                "[dim]  Run 'gip deploy websearch' first.[/dim]"
            )
            return

        claude_dir = output_dir / "gip-settings"
        claude_dir.mkdir(exist_ok=True)
        mcp_config = {
            "mcpServers": {
                WEBSEARCH_MCP_SERVER_NAME: {
                    "type": "http",
                    "url": gateway_url,
                    # Per-OS helper path; install.sh/install.bat substitute the
                    # placeholder (websearch-headers vs websearch-headers.cmd).
                    "headersHelper": WEBSEARCH_HEADERS_HELPER_PLACEHOLDER,
                }
            }
        }
        (claude_dir / "mcp.json").write_text(json.dumps(mcp_config, indent=2) + "\n", encoding="utf-8")
        console.print(f"  [green]✓[/green] gip-settings/mcp.json ({WEBSEARCH_MCP_SERVER_NAME} → {gateway_url})")

    def _check_build_status(self, build_id: str, console: Console) -> int:
        """Check the status of a CodeBuild build."""
        import json
        from pathlib import Path

        import boto3

        try:
            # If no build ID provided, check for latest
            if not build_id or build_id == "latest":
                build_info_file = Path.home() / ".gip" / "latest-build.json"
                if not build_info_file.exists():
                    console.print("[red]No recent builds found. Start a build with 'poetry run gip package'[/red]")
                    return 1

                with open(build_info_file, encoding="utf-8") as f:
                    build_info = json.load(f)
                    build_id = build_info["build_id"]
                    console.print(f"[dim]Checking latest build: {build_id}[/dim]")

            # Get build status from CodeBuild
            # Load profile to get the correct region
            config = Config.load()
            profile_name = self.option("profile")
            profile = config.get_profile(profile_name)
            if not profile:
                console.print("[red]No configuration found. Run 'poetry run gip init' first.[/red]")
                return 1

            codebuild = boto3.client("codebuild", region_name=get_codebuild_region(profile))
            response = codebuild.batch_get_builds(ids=[build_id])

            if not response.get("builds"):
                console.print(f"[red]Build not found: {build_id}[/red]")
                return 1

            build = response["builds"][0]
            status = build["buildStatus"]

            # Display status
            if status == "IN_PROGRESS":
                console.print("[yellow]⏳ Build in progress[/yellow]")
                console.print(f"Phase: {build.get('currentPhase', 'Unknown')}")
                if "startTime" in build:
                    from datetime import datetime

                    start_time = build["startTime"]
                    elapsed = datetime.now(start_time.tzinfo) - start_time
                    console.print(f"Elapsed: {int(elapsed.total_seconds() / 60)} minutes")
            elif status == "SUCCEEDED":
                console.print("[green]✓ Build succeeded![/green]")
                console.print(f"Duration: {build.get('buildDurationInMinutes', 'Unknown')} minutes")
                console.print("\n[bold]Windows build artifacts are ready![/bold]")
                console.print("Next steps:")
                console.print("  Run: [cyan]poetry run gip distribute[/cyan]")
                console.print("  This will download Windows artifacts from S3 and create your distribution package")
            else:
                console.print(f"[red]✗ Build {status.lower()}[/red]")
                if "phases" in build:
                    for phase in build["phases"]:
                        if phase.get("phaseStatus") == "FAILED":
                            console.print(f"[red]Failed in phase: {phase.get('phaseType')}[/red]")

            # Show console link
            project_name = build_id.split(":")[0]
            build_uuid = build_id.split(":")[1]
            console.print(
                f"\n[dim]View logs: https://console.aws.amazon.com/codesuite/codebuild/projects/{project_name}/build/{build_uuid}[/dim]"
            )

            return 0

        except Exception as e:
            console.print(f"[red]Error checking build status: {e}[/red]")
            return 1

    def _build_go_binaries(self, output_dir: Path, platforms: list, monitoring_enabled: bool) -> dict:
        """Build binaries using Go cross-compilation.

        Linux and Windows targets cross-compile from any host. macOS credential
        binaries require CGO and therefore a macOS host with Apple build tools.

        Returns dict with 'executables' and 'otel_helpers' lists of (platform, Path) tuples.
        """
        unsupported = [plat for plat in platforms if plat not in _GO_PLATFORM_MAP]
        if unsupported:
            raise ValueError(f"Unsupported platform for Go build: {unsupported[0]}")

        go_src = Path(__file__).parents[3] / "go"
        if not go_src.exists():
            raise FileNotFoundError(f"Go source directory not found at {go_src}")

        # Verify Go is installed
        try:
            result = subprocess.run(["go", "version"], capture_output=True, text=True, check=True)  # nosec B603 — fixed argv (go), no shell
            self.line(f"  <info>{result.stdout.strip()}</info>")
        except (FileNotFoundError, subprocess.CalledProcessError):
            raise RuntimeError(
                "Go is not installed or not in PATH. Install from https://go.dev/dl/ or run: brew install go"
            )

        if any(_GO_PLATFORM_MAP[plat][0] == "darwin" for plat in platforms) and platform.system() != "Darwin":
            raise RuntimeError(
                "macOS credential binaries require CGO and Apple build tools. "
                "Run this package build on macOS or request only Linux and Windows targets."
            )

        executables = []
        otel_helpers = []
        failures = []
        attempted_paths = []
        staged_outputs = []

        def cleanup_outputs() -> None:
            for path in attempted_paths:
                path.unlink(missing_ok=True)
                path.with_name(f".{path.name}.building").unlink(missing_ok=True)

        binaries_to_build = ["credential-process"]
        if monitoring_enabled:
            binaries_to_build.append("otel-helper")

        for plat in platforms:
            goos, goarch = _GO_PLATFORM_MAP[plat]

            for binary in binaries_to_build:
                if plat == "windows":
                    suffix = "-windows.exe"
                else:
                    suffix = f"-{plat}"

                output_name = f"{binary}{suffix}"
                output_path = output_dir / output_name
                staged_path = output_path.with_name(f".{output_name}.building")
                attempted_paths.append(output_path)
                staged_path.unlink(missing_ok=True)

                self.line(f"  Building <comment>{output_name}</comment>...")

                # macOS credential-process needs CGO_ENABLED=1 for keychain access
                # (99designs/keyring's keychain backend requires cgo).
                cgo = "1" if goos == "darwin" and binary == "credential-process" else "0"
                env = {**os.environ, "GOOS": goos, "GOARCH": goarch, "CGO_ENABLED": cgo}
                # The .syso PE version-info files in cmd/*/ are auto-linked by the Go
                # compiler on Windows to further reduce AV false positives.
                try:
                    ldflags = _go_ldflags(goos)
                    cmd = [
                        "go",
                        "build",
                        "-trimpath",
                        "-ldflags",
                        ldflags,
                        "-o",
                        str(staged_path),
                        f"./cmd/{binary}/",
                    ]
                    result = run_checked(cmd, cwd=str(go_src), env=env, capture_output=True, text=True)  # nosec B603 — fixed argv (go), no shell
                except Exception:
                    cleanup_outputs()
                    raise
                if result.returncode != 0:
                    last_line = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown error"
                    self.line(f"  <error>Failed: {output_name} — {last_line}</error>")
                    failures.append(f"{output_name}: {last_line}")
                    continue

                staged_outputs.append((staged_path, output_path))

                if binary == "credential-process":
                    executables.append((plat, output_path))
                else:
                    otel_helpers.append((plat, output_path))

        if failures:
            cleanup_outputs()
            raise RuntimeError("Requested Go binaries failed to build: " + "; ".join(failures))

        try:
            for staged_path, output_path in staged_outputs:
                staged_path.replace(output_path)
        except OSError:
            cleanup_outputs()
            raise

        self.line(f"  <info>Built {len(executables) + len(otel_helpers)} binaries</info>")
        return {"executables": executables, "otel_helpers": otel_helpers}

    def _generate_collector_config(
        self,
        output_dir: Path,
        template_name: str,
        region: str,
        idc_user_email: str | None = None,
        otel_resource_attributes: str | None = None,
    ) -> None:
        """Write collector-config.yaml to output_dir from the named otel_helper template.

        Called for all three sidecar paths:
          - OIDC sidecar     → collector-config.yaml (runtime header injection)
          - IDC zero-binary  → collector-config-idc.yaml (static identity baked in)
          - IDC+quota sidecar → collector-config-idc.yaml (static identity baked in)
        """
        console = Console()
        template_src = Path(__file__).resolve().parent.parent.parent.parent / "otel_helper" / template_name
        if not template_src.exists():
            console.print(f"[yellow]Warning: {template_name} template not found[/yellow]")
            return

        content = template_src.read_text(encoding="utf-8")
        content = content.replace("${REGION}", region)

        if idc_user_email is not None:
            attrs: dict[str, str] = {}
            if otel_resource_attributes:
                for pair in otel_resource_attributes.split(","):
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        attrs[k.strip()] = v.strip()
            content = content.replace("${USER_EMAIL}", idc_user_email or "unknown@example.com")
            content = content.replace("${USER_NAME}", (idc_user_email or "unknown").split("@")[0])
            content = content.replace("${DEPARTMENT}", attrs.get("department", "default"))
            content = content.replace("${TEAM_ID}", attrs.get("team.id", "default"))
            content = content.replace("${COST_CENTER}", attrs.get("cost_center", "default"))
            content = content.replace("${ORGANIZATION}", attrs.get("organization", "default"))

        (output_dir / "collector-config.yaml").write_text(content, encoding="utf-8")
        label = f"IDC (identity: {idc_user_email})" if idc_user_email is not None else "OIDC"
        console.print(f"[dim]Generated {label} sidecar collector config[/dim]")

    def _build_otelcol(self, output_dir: Path, platforms_to_build: list[str]) -> None:
        """Build the minimal OTEL Collector sidecar via OCB for all target platforms.

        Produces otelcol-{os}-{arch} binaries that are shipped IN the package, the same
        model as credential-process and otel-helper. distribute.py, test.py, status.py and
        the otel-helper.sh/.ps1 wrappers all expect these bundled binaries to exist.

        Network + Go 1.23+ are required on the PACKAGING (admin) machine only — end users
        never download the collector. Skips gracefully when Go is missing or too old.

        Restores behavior dropped during the Go rewrite (PR #338), which removed this
        method and its call site as collateral damage of the build-path restructure.
        """
        import re
        import shutil
        import urllib.request

        console = Console()
        host_os = platform.system().lower()
        host_arch = platform.machine().lower()

        result = subprocess.run(["go", "version"], capture_output=True, text=True)  # nosec B603 — fixed argv (go), no shell
        if result.returncode != 0:
            console.print("[yellow]Go not found — skipping collector build[/yellow]")
            console.print("[dim]Install Go 1.23+ from https://go.dev/dl/ to build the collector sidecar[/dim]")
            return
        go_match = re.search(r"go(\d+)\.(\d+)", result.stdout)
        if not go_match or (int(go_match.group(1)), int(go_match.group(2))) < (1, 23):
            console.print("[yellow]Go 1.23+ required — skipping collector build[/yellow]")
            console.print(f"[dim]Found: {result.stdout.strip()}. Install Go 1.23+ from https://go.dev/dl/[/dim]")
            return

        OCB_VERSION = "0.120.0"
        if host_os == "darwin":
            ocb_os = "darwin"
        elif host_os == "windows":
            ocb_os = "windows"
        else:
            ocb_os = "linux"
        ocb_arch = "arm64" if host_arch in ["arm64", "aarch64"] else "amd64"
        ocb_dir = Path.home() / ".cache" / "ocb"
        ocb_dir.mkdir(parents=True, exist_ok=True)
        ocb_suffix = ".exe" if ocb_os == "windows" else ""
        ocb_path = ocb_dir / f"ocb_{OCB_VERSION}_{ocb_os}_{ocb_arch}{ocb_suffix}"

        if not ocb_path.exists():
            url = (
                f"https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/"
                f"cmd%2Fbuilder%2Fv{OCB_VERSION}/ocb_{OCB_VERSION}_{ocb_os}_{ocb_arch}{ocb_suffix}"
            )
            console.print(f"[dim]Downloading OCB v{OCB_VERSION}...[/dim]")
            try:
                urllib.request.urlretrieve(url, ocb_path)  # noqa: S310 (trusted GitHub release URL)  # nosec B310 — pinned-version https GitHub release URL
            except (urllib.error.URLError, OSError) as e:
                console.print(f"[red]Failed to download OCB: {e}[/red]")
                console.print(
                    "[yellow]For air-gapped environments, run "
                    "'gip package --prepare-offline' on a connected machine first.[/yellow]"
                )
                raise
            if ocb_os != "windows":
                ocb_path.chmod(0o755)

        manifest = Path(__file__).parent.parent.parent.parent / "otel_helper" / "ocb-manifest.yaml"
        if not manifest.exists():
            raise FileNotFoundError(f"OCB manifest not found: {manifest}")

        # Resolve each platform to (GOOS, GOARCH, output-binary-name). GOOS/GOARCH come
        # from the shared _GO_PLATFORM_MAP; only the otelcol-specific output name lives
        # here. "macos-universal" maps to arm64 (no fat binary — sidecar is per-arch).
        def _otelcol_name(goos: str, goarch: str) -> str:
            if goos == "windows":
                return "otelcol-windows.exe"
            if goos == "darwin":
                return "otelcol-macos-arm64" if goarch == "arm64" else "otelcol-macos-intel"
            return "otelcol-linux-arm64" if goarch == "arm64" else "otelcol-linux-x64"

        targets = []
        seen = set()
        for plat in platforms_to_build:
            resolved = _GO_PLATFORM_MAP.get("macos-arm64" if plat == "macos-universal" else plat)
            if not resolved:
                continue
            goos, goarch = resolved
            binary_name = _otelcol_name(goos, goarch)
            if binary_name not in seen:
                targets.append((goos, goarch, binary_name))
                seen.add(binary_name)

        if not targets:
            return

        build_dir = output_dir / "_otelcol_build"
        build_dir.mkdir(exist_ok=True)

        try:
            manifest_text = manifest.read_text().replace("output_path: ./build/otelcol", f"output_path: {build_dir}")
            temp_manifest = build_dir / "manifest.yaml"
            temp_manifest.write_text(manifest_text)

            console.print("[dim]Generating collector source code...[/dim]")
            result = run_checked(  # nosec B603 — list argv, no shell
                [str(ocb_path), "--config", str(temp_manifest), "--skip-compilation"],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(f"OCB source generation failed: {result.stderr}")

            console.print("[dim]Downloading Go modules...[/dim]")
            dl_result = subprocess.run(  # nosec B603 — fixed argv (go), no shell
                ["go", "mod", "download"],
                capture_output=True,
                text=True,
                cwd=build_dir,
            )
            if dl_result.returncode != 0:
                raise RuntimeError(f"go mod download failed: {dl_result.stderr}")

            for goos, goarch, binary_name in targets:
                console.print(f"[dim]Compiling collector for {goos}/{goarch}...[/dim]")
                output_binary = (output_dir / binary_name).resolve()
                env = {**os.environ, "GOOS": goos, "GOARCH": goarch, "CGO_ENABLED": "0"}
                ldflags = _go_ldflags(goos)
                result = subprocess.run(  # nosec B603 — fixed argv (go), no shell
                    ["go", "build", "-trimpath", f"-ldflags={ldflags}", "-o", str(output_binary), "."],
                    capture_output=True,
                    text=True,
                    env=env,
                    cwd=build_dir,
                )
                if result.returncode != 0:
                    console.print(f"[yellow]Warning: Failed to build {binary_name}: {result.stderr[:200]}[/yellow]")
                    continue
                if goos != "windows":
                    output_binary.chmod(0o755)
                console.print(f"[green]✓ {binary_name}[/green]")
        finally:
            shutil.rmtree(build_dir, ignore_errors=True)

    def _build_executable(self, output_dir: Path, target_platform: str) -> Path:
        """Build executable for target platform using appropriate tool."""
        import platform

        current_system = platform.system().lower()
        current_machine = platform.machine().lower()

        # Windows builds use Nuitka via CodeBuild
        if target_platform == "windows":
            if current_system == "windows":
                # Try native Windows build with Nuitka first
                try:
                    return self._build_native_executable_nuitka(output_dir, "windows")
                except RuntimeError as e:
                    # Check if this is a Nuitka availability issue (not a compilation failure)
                    error_msg = str(e)
                    nuitka_unavailable = (
                        "Nuitka not found" in error_msg
                        or "MinGW" in error_msg
                        or "FATAL: Only this specific gcc" in error_msg
                    )
                    if nuitka_unavailable:
                        # Nuitka is not properly configured, fall back to CodeBuild
                        console = Console()
                        console.print(f"[yellow]Local build unavailable: {error_msg.split(chr(10))[0]}[/yellow]")
                        console.print("[cyan]Falling back to AWS CodeBuild...[/cyan]")
                        self._build_windows_via_codebuild(output_dir)
                        return None  # CodeBuild async build started
                    else:
                        # Re-raise other RuntimeErrors (actual build failures)
                        raise
            else:
                # Use CodeBuild for Windows builds on non-Windows platforms
                # Don't return - just start the build and continue
                self._build_windows_via_codebuild(output_dir)
                return None  # No local binary created

        # macOS builds use PyInstaller for cross-architecture support
        if target_platform == "macos-arm64":
            return self._build_macos_pyinstaller(output_dir, "arm64")
        elif target_platform == "macos-intel":
            return self._build_macos_pyinstaller(output_dir, "x86_64")
        elif target_platform == "macos-universal":
            return self._build_macos_pyinstaller(output_dir, "universal2")
        elif target_platform == "linux-x64":
            # Build Linux x64 binary via Docker with PyInstaller
            return self._build_linux_via_docker(output_dir, "x64")
        elif target_platform == "linux-arm64":
            # Build Linux ARM64 binary via Docker with PyInstaller
            return self._build_linux_via_docker(output_dir, "arm64")
        elif target_platform == "linux":
            # Native Linux build with PyInstaller
            return self._build_linux_pyinstaller(output_dir)
        elif target_platform == "macos":
            # Default macOS build for current architecture
            if current_machine == "arm64":
                return self._build_macos_pyinstaller(output_dir, "arm64")
            else:
                return self._build_macos_pyinstaller(output_dir, "x86_64")

        # Fallback - shouldn't reach here
        raise ValueError(f"Unsupported target platform: {target_platform}")

    def _build_native_executable_nuitka(self, output_dir: Path, target_platform: str) -> Path:
        """Build executable using native Nuitka compiler (for Windows only)."""
        import platform

        current_system = platform.system().lower()
        current_machine = platform.machine().lower()

        # Platform compatibility matrix for Nuitka (no cross-compilation)
        PLATFORM_COMPATIBILITY = {
            "macos": {
                "arm64": ["darwin-arm64"],
                "intel": ["darwin-x86_64"],
            },
            "linux": {
                "x86_64": ["linux-x86_64"],
            },
            "windows": {
                "x86_64": ["windows-amd64"],
            },
        }

        # Determine the specific platform variant
        if target_platform == "macos":
            # On macOS, determine if we're building for ARM64 or Intel
            # Check if user requested a specific variant via environment variable
            macos_variant = os.environ.get("GIP_MACOS_VARIANT", "").lower()

            if macos_variant == "intel":
                # Force Intel build (useful on ARM Macs with Rosetta)
                platform_variant = "intel"
                binary_name = "credential-process-macos-intel"
            elif macos_variant == "arm64":
                # Force ARM64 build
                platform_variant = "arm64"
                binary_name = "credential-process-macos-arm64"
            elif current_machine == "arm64":
                # Default to ARM64 on ARM Macs
                platform_variant = "arm64"
                binary_name = "credential-process-macos-arm64"
            else:
                # Default to Intel on Intel Macs
                platform_variant = "intel"
                binary_name = "credential-process-macos-intel"
        elif target_platform == "linux":
            platform_variant = "x86_64"
            binary_name = "credential-process-linux"
        elif target_platform == "windows":
            platform_variant = "x86_64"
            binary_name = "credential-process-windows.exe"
        else:
            raise ValueError(f"Unsupported target platform: {target_platform}")

        # Check platform compatibility
        current_platform_str = f"{current_system}-{current_machine}"
        compatible_platforms = PLATFORM_COMPATIBILITY.get(target_platform, {}).get(platform_variant, [])

        # Special case: Allow Intel builds on ARM Macs via Rosetta
        if (
            target_platform == "macos"
            and platform_variant == "intel"
            and current_system == "darwin"
            and current_machine == "arm64"
        ):
            # Check if Rosetta is available
            result = subprocess.run(["arch", "-x86_64", "true"], capture_output=True)  # nosec B603 — fixed argv (arch), no shell
            if result.returncode == 0:
                console = Console()
                console.print("[yellow]Building Intel binary on ARM Mac using Rosetta 2[/yellow]")
                # Rosetta is available, allow the build
                pass
            else:
                raise RuntimeError(
                    "Cannot build Intel binary on ARM Mac without Rosetta 2.\n"
                    "Install Rosetta: softwareupdate --install-rosetta"
                )
        elif current_platform_str not in compatible_platforms:
            raise RuntimeError(
                f"Cannot build {target_platform} ({platform_variant}) binary on {current_platform_str}.\n"
                f"Nuitka requires native builds. Please build on a {target_platform} machine."
            )

        # Check if Nuitka is available (through Poetry)
        source_dir = Path(__file__).parent.parent.parent.parent
        nuitka_check = subprocess.run(  # nosec B603 — fixed argv (poetry), no shell
            ["poetry", "run", "python", "-m", "nuitka", "--version"], capture_output=True, text=True, cwd=source_dir
        )
        if nuitka_check.returncode != 0:
            raise RuntimeError(
                "Nuitka not found. Please install it:\n"
                "  poetry add --group dev nuitka ordered-set zstandard\n\n"
                "Note: Nuitka requires Python 3.10-3.12."
            )

        # Find the source file
        src_file = Path(__file__).parent.parent.parent.parent.parent / "source" / "credential_provider" / "__main__.py"

        if not src_file.exists():
            raise FileNotFoundError(f"Source file not found: {src_file}")

        # Build Nuitka command (use poetry run to ensure correct Python version)
        # If building Intel binary on ARM Mac, use Rosetta
        if (
            target_platform == "macos"
            and platform_variant == "intel"
            and current_system == "darwin"
            and current_machine == "arm64"
        ):
            cmd = [
                "arch",
                "-x86_64",  # Run under Rosetta
                "poetry",
                "run",
                "nuitka",
            ]
        else:
            cmd = [
                "poetry",
                "run",
                "nuitka",
            ]

        # Add common Nuitka flags
        nuitka_flags = [
            "--standalone",
            "--onefile",
            "--assume-yes-for-downloads",
            f"--output-filename={binary_name}",
            f"--output-dir={str(output_dir)}",
        ]

        # Only add --quiet if not in verbose mode
        verbose = self.option("build-verbose")
        if not verbose:
            nuitka_flags.append("--quiet")

        nuitka_flags.extend(
            [
                "--remove-output",  # Clean up build artifacts
                "--python-flag=no_site",  # Don't include site packages
            ]
        )

        cmd.extend(nuitka_flags)

        # Add platform-specific flags
        if target_platform == "macos":
            cmd.extend(
                [
                    "--macos-create-app-bundle",
                    "--macos-app-name=Claude Code Credential Process",
                    "--disable-console",  # GUI app on macOS
                ]
            )
        elif target_platform == "linux":
            cmd.extend(
                [
                    "--linux-onefile-icon=NONE",  # No icon for Linux
                ]
            )

        # Add the source file
        cmd.append(str(src_file))

        # Run Nuitka (from source directory where pyproject.toml is located)
        source_dir = Path(__file__).parent.parent.parent.parent
        result = run_checked(cmd, capture_output=not verbose, text=True, cwd=source_dir)  # nosec B603 — internal build argv, no shell
        if result.returncode != 0:
            raise RuntimeError(f"Nuitka build failed: {result.stderr}")

        return output_dir / binary_name

    def _build_macos_pyinstaller(self, output_dir: Path, arch: str) -> Path:
        """Build macOS executable using PyInstaller with target architecture."""
        _assert_host_os_can_build_macos()
        console = Console()
        verbose = self.option("build-verbose")

        # Determine binary name based on architecture
        if arch == "arm64":
            binary_name = "credential-process-macos-arm64"
        elif arch == "x86_64":
            binary_name = "credential-process-macos-intel"
        elif arch == "universal2":
            binary_name = "credential-process-macos-universal"
        else:
            raise ValueError(f"Unsupported macOS architecture: {arch}")

        # Find the source file
        src_file = Path(__file__).parent.parent.parent.parent.parent / "source" / "credential_provider" / "__main__.py"
        if not src_file.exists():
            raise FileNotFoundError(f"Source file not found: {src_file}")

        console.print(f"[yellow]Building macOS {arch} binary with PyInstaller...[/yellow]")

        host_arch = platform.machine().lower()
        cross_arch = arch != host_arch and arch != "universal2"

        # Determine log level based on verbose flag
        log_level = "INFO" if verbose else "WARN"

        if cross_arch:
            # Cross-arch build: need a per-arch venv seeded from a universal2 Python
            universal2_python = _find_universal2_python()
            if universal2_python is None:
                raise RuntimeError(
                    f"Cross-arch macOS build requires a universal2 Python but none was found.\n\n"
                    f"Install Python universal2 from python.org:\n"
                    f"  https://www.python.org/downloads/macos/\n\n"
                    f"Download the 'macOS 64-bit universal2 installer' for Python 3.12, then re-run.\n\n"
                    f"To build only the host arch ({host_arch}), omit the cross-arch target."
                )
            venv_dir = _ensure_cross_arch_venv(arch, universal2_python, _CREDENTIAL_PROVIDER_RUNTIME_DEPS, console)
            work_root = Path.home() / ".gip" / "build-work"
            work_root.mkdir(parents=True, exist_ok=True)
            cmd = [
                "/usr/bin/arch",
                f"-{arch}",
                str(venv_dir / "bin" / "pyinstaller"),
                "--onefile",
                "--clean",
                "--noconfirm",
                f"--name={binary_name}",
                f"--distpath={str(output_dir)}",
                f"--workpath={str(work_root / arch)}",
                f"--specpath={str(work_root / arch)}",
                f"--log-level={log_level}",
                "--hidden-import=keyring.backends.macOS",
                "--hidden-import=keyring.backends.SecretService",
                "--hidden-import=keyring.backends.Windows",
                "--hidden-import=keyring.backends.chainer",
                "--hidden-import=charset_normalizer",
                str(src_file),
            ]
        else:
            # Native build: use Poetry environment directly
            cmd = [
                "poetry",
                "run",
                "pyinstaller",
                "--onefile",
                "--clean",
                "--noconfirm",
                f"--target-arch={arch}",
                f"--name={binary_name}",
                f"--distpath={str(output_dir)}",
                "--workpath=/tmp/pyinstaller",
                "--specpath=/tmp/pyinstaller",
                f"--log-level={log_level}",
                "--hidden-import=keyring.backends.macOS",
                "--hidden-import=keyring.backends.SecretService",
                "--hidden-import=keyring.backends.Windows",
                "--hidden-import=keyring.backends.chainer",
                "--hidden-import=charset_normalizer",
                str(src_file),
            ]

        # Run PyInstaller from source directory
        source_dir = Path(__file__).parent.parent.parent.parent
        result = run_checked(cmd, capture_output=not verbose, text=True, cwd=source_dir)  # nosec B603 — internal build argv, no shell

        if result.returncode != 0:
            console.print(f"[red]PyInstaller build failed: {result.stderr}[/red]")
            raise RuntimeError(f"PyInstaller build failed: {result.stderr}")

        binary_path = output_dir / binary_name
        if binary_path.exists():
            binary_path.chmod(0o755)
            console.print(f"[green]✓ macOS {arch} binary built successfully with PyInstaller[/green]")
            return binary_path
        else:
            raise RuntimeError(f"Binary not created: {binary_path}")

    def _build_linux_pyinstaller(self, output_dir: Path) -> Path:
        """Build Linux executable using PyInstaller."""
        console = Console()
        verbose = self.option("build-verbose")

        # Detect architecture and set appropriate binary name
        import platform

        machine = platform.machine().lower()
        if machine in ["aarch64", "arm64"]:
            binary_name = "credential-process-linux-arm64"
        else:
            binary_name = "credential-process-linux-x64"

        # Find the source file
        src_file = Path(__file__).parent.parent.parent.parent.parent / "source" / "credential_provider" / "__main__.py"
        if not src_file.exists():
            raise FileNotFoundError(f"Source file not found: {src_file}")

        console.print("[yellow]Building Linux binary with PyInstaller...[/yellow]")

        # Determine log level based on verbose flag
        log_level = "INFO" if verbose else "WARN"

        # Build PyInstaller command
        cmd = [
            "poetry",
            "run",
            "pyinstaller",
            "--onefile",
            "--clean",
            "--noconfirm",
            f"--name={binary_name}",
            f"--distpath={str(output_dir)}",
            "--workpath=/tmp/pyinstaller",
            "--specpath=/tmp/pyinstaller",
            f"--log-level={log_level}",
            # Hidden imports for our dependencies
            "--hidden-import=keyring.backends.SecretService",
            "--hidden-import=keyring.backends.chainer",
            "--hidden-import=charset_normalizer",
            "--hidden-import=six",
            "--hidden-import=six.moves",
            "--hidden-import=six.moves._thread",
            "--hidden-import=six.moves.urllib",
            "--hidden-import=six.moves.urllib.parse",
            "--hidden-import=dateutil",
            str(src_file),
        ]

        # Run PyInstaller from source directory
        source_dir = Path(__file__).parent.parent.parent.parent
        result = run_checked(cmd, capture_output=not verbose, text=True, cwd=source_dir)  # nosec B603 — internal build argv, no shell

        if result.returncode != 0:
            console.print(f"[red]PyInstaller build failed: {result.stderr}[/red]")
            raise RuntimeError(f"PyInstaller build failed: {result.stderr}")

        binary_path = output_dir / binary_name
        if binary_path.exists():
            binary_path.chmod(0o755)
            console.print("[green]✓ Linux binary built successfully with PyInstaller[/green]")
            return binary_path
        else:
            raise RuntimeError(f"Binary not created: {binary_path}")

    def _build_linux_via_docker(self, output_dir: Path, arch: str = "x64") -> Path:
        """Build Linux binaries using Docker with PyInstaller."""
        import shutil
        import tempfile

        console = Console()
        verbose = self.option("build-verbose")

        # Determine platform and binary name
        if arch == "arm64":
            docker_platform = "linux/arm64"
            binary_name = "credential-process-linux-arm64"
        else:
            docker_platform = "linux/amd64"
            binary_name = "credential-process-linux-x64"

        # Check if Docker is available and running
        try:
            docker_check = subprocess.run(["docker", "--version"], capture_output=True)  # nosec B603 — fixed argv (docker), no shell
            docker_installed = docker_check.returncode == 0
        except FileNotFoundError:
            docker_installed = False
        if not docker_installed:
            console.print(f"\n[yellow]⚠️  Docker not found - skipping Linux {arch} build[/yellow]")
            console.print("[dim]Linux binaries require Docker Desktop to be installed and running.[/dim]")
            console.print("[dim]Install Docker: https://docs.docker.com/get-docker/[/dim]")
            console.print(f"[dim]Skipping credential-process-linux-{arch}[/dim]\n")
            # Return a dummy path that won't be included in the package
            return None

        # Check if Docker daemon is running
        daemon_check = subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # nosec B603 — fixed argv (docker), no shell
        if daemon_check.returncode != 0:
            console.print(f"\n[yellow]⚠️  Docker daemon not running - skipping Linux {arch} build[/yellow]")
            console.print("[dim]Please start Docker Desktop and try again.[/dim]")
            console.print(f"[dim]Skipping credential-process-linux-{arch}[/dim]\n")
            # Return a dummy path that won't be included in the package
            return None

        # Create a temporary directory for the Docker build
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # Copy source files to temp directory
            source_dir = Path(__file__).parent.parent.parent.parent
            shutil.copytree(source_dir / "credential_provider", temp_path / "credential_provider")

            # Create Dockerfile with PyInstaller
            dockerfile_content = f"""FROM --platform={docker_platform} public.ecr.aws/docker/library/ubuntu@sha256:2edbbc5dc405e9612ba3584ce95480277e3eb374407b5505fe26f17df77c7dbc

# Set non-interactive to avoid tzdata prompts
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

# Install Python 3.12 and build dependencies
RUN apt-get update && apt-get install -y \
    software-properties-common \
    build-essential \
    binutils \
    curl \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update \
    && apt-get install -y python3.12 python3.12-dev python3.12-venv \
    && python3.12 -m ensurepip \
    && python3.12 -m pip install --upgrade pip \
    && rm -rf /var/lib/apt/lists/*

# Set Python 3.12 as default python3
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.12 1

# Install Python packages
RUN python3 -m pip install --no-cache-dir \
    pyinstaller==6.3.0 \
    boto3 \
    requests \
    PyJWT \
    cryptography \
    keyring \
    keyrings.alt \
    questionary \
    rich \
    cleo \
    pydantic \
    pyyaml \
    six==1.16.0 \
    python-dateutil

# Set working directory
WORKDIR /build

# Copy source code
COPY credential_provider /build/credential_provider

# Build the binary with PyInstaller
RUN pyinstaller \
    --onefile \
    --clean \
    --noconfirm \
    --name {binary_name} \
    --distpath /output \
    --workpath /tmp/build \
    --specpath /tmp \
    --log-level WARN \
    --hidden-import keyring.backends.SecretService \
    --hidden-import keyring.backends.chainer \
    --hidden-import charset_normalizer \
    --hidden-import six \
    --hidden-import six.moves \
    --hidden-import six.moves._thread \
    --hidden-import six.moves.urllib \
    --hidden-import six.moves.urllib.parse \
    --hidden-import dateutil \
    credential_provider/__main__.py

# The binary will be in /output/{binary_name}
"""  # nosec B608 — Dockerfile template, not SQL

            (temp_path / "Dockerfile").write_text(dockerfile_content)

            # Generate unique image tag to avoid reusing cached images
            import time

            image_tag = f"gip-linux-{arch}-builder-{int(time.time())}"

            # Remove any existing image with similar name to ensure fresh build
            if verbose:
                console.print("[dim]Cleaning up old Docker images...[/dim]")
            subprocess.run(  # nosec B603 — fixed argv (docker), no shell
                ["docker", "rmi", "-f", f"gip-linux-{arch}-builder"],
                capture_output=True,
            )

            # Build Docker image
            console.print(f"[yellow]Building Linux {arch} binary via Docker (this may take a few minutes)...[/yellow]")
            if verbose:
                console.print("[dim]Docker build output:[/dim]")
            build_result = subprocess.run(  # nosec B603 — fixed argv (docker), no shell
                [
                    "docker",
                    "buildx",
                    "build",
                    "--no-cache",
                    "--platform",
                    docker_platform,
                    "-t",
                    image_tag,
                    "--load",
                    ".",
                ],
                cwd=temp_path,
                capture_output=not verbose,
                text=True,
            )

            if build_result.returncode != 0:
                raise RuntimeError(f"Docker build failed: {build_result.stderr}")

            # Run container and copy binary out
            import time

            container_name = f"gip-extract-{arch}-{int(time.time())}"

            # Create container from the newly built image
            run_result = subprocess.run(  # nosec B603 — fixed argv (docker), no shell
                ["docker", "create", "--name", container_name, image_tag],
                capture_output=True,
                text=True,
            )

            if run_result.returncode != 0:
                raise RuntimeError(f"Failed to create container: {run_result.stderr}")

            try:
                # Copy binary from container
                copy_result = subprocess.run(  # nosec B603 — fixed argv (docker), no shell
                    ["docker", "cp", f"{container_name}:/output/{binary_name}", str(output_dir)],
                    capture_output=True,
                    text=True,
                )

                if copy_result.returncode != 0:
                    raise RuntimeError(f"Failed to copy binary from container: {copy_result.stderr}")

                # Verify the binary was created
                binary_path = output_dir / binary_name
                if not binary_path.exists():
                    raise RuntimeError(f"Linux {arch} binary was not created successfully")

                # Make it executable
                binary_path.chmod(0o755)

                console.print(f"[green]✓ Linux {arch} binary built successfully via Docker[/green]")
                return binary_path

            finally:
                # Clean up container and image
                subprocess.run(["docker", "rm", container_name], capture_output=True)  # nosec B603 — fixed argv (docker), no shell
                subprocess.run(["docker", "rmi", image_tag], capture_output=True)  # nosec B603 — fixed argv (docker), no shell

    def _build_linux_otel_helper_via_docker(self, output_dir: Path, arch: str = "x64") -> Path:
        """Build Linux OTEL helper binary using Docker with PyInstaller."""
        import shutil
        import tempfile

        console = Console()
        verbose = self.option("build-verbose")

        # Determine platform and binary name
        if arch == "arm64":
            docker_platform = "linux/arm64"
            binary_name = "otel-helper-linux-arm64"
        else:
            docker_platform = "linux/amd64"
            binary_name = "otel-helper-linux-x64"

        # Check if Docker is available and running
        try:
            docker_check = subprocess.run(["docker", "--version"], capture_output=True)  # nosec B603 — fixed argv (docker), no shell
            docker_installed = docker_check.returncode == 0
        except FileNotFoundError:
            docker_installed = False
        if not docker_installed:
            console.print(f"\n[yellow]⚠️  Docker not found - skipping Linux {arch} OTEL helper build[/yellow]")
            console.print("[dim]Linux binaries require Docker Desktop to be installed and running.[/dim]")
            console.print(f"[dim]Skipping otel-helper-linux-{arch}[/dim]\n")
            # Return a dummy path that won't be included in the package
            return None

        # Check if Docker daemon is running
        daemon_check = subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # nosec B603 — fixed argv (docker), no shell
        if daemon_check.returncode != 0:
            console.print(f"\n[yellow]⚠️  Docker daemon not running - skipping Linux {arch} OTEL helper build[/yellow]")
            console.print("[dim]Please start Docker Desktop and try again.[/dim]")
            console.print(f"[dim]Skipping otel-helper-linux-{arch}[/dim]\n")
            # Return a dummy path that won't be included in the package
            return None

        # Create a temporary directory for the Docker build
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # Copy source files to temp directory
            source_dir = Path(__file__).parent.parent.parent.parent
            shutil.copytree(source_dir / "otel_helper", temp_path / "otel_helper")

            # Create Dockerfile for OTEL helper with PyInstaller
            dockerfile_content = f"""FROM --platform={docker_platform} public.ecr.aws/docker/library/ubuntu@sha256:2edbbc5dc405e9612ba3584ce95480277e3eb374407b5505fe26f17df77c7dbc

# Set non-interactive to avoid tzdata prompts
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

# Install Python 3.12 and build dependencies
RUN apt-get update && apt-get install -y \
    software-properties-common \
    build-essential \
    binutils \
    curl \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update \
    && apt-get install -y python3.12 python3.12-dev python3.12-venv \
    && python3.12 -m ensurepip \
    && python3.12 -m pip install --upgrade pip \
    && rm -rf /var/lib/apt/lists/*

# Set Python 3.12 as default python3
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.12 1

# Install Python packages
RUN python3 -m pip install --no-cache-dir \
    pyinstaller==6.3.0 \
    PyJWT \
    cryptography \
    six

# Set working directory
WORKDIR /build

# Copy source code
COPY otel_helper /build/otel_helper

# Build the binary with PyInstaller
RUN pyinstaller \
    --onefile \
    --clean \
    --noconfirm \
    --name {binary_name} \
    --distpath /output \
    --workpath /tmp/build \
    --specpath /tmp \
    --log-level WARN \
    --hidden-import six \
    --hidden-import six.moves \
    otel_helper/__main__.py

# The binary will be in /output/{binary_name}
"""  # nosec B608 — Dockerfile template, not SQL

            (temp_path / "Dockerfile").write_text(dockerfile_content)

            # Generate unique image tag to avoid reusing cached images
            import time

            image_tag = f"gip-otel-{arch}-builder-{int(time.time())}"

            # Remove any existing image with similar name to ensure fresh build
            if verbose:
                console.print("[dim]Cleaning up old Docker images...[/dim]")
            subprocess.run(  # nosec B603 — fixed argv (docker), no shell
                ["docker", "rmi", "-f", f"gip-otel-{arch}-builder"],
                capture_output=True,
            )

            # Build Docker image
            console.print(f"[yellow]Building Linux {arch} OTEL helper via Docker...[/yellow]")
            if verbose:
                console.print("[dim]Docker build output:[/dim]")
            build_result = subprocess.run(  # nosec B603 — fixed argv (docker), no shell
                [
                    "docker",
                    "buildx",
                    "build",
                    "--no-cache",
                    "--platform",
                    docker_platform,
                    "-t",
                    image_tag,
                    "--load",
                    ".",
                ],
                cwd=temp_path,
                capture_output=not verbose,
                text=True,
            )

            if build_result.returncode != 0:
                raise RuntimeError(f"Docker build failed for OTEL helper: {build_result.stderr}")

            # Run container and copy binary out
            import time

            container_name = f"gip-otel-extract-{arch}-{int(time.time())}"

            # Create container from the newly built image
            run_result = subprocess.run(  # nosec B603 — fixed argv (docker), no shell
                ["docker", "create", "--name", container_name, image_tag],
                capture_output=True,
                text=True,
            )

            if run_result.returncode != 0:
                raise RuntimeError(f"Failed to create container: {run_result.stderr}")

            try:
                # Copy binary from container
                copy_result = subprocess.run(  # nosec B603 — fixed argv (docker), no shell
                    ["docker", "cp", f"{container_name}:/output/{binary_name}", str(output_dir)],
                    capture_output=True,
                    text=True,
                )

                if copy_result.returncode != 0:
                    raise RuntimeError(f"Failed to copy OTEL binary from container: {copy_result.stderr}")

                # Verify the binary was created
                binary_path = output_dir / binary_name
                if not binary_path.exists():
                    raise RuntimeError(f"Linux {arch} OTEL helper binary was not created successfully")

                # Make it executable
                binary_path.chmod(0o755)

                console.print(f"[green]✓ Linux {arch} OTEL helper built successfully via Docker[/green]")
                return binary_path

            finally:
                # Clean up container and image
                subprocess.run(["docker", "rm", container_name], capture_output=True)  # nosec B603 — fixed argv (docker), no shell
                subprocess.run(["docker", "rmi", image_tag], capture_output=True)  # nosec B603 — fixed argv (docker), no shell

    def _build_windows_via_codebuild(self, output_dir: Path) -> Path:
        """Build Windows binaries using AWS CodeBuild."""
        import json

        import boto3
        from botocore.exceptions import ClientError

        console = Console()

        # Check for in-progress builds only (not completed ones)
        try:
            config = Config.load()
            profile_name = self.option("profile")
            profile = config.get_profile(profile_name)

            if profile:
                project_name = f"{profile.identity_pool_name}-windows-build"
                codebuild = boto3.client("codebuild", region_name=get_codebuild_region(profile))

                # List recent builds
                response = codebuild.list_builds_for_project(projectName=project_name, sortOrder="DESCENDING")

                if response.get("ids"):
                    # Check only the most recent builds
                    build_ids = response["ids"][:3]
                    builds_response = codebuild.batch_get_builds(ids=build_ids)

                    for build in builds_response.get("builds", []):
                        if build["buildStatus"] == "IN_PROGRESS":
                            console.print(
                                f"[yellow]Windows build already in progress (started "
                                f"{build['startTime'].strftime('%Y-%m-%d %H:%M')})[/yellow]"
                            )
                            console.print("Check status: [cyan]poetry run gip builds[/cyan]")
                            console.print("[dim]Note: Package will be created without Windows binaries[/dim]")
                            # Don't return early - continue to create package with available binaries
        except Exception as e:
            console.print(f"[dim]Could not check for recent builds: {e}[/dim]")

        # Load profile to get CodeBuild configuration
        config = Config.load()
        profile_name = self.option("profile")
        profile = config.get_profile(profile_name)

        if not profile or not profile.enable_codebuild:
            console.print("[red]CodeBuild is not enabled for this profile.[/red]")
            console.print("To enable CodeBuild for Windows builds:")
            console.print("  1. Run: poetry run gip init")
            console.print("  2. Answer 'Yes' when asked about Windows build support")
            console.print("  3. Run: poetry run gip deploy codebuild")
            raise RuntimeError("CodeBuild not enabled")

        # Get CodeBuild stack outputs
        stack_name = profile.stack_names.get("codebuild", f"{profile.identity_pool_name}-codebuild")
        try:
            stack_outputs = get_stack_outputs(stack_name, get_codebuild_region(profile))
        except Exception:
            console.print(f"[red]CodeBuild stack not found: {stack_name}[/red]")
            console.print("Run: poetry run gip deploy codebuild")
            raise RuntimeError("CodeBuild stack not deployed") from None

        bucket_name = stack_outputs.get("BuildBucket")
        project_name = stack_outputs.get("ProjectName")

        if not bucket_name or not project_name:
            console.print("[red]CodeBuild stack outputs not found[/red]")
            raise RuntimeError("Invalid CodeBuild stack")

        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console
        ) as progress:
            # Package source code
            task = progress.add_task("Packaging source code for CodeBuild...", total=None)
            source_zip = self._package_source_for_codebuild()

            # Upload to S3
            progress.update(task, description="Uploading source to S3...")
            s3 = boto3.client("s3", region_name=get_codebuild_region(profile))
            try:
                s3.upload_file(str(source_zip), bucket_name, "source.zip")
            except ClientError as e:
                console.print(f"[red]Failed to upload source: {e}[/red]")
                raise

            # Start build
            progress.update(task, description="Starting CodeBuild project...")
            codebuild = boto3.client("codebuild", region_name=get_codebuild_region(profile))
            try:
                response = codebuild.start_build(projectName=project_name)
                build_id = response["build"]["id"]
            except ClientError as e:
                console.print(f"[red]Failed to start build: {e}[/red]")
                raise

            # Monitor build
            progress.update(task, description="Building Windows binaries (20+ minutes)...")
            console.print(f"[dim]Build ID: {build_id}[/dim]")

            # Store build ID for later retrieval
            from pathlib import Path

            build_info_file = Path.home() / ".gip" / "latest-build.json"
            try:
                build_info_file.parent.mkdir(exist_ok=True)
                with open(build_info_file, "w", encoding="utf-8") as f:
                    json.dump(
                        {
                            "build_id": build_id,
                            "started_at": datetime.now().isoformat(),
                            "project": project_name,
                            "bucket": bucket_name,
                        },
                        f,
                    )
            except OSError as exc:
                console.print(
                    f"[yellow]Build started, but local status metadata could not be written: {exc}. "
                    f"Save this build ID: {build_id}[/yellow]"
                )

            # Clean up source zip
            try:
                source_zip.unlink()
            except OSError as exc:
                console.print(f"[yellow]Build started, but temporary source cleanup failed: {exc}[/yellow]")
            progress.update(task, completed=True)

        # Don't wait - return build info immediately
        console.print("\n[bold yellow]Windows build started![/bold yellow]")
        console.print(f"[dim]Build ID: {build_id}[/dim]")
        console.print("Build will take approximately 20+ minutes to complete.")

        console.print("\n[bold]Monitor build progress:[/bold]")
        console.print("  [cyan]poetry run gip builds[/cyan]")
        console.print("  This shows the current status and elapsed time")

        console.print("\n[bold]Next steps:[/bold]")
        console.print("  1. Wait for build to complete (you can continue working)")
        console.print("  2. Run [cyan]poetry run gip builds[/cyan] to check completion status")
        console.print("  3. Once complete, run [cyan]poetry run gip distribute[/cyan]")
        console.print("     This will download Windows binaries and create your distribution package")

        # Get profile to show distribution-specific info
        config = Config.load()
        profile_obj = config.get_profile(self.option("profile"))

        if profile_obj and profile_obj.enable_distribution:
            console.print("\n[dim]Note: Package will be uploaded to S3 with presigned URL or landing page[/dim]")
        else:
            console.print("\n[dim]Note: Package will be saved locally in the dist/ folder[/dim]")

        console.print("\n[dim]View logs in AWS Console:[/dim]")
        console.print(
            f"  [dim]https://console.aws.amazon.com/codesuite/codebuild/projects/{project_name}/build/{build_id.split(':')[1]}[/dim]"
        )

        # Return None since we don't have a local binary path
        return None

    def _package_source_for_codebuild(self) -> Path:
        """Package source code for CodeBuild."""
        import tempfile
        import zipfile

        # Create a temporary zip file
        temp_dir = Path(tempfile.mkdtemp())
        source_zip = temp_dir / "source.zip"

        # Get the source directory (parent of package.py)
        source_dir = Path(__file__).parents[3]  # Go up to source/ directory

        with zipfile.ZipFile(source_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            # Add all Python files from source directory
            for py_file in source_dir.rglob("*.py"):
                # Use forward slashes in zip (POSIX format) for CodeBuild compatibility
                arcname = py_file.relative_to(source_dir.parent).as_posix()
                zf.write(py_file, arcname)

            # Add pyproject.toml for dependencies
            pyproject_file = source_dir / "pyproject.toml"
            if pyproject_file.exists():
                zf.write(pyproject_file, "pyproject.toml")

        return source_zip

    def _build_otel_helper(self, output_dir: Path, target_platform: str) -> Path:
        """Build executable for OTEL helper script."""
        import platform as platform_mod

        # Windows builds
        if target_platform == "windows":
            if platform_mod.system().lower() == "windows":
                # Native Windows build with Nuitka
                return self._build_native_otel_helper(output_dir, "windows")
            # Check if the Windows binary already exists (built via CodeBuild)
            windows_binary = output_dir / "otel-helper-windows.exe"
            if windows_binary.exists():
                return windows_binary
            else:
                raise RuntimeError("Windows otel-helper should have been built with credential-process")

        # macOS builds use PyInstaller
        if target_platform == "macos-arm64":
            return self._build_otel_helper_pyinstaller(output_dir, "macos", "arm64")
        elif target_platform == "macos-intel":
            return self._build_otel_helper_pyinstaller(output_dir, "macos", "x86_64")
        elif target_platform == "macos-universal":
            return self._build_otel_helper_pyinstaller(output_dir, "macos", "universal2")
        elif target_platform == "macos":
            import platform

            current_machine = platform.machine().lower()
            if current_machine == "arm64":
                return self._build_otel_helper_pyinstaller(output_dir, "macos", "arm64")
            else:
                return self._build_otel_helper_pyinstaller(output_dir, "macos", "x86_64")

        # Linux builds use PyInstaller via Docker
        elif target_platform == "linux-x64":
            return self._build_linux_otel_helper_via_docker(output_dir, "x64")
        elif target_platform == "linux-arm64":
            return self._build_linux_otel_helper_via_docker(output_dir, "arm64")
        elif target_platform == "linux":
            return self._build_otel_helper_pyinstaller(output_dir, "linux", None)

        # Fallback
        raise ValueError(f"Unsupported target platform for OTEL helper: {target_platform}")

    def _build_otel_helper_pyinstaller(self, output_dir: Path, platform_name: str, arch: str | None) -> Path:
        """Build OTEL helper using PyInstaller."""
        import platform as platform_module

        if platform_name == "macos":
            _assert_host_os_can_build_macos()

        console = Console()
        verbose = self.option("build-verbose")

        # Determine binary name
        if platform_name == "macos":
            if arch == "arm64":
                binary_name = "otel-helper-macos-arm64"
            elif arch == "x86_64":
                binary_name = "otel-helper-macos-intel"
            elif arch == "universal2":
                binary_name = "otel-helper-macos-universal"
            else:
                binary_name = "otel-helper-macos"
        elif platform_name == "linux":
            # Detect architecture and set appropriate binary name
            machine = platform_module.machine().lower()
            if machine in ["aarch64", "arm64"]:
                binary_name = "otel-helper-linux-arm64"
            else:
                binary_name = "otel-helper-linux-x64"
        else:
            raise ValueError(f"Unsupported platform for OTEL helper: {platform_name}")

        # Find the source file
        src_file = Path(__file__).parent.parent.parent.parent / "otel_helper" / "__main__.py"
        if not src_file.exists():
            raise FileNotFoundError(f"OTEL helper source not found: {src_file}")

        console.print(f"[yellow]Building OTEL helper for {platform_name} {arch or ''} with PyInstaller...[/yellow]")

        # Determine log level based on verbose flag
        log_level = "INFO" if verbose else "WARN"

        host_arch = platform_module.machine().lower()
        cross_arch = platform_name == "macos" and arch is not None and arch != host_arch and arch != "universal2"

        # Build PyInstaller command
        if cross_arch:
            # Cross-arch build: need a per-arch venv seeded from a universal2 Python
            universal2_python = _find_universal2_python()
            if universal2_python is None:
                console.print(
                    f"[yellow]Warning: Skipping {binary_name} — cross-arch build requires universal2 Python (not found)[/yellow]"
                )
                return output_dir / binary_name
            venv_dir = _ensure_cross_arch_venv(arch, universal2_python, _OTEL_HELPER_RUNTIME_DEPS, console)
            work_root = Path.home() / ".gip" / "build-work"
            work_root.mkdir(parents=True, exist_ok=True)
            cmd = [
                "/usr/bin/arch",
                f"-{arch}",
                str(venv_dir / "bin" / "pyinstaller"),
                "--onefile",
                "--clean",
                "--noconfirm",
                f"--name={binary_name}",
                f"--distpath={str(output_dir)}",
                f"--workpath={str(work_root / arch)}",
                f"--specpath={str(work_root / arch)}",
                f"--log-level={log_level}",
                str(src_file),
            ]
        else:
            cmd = [
                "poetry",
                "run",
                "pyinstaller",
                "--onefile",
                "--clean",
                "--noconfirm",
                f"--name={binary_name}",
                f"--distpath={str(output_dir)}",
                "--workpath=/tmp/pyinstaller",
                "--specpath=/tmp/pyinstaller",
                f"--log-level={log_level}",
                str(src_file),
            ]

        # Add target architecture for macOS (only for native Poetry build)
        if not cross_arch and platform_name == "macos" and arch:
            cmd.insert(5, f"--target-arch={arch}")

        # Run PyInstaller from source directory
        source_dir = Path(__file__).parent.parent.parent.parent
        result = run_checked(cmd, capture_output=not verbose, text=True, cwd=source_dir)  # nosec B603 — internal build argv, no shell

        if result.returncode != 0:
            console.print(f"[red]PyInstaller build failed for OTEL helper: {result.stderr}[/red]")
            raise RuntimeError(f"PyInstaller build failed: {result.stderr}")

        binary_path = output_dir / binary_name
        if binary_path.exists():
            binary_path.chmod(0o755)
            console.print("[green]✓ OTEL helper built successfully with PyInstaller[/green]")
            return binary_path
        else:
            raise RuntimeError(f"OTEL helper binary not created: {binary_path}")

    def _build_native_otel_helper(self, output_dir: Path, target_platform: str) -> Path:
        """Build OTEL helper using native Nuitka compiler."""
        import platform

        current_system = platform.system().lower()
        current_machine = platform.machine().lower()

        # Determine the binary name based on platform and architecture
        if target_platform == "macos":
            # Check if user requested a specific variant via environment variable
            macos_variant = os.environ.get("GIP_MACOS_VARIANT", "").lower()

            if macos_variant == "intel":
                platform_variant = "intel"
                binary_name = "otel-helper-macos-intel"
            elif macos_variant == "arm64":
                platform_variant = "arm64"
                binary_name = "otel-helper-macos-arm64"
            elif current_machine == "arm64":
                platform_variant = "arm64"
                binary_name = "otel-helper-macos-arm64"
            else:
                platform_variant = "intel"
                binary_name = "otel-helper-macos-intel"
        elif target_platform == "linux":
            platform_variant = "x86_64"
            binary_name = "otel-helper-linux"
        elif target_platform == "windows":
            platform_variant = "x86_64"
            binary_name = "otel-helper-windows.exe"
        else:
            raise ValueError(f"Unsupported target platform: {target_platform}")

        # Check platform compatibility (same as credential-process)
        if target_platform == "macos" and current_system != "darwin":
            raise RuntimeError(f"Cannot build macOS binary on {current_system}. Nuitka requires native builds.")
        elif target_platform == "linux" and current_system != "linux":
            raise RuntimeError(f"Cannot build Linux binary on {current_system}. Nuitka requires native builds.")
        elif target_platform == "windows" and current_system != "windows":
            raise RuntimeError(f"Cannot build Windows binary on {current_system}. Nuitka requires native builds.")

        # Find the source file
        src_file = Path(__file__).parent.parent.parent.parent / "otel_helper" / "__main__.py"

        if not src_file.exists():
            raise FileNotFoundError(f"OTEL helper script not found: {src_file}")

        # Build Nuitka command (use poetry run to ensure correct Python version)
        # If building Intel binary on ARM Mac, use Rosetta
        if (
            target_platform == "macos"
            and platform_variant == "intel"
            and current_system == "darwin"
            and current_machine == "arm64"
        ):
            cmd = [
                "arch",
                "-x86_64",  # Run under Rosetta
                "poetry",
                "run",
                "nuitka",
            ]
        else:
            cmd = [
                "poetry",
                "run",
                "nuitka",
            ]

        # Add common Nuitka flags
        cmd.extend(
            [
                "--standalone",
                "--onefile",
                "--assume-yes-for-downloads",
                f"--output-filename={binary_name}",
                f"--output-dir={str(output_dir)}",
                "--quiet",
                "--remove-output",
                "--python-flag=no_site",
            ]
        )

        # Add platform-specific flags
        if target_platform == "macos":
            cmd.extend(
                [
                    "--macos-create-app-bundle",
                    "--macos-app-name=Claude Code OTEL Helper",
                    "--disable-console",
                ]
            )
        elif target_platform == "linux":
            cmd.extend(
                [
                    "--linux-onefile-icon=NONE",
                ]
            )

        # Add the source file
        cmd.append(str(src_file))

        # Run Nuitka (from source directory where pyproject.toml is located)
        source_dir = Path(__file__).parent.parent.parent.parent
        result = run_checked(cmd, capture_output=True, text=True, cwd=source_dir)  # nosec B603 — internal build argv, no shell
        if result.returncode != 0:
            raise RuntimeError(f"Nuitka build failed for OTEL helper: {result.stderr}")

        return output_dir / binary_name

    def _prepare_offline_bundle(self, console: Console) -> int:
        """Prepare an offline bundle for air-gapped Go and OTEL collector builds.

        Downloads the OCB binary and pre-seeds the Go module cache so that
        `gip package` can run without network access. The bundle is saved
        to `gip-offline-go-bundle/` in the repo root.
        """
        import subprocess

        script_path = Path(__file__).resolve().parents[4] / "scripts" / "prepare-offline-go-bundle.sh"
        if not script_path.exists():
            console.print(f"[red]Offline bundle script not found at {script_path}[/red]")
            return 1

        console.print("[bold]Preparing offline bundle...[/bold]")
        console.print("[dim]This downloads OCB + Go modules and verifies the bundle with a test build.[/dim]\n")

        try:
            result = subprocess.run(  # nosec B603 — repo-bundled script via bash, fixed argv
                ["bash", str(script_path), "prepare"],
                cwd=script_path.parent.parent,
            )
            if result.returncode == 0:
                console.print("\n[green]✓ Offline bundle ready.[/green]")
                console.print("\n[bold]Next steps:[/bold]")
                console.print("  1. Transfer [cyan]gip-offline-go-bundle.tar.gz[/cyan] to the air-gapped machine")
                console.print("  2. Extract: [cyan]tar xzf gip-offline-go-bundle.tar.gz[/cyan]")
                console.print("  3. Install: [cyan]./scripts/prepare-offline-go-bundle.sh install[/cyan]")
                console.print("  4. Source env: [cyan]source gip-offline-go-bundle/offline-env.sh[/cyan]")
                console.print("  5. Build: [cyan]poetry run gip package[/cyan]")
            return result.returncode
        except FileNotFoundError:
            console.print("[red]bash not found. This command requires a Unix-like environment.[/red]")
            return 1

    def _regenerate_installers(self, profile, profile_name: str, console: Console) -> int:
        """Regenerate installer scripts using existing binaries from the latest dist folder."""
        import shutil

        # Find latest dist folder for this profile
        dist_base = Path("./dist") / profile_name
        if not dist_base.exists():
            console.print(f"[red]No dist folder found for profile '{profile_name}'.[/red]")
            console.print("Run 'gip package' first to build binaries.")
            return 1

        # Find the latest timestamped directory
        timestamp_dirs = sorted(
            [d for d in dist_base.iterdir() if d.is_dir()],
            key=lambda d: d.name,
            reverse=True,
        )
        if not timestamp_dirs:
            console.print(f"[red]No builds found in {dist_base}.[/red]")
            return 1

        source_dir = timestamp_dirs[0]
        console.print(f"[cyan]Using existing binaries from: {source_dir}[/cyan]")

        # Detect existing binaries and otel helpers
        binary_patterns = {
            "macos-arm64": "credential-process-macos-arm64",
            "macos-intel": "credential-process-macos-intel",
            "linux-x64": "credential-process-linux-x64",
            "linux-arm64": "credential-process-linux-arm64",
            "windows": "credential-process-windows.exe",
        }
        otel_patterns = {
            "macos-arm64": "otel-helper-macos-arm64",
            "macos-intel": "otel-helper-macos-intel",
            "linux-x64": "otel-helper-linux-x64",
            "linux-arm64": "otel-helper-linux-arm64",
            "windows": "otel-helper-windows.exe",
        }

        built_executables = []
        built_otel_helpers = []
        for plat, binary_name in binary_patterns.items():
            binary_path = source_dir / binary_name
            if binary_path.exists():
                built_executables.append((plat, binary_path))
        for plat, helper_name in otel_patterns.items():
            helper_path = source_dir / helper_name
            if helper_path.exists():
                built_otel_helpers.append((plat, helper_path))

        if not built_executables:
            console.print("[red]No binaries found in the dist folder.[/red]")
            return 1

        console.print(f"[green]Found {len(built_executables)} binaries, {len(built_otel_helpers)} OTEL helpers[/green]")
        for _plat, path in built_executables:
            console.print(f"  • {path.name}")

        # Create new timestamped output directory
        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        output_dir = Path("./dist") / profile_name / timestamp
        output_dir.mkdir(parents=True, exist_ok=True)

        # Copy existing binaries to new output dir
        console.print("\n[cyan]Copying binaries...[/cyan]")
        for _plat, binary_path in built_executables:
            shutil.copy2(binary_path, output_dir / binary_path.name)
        for _plat, helper_path in built_otel_helpers:
            shutil.copy2(helper_path, output_dir / helper_path.name)

        # Include PowerShell otel-helper fallback for Windows
        if any(plat == "windows" for plat, _ in built_otel_helpers):
            otel_src = Path(__file__).resolve().parent.parent.parent.parent / "otel_helper"
            for script_name in ("otel-helper.ps1", "otel-helper.cmd"):
                script_src = otel_src / script_name
                if script_src.exists():
                    shutil.copy2(script_src, output_dir / script_name)

        # Resolve federation identifier (role ARN for direct STS, Identity Pool ID
        # for Cognito) — see _resolve_federation. Cognito ALWAYS reads the pool ID
        # from stack outputs; identity_pool_name is a name, not the pool ID.
        federation_type, identity_pool_id, federated_role_arn = self._resolve_federation(profile, console)
        federation_identifier = federated_role_arn if federation_type == "direct" else identity_pool_id

        if not federation_identifier or federation_identifier == "N/A":
            console.print("[red]Federation identifier not found in profile or stack outputs.[/red]")
            return 1

        # Prompt for co-authorship and OTEL attributes
        if _is_interactive():
            include_coauthored_by = questionary.confirm(
                "Include 'Co-Authored-By: Claude' in git commits?", default=False
            ).ask()
        else:
            include_coauthored_by = False

        otel_resource_attributes = None
        if profile.monitoring_enabled:
            if _is_interactive():
                customize_otel = questionary.confirm("Customize telemetry resource attributes?", default=False).ask()
            else:
                customize_otel = False
            if customize_otel:
                department = questionary.text("Department:", default="engineering").ask()
                team_id = questionary.text("Team ID:", default="default").ask()
                cost_center = questionary.text("Cost center:", default="default").ask()
                organization = questionary.text("Organization:", default="default").ask()
                otel_resource_attributes = (
                    f"department={department},team.id={team_id},cost_center={cost_center},organization={organization}"
                )

        # Regenerate config.json
        console.print("[cyan]Generating configuration...[/cyan]")
        self._create_config(output_dir, profile, federation_identifier, federation_type, profile_name)

        # Regenerate installer scripts
        console.print("[cyan]Generating installer scripts...[/cyan]")
        self._create_installer(output_dir, profile, built_executables, built_otel_helpers)

        # Regenerate documentation
        console.print("[cyan]Generating documentation...[/cyan]")
        self._create_documentation(output_dir, profile, timestamp)

        # Regenerate Claude Code settings
        console.print("[cyan]Generating Claude Code settings...[/cyan]")
        self._create_claude_settings(
            output_dir,
            profile,
            include_coauthored_by,
            profile_name,
            otel_resource_attributes,
            settings_version=timestamp,
        )
        self._create_claude_mcp_config(output_dir, profile, console)

        # Summary
        console.print("\n[green]✓ Installers regenerated successfully![/green]")
        console.print(f"\nOutput directory: [cyan]{output_dir}[/cyan]")
        console.print("\nRegenerated files:")
        console.print("  • config.json")
        console.print("  • install.sh")
        if (output_dir / "install.bat").exists():
            console.print("  • install.bat")
            console.print("  • gip-install.ps1")
        console.print("  • README.md")
        if (output_dir / "gip-settings" / "settings.json").exists():
            console.print("  • gip-settings/settings.json")
        console.print(f"\nBinaries copied from: [dim]{source_dir}[/dim]")
        console.print(
            "\n[bold]Next: Run '[cyan]poetry run gip distribute --per-os[/cyan]' to create distribution packages.[/bold]"
        )
        return 0

    def _resolve_federation(self, profile, console):
        r"""Resolve federation details for packaging.

        Returns a ``(federation_type, identity_pool_id, federated_role_arn)``
        tuple. On success exactly one of ``identity_pool_id`` /
        ``federated_role_arn`` is set; both are ``None`` when SSO is disabled or
        when resolution fails (the caller is expected to handle the failure).

        IMPORTANT — Cognito Identity Pool federation ALWAYS resolves the pool ID
        from the deployed CloudFormation stack outputs. ``profile.identity_pool_name``
        holds only a human-readable name (e.g. ``"gip-auth"``); the real
        pool ID has the form ``"<region>:<uuid>"`` and is created by
        ``gip deploy``. Using the name as the identity pool ID produces a
        ``config.json`` that fails Cognito ``GetId`` validation
        (``[\w-]+:[0-9a-f-]+``), breaking authentication for every distributed
        user. Direct STS keeps a profile shortcut because the profile stores the
        real role ARN.
        """
        federation_type = profile.federation_type

        if not getattr(profile, "sso_enabled", True):
            # SSO disabled — no auth stack to query, no federation needed.
            console.print("[dim]SSO disabled — skipping auth stack lookup[/dim]")
            return federation_type, None, None

        # Direct STS: the profile stores the real role ARN, so it is a safe shortcut.
        if federation_type == "direct" and getattr(profile, "federated_role_arn", None):
            console.print(f"[dim]Using role ARN from profile: {profile.federated_role_arn}[/dim]")
            return federation_type, None, profile.federated_role_arn

        # Cognito (and direct without a cached ARN) must read from the stack: the
        # profile never holds the real identity pool ID, only its name.
        console.print("[yellow]Fetching deployment information from CloudFormation...[/yellow]")
        stack_outputs = get_stack_outputs(
            profile.stack_names.get("auth", f"{profile.identity_pool_name}-stack"), profile.aws_region
        )
        if not stack_outputs:
            console.print("[red]Could not fetch stack outputs. Is the stack deployed?[/red]")
            return federation_type, None, None

        federation_type = stack_outputs.get("FederationType", profile.federation_type)

        if federation_type == "direct":
            federated_role_arn = stack_outputs.get("DirectSTSRoleArn")
            if not federated_role_arn or federated_role_arn == "N/A":
                federated_role_arn = stack_outputs.get("FederatedRoleArn")
            if not federated_role_arn or federated_role_arn == "N/A":
                console.print("[red]Direct STS Role ARN not found in stack outputs.[/red]")
                return federation_type, None, None
            return federation_type, None, federated_role_arn

        identity_pool_id = stack_outputs.get("IdentityPoolId")
        if not identity_pool_id:
            console.print("[red]Identity Pool ID not found in stack outputs.[/red]")
            return federation_type, None, None
        return federation_type, identity_pool_id, None

    def _create_config(
        self,
        output_dir: Path,
        profile,
        federation_identifier: str,
        federation_type: str = "cognito",
        profile_name: str = "gip",
        console=None,
    ) -> Path:
        """Create the configuration file.

        Args:
            output_dir: Directory to write config.json to
            profile: Profile object with configuration
            federation_identifier: Identity pool ID or role ARN
            federation_type: "cognito" or "direct"
            profile_name: Name to use as key in config.json (defaults to "gip" for backward compatibility)
        """
        sso_enabled = getattr(profile, "sso_enabled", True)
        # F-010: All packaged consumers share the canonical nested schema.
        config = {
            "profiles": {
                profile_name: {
                    "provider_domain": profile.provider_domain,
                    "client_id": profile.client_id,
                    "aws_region": profile.aws_region,
                    "provider_type": profile.provider_type or self._detect_provider_type(profile.provider_domain),
                    "credential_storage": profile.credential_storage,
                    "cross_region_profile": profile.cross_region_profile or "us",
                    "sso_enabled": sso_enabled,
                }
            }
        }
        profile_config = config["profiles"][profile_name]

        # Add the appropriate federation field based on type
        if not sso_enabled:
            # IDC path: include IDC fields so credential-process can drive SSO auth.
            auth_type = getattr(profile, "auth_type", None)
            if auth_type == "idc":
                profile_config["auth_type"] = "idc"
                if getattr(profile, "idc_start_url", None):
                    profile_config["idc_start_url"] = profile.idc_start_url
                if getattr(profile, "idc_account_id", None):
                    profile_config["idc_account_id"] = profile.idc_account_id
                if getattr(profile, "idc_permission_set_name", None):
                    profile_config["idc_permission_set_name"] = profile.idc_permission_set_name
                idc_region = (
                    getattr(profile, "sso_region", None) or getattr(profile, "idc_region", None) or profile.aws_region
                )
                profile_config["idc_region"] = idc_region
        elif federation_type == "direct":
            profile_config["federated_role_arn"] = federation_identifier
            profile_config["federation_type"] = "direct"
            profile_config["max_session_duration"] = profile.max_session_duration
            session_binding = getattr(profile, "session_name_binding", "none") or "none"
            if session_binding != "none":
                profile_config["session_name_binding"] = session_binding
        else:
            profile_config["identity_pool_id"] = federation_identifier
            profile_config["federation_type"] = "cognito"

        # Add cognito_user_pool_id if it's a Cognito provider
        if profile.provider_type == "cognito" and profile.cognito_user_pool_id:
            profile_config["cognito_user_pool_id"] = profile.cognito_user_pool_id

        # Okta authorization server: the profile is the single source of truth.
        # Written under BOTH runtime key names — the Python credential provider
        # reads "okta_auth_server", the Go provider reads "okta_auth_server_id".
        # Without this, the runtime always fell back to the Org Authorization
        # Server (iss=https://<domain>) while deploy-side JWT authorizers pin
        # /oauth2/<id> issuers — a split-brain that 401s every quota /check.
        # Empty string is meaningful (Org AS on paid plans) and written as-is.
        if profile.provider_type == "okta":
            okta_auth_server = getattr(profile, "okta_auth_server", "") or ""
            profile_config["okta_auth_server"] = okta_auth_server
            profile_config["okta_auth_server_id"] = okta_auth_server

        # Add selected_model if available
        if hasattr(profile, "selected_model") and profile.selected_model:
            profile_config["selected_model"] = profile.selected_model

        # Google OAuth requires client_secret for server-side token exchange (PKCE alone
        # is insufficient for the installed-app flow used by the credential-process binary).
        if getattr(profile, "provider_type", None) == "google" and getattr(profile, "client_secret", None):
            profile_config["client_secret"] = profile.client_secret

        # Add confidential client fields for Azure AD if present.
        # Azure client_secret is never written to config.json — it lives in the OS keyring.
        # End users set it with: credential-process --set-client-secret --profile <profile>
        if getattr(profile, "azure_auth_mode", None):
            profile_config["azure_auth_mode"] = profile.azure_auth_mode
        if getattr(profile, "client_certificate_path", None):
            profile_config["client_certificate_path"] = profile.client_certificate_path
            profile_config["client_certificate_key_path"] = profile.client_certificate_key_path
            # Warn if the paths are absolute — they are machine-specific and will not
            # resolve on end-user machines with different install layouts.
            cert_is_absolute = Path(profile.client_certificate_path).is_absolute()
            key_is_absolute = Path(profile.client_certificate_key_path).is_absolute()
            if (cert_is_absolute or key_is_absolute) and console:
                console.print(
                    "\n[yellow]Warning: certificate paths in config.json are absolute and will not "
                    "resolve on machines where the files are stored elsewhere.[/yellow]"
                )
                console.print("[yellow]Instruct end users to set the following environment variables:[/yellow]")
                console.print("[dim]  AZURE_CLIENT_CERTIFICATE_PATH=<path/to/cert.pem>[/dim]")
                console.print("[dim]  AZURE_CLIENT_CERTIFICATE_KEY_PATH=<path/to/key.pem>[/dim]\n")

        # Add Generic OIDC endpoint fields (CyberArk, PingFederate, Keycloak, ForgeRock, etc.)
        if profile.provider_type == "generic":
            for field in (
                "oidc_issuer_url",
                "oidc_authorization_endpoint",
                "oidc_token_endpoint",
                "oidc_jwks_uri",
                "oidc_thumbprint",
            ):
                value = getattr(profile, field, None)
                if value:
                    profile_config[field] = value

        # Add custom redirect port if configured
        if getattr(profile, "redirect_port", None):
            profile_config["redirect_port"] = profile.redirect_port

        # Add quota enforcement settings if configured
        if getattr(profile, "quota_api_endpoint", None):
            profile_config["quota_api_endpoint"] = profile.quota_api_endpoint
            profile_config["quota_fail_mode"] = getattr(profile, "quota_fail_mode", "closed")
            profile_config["quota_check_interval"] = getattr(profile, "quota_check_interval", 30)

        config_path = output_dir / "config.json"
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        return config_path

    def _get_bedrock_region_for_profile(self, profile) -> str:
        """Get the correct AWS region for Bedrock API calls based on user-selected source region."""
        return get_source_region_for_profile(profile)

    def _detect_provider_type(self, domain: str) -> str:
        """Auto-detect provider type from domain."""
        from urllib.parse import urlparse

        if not domain:
            return "oidc"

        # Handle both full URLs and domain-only inputs
        url_to_parse = domain if domain.startswith(("http://", "https://")) else f"https://{domain}"

        try:
            parsed = urlparse(url_to_parse)
            hostname = parsed.hostname

            if not hostname:
                return "oidc"

            hostname_lower = hostname.lower()

            # Check for exact domain match or subdomain match
            # Using endswith with leading dot prevents bypass attacks
            okta_domains = (".okta.com", ".oktapreview.com", ".okta-emea.com")
            if hostname_lower.endswith(okta_domains) or hostname_lower in (
                "okta.com",
                "oktapreview.com",
                "okta-emea.com",
            ):
                return "okta"
            elif hostname_lower.endswith(".auth0.com") or hostname_lower == "auth0.com":
                return "auth0"
            elif hostname_lower.endswith(".microsoftonline.com") or hostname_lower == "microsoftonline.com":
                return "azure"
            elif hostname_lower.endswith(".windows.net") or hostname_lower == "windows.net":
                return "azure"
            elif hostname_lower.endswith(".amazoncognito.com") or hostname_lower == "amazoncognito.com":
                return "cognito"
            elif hostname_lower.startswith("cognito-idp.") and ".amazonaws.com" in hostname_lower:
                return "cognito"
            else:
                return "auto"  # Let credential_provider auto-detect from domain at runtime
        except Exception:
            return "auto"  # Let credential_provider auto-detect from domain at runtime

    def _create_installer(self, output_dir: Path, profile, built_executables, built_otel_helpers=None) -> Path:
        """Create simple installer script.

        Installer scripts are always generated here so their ownership checks
        cannot be replaced by unverified package artifacts.
        """
        installer_path = output_dir / "install.sh"
        external_ps1 = output_dir / "gip-install.ps1"
        existing_installers = [path.name for path in (installer_path, external_ps1) if path.exists()]
        if existing_installers:
            raise RuntimeError("Refusing unverified external installer scripts: " + ", ".join(existing_installers))

        # IDC zero-binary mode: no credential-process binary, auth via aws sso login
        _is_idc = getattr(profile, "effective_auth_type", getattr(profile, "auth_type", "oidc")) == "idc"
        _has_quota = bool(getattr(profile, "quota_api_endpoint", None))
        if _is_idc and not _has_quota and not built_executables:
            idc_start_url = getattr(profile, "idc_start_url", "") or ""
            idc_account_id = getattr(profile, "idc_account_id", "") or ""
            idc_permission_set = (
                getattr(profile, "idc_permission_set_name", "BedrockDeveloperAccess") or "BedrockDeveloperAccess"
            )
            aws_region = getattr(profile, "aws_region", "us-east-1") or "us-east-1"
            sso_region = getattr(profile, "sso_region", aws_region) or aws_region
            profile_section = (
                "[profile gip]\n"
                "sso_session = gip-session\n"
                f"sso_account_id = {idc_account_id}\n"
                f"sso_role_name = {idc_permission_set}\n"
                f"region = {aws_region}\n\n"
            )
            session_section = (
                "[sso-session gip-session]\n"
                f"sso_start_url = {idc_start_url}\n"
                f"sso_region = {sso_region}\n"
                "sso_registration_scopes = sso:account:access\n"
            )
            profile_digest = hashlib.sha256(profile_section.encode()).hexdigest()
            session_digest = hashlib.sha256(session_section.encode()).hexdigest()
            idc_content = f"""#!/bin/bash
# Governed Inference Platform — IAM Identity Center Installer
# Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

set -e

SCRIPT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
cd "$SCRIPT_DIR"

if [ -n "$SUDO_USER" ] || [ "$(id -u)" -eq 0 ]; then
    echo "ERROR: Do not run this installer with sudo or as root."
    echo "       User-scoped files must be installed by their owner; managed settings elevate separately."
    exit 1
fi
ACTUAL_USER="$USER"
ACTUAL_HOME="$HOME"

echo "======================================"
echo "Governed Inference Platform — IDC Setup"
echo "======================================"
echo

# Prerequisites
if ! command -v aws &>/dev/null; then
    echo "ERROR: AWS CLI v2 is required."
    echo "       Install from: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
    exit 1
fi
echo "✓ AWS CLI found"

if command -v sha256sum &>/dev/null; then
    GIP_SHA256="sha256sum"
elif command -v shasum &>/dev/null; then
    GIP_SHA256="shasum -a 256"
else
    echo "ERROR: sha256sum or shasum is required to record safe cleanup ownership."
    exit 1
fi

gip_sha256() {{
    $GIP_SHA256 "$1" | cut -d ' ' -f 1
}}

gip_manifest_digest() {{
    key="$1"
    manifest="$ACTUAL_HOME/gip/.gip-ownership.json"
    [ -f "$manifest" ] || return 1
    count=$(grep -F -c "\\\"$key\\\":" "$manifest" || true)
    [ "$count" = "1" ] || return 1
    value=$(grep -F "\\\"$key\\\":" "$manifest" | sed -E 's/.*"([0-9a-fA-F]{{64}})".*/\\1/')
    case "$value" in
        *[!0-9a-fA-F]*|'') return 1 ;;
    esac
    [ "${{#value}}" = "64" ] || return 1
    printf '%s' "$value"
}}

gip_section_digest() {{
    section_type="$1"
    section_name="$2"
    awk -v header="[$section_type $section_name]" '
        {{ line=$0; sub(/\r$/, "", line) }}
        line == header {{ active=1 }}
        active && line ~ /^\\[/ && line != header {{ exit }}
        active {{ print line }}
    ' "$ACTUAL_HOME/.aws/config" | tr -d '\r' | $GIP_SHA256 | cut -d ' ' -f 1
}}

gip_restore_quarantine() {{
    quarantine="$1"
    destination="$2"
    if [ -e "$destination" ] || [ -L "$destination" ]; then
        return 1
    fi
    if ln "$quarantine" "$destination"; then
        rm -f "$quarantine"
        return 0
    fi
    return 1
}}

gip_commit_snapshot() {{
    temporary="$1"
    destination="$2"
    expected="$3"
    parent=$(dirname "$destination")
    if [ -n "$expected" ]; then
        if [ ! -f "$destination" ] || [ -L "$destination" ] || [ "$(gip_sha256 "$destination")" != "$expected" ]; then
            rm -f "$temporary"
            echo "ERROR: Refusing to replace changed target: $destination"
            exit 1
        fi
        quarantine=$(mktemp "$parent/.gip-quarantine.XXXXXX")
        rm -f "$quarantine"
        mv "$destination" "$quarantine"
        if [ "$(gip_sha256 "$quarantine")" != "$expected" ]; then
            gip_restore_quarantine "$quarantine" "$destination" || true
            rm -f "$temporary"
            echo "ERROR: Target changed during replacement: $destination"
            exit 1
        fi
        if ! ln "$temporary" "$destination"; then
            if gip_restore_quarantine "$quarantine" "$destination"; then
                echo "ERROR: Could not install replacement; original restored: $destination"
            else
                echo "ERROR: Could not install replacement; original preserved at: $quarantine"
            fi
            rm -f "$temporary"
            exit 1
        fi
        rm -f "$temporary" "$quarantine"
        return
    fi
    if [ -e "$destination" ] || [ -L "$destination" ] || ! ln "$temporary" "$destination"; then
        rm -f "$temporary"
        echo "ERROR: Target appeared during installation: $destination"
        exit 1
    fi
    rm -f "$temporary"
}}

gip_atomic_copy() {{
    source="$1"
    destination="$2"
    key="$3"
    parent=$(dirname "$destination")
    if [ -L "$parent" ] || [ -L "$destination" ]; then
        echo "ERROR: Refusing symlinked install target: $destination"
        exit 1
    fi
    temporary=$(mktemp "$parent/.gip-install.XXXXXX")
    cp "$source" "$temporary"
    chmod 600 "$temporary"
    expected=""
    if [ -e "$destination" ] || [ -L "$destination" ]; then
        expected=$(gip_manifest_digest "$key" || true)
        if [ -z "$expected" ]; then
            rm -f "$temporary"
            echo "ERROR: Refusing to overwrite foreign target: $destination"
            exit 1
        fi
    fi
    gip_commit_snapshot "$temporary" "$destination" "$expected"
}}

for path in "$ACTUAL_HOME/gip" "$ACTUAL_HOME/.aws" "$ACTUAL_HOME/.claude" "$ACTUAL_HOME/.gip"; do
    if [ -L "$path" ]; then
        echo "ERROR: Refusing symlinked install path: $path"
        exit 1
    fi
done
for path in "$ACTUAL_HOME/.aws/config" "$ACTUAL_HOME/.claude/settings.json" "$ACTUAL_HOME/.gip/collector-config.yaml" "$ACTUAL_HOME/gip/.gip-ownership.json"; do
    if [ -L "$path" ]; then
        echo "ERROR: Refusing symlinked install target: $path"
        exit 1
    fi
done
GIP_MANIFEST="$ACTUAL_HOME/gip/.gip-ownership.json"
if [ -e "$GIP_MANIFEST" ] && ! grep -Fq '"schema_version": 1' "$GIP_MANIFEST"; then
    echo "ERROR: Invalid ownership manifest; run gip cleanup before reinstalling."
    exit 1
fi
if [ -f "$GIP_MANIFEST" ]; then
    GIP_MANIFEST_DIGEST=$(gip_sha256 "$GIP_MANIFEST")
    for foreign_key in \
        "gip/credential-process" \
        "gip/websearch-headers" \
        "gip/otel-helper" \
        "gip/otelcol" \
        "gip/collector-config.yaml" \
        "gip/claude-bedrock"; do
        if grep -Fq "\\\"$foreign_key\\\":" "$GIP_MANIFEST"; then
            echo "ERROR: Ownership manifest contains state from another installer mode; run gip cleanup first."
            exit 1
        fi
    done
else
    GIP_MANIFEST_DIGEST=""
fi

for entry in \
    "gip/config.json|$ACTUAL_HOME/gip/config.json" \
    ".gip/collector-config.yaml|$ACTUAL_HOME/.gip/collector-config.yaml"; do
    key=${{entry%%|*}}
    target=${{entry#*|}}
    if [ -e "$target" ]; then
        expected=$(gip_manifest_digest "$key" || true)
        actual=$(gip_sha256 "$target")
        if [ -z "$expected" ] || [ "$actual" != "$expected" ]; then
            if [ -n "$expected" ]; then classification="owned-modified"; else classification="foreign"; fi
            echo "ERROR: Refusing to overwrite $classification target: $target"
            exit 1
        fi
    fi
done

GIP_SETTINGS_CREATED=0
if [ -e "$ACTUAL_HOME/.claude/settings.json" ]; then
    expected=$(gip_manifest_digest ".claude/settings.json" || true)
    if [ -n "$expected" ]; then
        actual=$(gip_sha256 "$ACTUAL_HOME/.claude/settings.json")
        if [ "$actual" != "$expected" ]; then
            echo "ERROR: Refusing to overwrite owned-modified target: $ACTUAL_HOME/.claude/settings.json"
            exit 1
        fi
        GIP_SETTINGS_CREATED=1
    fi
fi

if [ -f "$ACTUAL_HOME/.aws/config" ]; then
    for section in "profile|gip" "sso-session|gip-session"; do
        section_type=${{section%%|*}}
        section_name=${{section#*|}}
        if tr -d '\r' < "$ACTUAL_HOME/.aws/config" | grep -Fqx "[$section_type $section_name]"; then
            expected=$(gip_manifest_digest "$section_name" || true)
            actual=$(gip_section_digest "$section_type" "$section_name")
            if [ -z "$expected" ] || [ "$actual" != "$expected" ]; then
                if [ -n "$expected" ]; then classification="owned-modified"; else classification="foreign"; fi
                echo "ERROR: Refusing to overwrite $classification AWS $section_type: $section_name"
                exit 1
            fi
        fi
    done
fi

# Write config.json
mkdir -p "$ACTUAL_HOME/gip"
gip_atomic_copy config.json "$ACTUAL_HOME/gip/config.json" \
    "gip/config.json"
echo "✓ Configuration installed"

# Install collector sidecar if present
GIP_COLLECTOR_CREATED=0
if [ -f "collector-config.yaml" ]; then
    mkdir -p "$ACTUAL_HOME/.gip"
    gip_atomic_copy collector-config.yaml "$ACTUAL_HOME/.gip/collector-config.yaml" \
        ".gip/collector-config.yaml"
    GIP_COLLECTOR_CREATED=1
    echo "✓ OTel collector config installed"
fi

# Configure AWS SSO profile
echo
echo "Configuring AWS SSO profile 'gip'..."
mkdir -p "$ACTUAL_HOME/.aws"
AWS_CONFIG="$ACTUAL_HOME/.aws/config"
if [ -f "$AWS_CONFIG" ]; then
    AWS_CONFIG_DIGEST=$(gip_sha256 "$AWS_CONFIG")
    AWS_CONFIG_INPUT="$AWS_CONFIG"
else
    AWS_CONFIG_DIGEST=""
    AWS_CONFIG_INPUT="/dev/null"
fi
AWS_CONFIG_TMP=$(mktemp "$ACTUAL_HOME/.aws/.config.gip.XXXXXX")
awk '
    {{ raw=$0; line=$0; sub(/\r$/, "", line) }}
    line == "[profile gip]" || line == "[sso-session gip-session]" {{ skip=1; next }}
    skip && line ~ /^\\[/ {{ skip=0 }}
    !skip {{ print raw }}
' "$AWS_CONFIG_INPUT" > "$AWS_CONFIG_TMP"
if [ -s "$AWS_CONFIG_TMP" ]; then printf '\n' >> "$AWS_CONFIG_TMP"; fi
cat >> "$AWS_CONFIG_TMP" << 'AWSCONFIG'
{profile_section}{session_section}AWSCONFIG
chmod 600 "$AWS_CONFIG_TMP"
gip_commit_snapshot "$AWS_CONFIG_TMP" "$AWS_CONFIG" "$AWS_CONFIG_DIGEST"
echo "✓ AWS profile 'gip' configured"

# Install Claude Code settings
if [ -d "gip-settings" ] && [ -f "gip-settings/settings.json" ]; then
    mkdir -p "$ACTUAL_HOME/.claude"
    if [ -e "$ACTUAL_HOME/.claude/settings.json" ] && [ "$GIP_SETTINGS_CREATED" != "1" ]; then
        echo "WARNING: Preserving existing Claude Code settings."
        echo "         Review gip-settings/settings.json and merge its AWS_PROFILE keys manually."
    else
        gip_atomic_copy "gip-settings/settings.json" "$ACTUAL_HOME/.claude/settings.json" \
            ".claude/settings.json"
        GIP_SETTINGS_CREATED=1
        echo "✓ Claude Code settings installed"
    fi
fi

echo "Recording gip ownership..."
GIP_MANIFEST_TMP=$(mktemp "$ACTUAL_HOME/gip/.gip-ownership.XXXXXX")
CONFIG_DIGEST=$(gip_sha256 "$ACTUAL_HOME/gip/config.json")
{{
    printf '{{\n  "schema_version": 1,\n  "files": {{\n'
    printf '    "gip/config.json": "%s"' "$CONFIG_DIGEST"
    if [ "$GIP_COLLECTOR_CREATED" = "1" ]; then
        COLLECTOR_DIGEST=$(gip_sha256 "$ACTUAL_HOME/.gip/collector-config.yaml")
        printf ',\n    ".gip/collector-config.yaml": "%s"' "$COLLECTOR_DIGEST"
    fi
    if [ "$GIP_SETTINGS_CREATED" = "1" ]; then
        SETTINGS_DIGEST=$(gip_sha256 "$ACTUAL_HOME/.claude/settings.json")
        printf ',\n    ".claude/settings.json": "%s"' "$SETTINGS_DIGEST"
    fi
    printf '\n  }},\n  "aws_profiles": {{\n'
    printf '    "gip": "{profile_digest}"\n'
    printf '  }},\n  "aws_sso_sessions": {{\n'
    printf '    "gip-session": "{session_digest}"\n'
    printf '  }}\n}}\n'
}} > "$GIP_MANIFEST_TMP"
chmod 600 "$GIP_MANIFEST_TMP"
gip_commit_snapshot "$GIP_MANIFEST_TMP" "$GIP_MANIFEST" "$GIP_MANIFEST_DIGEST"
echo "✓ Ownership manifest recorded"

echo
echo "======================================"
echo "Installation complete!"
echo "======================================"
echo
echo "Next step — authenticate with AWS SSO:"
echo
echo "  aws sso login --profile gip"
echo
echo "Then verify:"
echo
echo "  aws sts get-caller-identity --profile gip"
echo
echo "Claude Code will use the gip profile automatically."
echo "Re-run 'aws sso login --profile gip' when your session expires (every 8 hours)."
"""
            with open(installer_path, "w", encoding="utf-8") as f:
                f.write(idc_content)
            installer_path.chmod(0o755)
            return installer_path

        # Determine which binaries were built
        platforms_built = [platform for platform, _ in built_executables]
        [platform for platform, _ in built_otel_helpers] if built_otel_helpers else []

        installer_content = f"""#!/bin/bash
# Claude Code Authentication Installer
# Organization: {profile.provider_domain}
# Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

set -e

SCRIPT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
cd "$SCRIPT_DIR"

if [ -n "$SUDO_USER" ] || [ "$(id -u)" -eq 0 ]; then
    echo "ERROR: Do not run this installer with sudo or as root."
    echo "       User-scoped files must be installed by their owner; managed settings elevate separately."
    exit 1
fi
ACTUAL_USER="$USER"
ACTUAL_HOME="$HOME"

echo "======================================"
echo "Claude Code Authentication Installer"
echo "======================================"
echo
echo "Organization: {profile.provider_domain}"
echo


# Check prerequisites
echo "Checking prerequisites..."
HAS_ERRORS=false

if command -v aws &> /dev/null; then
    echo "✓ AWS CLI found (optional)"
else
    echo "ℹ  AWS CLI not found — not required. The credential process binary handles authentication directly."
fi

if [ ! -f "config.json" ]; then
    echo "ERROR: config.json not found in current directory"
    echo "       Make sure you are running this from the extracted package folder"
    HAS_ERRORS=true
fi

# Find a Python interpreter (needed for config parsing)
PYTHON=""
if command -v python3 &> /dev/null; then
    PYTHON="python3"
elif command -v python &> /dev/null; then
    PYTHON="python"
else
    echo "ERROR: Python is not installed (python3 or python)"
    echo "       Python is needed to parse configuration files"
    HAS_ERRORS=true
fi

if [ "$HAS_ERRORS" = "true" ]; then
    exit 1
fi

if [ ! -f "gip-settings/settings.json" ] && [ ! -f "gip-settings/managed-settings.json" ]; then
    echo "WARNING: gip-settings/settings.json not found"
    echo "         Claude Code IDE settings will not be configured automatically"
    echo ""
fi

echo "OK Prerequisites validated"

# Detect platform and architecture
echo
echo "Detecting platform and architecture..."
if [[ "$OSTYPE" == "darwin"* ]]; then
    PLATFORM="macos"
    ARCH=$(uname -m)
    if [[ "$ARCH" == "arm64" ]]; then
        echo "✓ Detected macOS ARM64 (Apple Silicon)"
        BINARY_SUFFIX="macos-arm64"
    else
        echo "✓ Detected macOS Intel"
        BINARY_SUFFIX="macos-intel"
    fi
elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
    PLATFORM="linux"
    ARCH=$(uname -m)
    if [[ "$ARCH" == "aarch64" ]] || [[ "$ARCH" == "arm64" ]]; then
        echo "✓ Detected Linux ARM64"
        BINARY_SUFFIX="linux-arm64"
    else
        echo "✓ Detected Linux x64"
        BINARY_SUFFIX="linux-x64"
    fi
else
    echo "❌ Unsupported platform: $OSTYPE"
    echo "   This installer supports macOS and Linux only."
    exit 1
fi

# Check if binary for platform exists
CREDENTIAL_BINARY="credential-process-$BINARY_SUFFIX"
OTEL_BINARY="otel-helper-$BINARY_SUFFIX"

if [ ! -f "$CREDENTIAL_BINARY" ]; then
    echo "❌ Binary not found for your platform: $CREDENTIAL_BINARY"
    echo "   Please ensure you have the correct package for your architecture."
    exit 1
fi
"""

        installer_content += r"""
# Classify every shared target before the first write. Existing managed files
# and AWS sections must either be absent or match this install's manifest.
GIP_SETTINGS_WAS_OWNED=$(
GIP_HOME="$ACTUAL_HOME" GIP_PACKAGE="$SCRIPT_DIR" "$PYTHON" <<'PY'
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

home = Path(os.environ["GIP_HOME"])
package = Path(os.environ["GIP_PACKAGE"])
install = home / "gip"
manifest_path = install / ".gip-ownership.json"

def fail(message):
    raise SystemExit(f"ERROR: {message}")

def digest(path):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or path.is_symlink():
        fail(f"refusing non-regular install target: {path}")
    value = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            fail(f"install target changed while opening: {path}")
        while block := os.read(descriptor, 65536):
            value.update(block)
    finally:
        os.close(descriptor)
    after = path.lstat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
    ):
        fail(f"install target changed during preflight: {path}")
    return value.hexdigest()

for candidate in (install, home / ".aws", home / ".claude"):
    current = candidate
    while current != home.parent:
        if current.is_symlink():
            fail(f"refusing symlinked install path: {current}")
        if current == home:
            break
        current = current.parent

manifest = None
if manifest_path.exists() or manifest_path.is_symlink():
    if manifest_path.is_symlink() or not manifest_path.is_file():
        fail(f"refusing non-regular ownership manifest: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        fail(f"cannot validate ownership manifest: {error}")
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or not isinstance(manifest.get("files"), dict)
        or not isinstance(manifest.get("aws_profiles"), dict)
        or not isinstance(manifest.get("aws_sso_sessions"), dict)
    ):
        fail("invalid ownership manifest; run gip cleanup before reinstalling")

files = manifest.get("files", {}) if manifest else {}
known_names = (
    "credential-process",
    "config.json",
    "websearch-headers",
    "otel-helper",
    "otelcol",
    "collector-config.yaml",
    "claude-bedrock",
)
known_keys = {f"gip/{name}" for name in known_names}
known_keys.add(".claude/settings.json")
known_keys.update({"gip/cowork-3p.mobileconfig", "gip/cowork-3p-config.json", "gip/gip-settings/mcp.json"})

def valid_harness_key(key):
    relative = Path(key)
    return (
        key.startswith("gip/harnesses/")
        and not relative.is_absolute()
        and ".." not in relative.parts
        and len(relative.parts) > 2
    )

unknown_keys = {key for key in files if key not in known_keys and not valid_harness_key(key)}
if unknown_keys:
    fail("ownership manifest contains state from another installer mode; run gip cleanup first")

planned_keys = set()
for name in ("cowork-3p.mobileconfig", "cowork-3p-config.json"):
    source = package / name
    if source.is_file() and not source.is_symlink() and "__GIP_HOME__" in source.read_text(encoding="utf-8"):
        planned_keys.add(f"gip/{name}")

mcp_source = package / "gip-settings" / "mcp.json"
if mcp_source.is_file() and not mcp_source.is_symlink():
    planned_keys.add("gip/gip-settings/mcp.json")

harness_root = package / "harnesses"
if harness_root.is_dir() and not harness_root.is_symlink():
    for source in harness_root.rglob("*"):
        if source.is_symlink():
            fail(f"refusing symlinked harness package path: {source}")
        if source.is_file():
            planned_keys.add(f"gip/harnesses/{source.relative_to(harness_root).as_posix()}")

target_keys = {f"gip/{name}" for name in known_names} | planned_keys | (set(files) - {".claude/settings.json"})
for key in sorted(target_keys):
    relative = Path(key)
    if relative.is_absolute() or ".." in relative.parts or not key.startswith("gip/"):
        fail(f"invalid managed install target in ownership state: {key}")
    path = home / relative
    if not (path.exists() or path.is_symlink()):
        continue
    expected = files.get(key)
    if not isinstance(expected, str) or digest(path) != expected.lower():
        classification = "owned-modified" if expected else "foreign"
        fail(f"refusing to overwrite {classification} target: {path}")

settings_owned = False
settings = home / ".claude" / "settings.json"
if settings.exists() or settings.is_symlink():
    expected = files.get(".claude/settings.json")
    if expected:
        if not isinstance(expected, str) or digest(settings) != expected.lower():
            fail(f"refusing to overwrite owned-modified target: {settings}")
        settings_owned = True
    elif settings.is_symlink() or not settings.is_file():
        fail(f"refusing non-regular settings target: {settings}")

managed_source = package / "gip-settings" / "managed-settings.json"
if managed_source.is_file():
    managed_dir = Path("/Library/Application Support/ClaudeCode") if sys.platform == "darwin" else Path("/etc/claude-code")
    managed_target = managed_dir / "managed-settings.json"
    managed_marker = managed_dir / ".managed-settings.gip-sha256"
    if managed_target.exists() or managed_target.is_symlink():
        if managed_target.is_symlink() or not managed_target.is_file():
            fail(f"refusing non-regular managed settings target: {managed_target}")
        try:
            expected = managed_marker.read_text(encoding="ascii").strip().lower()
        except OSError:
            fail(f"preserving existing foreign managed settings: {managed_target}")
        if len(expected) != 64 or digest(managed_target) != expected:
            fail(f"refusing to overwrite changed managed settings: {managed_target}")

try:
    profile_names = list(json.loads((package / "config.json").read_text(encoding="utf-8"))["profiles"])
except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
    fail(f"cannot read package profiles: {error}")
if (install / "collector-config.yaml").is_file() or (package / "collector-config.yaml").is_file():
    profile_names += [f"{name}-collector" for name in profile_names]

config_path = home / ".aws" / "config"
target_profiles = set(profile_names)
if manifest:
    if set(manifest["aws_profiles"]) - target_profiles or manifest["aws_sso_sessions"]:
        fail("ownership manifest contains profiles from another installer mode; run gip cleanup first")
if config_path.exists() or config_path.is_symlink():
    if config_path.is_symlink() or not config_path.is_file():
        fail(f"refusing non-regular AWS config target: {config_path}")
    lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)
    for name in profile_names:
        header = f"[profile {name}]"
        start = next((index for index, line in enumerate(lines) if line.strip() == header), None)
        if start is None:
            continue
        end = next(
            (index for index in range(start + 1, len(lines)) if lines[index].lstrip().startswith("[")),
            len(lines),
        )
        section = "".join(lines[start:end]).replace("\r\n", "\n").replace("\r", "\n")
        actual = hashlib.sha256(section.encode()).hexdigest()
        expected = manifest.get("aws_profiles", {}).get(name) if manifest else None
        if not isinstance(expected, str) or actual != expected.lower():
            classification = "owned-modified" if expected else "foreign"
            fail(f"refusing to overwrite {classification} AWS profile: {name}")

print("1" if settings_owned else "0")
PY
) || exit 1

gip_atomic_copy() {
    GIP_HOME="$ACTUAL_HOME" GIP_SOURCE="$1" GIP_TARGET="$2" GIP_KEY="$3" GIP_MODE="$4" "$PYTHON" <<'PY'
import hashlib
import json
import os
import stat
import uuid
from pathlib import Path

home = Path(os.environ["GIP_HOME"])
source = Path(os.environ["GIP_SOURCE"])
relative = Path(os.environ["GIP_TARGET"])
key = os.environ["GIP_KEY"]
mode = int(os.environ["GIP_MODE"], 8)
if relative.is_absolute() or ".." in relative.parts or len(relative.parts) < 2:
    raise SystemExit("ERROR: invalid managed install target")

def restore_no_clobber(directory_fd, quarantine, name):
    source_fd = None
    destination_fd = None
    destination_created = False
    try:
        source_fd = os.open(
            quarantine,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            dir_fd=directory_fd,
        )
        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode):
            return False
        destination_fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            stat.S_IMODE(source_stat.st_mode),
            dir_fd=directory_fd,
        )
        destination_created = True
        while block := os.read(source_fd, 65536):
            remaining = memoryview(block)
            while remaining:
                remaining = remaining[os.write(destination_fd, remaining) :]
        os.fsync(destination_fd)
        os.close(destination_fd)
        destination_fd = None
        os.close(source_fd)
        source_fd = None
        os.unlink(quarantine, dir_fd=directory_fd)
        return True
    except OSError:
        if destination_fd is not None:
            os.close(destination_fd)
        if source_fd is not None:
            os.close(source_fd)
        if destination_created:
            try:
                os.unlink(name, dir_fd=directory_fd)
            except OSError:
                pass
        return False

flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
directory_fd = os.open(home, flags)
try:
    for part in relative.parts[:-1]:
        try:
            next_fd = os.open(part, flags, dir_fd=directory_fd)
        except FileNotFoundError:
            os.mkdir(part, 0o700, dir_fd=directory_fd)
            next_fd = os.open(part, flags, dir_fd=directory_fd)
        os.close(directory_fd)
        directory_fd = next_fd

    name = relative.name
    temporary = f".{name}.gip-install-{uuid.uuid4().hex}"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode, dir_fd=directory_fd)
    try:
        with source.open("rb") as source_handle:
            while block := source_handle.read(65536):
                remaining = memoryview(block)
                while remaining:
                    remaining = remaining[os.write(descriptor, remaining) :]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        try:
            os.link(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
        finally:
            os.unlink(temporary, dir_fd=directory_fd)
    else:
        manifest_path = home / "gip" / ".gip-ownership.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            expected = manifest["files"][key]
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            os.unlink(temporary, dir_fd=directory_fd)
            raise SystemExit(f"ERROR: Refusing to overwrite foreign target: {home / relative}")
        quarantine = f".{name}.gip-quarantine-{uuid.uuid4().hex}"
        os.replace(name, quarantine, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        try:
            file_fd = os.open(quarantine, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
            try:
                opened = os.fstat(file_fd)
                if not stat.S_ISREG(opened.st_mode):
                    raise OSError("not a regular file")
                digest = hashlib.sha256()
                while block := os.read(file_fd, 65536):
                    digest.update(block)
            finally:
                os.close(file_fd)
            if digest.hexdigest() != expected.lower():
                raise OSError("digest changed")
            os.link(
                temporary,
                name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
            os.unlink(temporary, dir_fd=directory_fd)
            os.unlink(quarantine, dir_fd=directory_fd)
        except Exception as error:
            restored = restore_no_clobber(directory_fd, quarantine, name)
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            if restored:
                raise SystemExit(f"ERROR: Refusing to overwrite changed target; original restored: {home / relative}")
            raise SystemExit(
                f"ERROR: Refusing to overwrite changed target; original preserved as {quarantine}: {error}"
            )
finally:
    os.close(directory_fd)
PY
}
"""

        # Web search headersHelper: when web search is enabled (OIDC only), the
        # installer drops a small wrapper next to credential-process that emits
        # {"Authorization":"Bearer <id_token>"} for Claude Desktop's
        # managedMcpServers headersHelper. Bound to this deployment profile.
        # The same wrapper backs Claude Code CLI's native headersHelper, so the
        # block also registers the gateway MCP server with `claude mcp add-json
        # -s user` (guarded: claude on PATH, not under sudo — claude would write
        # root's ~/.claude.json) and resolves the helper-path placeholder in
        # gip-settings/mcp.json (the distributable fallback artifact).
        websearch_helper_block = ""
        _ws_enabled = getattr(profile, "web_search_enabled", False)
        _ws_auth = getattr(profile, "effective_auth_type", getattr(profile, "auth_type", "oidc"))
        if _ws_enabled and _ws_auth != "idc":
            from governed_inference_platform.cli.utils.cowork_3p import (
                WEBSEARCH_MCP_SERVER_NAME,
                resolve_websearch_gateway_url,
            )

            _ws_gateway_url = resolve_websearch_gateway_url(profile)
            websearch_claude_mcp_block = ""
            if _ws_gateway_url:
                websearch_claude_mcp_block = f"""
# Register the gateway MCP server with Claude Code CLI (user scope). Claude
# Code re-runs the headersHelper per connection and on 401/403, so the ~1h
# id_token expiry never bites. Skipped under sudo (claude would write root's
# ~/.claude.json) and when the claude CLI is not installed yet — the exact
# command is printed instead.
WS_MCP_JSON="{{\\"type\\":\\"http\\",\\"url\\":\\"{_ws_gateway_url}\\",\\"headersHelper\\":\\"$WS_HELPER\\"}}"
if [ -z "$SUDO_USER" ] && command -v claude >/dev/null 2>&1; then
    claude mcp remove {WEBSEARCH_MCP_SERVER_NAME} -s user >/dev/null 2>&1 || true
    if claude mcp add-json {WEBSEARCH_MCP_SERVER_NAME} -s user "$WS_MCP_JSON" >/dev/null 2>&1; then
        echo "OK Web search MCP server registered with Claude Code ({WEBSEARCH_MCP_SERVER_NAME})"
    else
        echo "WARN Could not register the web search MCP server automatically. Run:"
        echo "  claude mcp add-json {WEBSEARCH_MCP_SERVER_NAME} -s user '$WS_MCP_JSON'"
    fi
else
    echo "To enable web search in Claude Code, run:"
    echo "  claude mcp add-json {WEBSEARCH_MCP_SERVER_NAME} -s user '$WS_MCP_JSON'"
fi
"""
            websearch_helper_block = f"""
# Web search headersHelper (Cowork web search via AgentCore Gateway).
# Emits {{"Authorization":"Bearer <id_token>"}} so Claude Desktop authenticates
# managedMcpServers requests to the gateway. Bound to the '{profile.name}' profile.
echo
echo "Installing web search headersHelper..."
WS_HELPER="$ACTUAL_HOME/gip/websearch-headers"
WS_HELPER_SOURCE=$(mktemp "$SCRIPT_DIR/.websearch-headers.XXXXXX")
cat > "$WS_HELPER_SOURCE" <<WS_EOF
#!/bin/sh
exec "$ACTUAL_HOME/gip/credential-process" --profile {profile.name} --get-mcp-auth-header
WS_EOF
gip_atomic_copy "$WS_HELPER_SOURCE" "gip/websearch-headers" \
    "gip/websearch-headers" 755
rm -f "$WS_HELPER_SOURCE"
echo "OK Web search headersHelper installed: $WS_HELPER"
{websearch_claude_mcp_block}"""

        installer_content += f"""
# Create directory
echo
echo "Installing authentication tools..."
mkdir -p "$ACTUAL_HOME/gip"

# Copy appropriate binary
gip_atomic_copy "$CREDENTIAL_BINARY" "gip/credential-process" \
    "gip/credential-process" 755

# Copy config
gip_atomic_copy config.json "gip/config.json" \
    "gip/config.json" 600
chmod +x "$ACTUAL_HOME/gip/credential-process"

{websearch_helper_block}
# F-017: Install resolved CoWork MDM files without changing the source package.
for _gip_mdm in "cowork-3p.mobileconfig" "cowork-3p-config.json"; do
    if [ -f "$_gip_mdm" ] && [ ! -L "$_gip_mdm" ] && grep -q "__GIP_HOME__" "$_gip_mdm" 2>/dev/null; then
        _gip_mdm_source=$(mktemp "$SCRIPT_DIR/.gip-mdm.XXXXXX")
        sed "s|__GIP_HOME__|$ACTUAL_HOME|g" "$_gip_mdm" > "$_gip_mdm_source"
        gip_atomic_copy "$_gip_mdm_source" "gip/$_gip_mdm" "gip/$_gip_mdm" 600
        rm -f "$_gip_mdm_source"
        echo "OK Installed resolved $_gip_mdm at $ACTUAL_HOME/gip/$_gip_mdm"
    fi
done

# F-020: Install generated harness configs in a customer-usable location. Resolve only
# staged copies so the source package remains byte-for-byte unchanged.
if [ -d "harnesses" ]; then
    while IFS= read -r _gip_harness_cfg; do
        _gip_harness_rel=${{_gip_harness_cfg#harnesses/}}
        _gip_harness_source=$(mktemp "$SCRIPT_DIR/.gip-harness.XXXXXX")
        sed "s|__CREDENTIAL_PROCESS_PATH__|$ACTUAL_HOME/gip/credential-process|g" "$_gip_harness_cfg" > "$_gip_harness_source"
        gip_atomic_copy "$_gip_harness_source" "gip/harnesses/$_gip_harness_rel" "gip/harnesses/$_gip_harness_rel" 600
        rm -f "$_gip_harness_source"
        echo "OK Installed harness config: $ACTUAL_HOME/gip/harnesses/$_gip_harness_rel"
    done < <(find harnesses -type f -print)
fi

if [ -f "gip-settings/mcp.json" ] && [ ! -L "gip-settings/mcp.json" ]; then
    _gip_mcp_source=$(mktemp "$SCRIPT_DIR/.gip-mcp.XXXXXX")
    sed "s|__WEBSEARCH_HEADERS_HELPER__|$ACTUAL_HOME/gip/websearch-headers|g" "gip-settings/mcp.json" > "$_gip_mcp_source"
    gip_atomic_copy "$_gip_mcp_source" "gip/gip-settings/mcp.json" "gip/gip-settings/mcp.json" 600
    rm -f "$_gip_mcp_source"
    echo "OK Installed resolved MCP config: $ACTUAL_HOME/gip/gip-settings/mcp.json"
fi

# macOS Gatekeeper + Keychain notices
if [[ "$OSTYPE" == "darwin"* ]]; then
    # Remove quarantine flag added by macOS when downloading unsigned binaries.
    # Without this, Gatekeeper blocks execution with "Apple could not verify..." dialog.
    xattr -d com.apple.quarantine "$ACTUAL_HOME/gip/credential-process" 2>/dev/null || true
    # F-018: Session-only profiles do not access macOS Keychain.
    if $PYTHON -c "import json,sys; sys.exit(0 if any(p.get('credential_storage') == 'keyring' for p in json.load(open('config.json'))['profiles'].values()) else 1)"; then
        echo
        echo "macOS Keychain Access:"
        echo "   On first use, macOS will ask for permission to access the keychain."
        echo "   This is normal and required for secure credential storage."
        echo "   Click 'Always Allow' when prompted."
    fi
fi

# Copy Claude Code settings if present
GIP_SETTINGS_CREATED="$GIP_SETTINGS_WAS_OWNED"
if [ -d "gip-settings" ]; then
    echo
    echo "Installing Claude Code settings..."
    mkdir -p "$ACTUAL_HOME/.claude"

    # Install managed-settings.json (OS-level enforcement) if present
    if [ -f "gip-settings/managed-settings.json" ]; then
        echo "Managed settings detected (organization-wide enforcement)..."

        # Determine OS-appropriate managed-settings path
        if [[ "$OSTYPE" == "darwin"* ]]; then
            MANAGED_DIR="/Library/Application Support/ClaudeCode"
        else
            MANAGED_DIR="/etc/claude-code"
        fi

        MANAGED_TARGET="$MANAGED_DIR/managed-settings.json"
        MANAGED_MARKER="$MANAGED_DIR/.managed-settings.gip-sha256"
        if sudo test -e "$MANAGED_TARGET"; then
            MANAGED_EXPECTED=$(sudo cat "$MANAGED_MARKER" 2>/dev/null || true)
            MANAGED_ACTUAL=$(sudo cat "$MANAGED_TARGET" | $GIP_SHA256 | cut -d ' ' -f 1)
            if [ -z "$MANAGED_EXPECTED" ] || [ "$MANAGED_ACTUAL" != "$MANAGED_EXPECTED" ]; then
                echo "ERROR: Preserving existing foreign or changed managed settings: $MANAGED_TARGET"
                exit 1
            fi
        fi
        MANAGED_SOURCE=$(mktemp "$SCRIPT_DIR/.managed-settings.json.XXXXXX")
        sed -e "s|__OTEL_HELPER_PATH__|$ACTUAL_HOME/gip/otel-helper|g" \
            -e "s|__CREDENTIAL_PROCESS_PATH__|$ACTUAL_HOME/gip/credential-process|g" \
            "gip-settings/managed-settings.json" > "$MANAGED_SOURCE"
        echo "  Managed settings require root; elevating only this system-scoped write."
        sudo mkdir -p "$MANAGED_DIR"
        sudo install -m 644 "$MANAGED_SOURCE" "$MANAGED_TARGET"
        MANAGED_DIGEST=$(gip_sha256 "$MANAGED_SOURCE")
        printf '%s\n' "$MANAGED_DIGEST" | sudo tee "$MANAGED_MARKER" >/dev/null
        rm -f "$MANAGED_SOURCE"

        # Verify placeholders were replaced
        if grep -q '__CREDENTIAL_PROCESS_PATH__\\|__OTEL_HELPER_PATH__' "$MANAGED_TARGET" 2>/dev/null; then
            echo "WARNING: Some path placeholders were not replaced in managed-settings.json"
        else
            echo "OK Managed settings installed: $MANAGED_DIR/managed-settings.json"
            echo "   These settings have highest precedence and cannot be overridden by users."
        fi
    fi

    # Copy user-scope settings.json if present
    if [ -f "gip-settings/settings.json" ]; then
        SETTINGS_SOURCE=$(mktemp "$SCRIPT_DIR/.settings.json.XXXXXX")
        sed -e "s|__OTEL_HELPER_PATH__|$ACTUAL_HOME/gip/otel-helper|g" \
            -e "s|__CREDENTIAL_PROCESS_PATH__|$ACTUAL_HOME/gip/credential-process|g" \
            "gip-settings/settings.json" > "$SETTINGS_SOURCE"
        if [ ! -e "$ACTUAL_HOME/.claude/settings.json" ] || [ "$GIP_SETTINGS_WAS_OWNED" = "1" ]; then
            gip_atomic_copy "$SETTINGS_SOURCE" ".claude/settings.json" ".claude/settings.json" 600
            GIP_SETTINGS_CREATED=1
            echo "OK Claude Code settings configured: $ACTUAL_HOME/.claude/settings.json"
        else
            echo "WARNING: Preserving existing foreign Claude Code settings."
            echo "         Review gip-settings/settings.json and merge its Bedrock keys manually."
        fi
        rm -f "$SETTINGS_SOURCE"
    fi
fi

# Copy OTEL helper executable if present
if [ -f "$OTEL_BINARY" ]; then
    echo
    echo "Installing OTEL helper..."
    gip_atomic_copy "$OTEL_BINARY" "gip/otel-helper" \
        "gip/otel-helper" 755
    xattr -d com.apple.quarantine "$ACTUAL_HOME/gip/otel-helper" 2>/dev/null || true
    echo "✓ OTEL helper installed"
fi

# Add debug info if OTEL helper was installed
if [ -f "$ACTUAL_HOME/gip/otel-helper" ]; then
    echo "The OTEL helper will extract user attributes from authentication tokens"
    echo "and include them in metrics. To test the helper, run:"
    echo "  $ACTUAL_HOME/gip/otel-helper --test"
fi

# Install otelcol sidecar collector (present only in sidecar-mode packages).
# The collector binary is built via OCB and SHIPPED in the package as
# otelcol-$BINARY_SUFFIX, the same model as credential-process and otel-helper —
# end users never download it. It receives OTLP from Claude Code on localhost:4318,
# injects the user-attribution headers written by otel-helper, and forwards to
# CloudWatch with SigV4.
OTELCOL_BINARY="otelcol-$BINARY_SUFFIX"
if [ -f "collector-config.yaml" ] && [ -f "$OTELCOL_BINARY" ]; then
    echo
    echo "Installing OTEL Collector sidecar..."

    OTELCOL_DEST="$ACTUAL_HOME/gip/otelcol"
    gip_atomic_copy "$OTELCOL_BINARY" "gip/otelcol" \
        "gip/otelcol" 755
    xattr -d com.apple.quarantine "$OTELCOL_DEST" 2>/dev/null || true
    echo "✓ otelcol installed: $OTELCOL_DEST"

    # Install collector config alongside the binary
    gip_atomic_copy "collector-config.yaml" "gip/collector-config.yaml" \
        "gip/collector-config.yaml" 600
    echo "✓ Collector config installed"

    # A dedicated <profile>-collector AWS profile is registered in the AWS profiles
    # section below. otelcol resolves CloudWatch credentials through it via
    # credential_process. The separate profile is needed because a user's static
    # ~/.aws/credentials would otherwise shadow credential_process and cannot
    # auto-refresh (see otel-helper.sh).
elif [ -f "collector-config.yaml" ] && [ ! -f "$OTELCOL_BINARY" ]; then
    echo
    echo "⚠️  Sidecar config present but collector binary '$OTELCOL_BINARY' is missing."
    echo "   The admin must run 'gip package' with Go 1.23+ installed to build the collector."
    echo "   Telemetry will not be forwarded until the collector is installed."
fi

# Update AWS config
echo
echo "Configuring AWS profiles..."
mkdir -p "$ACTUAL_HOME/.aws"

# Read all profiles from config.json
PROFILES=$($PYTHON -c "import json; profiles = list(json.load(open('config.json'))['profiles']); print(' '.join(profiles))")

if [ -z "$PROFILES" ]; then
    echo "❌ No profiles found in config.json"
    exit 1
fi

echo "Found profiles: $PROFILES"
echo

# Get region from package settings (for Bedrock calls, not infrastructure)
if [ -f "gip-settings/settings.json" ]; then
    DEFAULT_REGION=$($PYTHON -c "
import json
print(json.load(open('gip-settings/settings.json'))['env']['AWS_REGION'])
" 2>/dev/null || echo "{profile.aws_region}")
elif [ -f "gip-settings/managed-settings.json" ]; then
    DEFAULT_REGION=$($PYTHON -c "
import json
print(json.load(open('gip-settings/managed-settings.json'))['env']['AWS_REGION'])
" 2>/dev/null || echo "{profile.aws_region}")
else
    DEFAULT_REGION="{profile.aws_region}"
fi

# Rewrite all owned profile sections in one compare-before-replace transaction.
GIP_HOME="$ACTUAL_HOME" GIP_DEFAULT_REGION="$DEFAULT_REGION" "$PYTHON" <<'PY'
import hashlib
import json
import os
import stat
import tempfile
import uuid
from pathlib import Path

home = Path(os.environ["GIP_HOME"])
config_path = home / ".aws" / "config"
package_config = json.loads(Path("config.json").read_text(encoding="utf-8"))["profiles"]
profile_names = list(package_config)
collector_enabled = (home / "gip" / "collector-config.yaml").is_file()
target_names = profile_names + ([f"{{name}}-collector" for name in profile_names] if collector_enabled else [])

manifest_path = home / "gip" / ".gip-ownership.json"
manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else None

def restore_no_clobber(quarantine, destination):
    source_fd = None
    destination_fd = None
    destination_created = False
    try:
        source_fd = os.open(
            quarantine,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode):
            return False
        destination_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            stat.S_IMODE(source_stat.st_mode),
        )
        destination_created = True
        while block := os.read(source_fd, 65536):
            remaining = memoryview(block)
            while remaining:
                remaining = remaining[os.write(destination_fd, remaining) :]
        os.fsync(destination_fd)
        os.close(destination_fd)
        destination_fd = None
        os.close(source_fd)
        source_fd = None
        quarantine.unlink()
        return True
    except OSError:
        if destination_fd is not None:
            os.close(destination_fd)
        if source_fd is not None:
            os.close(source_fd)
        if destination_created:
            destination.unlink(missing_ok=True)
        return False

if config_path.exists():
    before = config_path.lstat()
    if not stat.S_ISREG(before.st_mode) or config_path.is_symlink():
        raise SystemExit("ERROR: Refusing non-regular AWS config target")
    original = config_path.read_bytes()
    after = config_path.lstat()
    snapshot = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, hashlib.sha256(original).hexdigest())
    if snapshot[:4] != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise SystemExit("ERROR: AWS config changed while reading")
    mode = stat.S_IMODE(before.st_mode)
else:
    original = b""
    snapshot = None
    mode = 0o600

lines = original.decode("utf-8").splitlines(keepends=True)
ranges = []
for name in target_names:
    header = f"[profile {{name}}]"
    start = next((index for index, line in enumerate(lines) if line.strip() == header), None)
    if start is None:
        continue
    end = next((index for index in range(start + 1, len(lines)) if lines[index].lstrip().startswith("[")), len(lines))
    section = "".join(lines[start:end])
    expected = manifest.get("aws_profiles", {{}}).get(name) if manifest else None
    canonical_section = section.replace("\\r\\n", "\\n").replace("\\r", "\\n")
    if not isinstance(expected, str) or hashlib.sha256(canonical_section.encode()).hexdigest() != expected.lower():
        raise SystemExit(f"ERROR: Refusing to overwrite changed AWS profile: {{name}}")
    ranges.append((start, end))
for start, end in sorted(ranges, reverse=True):
    del lines[start:end]

content = "".join(lines)
if content and not content.endswith("\\n"):
    content += "\\n"
for name in profile_names:
    region = package_config.get(name, {{}}).get("aws_region", os.environ["GIP_DEFAULT_REGION"])
    content += (
        f"[profile {{name}}]\\n"
        f"credential_process = {{home}}/gip/credential-process --profile {{name}}\\n"
        f"region = {{region}}\\n"
    )
    if collector_enabled:
        content += (
            f"[profile {{name}}-collector]\\n"
            f"credential_process = {{home}}/gip/credential-process --profile {{name}}\\n"
            f"region = {{region}}\\n"
        )

with tempfile.NamedTemporaryFile(dir=config_path.parent, prefix=".config.gip-", delete=False) as handle:
    temporary = Path(handle.name)
    handle.write(content.encode())
    handle.flush()
    os.fsync(handle.fileno())
os.chmod(temporary, mode)
try:
    if snapshot is None:
        os.link(temporary, config_path)
        temporary.unlink()
    else:
        quarantine = config_path.with_name(f".config.gip-quarantine-{{uuid.uuid4().hex}}")
        os.replace(config_path, quarantine)
        current = quarantine.read_bytes()
        current_stat = quarantine.lstat()
        current_snapshot = (
            current_stat.st_dev,
            current_stat.st_ino,
            current_stat.st_size,
            current_stat.st_mtime_ns,
            hashlib.sha256(current).hexdigest(),
        )
        if current_snapshot != snapshot:
            if not restore_no_clobber(quarantine, config_path):
                raise OSError(f"AWS config changed; original preserved at {{quarantine}}")
            raise OSError("AWS config changed during installation")
        try:
            os.link(temporary, config_path)
        except OSError as link_error:
            if restore_no_clobber(quarantine, config_path):
                raise OSError("Could not install AWS config; original restored") from link_error
            raise OSError(f"Could not install AWS config; original preserved at {{quarantine}}") from link_error
        temporary.unlink()
        quarantine.unlink()
except Exception as error:
    temporary.unlink(missing_ok=True)
    raise SystemExit(f"ERROR: {{error}}")
PY
for PROFILE_NAME in $PROFILES; do echo "  ✓ Created AWS profile '$PROFILE_NAME'"; done

# Post-install validation
echo
echo "Validating installation..."
if [ -f "$ACTUAL_HOME/gip/credential-process" ]; then
    echo "  OK credential-process: $ACTUAL_HOME/gip/credential-process"
else
    echo "  FAIL credential-process not found at: $ACTUAL_HOME/gip/credential-process"
fi
if [ -f "$ACTUAL_HOME/.claude/settings.json" ]; then
    echo "  OK settings.json: $ACTUAL_HOME/.claude/settings.json"
else
    echo "  WARN settings.json not found at: $ACTUAL_HOME/.claude/settings.json"
fi
"""

        # IDC auth keeps a launcher wrapper that signs in before launching Claude.
        # OIDC auth handles sign-in transparently, so no launcher is needed.
        #
        # As of the awsAuthRefresh --login gate fix (credential-process now polls
        # device-auth and surfaces the verification URL/code live when invoked via
        # awsAuthRefresh), in-session recovery DOES work: an expired SSO session
        # can be renewed from inside a running `claude` via the credential hook.
        # The launcher is nonetheless kept for now because:
        #   1. Claude Code does not auto-invoke awsAuthRefresh on a 401 yet
        #      (anthropics/claude-code#67529, open) \u2014 recovery is manual via
        #      /login -> "Claude Platform on AWS - refresh credentials" (v2.1.186+).
        #      The launcher's pre-flight signs in BEFORE the first prompt, so the
        #      user never hits the manual-recovery step.
        #   2. Session-start behavior when the SSO session is fully dead (silent
        #      awsCredentialExport fails, then whether Claude Code falls through to
        #      awsAuthRefresh) is not yet verified across OSes.
        # Revisit removing the launcher once #67529 lands and start-up fallback is
        # confirmed; track via LOCAL_TESTING.md's "why a launcher" note.
        _is_idc = getattr(profile, "effective_auth_type", getattr(profile, "auth_type", None)) == "idc"
        if _is_idc:
            installer_content += """
# Generate a 'claude-bedrock' launcher wrapper.
# It signs in (a no-op when the session is still valid) so the verification URL
# is shown live in the user's terminal, THEN launches Claude Code. In-session
# recovery via the awsAuthRefresh hook now works too, but Claude Code does not
# auto-trigger it on a 401 yet (anthropics/claude-code#67529) \u2014 front-running the
# sign-in here means the user is authenticated before the first prompt.
CRED_PROC="$ACTUAL_HOME/gip/credential-process"
LAUNCHER="$ACTUAL_HOME/gip/claude-bedrock"
FIRST_PROFILE=$(echo $PROFILES | awk '{print $1}')
LAUNCHER_SOURCE=$(mktemp "$SCRIPT_DIR/.claude-bedrock.XXXXXX")
cat > "$LAUNCHER_SOURCE" << EOF
#!/bin/bash
# Launch Claude Code using Bedrock authentication.
# Signs in first (no-op if already signed in), then runs claude.
PROFILE="\\${AWS_PROFILE:-$FIRST_PROFILE}"
"$CRED_PROC" --login --profile "\\$PROFILE" || exit 1
export AWS_PROFILE="\\$PROFILE"
exec claude "\\$@"
EOF
gip_atomic_copy "$LAUNCHER_SOURCE" "gip/claude-bedrock" \
    "gip/claude-bedrock" 755
rm -f "$LAUNCHER_SOURCE"
echo "  \u2713 Created launcher: $LAUNCHER"

echo
echo "======================================"
echo "Installation complete!"
echo "======================================"
echo
echo "Available profiles:"
for PROFILE_NAME in $PROFILES; do
    echo "  - $PROFILE_NAME"
done
echo
echo ">>> Start Claude Code the usual way:"
echo "      claude"
echo
echo "    It signs you in automatically when needed \u2014 opening your browser, or"
echo "    showing a sign-in link on headless/SSH hosts \u2014 and refreshes your session"
echo "    on its own after that."
echo
echo "    Optional: a 'claude-bedrock' launcher is also installed. It signs you in"
echo "    first, then starts Claude Code, which can make the very first sign-in"
echo "    smoother (no time limit on the sign-in step). Use it if you prefer:"
echo "      $LAUNCHER"
echo
echo "Tip: add it to your PATH so you can just run 'claude-bedrock':"
echo "  export PATH=\\"$ACTUAL_HOME/gip:\\$PATH\\""
echo
echo "To use a non-default profile, set AWS_PROFILE before launching:"
echo "  AWS_PROFILE=<profile-name> $LAUNCHER"
echo
"""
        else:
            installer_content += """
echo
echo "======================================"
echo "Installation complete!"
echo "======================================"
echo
echo "Available profiles:"
for PROFILE_NAME in $PROFILES; do
    echo "  - $PROFILE_NAME"
done
echo
echo ">>> Start Claude Code:"
echo "      claude"
echo
echo "    Authentication is handled automatically via your configured credential"
echo "    process. Simply run 'claude' to start."
echo
echo "To use a non-default profile, set AWS_PROFILE before launching:"
echo "  AWS_PROFILE=<profile-name> claude"
echo
"""

        installer_content += r"""
# Record exact ownership only after installation succeeds. Foreign shared
# settings remain deliberately unowned and untouched.
echo "Recording gip ownership..."
GIP_HOME="$ACTUAL_HOME" GIP_PACKAGE="$SCRIPT_DIR" GIP_PROFILES="$PROFILES" GIP_SETTINGS_CREATED="$GIP_SETTINGS_CREATED" "$PYTHON" <<'PY'
import hashlib
import json
import os
import stat
import tempfile
import uuid
from pathlib import Path

home = Path(os.environ["GIP_HOME"])
package = Path(os.environ["GIP_PACKAGE"])
install = home / "gip"

def digest(path):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or path.is_symlink():
        raise SystemExit(f"ERROR: Refusing non-regular managed file during validation: {path}")
    value = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise SystemExit(f"ERROR: Managed file changed while opening: {path}")
        while block := os.read(descriptor, 65536):
            value.update(block)
    finally:
        os.close(descriptor)
    after = path.lstat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
    ):
        raise SystemExit(f"ERROR: Managed file changed during validation: {path}")
    return value.hexdigest()

def restore_no_clobber(quarantine, destination):
    source_fd = None
    destination_fd = None
    destination_created = False
    try:
        source_fd = os.open(
            quarantine,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode):
            return False
        destination_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            stat.S_IMODE(source_stat.st_mode),
        )
        destination_created = True
        while block := os.read(source_fd, 65536):
            remaining = memoryview(block)
            while remaining:
                remaining = remaining[os.write(destination_fd, remaining) :]
        os.fsync(destination_fd)
        os.close(destination_fd)
        destination_fd = None
        os.close(source_fd)
        source_fd = None
        quarantine.unlink()
        return True
    except OSError:
        if destination_fd is not None:
            os.close(destination_fd)
        if source_fd is not None:
            os.close(source_fd)
        if destination_created:
            destination.unlink(missing_ok=True)
        return False

managed_keys = {f"gip/{name}" for name in (
    "credential-process",
    "config.json",
    "websearch-headers",
    "otel-helper",
    "otelcol",
    "collector-config.yaml",
    "claude-bedrock",
)}

for name in ("cowork-3p.mobileconfig", "cowork-3p-config.json"):
    source = package / name
    if source.is_file() and not source.is_symlink() and "__GIP_HOME__" in source.read_text(encoding="utf-8"):
        managed_keys.add(f"gip/{name}")

mcp_source = package / "gip-settings" / "mcp.json"
if mcp_source.is_file() and not mcp_source.is_symlink():
    managed_keys.add("gip/gip-settings/mcp.json")

harness_root = package / "harnesses"
if harness_root.is_dir() and not harness_root.is_symlink():
    for source in harness_root.rglob("*"):
        if source.is_symlink():
            raise SystemExit(f"ERROR: Refusing symlinked harness package path: {source}")
        if source.is_file():
            managed_keys.add(f"gip/harnesses/{source.relative_to(harness_root).as_posix()}")

target = install / ".gip-ownership.json"
if target.is_file() and not target.is_symlink():
    previous = json.loads(target.read_text(encoding="utf-8"))
    managed_keys.update(key for key in previous.get("files", {}) if key != ".claude/settings.json")

files = {}
for key in sorted(managed_keys):
    relative = Path(key)
    if relative.is_absolute() or ".." in relative.parts or not key.startswith("gip/"):
        raise SystemExit(f"ERROR: Invalid managed file key during validation: {key}")
    path = home / relative
    if path.exists() or path.is_symlink():
        files[key] = digest(path)

settings = home / ".claude" / "settings.json"
if os.environ.get("GIP_SETTINGS_CREATED") == "1" and settings.is_file() and not settings.is_symlink():
    files[".claude/settings.json"] = digest(settings)

profiles = {}
config_path = home / ".aws" / "config"
if config_path.is_file() and not config_path.is_symlink():
    lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)
    names = os.environ.get("GIP_PROFILES", "").split()
    if (install / "collector-config.yaml").is_file():
        names += [f"{name}-collector" for name in names]
    for name in names:
        header = f"[profile {name}]"
        start = next((index for index, line in enumerate(lines) if line.strip() == header), None)
        if start is None:
            continue
        end = next(
            (index for index in range(start + 1, len(lines)) if lines[index].lstrip().startswith("[")),
            len(lines),
        )
        section = "".join(lines[start:end]).replace("\r\n", "\n").replace("\r", "\n")
        profiles[name] = hashlib.sha256(section.encode()).hexdigest()

manifest = {
    "schema_version": 1,
    "files": files,
    "aws_profiles": profiles,
    "aws_sso_sessions": {},
}
if target.exists() or target.is_symlink():
    before = target.lstat()
    if target.is_symlink() or not target.is_file():
        raise SystemExit("ERROR: Refusing non-regular ownership manifest")
    before_content = target.read_bytes()
    manifest_snapshot = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        hashlib.sha256(before_content).hexdigest(),
    )
else:
    manifest_snapshot = None
with tempfile.NamedTemporaryFile(
    mode="w", encoding="utf-8", dir=install, prefix=".gip-ownership-", delete=False
) as handle:
    temporary = Path(handle.name)
    json.dump(manifest, handle, indent=2, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
temporary.chmod(0o600)
if manifest_snapshot is None:
    try:
        os.link(temporary, target)
    except FileExistsError:
        temporary.unlink(missing_ok=True)
        raise SystemExit("ERROR: Ownership manifest appeared during installation")
    temporary.unlink()
else:
    quarantine = target.with_name(f".gip-ownership-quarantine-{uuid.uuid4().hex}")
    os.replace(target, quarantine)
    current = quarantine.read_bytes()
    current_stat = quarantine.lstat()
    current_snapshot = (
        current_stat.st_dev,
        current_stat.st_ino,
        current_stat.st_size,
        current_stat.st_mtime_ns,
        hashlib.sha256(current).hexdigest(),
    )
    if current_snapshot != manifest_snapshot:
        restored = restore_no_clobber(quarantine, target)
        temporary.unlink(missing_ok=True)
        if not restored:
            raise SystemExit(f"ERROR: Ownership manifest changed; original preserved at {quarantine}")
        raise SystemExit("ERROR: Ownership manifest changed during installation")
    try:
        os.link(temporary, target)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        if restore_no_clobber(quarantine, target):
            raise SystemExit("ERROR: Could not replace ownership manifest; original restored") from error
        raise SystemExit(f"ERROR: Could not replace ownership manifest; original preserved at {quarantine}") from error
    temporary.unlink()
    quarantine.unlink()
PY
echo "  OK Ownership manifest recorded"
"""

        installer_path = output_dir / "install.sh"
        with open(installer_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(installer_content)
        installer_path.chmod(0o755)

        # Create Windows installer only if Windows builds are enabled (CodeBuild)
        if "windows" in platforms_built or (hasattr(profile, "enable_codebuild") and profile.enable_codebuild):
            self._create_windows_installer(output_dir, profile)

        return installer_path

    def _create_windows_installer(self, output_dir: Path, profile) -> Path:
        """Create Windows batch installer script."""

        preflight_content = r"""param(
    [switch]$InstallManaged,
    [switch]$InstallAll,
    [switch]$InstallFile,
    [switch]$ConfigureAws,
    [switch]$RecordManifest,
    [switch]$TransactionLibraryOnly,
    [switch]$InstallLauncher,
    [switch]$RequireOtelHelpers,
    [string]$UserHome = "",
    [string]$Source = "",
    [string]$RelativeTarget = "",
    [string]$ManifestKey = "",
    [string]$ExpectedDigest = "",
    [string]$SettingsCreated = "0",
    [string]$WebSearchProfile = ""
)
$ErrorActionPreference = "Stop"
$homePath = if ($UserHome) { [IO.Path]::GetFullPath($UserHome) } else { $env:USERPROFILE }
$package = $PSScriptRoot
$install = Join-Path $homePath "gip"
$manifestPath = Join-Path $install ".gip-ownership.json"

function Fail([string]$Message) {
    [Console]::Error.WriteLine("ERROR: $Message")
    exit 1
}

function Test-GipReparsePoint([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    return [bool]((Get-Item -LiteralPath $Path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)
}

function Get-GipDigest([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Invoke-GipFileTransaction([object[]]$Entries) {
    $prepared = @()
    $committed = @()
    try {
        foreach ($entry in $Entries) {
            $target = [IO.Path]::GetFullPath([string]$entry.Target)
            $staged = [IO.Path]::GetFullPath([string]$entry.Staged)
            $directory = [IO.Path]::GetDirectoryName($target)
            if ([IO.Path]::GetDirectoryName($staged) -ne $directory) { throw "Staged file is outside target directory: $target" }
            if (Test-GipReparsePoint $directory) { throw "Refusing reparse-point target directory: $directory" }
            if (-not (Test-Path -LiteralPath $staged -PathType Leaf) -or (Test-GipReparsePoint $staged)) {
                throw "Refusing non-regular staged file: $staged"
            }
            $stagedDigest = Get-GipDigest $staged
            $quarantine = $null
            if (Test-Path -LiteralPath $target) {
                if ($null -eq $entry.ExpectedDigest) { throw "Target appeared or is foreign: $target" }
                if (-not (Test-Path -LiteralPath $target -PathType Leaf) -or (Test-GipReparsePoint $target)) {
                    throw "Refusing non-regular target: $target"
                }
                if ((Get-GipDigest $target) -ne $entry.ExpectedDigest) { throw "Target changed before replacement: $target" }
                $quarantine = Join-Path $directory ("." + [IO.Path]::GetFileName($target) + ".gip-quarantine-" + [Guid]::NewGuid().ToString("N"))
                [IO.File]::Move($target, $quarantine)
                $prepared += [PSCustomObject]@{ Target = $target; Staged = $staged; StagedDigest = $stagedDigest; Quarantine = $quarantine }
                if ((Get-GipDigest $quarantine) -ne $entry.ExpectedDigest) { throw "Target changed during replacement: $target" }
            } elseif ($null -ne $entry.ExpectedDigest) {
                throw "Owned target disappeared before replacement: $target"
            } else {
                $prepared += [PSCustomObject]@{ Target = $target; Staged = $staged; StagedDigest = $stagedDigest; Quarantine = $null }
            }
        }
        foreach ($entry in $prepared) {
            [IO.File]::Move($entry.Staged, $entry.Target)
            $committed += $entry
        }
        $retained = @()
        foreach ($entry in $prepared) {
            if ($entry.Quarantine) {
                try { Remove-Item -LiteralPath $entry.Quarantine -Force -ErrorAction Stop }
                catch { $retained += $entry.Quarantine }
            }
        }
        if ($retained.Count -gt 0) { Write-Warning ("Commit succeeded, but previous files remain at: " + ($retained -join ", ")) }
    } catch {
        $preserved = @()
        for ($index = $committed.Count - 1; $index -ge 0; $index--) {
            $entry = $committed[$index]
            if (Test-Path -LiteralPath $entry.Target -PathType Leaf) {
                $rollback = Join-Path ([IO.Path]::GetDirectoryName($entry.Target)) ("." + [IO.Path]::GetFileName($entry.Target) + ".gip-rollback-" + [Guid]::NewGuid().ToString("N"))
                try {
                    [IO.File]::Move($entry.Target, $rollback)
                    if ((Get-GipDigest $rollback) -eq $entry.StagedDigest) { Remove-Item -LiteralPath $rollback -Force }
                    elseif (-not (Test-Path -LiteralPath $entry.Target)) { [IO.File]::Move($rollback, $entry.Target) }
                    else { $preserved += $rollback }
                } catch {
                    if (Test-Path -LiteralPath $rollback) { $preserved += $rollback }
                    elseif (Test-Path -LiteralPath $entry.Target) { $preserved += $entry.Target }
                }
            }
        }
        foreach ($entry in $prepared) {
            if ($entry.Quarantine -and (Test-Path -LiteralPath $entry.Quarantine)) {
                if (-not (Test-Path -LiteralPath $entry.Target)) {
                    try { [IO.File]::Move($entry.Quarantine, $entry.Target) } catch { $preserved += $entry.Quarantine }
                } else { $preserved += $entry.Quarantine }
            }
        }
        if ($preserved.Count -gt 0) { throw ("Transaction failed; originals preserved at: " + ($preserved -join ", ")) }
        throw
    }
}

function Get-GipManifestDigest([string]$Key) {
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { return $null }
    if (Test-GipReparsePoint $manifestPath) { throw "Refusing reparse-point ownership manifest: $manifestPath" }
    $value = (Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json).files.PSObject.Properties[$Key]
    if ($null -eq $value -or $value.Value -notmatch "^[0-9a-fA-F]{64}$") { return $null }
    return $value.Value.ToLowerInvariant()
}

if ($TransactionLibraryOnly) { return }

if ($InstallAll) {
    $oldManifest = $null
    if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
        if (Test-GipReparsePoint $manifestPath) { Fail "Refusing reparse-point ownership manifest" }
        $oldManifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
        if ($oldManifest.schema_version -ne 1 -or $null -eq $oldManifest.files -or $null -eq $oldManifest.aws_profiles -or $null -eq $oldManifest.aws_sso_sessions) {
            Fail "Invalid ownership manifest; run gip cleanup before reinstalling"
        }
    }
    if ($RequireOtelHelpers) {
        foreach ($required in @("otel-helper.cmd", "otel-helper.ps1")) {
            if (-not (Test-Path -LiteralPath (Join-Path $package $required) -PathType Leaf)) { Fail "Required package file is missing: $required" }
        }
    }
    $entries = @()
    $stagedPaths = @()
    $fileEntries = @{}
    $createdDirectories = @()

    function Ensure-GipDirectory([string]$Directory) {
        if (-not (Test-Path -LiteralPath $Directory)) {
            New-Item -ItemType Directory -Path $Directory -Force | Out-Null
            $script:createdDirectories += $Directory
        }
        if (Test-GipReparsePoint $Directory) { throw "Refusing reparse-point target directory: $Directory" }
    }

    function Add-GipInstallStage([string]$SourcePath, [string]$Relative, [string]$Key, $Content = $null) {
        $target = [IO.Path]::GetFullPath((Join-Path $homePath $Relative))
        $homePrefix = [IO.Path]::GetFullPath($homePath).TrimEnd('\') + '\'
        if (-not $target.StartsWith($homePrefix, [StringComparison]::OrdinalIgnoreCase)) { throw "Install target escapes the user home: $target" }
        $directory = [IO.Path]::GetDirectoryName($target)
        Ensure-GipDirectory $directory
        $staged = Join-Path $directory ("." + [IO.Path]::GetFileName($target) + ".gip-install-" + [Guid]::NewGuid().ToString("N"))
        if ($null -ne $Content) { [IO.File]::WriteAllText($staged, $Content, (New-Object Text.UTF8Encoding($false))) }
        else {
            if (-not (Test-Path -LiteralPath $SourcePath -PathType Leaf) -or (Test-GipReparsePoint $SourcePath)) { throw "Install source is missing or unsafe: $SourcePath" }
            [IO.File]::Copy([IO.Path]::GetFullPath($SourcePath), $staged, $false)
        }
        $script:stagedPaths += $staged
        $expected = Get-GipManifestDigest $Key
        $script:entries += [PSCustomObject]@{ Target = $target; Staged = $staged; ExpectedDigest = $expected }
        $script:fileEntries[$Key] = $staged
    }

    try {
        Add-GipInstallStage (Join-Path $package "credential-process-windows.exe") "gip\credential-process.exe" "gip/credential-process.exe"
        Add-GipInstallStage (Join-Path $package "config.json") "gip\config.json" "gip/config.json"
        if ($WebSearchProfile) {
            $webSearch = "@echo off`r`n`"%USERPROFILE%\gip\credential-process.exe`" --profile $WebSearchProfile --get-mcp-auth-header`r`n"
            Add-GipInstallStage "" "gip\websearch-headers.cmd" "gip/websearch-headers.cmd" $webSearch
        }
        foreach ($mapping in @(
            @("otel-helper-windows.exe", "otel-helper.exe"),
            @("otel-helper.cmd", "otel-helper.cmd"),
            @("otel-helper.ps1", "otel-helper.ps1")
        )) {
            $sourcePath = Join-Path $package $mapping[0]
            if (Test-Path -LiteralPath $sourcePath -PathType Leaf) {
                Add-GipInstallStage $sourcePath ("gip\" + $mapping[1]) ("gip/" + $mapping[1])
            }
        }
        $collectorSource = Join-Path $package "collector-config.yaml"
        $collectorBinary = Join-Path $package "otelcol-windows.exe"
        $installCollector = (Test-Path -LiteralPath $collectorSource -PathType Leaf) -and (Test-Path -LiteralPath $collectorBinary -PathType Leaf)
        $retainedCollector = $false
        if (-not $installCollector -and $null -ne $oldManifest) {
            $retainedCollectorPath = Join-Path $install "collector-config.yaml"
            $retainedCollectorDigest = Get-GipManifestDigest "gip/collector-config.yaml"
            $retainedCollector = $retainedCollectorDigest -and (Test-Path -LiteralPath $retainedCollectorPath -PathType Leaf) -and -not (Test-GipReparsePoint $retainedCollectorPath) -and (Get-GipDigest $retainedCollectorPath) -eq $retainedCollectorDigest
        }
        $collectorActive = $installCollector -or $retainedCollector
        if ($installCollector) {
            Add-GipInstallStage $collectorBinary "gip\otelcol.exe" "gip/otelcol.exe"
            Add-GipInstallStage $collectorSource "gip\collector-config.yaml" "gip/collector-config.yaml"
        }
        $settingsSource = Join-Path $package "gip-settings\settings.json"
        $settingsTarget = Join-Path $homePath ".claude\settings.json"
        $settingsExpected = Get-GipManifestDigest ".claude/settings.json"
        if ((Test-Path -LiteralPath $settingsSource -PathType Leaf) -and ((-not (Test-Path -LiteralPath $settingsTarget)) -or $settingsExpected)) {
            $otelPath = ($homePath + "\gip\otel-helper.cmd").Replace("\", "\\")
            $credPath = ($homePath + "\gip\credential-process.exe").Replace("\", "/")
            $settingsContent = [IO.File]::ReadAllText($settingsSource).Replace("__OTEL_HELPER_PATH__", $otelPath).Replace("__CREDENTIAL_PROCESS_PATH__", $credPath)
            Add-GipInstallStage "" ".claude\settings.json" ".claude/settings.json" $settingsContent
        }
        if ($InstallLauncher) {
            $firstProfile = @((Get-Content -LiteralPath (Join-Path $package "config.json") -Raw | ConvertFrom-Json).profiles.PSObject.Properties.Name)[0]
            if (-not $firstProfile) { throw "Cannot determine launcher profile" }
            $launcher = "@echo off`r`nif `"%AWS_PROFILE%`"==`"`" set AWS_PROFILE=$firstProfile`r`n`"%USERPROFILE%\gip\credential-process.exe`" --login --profile %AWS_PROFILE%`r`nif errorlevel 1 exit /b 1`r`nclaude %*`r`n"
            Add-GipInstallStage "" "gip\claude-bedrock.cmd" "gip/claude-bedrock.cmd" $launcher
        }
        $homeJson = ($homePath + "\gip\credential-process.exe").Replace("\", "/")
        $harnessRoot = Join-Path $package "harnesses"
        if (Test-Path -LiteralPath $harnessRoot -PathType Container) {
            foreach ($harnessFile in (Get-ChildItem -LiteralPath $harnessRoot -File -Recurse)) {
                $relative = $harnessFile.FullName.Substring($harnessRoot.Length).TrimStart("\")
                $content = [IO.File]::ReadAllText($harnessFile.FullName).Replace("__CREDENTIAL_PROCESS_PATH__", $homeJson)
                Add-GipInstallStage "" ("gip\harnesses\" + $relative) ("gip/harnesses/" + $relative.Replace("\", "/")) $content
            }
        }
        $mcpSource = Join-Path $package "gip-settings\mcp.json"
        if (Test-Path -LiteralPath $mcpSource -PathType Leaf) {
            $helperPath = ($homePath + "\gip\websearch-headers.cmd").Replace("\", "/")
            $mcpContent = [IO.File]::ReadAllText($mcpSource).Replace("__WEBSEARCH_HEADERS_HELPER__", $helperPath)
            Add-GipInstallStage "" "gip\gip-settings\mcp.json" "gip/gip-settings/mcp.json" $mcpContent
        }
        $regSource = Join-Path $package "cowork-3p.reg"
        if (Test-Path -LiteralPath $regSource -PathType Leaf) {
            $escapedHome = $homePath.Replace("\", "\\")
            $regContent = [IO.File]::ReadAllText($regSource).Replace("__GIP_HOME__", $escapedHome)
            Add-GipInstallStage "" "gip\cowork-3p.reg" "gip/cowork-3p.reg" $regContent
        }

        $configDirectory = Join-Path $homePath ".aws"
        Ensure-GipDirectory $configDirectory
        $configTarget = Join-Path $configDirectory "config"
        $configExpected = $null
        $configContent = ""
        if (Test-Path -LiteralPath $configTarget) {
            if (-not (Test-Path -LiteralPath $configTarget -PathType Leaf) -or (Test-GipReparsePoint $configTarget)) { throw "Refusing non-regular AWS config target" }
            $configExpected = Get-GipDigest $configTarget
            $configContent = [IO.File]::ReadAllText($configTarget)
        }
        $profiles = (Get-Content -LiteralPath (Join-Path $package "config.json") -Raw | ConvertFrom-Json).profiles.PSObject.Properties
        $profileNames = @($profiles.Name)
        if ($collectorActive) { $profileNames += @($profiles.Name | ForEach-Object { "$_-collector" }) }
        foreach ($name in $profileNames) {
            $pattern = "(?ms)^\[profile " + [regex]::Escape($name) + "\]\r?\n.*?(?=^\[|\z)"
            $matches = [regex]::Matches($configContent, $pattern)
            if ($matches.Count -gt 1) { throw "Refusing duplicate AWS profile sections: $name" }
            if ($matches.Count -eq 1) {
                $match = $matches[0]
                $owned = if ($null -eq $oldManifest) { $null } else { $oldManifest.aws_profiles.PSObject.Properties[$name] }
                $normalized = $match.Value.Replace("`r`n", "`n")
                $sha = [Security.Cryptography.SHA256]::Create()
                try { $actual = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($normalized)))).Replace("-", "").ToLowerInvariant() } finally { $sha.Dispose() }
                if ($null -eq $owned -or $actual -ne $owned.Value.ToLowerInvariant()) { throw "Refusing to overwrite foreign or changed AWS profile: $name" }
                $configContent = [regex]::Replace($configContent, $pattern, "")
            }
        }
        $newline = if ($configContent.Contains("`r`n")) { "`r`n" } else { "`n" }
        foreach ($property in $profiles) {
            $region = if ($property.Value.aws_region) { $property.Value.aws_region } else { "us-east-1" }
            $credential = ($homePath + "\gip\credential-process.exe --profile " + $property.Name).Replace("\", "/")
            if ($configContent.Length -gt 0 -and -not $configContent.EndsWith($newline)) { $configContent += $newline }
            $configContent += "[profile $($property.Name)]${newline}region = $region${newline}credential_process = $credential${newline}"
            if ($collectorActive) { $configContent += "[profile $($property.Name)-collector]${newline}region = $region${newline}credential_process = $credential${newline}" }
        }
        $configStaged = Join-Path $configDirectory (".config.gip-install-" + [Guid]::NewGuid().ToString("N"))
        [IO.File]::WriteAllText($configStaged, $configContent, (New-Object Text.UTF8Encoding($false)))
        $stagedPaths += $configStaged
        $entries += [PSCustomObject]@{ Target = $configTarget; Staged = $configStaged; ExpectedDigest = $configExpected }

        $manifestFiles = @{}
        foreach ($key in $fileEntries.Keys) { $manifestFiles[$key] = Get-GipDigest $fileEntries[$key] }
        if ($null -ne $oldManifest) {
            foreach ($property in $oldManifest.files.PSObject.Properties) {
                if ($manifestFiles.ContainsKey($property.Name)) { continue }
                $retained = Join-Path $homePath ($property.Name.Replace("/", "\"))
                if ((Test-Path -LiteralPath $retained -PathType Leaf) -and -not (Test-GipReparsePoint $retained) -and (Get-GipDigest $retained) -eq $property.Value.ToLowerInvariant()) {
                    $manifestFiles[$property.Name] = $property.Value.ToLowerInvariant()
                }
            }
        }
        $manifestProfiles = @{}
        $normalizedConfig = $configContent.Replace("`r`n", "`n")
        foreach ($name in $profileNames) {
            $match = [regex]::Match($normalizedConfig, "(?ms)^\[profile " + [regex]::Escape($name) + "\]\n.*?(?=^\[|\z)")
            if ($match.Success) {
                $sha = [Security.Cryptography.SHA256]::Create()
                try { $manifestProfiles[$name] = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($match.Value)))).Replace("-", "").ToLowerInvariant() } finally { $sha.Dispose() }
            }
        }
        $manifestExpected = if (Test-Path -LiteralPath $manifestPath -PathType Leaf) { Get-GipDigest $manifestPath } else { $null }
        $manifestStaged = Join-Path $install (".gip-ownership-install-" + [Guid]::NewGuid().ToString("N"))
        $manifestValue = @{ schema_version = 1; files = $manifestFiles; aws_profiles = $manifestProfiles; aws_sso_sessions = @{} } | ConvertTo-Json -Depth 5
        [IO.File]::WriteAllText($manifestStaged, $manifestValue + "`n", (New-Object Text.UTF8Encoding($false)))
        $stagedPaths += $manifestStaged
        $entries += [PSCustomObject]@{ Target = $manifestPath; Staged = $manifestStaged; ExpectedDigest = $manifestExpected }

        Invoke-GipFileTransaction $entries
    } finally {
        foreach ($path in $stagedPaths) { Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue }
        foreach ($directory in @($createdDirectories | Sort-Object Length -Descending)) {
            try {
                if ([IO.Directory]::Exists($directory) -and [IO.Directory]::GetFileSystemEntries($directory).Length -eq 0) {
                    [IO.Directory]::Delete($directory, $false)
                }
            } catch {}
        }
    }
    exit 0
}

if ($InstallFile) {
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf) -or (Test-GipReparsePoint $Source)) { Fail "Install source is missing or unsafe: $Source" }
    $target = [IO.Path]::GetFullPath((Join-Path $homePath $RelativeTarget))
    $homePrefix = [IO.Path]::GetFullPath($homePath).TrimEnd('\') + '\'
    if (-not $target.StartsWith($homePrefix, [StringComparison]::OrdinalIgnoreCase)) { Fail "Install target escapes the user home" }
    $directory = [IO.Path]::GetDirectoryName($target)
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    if (Test-GipReparsePoint $directory) { Fail "Refusing reparse-point target directory: $directory" }
    $staged = Join-Path $directory ("." + [IO.Path]::GetFileName($target) + ".gip-install-" + [Guid]::NewGuid().ToString("N"))
    [IO.File]::Copy([IO.Path]::GetFullPath($Source), $staged, $false)
    try {
        $expected = Get-GipManifestDigest $ManifestKey
        Invoke-GipFileTransaction @([PSCustomObject]@{ Target = $target; Staged = $staged; ExpectedDigest = $expected })
    } finally {
        Remove-Item -LiteralPath $staged -Force -ErrorAction SilentlyContinue
    }
    exit 0
}

if ($ConfigureAws) {
    $configDirectory = Join-Path $homePath ".aws"
    $target = Join-Path $configDirectory "config"
    New-Item -ItemType Directory -Path $configDirectory -Force | Out-Null
    if (Test-GipReparsePoint $configDirectory) { Fail "Refusing reparse-point AWS config directory" }
    $expectedDigest = $null
    $content = ""
    $manifest = $null
    if (Test-Path -LiteralPath $manifestPath -PathType Leaf) { $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json }
    if (Test-Path -LiteralPath $target) {
        if (-not (Test-Path -LiteralPath $target -PathType Leaf) -or (Test-GipReparsePoint $target)) { Fail "Refusing non-regular AWS config target" }
        $expectedDigest = Get-GipDigest $target
        $content = [IO.File]::ReadAllText($target)
    }
    $profiles = (Get-Content -LiteralPath (Join-Path $package "config.json") -Raw | ConvertFrom-Json).profiles.PSObject.Properties
    $names = @($profiles.Name)
    if (Test-Path -LiteralPath (Join-Path $install "collector-config.yaml")) { $names += @($profiles.Name | ForEach-Object { "$_-collector" }) }
    foreach ($name in $names) {
        $pattern = "(?ms)^\[profile " + [regex]::Escape($name) + "\]\r?\n.*?(?=^\[|\z)"
        $match = [regex]::Match($content, $pattern)
        if ($match.Success) {
            $owned = if ($null -eq $manifest) { $null } else { $manifest.aws_profiles.PSObject.Properties[$name] }
            $normalized = $match.Value.Replace("`r`n", "`n")
            $sha = [Security.Cryptography.SHA256]::Create()
            try { $actual = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($normalized)))).Replace("-", "").ToLowerInvariant() } finally { $sha.Dispose() }
            if ($null -eq $owned -or $actual -ne $owned.Value.ToLowerInvariant()) { Fail "Refusing to overwrite foreign or changed AWS profile: $name" }
            $content = [regex]::Replace($content, $pattern, "")
        }
    }
    $newline = if ($content.Contains("`r`n")) { "`r`n" } else { "`n" }
    foreach ($property in $profiles) {
        $region = if ($property.Value.aws_region) { $property.Value.aws_region } else { "us-east-1" }
        $credential = ($homePath + "\gip\credential-process.exe --profile " + $property.Name).Replace("\", "/")
        if ($content.Length -gt 0 -and -not $content.EndsWith($newline)) { $content += $newline }
        $content += "[profile $($property.Name)]${newline}region = $region${newline}credential_process = $credential${newline}"
        if (Test-Path -LiteralPath (Join-Path $install "collector-config.yaml")) {
            $content += "[profile $($property.Name)-collector]${newline}region = $region${newline}credential_process = $credential${newline}"
        }
    }
    $staged = Join-Path $configDirectory (".config.gip-install-" + [Guid]::NewGuid().ToString("N"))
    try {
        [IO.File]::WriteAllText($staged, $content, (New-Object Text.UTF8Encoding($false)))
        Invoke-GipFileTransaction @([PSCustomObject]@{ Target = $target; Staged = $staged; ExpectedDigest = $expectedDigest })
    } finally { Remove-Item -LiteralPath $staged -Force -ErrorAction SilentlyContinue }
    exit 0
}

if ($RecordManifest) {
    $files = @{}
    $names = @("credential-process.exe", "config.json", "websearch-headers.cmd", "otel-helper.exe", "otel-helper.cmd", "otel-helper.ps1", "otelcol.exe", "collector-config.yaml", "claude-bedrock.cmd")
    foreach ($name in $names) {
        $path = Join-Path $install $name
        if ((Test-Path -LiteralPath $path -PathType Leaf) -and -not (Test-GipReparsePoint $path)) { $files["gip/$name"] = Get-GipDigest $path }
    }
    if ($SettingsCreated -eq "1") {
        $settings = Join-Path $homePath ".claude\settings.json"
        if ((Test-Path -LiteralPath $settings -PathType Leaf) -and -not (Test-GipReparsePoint $settings)) { $files[".claude/settings.json"] = Get-GipDigest $settings }
    }
    $ownedProfiles = @{}
    $configPath = Join-Path $homePath ".aws\config"
    if (Test-Path -LiteralPath $configPath -PathType Leaf) {
        $content = [IO.File]::ReadAllText($configPath).Replace("`r`n", "`n")
        $profileNames = @((Get-Content -LiteralPath (Join-Path $package "config.json") -Raw | ConvertFrom-Json).profiles.PSObject.Properties.Name)
        if (Test-Path -LiteralPath (Join-Path $install "collector-config.yaml")) { $profileNames += @($profileNames | ForEach-Object { "$_-collector" }) }
        foreach ($name in $profileNames) {
            $match = [regex]::Match($content, "(?ms)^\[profile " + [regex]::Escape($name) + "\]\n.*?(?=^\[|\z)")
            if ($match.Success) {
                $sha = [Security.Cryptography.SHA256]::Create()
                try { $ownedProfiles[$name] = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($match.Value)))).Replace("-", "").ToLowerInvariant() } finally { $sha.Dispose() }
            }
        }
    }
    $target = $manifestPath
    $currentExpected = if ($ExpectedDigest) { $ExpectedDigest.ToLowerInvariant() } else { $null }
    if ($currentExpected) {
        if (-not (Test-Path -LiteralPath $target -PathType Leaf) -or (Get-GipDigest $target) -ne $currentExpected) { Fail "Ownership manifest changed during installation" }
    } elseif (Test-Path -LiteralPath $target) { Fail "Ownership manifest appeared during installation" }
    $staged = Join-Path $install (".gip-ownership-install-" + [Guid]::NewGuid().ToString("N"))
    try {
        $value = @{ schema_version = 1; files = $files; aws_profiles = $ownedProfiles; aws_sso_sessions = @{} } | ConvertTo-Json -Depth 5
        [IO.File]::WriteAllText($staged, $value + "`n", (New-Object Text.UTF8Encoding($false)))
        Invoke-GipFileTransaction @([PSCustomObject]@{ Target = $target; Staged = $staged; ExpectedDigest = $currentExpected })
    } finally { Remove-Item -LiteralPath $staged -Force -ErrorAction SilentlyContinue }
    exit 0
}

if ($InstallManaged) {
    $source = Join-Path $package "gip-settings\managed-settings.json"
    $targetDirectory = "C:\Program Files\ClaudeCode"
    $target = Join-Path $targetDirectory "managed-settings.json"
    $marker = Join-Path $targetDirectory ".managed-settings.gip-sha256"
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { Fail "Managed settings source is missing" }
    $targetWasPresent = Test-Path -LiteralPath $target -PathType Leaf
    $expected = $null
    $markerExpected = $null
    if ($targetWasPresent) {
        if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) { Fail "Preserving existing foreign managed settings: $target" }
        $expected = (Get-Content -LiteralPath $marker -Raw).Trim().ToLowerInvariant()
        $actual = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($expected -notmatch "^[0-9a-f]{64}$" -or $actual -ne $expected) {
            Fail "Refusing to overwrite changed managed settings: $target"
        }
        $markerExpected = Get-GipDigest $marker
    } elseif (Test-Path -LiteralPath $marker) {
        Fail "Preserving existing foreign managed settings marker: $marker"
    }
    New-Item -ItemType Directory -Path $targetDirectory -Force | Out-Null
    $otelPath = ($homePath + "\gip\otel-helper.cmd").Replace("\", "\\")
    $credPath = ($homePath + "\gip\credential-process.exe") -replace "\\", "/"
    $content = (Get-Content -LiteralPath $source -Raw).Replace("__OTEL_HELPER_PATH__", $otelPath).Replace("__CREDENTIAL_PROCESS_PATH__", $credPath)
    $temporary = Join-Path $targetDirectory (".managed-settings.gip-" + [IO.Path]::GetRandomFileName())
    $markerTemporary = Join-Path $targetDirectory (".managed-settings.marker.gip-" + [IO.Path]::GetRandomFileName())
    try {
        [IO.File]::WriteAllText($temporary, $content, (New-Object Text.UTF8Encoding($false)))
        $digest = Get-GipDigest $temporary
        [IO.File]::WriteAllText($markerTemporary, $digest + "`n", (New-Object Text.UTF8Encoding($false)))
        Invoke-GipFileTransaction @(
            [PSCustomObject]@{ Target = $target; Staged = $temporary; ExpectedDigest = $expected },
            [PSCustomObject]@{ Target = $marker; Staged = $markerTemporary; ExpectedDigest = $markerExpected }
        )
    } finally {
        Remove-Item -LiteralPath $temporary,$markerTemporary -Force -ErrorAction SilentlyContinue
    }
    exit 0
}

foreach ($required in @("credential-process-windows.exe", "config.json")) {
    if (-not (Test-Path -LiteralPath (Join-Path $package $required) -PathType Leaf)) { Fail "Required package file is missing: $required" }
}
if ($RequireOtelHelpers) {
    foreach ($required in @("otel-helper.cmd", "otel-helper.ps1")) {
        if (-not (Test-Path -LiteralPath (Join-Path $package $required) -PathType Leaf)) { Fail "Required package file is missing: $required" }
    }
}

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail "Do not run the full installer as Administrator; managed settings elevate separately"
}

function Is-ReparsePoint([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    return [bool]((Get-Item -LiteralPath $Path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)
}

$managedSource = Join-Path $package "gip-settings\managed-settings.json"
if (Test-Path -LiteralPath $managedSource -PathType Leaf) {
    $managedTarget = "C:\Program Files\ClaudeCode\managed-settings.json"
    $managedMarker = "C:\Program Files\ClaudeCode\.managed-settings.gip-sha256"
    if (Test-Path -LiteralPath $managedTarget) {
        if (Is-ReparsePoint $managedTarget -or -not (Test-Path -LiteralPath $managedTarget -PathType Leaf)) {
            Fail "Refusing non-regular managed settings target: $managedTarget"
        }
        if (-not (Test-Path -LiteralPath $managedMarker -PathType Leaf)) {
            Fail "Preserving existing foreign managed settings: $managedTarget"
        }
        $managedExpected = (Get-Content -LiteralPath $managedMarker -Raw).Trim().ToLowerInvariant()
        $managedActual = (Get-FileHash -LiteralPath $managedTarget -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($managedExpected -notmatch "^[0-9a-f]{64}$" -or $managedActual -ne $managedExpected) {
            Fail "Refusing to overwrite changed managed settings: $managedTarget"
        }
    }
}

foreach ($path in @($install, (Join-Path $homePath ".aws"), (Join-Path $homePath ".claude"))) {
    if (Is-ReparsePoint $path) { Fail "Refusing reparse-point install path: $path" }
}

$manifest = $null
if (Test-Path -LiteralPath $manifestPath) {
    if (Is-ReparsePoint $manifestPath) { Fail "Refusing reparse-point ownership manifest: $manifestPath" }
    try { $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json } catch {
        Fail "Cannot validate ownership manifest: $_"
    }
    if ($manifest.schema_version -ne 1 -or $null -eq $manifest.files -or
        $null -eq $manifest.aws_profiles -or $null -eq $manifest.aws_sso_sessions) {
        Fail "Invalid ownership manifest; run gip cleanup before reinstalling"
    }
}

function Manifest-Digest([string]$Group, [string]$Name) {
    if ($null -eq $manifest) { return $null }
    $property = $manifest.$Group.PSObject.Properties[$Name]
    if ($null -eq $property -or $property.Value -notmatch "^[0-9a-fA-F]{64}$") { return $null }
    return $property.Value.ToLowerInvariant()
}

$knownNames = @(
    "credential-process.exe", "config.json", "websearch-headers.cmd", "otel-helper.exe",
    "otel-helper.cmd", "otel-helper.ps1", "otelcol.exe", "collector-config.yaml", "claude-bedrock.cmd"
)
$knownKeys = @($knownNames | ForEach-Object { "gip/$_" }) + ".claude/settings.json"
if ($null -ne $manifest) {
    $knownKeys += @("gip/cowork-3p.reg", "gip/gip-settings/mcp.json")
    $unknown = @($manifest.files.PSObject.Properties.Name | Where-Object { $_ -notin $knownKeys -and -not $_.StartsWith("gip/harnesses/") })
    if ($unknown.Count -gt 0) { Fail "Ownership manifest contains state from another installer mode" }
}

foreach ($name in $knownNames) {
    $path = Join-Path $install $name
    if (-not (Test-Path -LiteralPath $path)) { continue }
    if (Is-ReparsePoint $path -or -not (Test-Path -LiteralPath $path -PathType Leaf)) {
        Fail "Refusing non-regular install target: $path"
    }
    $expected = Manifest-Digest "files" "gip/$name"
    $actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($null -eq $expected -or $actual -ne $expected) {
        $classification = if ($null -eq $expected) { "foreign" } else { "owned-modified" }
        Fail "Refusing to overwrite $classification target: $path"
    }
}

$settingsOwned = $false
$settings = Join-Path $homePath ".claude\settings.json"
if (Test-Path -LiteralPath $settings) {
    if (Is-ReparsePoint $settings -or -not (Test-Path -LiteralPath $settings -PathType Leaf)) {
        Fail "Refusing non-regular settings target: $settings"
    }
    $expected = Manifest-Digest "files" ".claude/settings.json"
    if ($null -ne $expected) {
        $actual = (Get-FileHash -LiteralPath $settings -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $expected) { Fail "Refusing to overwrite owned-modified target: $settings" }
        $settingsOwned = $true
    }
}

try { $profileNames = @((Get-Content (Join-Path $package "config.json") -Raw | ConvertFrom-Json).profiles.PSObject.Properties.Name) }
catch { Fail "Cannot read package profiles: $_" }
if ((Test-Path (Join-Path $package "collector-config.yaml")) -or
    (Test-Path (Join-Path $install "collector-config.yaml"))) {
    $profileNames += @($profileNames | ForEach-Object { "$_-collector" })
}
if ($null -ne $manifest) {
    $extraProfiles = @($manifest.aws_profiles.PSObject.Properties.Name | Where-Object { $_ -notin $profileNames })
    if ($extraProfiles.Count -gt 0 -or $manifest.aws_sso_sessions.PSObject.Properties.Count -gt 0) {
        Fail "Ownership manifest contains profiles from another installer mode"
    }
}

$configPath = Join-Path $homePath ".aws\config"
if (Test-Path -LiteralPath $configPath) {
    if (Is-ReparsePoint $configPath -or -not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
        Fail "Refusing non-regular AWS config target: $configPath"
    }
    $content = [IO.File]::ReadAllText($configPath).Replace("`r`n", "`n")
    foreach ($profileName in $profileNames) {
        $pattern = "(?ms)^\[profile " + [regex]::Escape($profileName) + "\]\n.*?(?=^\[|\z)"
        $match = [regex]::Match($content, $pattern)
        if (-not $match.Success) { continue }
        $sha = [Security.Cryptography.SHA256]::Create()
        try {
            $actual = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($match.Value)))).Replace("-", "").ToLowerInvariant()
        } finally { $sha.Dispose() }
        $expected = Manifest-Digest "aws_profiles" $profileName
        if ($null -eq $expected -or $actual -ne $expected) {
            $classification = if ($null -eq $expected) { "foreign" } else { "owned-modified" }
            Fail "Refusing to overwrite $classification AWS profile: $profileName"
        }
    }
}

if ($settingsOwned) { exit 10 }
exit 0
"""
        (output_dir / "gip-install.ps1").write_text(preflight_content, encoding="utf-8", newline="\r\n")

        # When monitoring is enabled, Claude Code's otelHeadersHelper points at
        # otel-helper.cmd (which falls back to otel-helper.ps1 if AV blocks the
        # .exe). Those files are then REQUIRED: a missing .cmd silently breaks all
        # telemetry export. So the installer must fail loudly if they're absent.
        # When monitoring is off, the helper isn't referenced, so their absence is
        # harmless and the copy stays best-effort.
        _otel_missing_is_fatal = bool(profile.monitoring_enabled)

        # Web search headersHelper (Windows): when web search is enabled (OIDC),
        # write %USERPROFILE%\gip\websearch-headers.cmd, a
        # wrapper that runs credential-process.exe --get-mcp-auth-header (the
        # browserless MCP-header mode). The .cmd itself keeps %USERPROFILE%: that
        # is fine because cmd.exe expands it at runtime when the wrapper EXECUTES.
        # (The registry path that points AT this .cmd is the one that must be
        # absolute — Claude reads it literally — handled via __GIP_HOME__ in the
        # .reg, resolved by the cowork-3p.reg substitution block below.)
        windows_websearch_block = ""
        windows_websearch_post_block = ""
        _ws_enabled = getattr(profile, "web_search_enabled", False)
        _ws_auth = getattr(profile, "effective_auth_type", getattr(profile, "auth_type", "oidc"))
        if _ws_enabled and _ws_auth != "idc":
            from governed_inference_platform.cli.utils.cowork_3p import (
                WEBSEARCH_MCP_SERVER_NAME,
                resolve_websearch_gateway_url,
            )

            _ws_gateway_url = resolve_websearch_gateway_url(profile)
            windows_claude_mcp_block = ""
            if _ws_gateway_url:
                # JSON paths use forward slashes (%USERPROFILE:\=/%) so no JSON
                # escaping of backslashes is needed — same trick the settings.json
                # PowerShell substitution uses. Registration is guarded on the
                # claude CLI being installed; otherwise the exact command is printed.
                windows_claude_mcp_block = f"""
REM Register the gateway MCP server with Claude Code CLI (user scope). Claude
REM Code re-runs the headersHelper per connection and on 401/403, so the ~1h
REM id_token expiry never bites.
set "WS_HELPER_JSON=%USERPROFILE:\\=/%/gip/websearch-headers.cmd"
set "WS_MCP_JSON={{\\"type\\":\\"http\\",\\"url\\":\\"{_ws_gateway_url}\\",\\"headersHelper\\":\\"!WS_HELPER_JSON!\\"}}"
where claude >nul 2>&1
if %errorlevel% equ 0 (
    REM "call" is required: claude is usually an npm .cmd shim, and invoking a
    REM .cmd from a .bat without call would transfer control and never return.
    call claude mcp remove {WEBSEARCH_MCP_SERVER_NAME} -s user >nul 2>&1
    call claude mcp add-json {WEBSEARCH_MCP_SERVER_NAME} -s user "!WS_MCP_JSON!" >nul 2>&1
    if !errorlevel! equ 0 (
        echo OK Web search MCP server registered with Claude Code
    ) else (
        echo WARN Could not register the web search MCP server automatically. Run:
        echo   claude mcp add-json {WEBSEARCH_MCP_SERVER_NAME} -s user "!WS_MCP_JSON!"
    )
) else (
    echo To enable web search in Claude Code, run after installing Claude Code:
    echo   claude mcp add-json {WEBSEARCH_MCP_SERVER_NAME} -s user "!WS_MCP_JSON!"
)
"""
            windows_websearch_post_block = windows_claude_mcp_block
            windows_websearch_block = f"""
REM Web search headersHelper (Cowork web search via AgentCore Gateway).
REM Emits {{"Authorization":"Bearer <id_token>"}} via the browserless
REM --get-mcp-auth-header mode, bound to the '{profile.name}' profile.
echo Installing web search headersHelper...
set "GIP_WS_SOURCE=%TEMP%\\gip-websearch-!RANDOM!-!RANDOM!.cmd"
(
echo @echo off
echo "%USERPROFILE%\\gip\\credential-process.exe" --profile {profile.name} --get-mcp-auth-header
) > "!GIP_WS_SOURCE!"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0gip-install.ps1" -InstallFile -Source "!GIP_WS_SOURCE!" -RelativeTarget "gip\\websearch-headers.cmd" -ManifestKey "gip/websearch-headers.cmd"
set "GIP_WS_STATUS=!errorlevel!"
del /q "!GIP_WS_SOURCE!" >nul 2>&1
if not "!GIP_WS_STATUS!"=="0" exit /b 1
echo OK Web search headersHelper installed
{windows_claude_mcp_block}"""

        installer_content = f"""@echo off
SETLOCAL ENABLEDELAYEDEXPANSION
cd /d "%~dp0"
REM Windows PowerShell must not inherit a PowerShell 7 PSModulePath (for example when
REM this file is started from a pwsh terminal): it would load PowerShell 7 modules and
REM fail on Get-FileHash. Clearing it restores the Windows PowerShell defaults.
set "PSModulePath="
REM Claude Code Authentication Installer for Windows
REM Organization: {profile.provider_domain}
REM Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

echo ======================================
echo Claude Code Authentication Installer
echo ======================================
echo.
echo Organization: {profile.provider_domain}
echo.

REM Check prerequisites
echo Checking prerequisites...

where aws >nul 2>&1
if %errorlevel% neq 0 (
    echo INFO: AWS CLI not found -- not required. Profiles will be configured directly.
) else (
    echo OK AWS CLI found
)

echo OK Prerequisites found
echo.

set GIP_SETTINGS_CREATED=0
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0gip-install.ps1"
set "GIP_PREFLIGHT_STATUS=!errorlevel!"
if "!GIP_PREFLIGHT_STATUS!"=="10" (
    set GIP_SETTINGS_CREATED=1
) else if not "!GIP_PREFLIGHT_STATUS!"=="0" (
    echo ERROR: Ownership preflight failed. No installation files were changed.
    exit /b 1
)
set "GIP_MANIFEST_DIGEST="
if exist "%USERPROFILE%\\gip\\.gip-ownership.json" (
    for /f %%h in ('powershell -NoProfile -Command "(Get-FileHash -LiteralPath (Join-Path $env:USERPROFILE 'gip\\.gip-ownership.json') -Algorithm SHA256).Hash.ToLowerInvariant()"') do set "GIP_MANIFEST_DIGEST=%%h"
    if !errorlevel! neq 0 exit /b 1
)

REM Create directory
echo Installing authentication tools...
if not exist "%USERPROFILE%\\gip" mkdir "%USERPROFILE%\\gip"

REM Copy credential process executable with renamed target
echo Copying credential process...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0gip-install.ps1" -InstallFile -Source "%~dp0credential-process-windows.exe" -RelativeTarget "gip\\credential-process.exe" -ManifestKey "gip/credential-process.exe"
if %errorlevel% neq 0 (
    echo ERROR: Failed to copy credential-process-windows.exe
    exit /b 1
)
{windows_websearch_block}
REM Copy OTEL helper if it exists with renamed target
if exist "otel-helper-windows.exe" (
    echo Copying OTEL helper...
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0gip-install.ps1" -InstallFile -Source "%~dp0otel-helper-windows.exe" -RelativeTarget "gip\\otel-helper.exe" -ManifestKey "gip/otel-helper.exe"
    if !errorlevel! neq 0 (
        echo ERROR: Failed to copy otel-helper-windows.exe
        exit /b 1
    )
)

REM Copy the OTEL helper wrapper (.cmd) and its PowerShell fallback (.ps1).
REM Claude Code's otelHeadersHelper points at otel-helper.cmd, which runs the
REM fast .exe and falls back to the .ps1 if antivirus blocks the binary. When
REM monitoring is enabled these files are REQUIRED — a missing .cmd makes Claude
REM Code fail every telemetry export with "is not recognized as an internal or
REM external command" and silently drops all metrics, so we fail the install
REM loudly rather than leave a broken telemetry config.
if exist "otel-helper.cmd" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0gip-install.ps1" -InstallFile -Source "%~dp0otel-helper.cmd" -RelativeTarget "gip\\otel-helper.cmd" -ManifestKey "gip/otel-helper.cmd"
    if !errorlevel! neq 0 (
        echo ERROR: Failed to copy otel-helper.cmd
        exit /b 1
    )
) else (
    echo {"ERROR" if _otel_missing_is_fatal else "INFO"}: otel-helper.cmd not found in package.
{
            '''    echo        Claude Code needs it to send telemetry [otelHeadersHelper].
    echo        Re-extract the full package [including .cmd and .ps1 files] and retry.
    exit /b 1'''
            if _otel_missing_is_fatal
            else "    REM Monitoring disabled - helper not required."
        }
)
if exist "otel-helper.ps1" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0gip-install.ps1" -InstallFile -Source "%~dp0otel-helper.ps1" -RelativeTarget "gip\\otel-helper.ps1" -ManifestKey "gip/otel-helper.ps1"
    if !errorlevel! neq 0 (
        echo ERROR: Failed to copy otel-helper.ps1
        exit /b 1
    )
) else (
    echo {"ERROR" if _otel_missing_is_fatal else "INFO"}: otel-helper.ps1 not found in package.
{
            '''    echo        It is the antivirus fallback for otel-helper.cmd and is required.
    echo        Re-extract the full package and run install.bat again.
    exit /b 1'''
            if _otel_missing_is_fatal
            else "    REM Monitoring disabled - fallback not required."
        }
)

REM Install OTEL Collector sidecar (sidecar-mode packages only). otelcol is built
REM via OCB and SHIPPED in the package as otelcol-windows.exe (same model as the
REM other binaries) — never downloaded at install time. otel-helper.ps1 launches it
REM from %USERPROFILE%\\gip\\otelcol.exe under the
REM <profile>-collector AWS profile created below.
if exist "collector-config.yaml" (
    if exist "otelcol-windows.exe" (
        echo Installing OTEL Collector sidecar...
        powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0gip-install.ps1" -InstallFile -Source "%~dp0otelcol-windows.exe" -RelativeTarget "gip\\otelcol.exe" -ManifestKey "gip/otelcol.exe"
        if !errorlevel! neq 0 (
            echo ERROR: Failed to copy otelcol-windows.exe
            exit /b 1
        )
        powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0gip-install.ps1" -InstallFile -Source "%~dp0collector-config.yaml" -RelativeTarget "gip\\collector-config.yaml" -ManifestKey "gip/collector-config.yaml"
        if !errorlevel! neq 0 (
            echo ERROR: Failed to copy collector-config.yaml
            exit /b 1
        )
        REM Unblock the downloaded binary so SmartScreen doesn't block subprocess launch
        powershell -NoProfile -Command "Get-ChildItem '%USERPROFILE%\\gip\\otelcol.exe' | Unblock-File" >nul 2>&1
        echo OK OTEL Collector sidecar installed
    ) else (
        echo WARNING: Sidecar config present but otelcol-windows.exe is missing.
        echo          The admin must run 'gip package' with Go 1.23+ to build the collector.
        echo          Telemetry will not be forwarded until the collector is installed.
    )
)

REM Copy configuration
echo Copying configuration...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0gip-install.ps1" -InstallFile -Source "%~dp0config.json" -RelativeTarget "gip\\config.json" -ManifestKey "gip/config.json"
if !errorlevel! neq 0 (
    echo ERROR: Failed to copy config.json
    exit /b 1
)

REM Copy Claude Code settings if they exist
if exist "gip-settings" (
    echo Copying Claude Code telemetry settings...
    if not exist "%USERPROFILE%\\.claude" mkdir "%USERPROFILE%\\.claude"

    REM Install managed-settings.json (organization-wide enforcement) if present
    if exist "gip-settings\\managed-settings.json" (
        echo Managed settings detected [organization-wide enforcement]...

        REM Elevate only the system-scoped write. The full installer remains
        REM unelevated so user paths cannot redirect administrator writes.
        powershell -NoProfile -Command "$script = (Join-Path (Get-Location) 'gip-install.ps1').Replace('''',''''''); $home = $env:USERPROFILE.Replace('''',''''''); $command = \"& '$script' -InstallManaged -UserHome '$home'\"; $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command)); $p = Start-Process -FilePath powershell.exe -Verb RunAs -Wait -PassThru -ArgumentList @('-NoProfile','-EncodedCommand',$encoded); exit $p.ExitCode"
        if !errorlevel! neq 0 (
            echo ERROR: Managed settings were not installed.
            exit /b 1
        )
        echo OK Managed settings installed: C:\\Program Files\\ClaudeCode\\managed-settings.json
        echo    These settings have highest precedence and cannot be overridden by users.
    )

    REM Copy user-scope settings.json only when absent or manifest-owned.
    if exist "gip-settings\\settings.json" (
        set WRITE_SETTINGS=false
        if exist "%USERPROFILE%\\.claude\\settings.json" (
            if "!GIP_SETTINGS_CREATED!"=="1" (
                set WRITE_SETTINGS=true
            ) else (
                echo WARNING: Preserving existing foreign Claude Code settings.
                echo          Review gip-settings\\settings.json and merge its Bedrock keys manually.
            )
        ) else (
            set WRITE_SETTINGS=true
            set GIP_SETTINGS_CREATED=1
        )

        if "!WRITE_SETTINGS!"=="true" (
            set "GIP_SETTINGS_SOURCE=%TEMP%\\gip-settings-!RANDOM!-!RANDOM!.json"
            powershell -NoProfile -Command "$ErrorActionPreference = 'Stop'; $otelPath = ($env:USERPROFILE + '\\gip\\otel-helper.cmd').Replace('\\','\\\\'); $credPath = $env:USERPROFILE + '\\gip\\credential-process.exe' -replace '\\\\', '/'; $content = (Get-Content 'gip-settings\\settings.json' -Raw) -replace '__OTEL_HELPER_PATH__', $otelPath -replace '__CREDENTIAL_PROCESS_PATH__', $credPath; [IO.File]::WriteAllText($env:GIP_SETTINGS_SOURCE, $content, (New-Object Text.UTF8Encoding($false)))"
            if !errorlevel! neq 0 (
                echo ERROR: Failed to prepare Claude Code settings.
                del /q "!GIP_SETTINGS_SOURCE!" >nul 2>&1
                exit /b 1
            )
            powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0gip-install.ps1" -InstallFile -Source "!GIP_SETTINGS_SOURCE!" -RelativeTarget ".claude\\settings.json" -ManifestKey ".claude/settings.json"
            set "GIP_SETTINGS_STATUS=!errorlevel!"
            del /q "!GIP_SETTINGS_SOURCE!" >nul 2>&1
            if not "!GIP_SETTINGS_STATUS!"=="0" (
                echo ERROR: Failed to install Claude Code settings.
                exit /b 1
            )
            echo OK Claude Code settings configured
        )
    )
)

REM Configure AWS profiles
echo.
echo Configuring AWS profiles...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0gip-install.ps1" -ConfigureAws
if !errorlevel! neq 0 (
    echo ERROR: Failed to configure AWS profiles transactionally.
    exit /b 1
)
echo   OK AWS profiles configured

"""

        install_all_flags = []
        if _is_idc := (getattr(profile, "effective_auth_type", getattr(profile, "auth_type", None)) == "idc"):
            install_all_flags.append("-InstallLauncher")
        if _otel_missing_is_fatal:
            install_all_flags.append("-RequireOtelHelpers")
        if _ws_enabled and _ws_auth != "idc":
            install_all_flags.extend(("-WebSearchProfile", f'"{profile.name}"'))
        install_all_arguments = " ".join(install_all_flags)
        if _otel_missing_is_fatal:
            installer_content = installer_content.replace(
                'powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0gip-install.ps1"',
                'powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0gip-install.ps1" -RequireOtelHelpers',
                1,
            )
        installer_content = (
            installer_content[: installer_content.index("REM Create directory")]
            + f"""
echo Installing authentication tools and AWS profiles transactionally...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0gip-install.ps1" -InstallAll {install_all_arguments}
if !errorlevel! neq 0 (
    echo ERROR: Installation failed and user-scoped changes were rolled back.
    exit /b 1
)
echo OK User files, AWS profiles, and ownership manifest installed

REM Managed settings are a separate elevated pair transaction. Run it only after
REM user ownership commits so rollback cannot strand policy references.
if exist "gip-settings\\managed-settings.json" (
    echo Managed settings detected [organization-wide enforcement]...
    powershell -NoProfile -Command "$script = (Join-Path (Get-Location) 'gip-install.ps1').Replace('''',''''''); $home = $env:USERPROFILE.Replace('''',''''''); $command = \"& '$script' -InstallManaged -UserHome '$home'\"; $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command)); $p = Start-Process -FilePath powershell.exe -Verb RunAs -Wait -PassThru -ArgumentList @('-NoProfile','-EncodedCommand',$encoded); exit $p.ExitCode"
    if !errorlevel! neq 0 (
        echo ERROR: User files were installed, but managed settings were not updated.
        exit /b 1
    )
)

if exist "%USERPROFILE%\\gip\\otelcol.exe" (
    powershell -NoProfile -Command "Get-ChildItem '%USERPROFILE%\\gip\\otelcol.exe' | Unblock-File" >nul 2>&1
    if !errorlevel! neq 0 exit /b 1
)
{windows_websearch_post_block}
"""
        )

        # IDC auth needs a launcher wrapper; OIDC auth handles sign-in transparently.
        if False and _is_idc:
            installer_content += """
REM Generate a 'claude-bedrock.cmd' launcher.
REM It signs in first (no-op if the session is still valid) so the verification
REM URL is shown live in the user's console, THEN launches Claude Code.
REM In-session recovery via the awsAuthRefresh hook now works too, but Claude Code
REM does not auto-trigger it on a 401 yet (anthropics/claude-code#67529) --
REM front-running the sign-in here means the user is authenticated before the
REM first prompt.
REM
REM Written with plain batch 'echo' redirection (NOT PowerShell) so the embedded
REM quotes survive. In this script %%X%% becomes literal %X% in the .cmd, and
REM %%* becomes %*, so the launcher's own runtime expansion is deferred.
echo.
echo Creating launcher...
set "LAUNCHER=%USERPROFILE%\\gip\\claude-bedrock.cmd"
set "LAUNCHER_SOURCE=%TEMP%\\gip-launcher-!RANDOM!-!RANDOM!.cmd"
set "CRED_PROC=%USERPROFILE%\\gip\\credential-process.exe"
set "FIRST_PROFILE="
for /f %%p in ('powershell -NoProfile -Command "(Get-Content config.json | ConvertFrom-Json).profiles.PSObject.Properties.Name | Select-Object -First 1"') do set "FIRST_PROFILE=%%p"
if not defined FIRST_PROFILE (
    echo ERROR: Failed to determine the launcher profile.
    exit /b 1
)
> "!LAUNCHER_SOURCE!" echo @echo off
>> "!LAUNCHER_SOURCE!" echo if "%%AWS_PROFILE%%"=="" set AWS_PROFILE=!FIRST_PROFILE!
>> "!LAUNCHER_SOURCE!" echo "%CRED_PROC%" --login --profile %%AWS_PROFILE%%
>> "!LAUNCHER_SOURCE!" echo if errorlevel 1 exit /b 1
>> "!LAUNCHER_SOURCE!" echo claude %%*
if !errorlevel! neq 0 (
    echo ERROR: Failed to prepare the launcher.
    del /q "!LAUNCHER_SOURCE!" >nul 2>&1
    exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0gip-install.ps1" -InstallFile -Source "!LAUNCHER_SOURCE!" -RelativeTarget "gip\\claude-bedrock.cmd" -ManifestKey "gip/claude-bedrock.cmd"
set "GIP_LAUNCHER_STATUS=!errorlevel!"
del /q "!LAUNCHER_SOURCE!" >nul 2>&1
if not "!GIP_LAUNCHER_STATUS!"=="0" exit /b 1
echo   OK Created launcher: %LAUNCHER%
"""

        legacy_manifest_content = r"""
REM Record exact ownership after all files and optional launchers are installed.
REM Existing settings that were merged are intentionally not owned as a whole.
echo Recording gip ownership...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0gip-install.ps1" -RecordManifest -ExpectedDigest "!GIP_MANIFEST_DIGEST!" -SettingsCreated "!GIP_SETTINGS_CREATED!"
if !errorlevel! neq 0 (
    echo ERROR: Failed to record the ownership manifest. Installation cannot be cleaned safely.
    exit /b 1
)
echo   OK Ownership manifest recorded
"""
        del legacy_manifest_content

        if _is_idc:
            installer_content += f"""

echo.
echo ======================================
REM ^^! prints a literal ! because this script runs with delayed expansion enabled.
echo Installation complete^^!
echo ======================================
echo.
echo Available profiles:
echo   - {profile.name}
echo.
echo ^>^>^> Start Claude Code the usual way:
echo       claude
echo.
echo     It signs you in automatically when needed - opening your browser, or
echo     showing a sign-in link on headless/SSH hosts - and refreshes your session
echo     on its own after that.
echo.
echo     Optional: a 'claude-bedrock' launcher is also installed. It signs you in
echo     first, then starts Claude Code, which can make the very first sign-in
echo     smoother ^(no time limit on the sign-in step^). Use it if you prefer:
echo       %USERPROFILE%\\gip\\claude-bedrock.cmd
echo.
echo Tip: add that folder to your PATH so you can just run 'claude-bedrock'.
echo.
echo To use a non-default profile, set AWS_PROFILE before launching:
echo   set AWS_PROFILE=^<profile-name^>
echo   %USERPROFILE%\\gip\\claude-bedrock.cmd
echo.
REM F-027: Successful headless installs must explicitly clear ERRORLEVEL.
exit /b 0
"""
        else:
            installer_content += f"""
echo.
echo ======================================
REM ^^! prints a literal ! because this script runs with delayed expansion enabled.
echo Installation complete^^!
echo ======================================
echo.
echo Available profiles:
echo   - {profile.name}
echo.
echo ^>^>^> Start Claude Code:
echo       claude
echo.
echo     Authentication is handled automatically via your configured credential
echo     process. Simply run 'claude' to start.
echo.
echo To use a non-default profile, set AWS_PROFILE before launching:
echo   set AWS_PROFILE=^<profile-name^>
echo   claude
echo.
REM F-027: Successful headless installs must explicitly clear ERRORLEVEL.
exit /b 0
"""

        installer_path = output_dir / "install.bat"
        with open(installer_path, "w", encoding="utf-8") as f:
            f.write(installer_content)

        # Note: chmod not needed on Windows batch files
        return installer_path

    def _create_documentation(self, output_dir: Path, profile, timestamp: str):
        """Create user documentation."""
        readme_content = f"""# Claude Code Authentication Setup

## Quick Start

### macOS/Linux

1. Install `unzip` on Linux if needed:
   ```bash
   sudo apt-get update && sudo apt-get install -y unzip  # Debian/Ubuntu
   sudo dnf install -y unzip                             # Fedora/RHEL/Amazon Linux 2023
   sudo yum install -y unzip                             # Amazon Linux 2/RHEL 7
   ```

2. Extract the package:
   ```bash
   unzip gip-package-*.zip
   cd gip-package
   ```

3. Run the installer:
   ```bash
   chmod +x install.sh && ./install.sh
   ```

4. Use the AWS profile:
   ```bash
   export AWS_PROFILE=gip
   aws sts get-caller-identity
   ```

### Windows

#### Step 1: Download the Package
```powershell
# Use the Invoke-WebRequest command provided by your IT administrator
Invoke-WebRequest -Uri "URL_PROVIDED" -OutFile "gip-package.zip"
```

#### Step 2: Extract the Package

**Option A: Using Windows Explorer**
1. Right-click on `gip-package.zip`
2. Select "Extract All..."
3. Choose a destination folder
4. Click "Extract"

**Option B: Using PowerShell**
```powershell
# Extract to current directory
Expand-Archive -Path "gip-package.zip" -DestinationPath "gip-package"

# Navigate to the extracted folder
cd gip-package
```

**Option C: Using Command Prompt**
```cmd
# If you have tar available (Windows 10 1803+)
tar -xf gip-package.zip

# Or use PowerShell from Command Prompt
powershell -command "Expand-Archive -Path 'gip-package.zip' -DestinationPath 'gip-package'"

cd gip-package
```

#### Step 3: Run the Installer
```cmd
install.bat
```

The installer will:
- Check for AWS CLI installation
- Copy authentication tools to `%USERPROFILE%\\gip`
- Configure the AWS profile "gip"
- Test the authentication

#### Step 4: Use Claude Code
```cmd
# Set the AWS profile
set AWS_PROFILE=gip

# Verify authentication works
aws sts get-caller-identity

# Your browser will open automatically for authentication if needed
```

For PowerShell users:
```powershell
$env:AWS_PROFILE = "gip"
aws sts get-caller-identity
```

## What This Does

- Installs the Claude Code authentication tools
- Configures your AWS CLI to use {profile.provider_domain} for authentication
- Sets up automatic credential refresh via your browser

## Requirements

- Python 3.8 or later
- AWS CLI v2
- pip3

## Troubleshooting

### macOS Keychain Access Popup
On first use, macOS will ask for permission to access the keychain. This is normal and required for \
secure credential storage. Click "Always Allow" to avoid repeated prompts.

### Authentication Issues
If you encounter issues with authentication:
- Ensure you're assigned to the Claude Code application in your identity provider
- Check that port 8400 is available for the callback
- Contact your IT administrator for help

### Authentication Behavior

The system handles authentication automatically:
- Your browser will open when authentication is needed
- Credentials are cached securely to avoid repeated logins
- Bad credentials are automatically cleared and re-authenticated

To manually clear cached credentials (if needed):
```bash
~/gip/credential-process --clear-cache
```

This will force re-authentication on your next AWS command.

### Browser doesn't open
Check that you're not in an SSH session. The browser needs to open on your local machine.

## Support

Contact your IT administrator for help.

Configuration Details:
- Organization: {profile.provider_domain}
- Region: {profile.aws_region}
- Package Version: {timestamp}"""  # nosec B608 — README template, not SQL

        # Add analytics information if enabled
        if profile.monitoring_enabled and getattr(profile, "analytics_enabled", True):
            analytics_section = f"""

## Analytics Dashboard

Your organization has enabled advanced analytics for Claude Code usage. You can access detailed metrics \
and reports through AWS Athena.

To view analytics:
1. Open the AWS Console in region {profile.aws_region}
2. Navigate to Athena
3. Select the analytics workgroup and database
4. Run pre-built queries or create custom reports

Available metrics include:
- Token usage by user
- Cost allocation
- Model usage patterns
- Activity trends
"""
            readme_content += analytics_section

        readme_content += "\n"

        with open(output_dir / "README.md", "w", encoding="utf-8") as f:
            f.write(readme_content)

    def _create_claude_settings(
        self,
        output_dir: Path,
        profile: object,
        include_coauthored_by: bool = True,
        profile_name: str = "gip",
        otel_resource_attributes: str | None = None,
        is_idc_zero_binary: bool = False,
        settings_version: str | None = None,
    ) -> None:
        """Create Claude Code settings.json with Bedrock and optional monitoring configuration."""
        console = Console()

        try:
            # Create gip-settings directory (visible, not hidden)
            claude_dir = output_dir / "gip-settings"
            claude_dir.mkdir(exist_ok=True)

            # Start with basic settings required for Bedrock
            settings = {
                "env": {
                    "CLAUDE_CODE_USE_BEDROCK": "1",
                    # AWS_REGION determines which regional Bedrock endpoint the SDK uses.
                    "AWS_REGION": self._get_bedrock_region_for_profile(profile),
                    # AWS_PROFILE is used by both AWS SDK and otel-helper
                    "AWS_PROFILE": profile_name,
                }
            }
            # IDC zero-binary: AWS_PROFILE + ~/.aws/config SSO profile handle creds
            # directly — no credential-process binary exists to reference.
            if not is_idc_zero_binary:
                # The __CREDENTIAL_PROCESS_PATH__ placeholder is replaced by
                # install.sh/install.bat with the actual binary path at install time.
                settings["env"]["AWS_CREDENTIAL_PROCESS"] = f"__CREDENTIAL_PROCESS_PATH__ --profile {profile_name}"

            # Add includeCoAuthoredBy setting if user wants to disable it (Claude Code defaults to true)
            # Only add the field if the user wants it disabled
            if not include_coauthored_by:
                settings["includeCoAuthoredBy"] = False

            # For IDC, disable the EC2 instance-metadata credential provider. If a
            # credential refresh ever fails, the AWS SDK credential chain would
            # otherwise fall through to the instance role on EC2 — silently running
            # Claude Code as the wrong identity (breaking cost attribution and quota,
            # and masking the failure). With IMDS disabled, a refresh failure surfaces
            # as a clear credentials error instead. IDC identity comes solely from the
            # credential-process binary, so nothing legitimately needs IMDS here.
            if profile.effective_auth_type == "idc":
                settings["env"]["AWS_EC2_METADATA_DISABLED"] = "true"

            # Credential refresh on expiry. AWS_CREDENTIAL_PROCESS (set above) is
            # ignored by Claude Code once a hook below is set, and on its own it is
            # resolved only at startup — so a long session would retry stale
            # credentials (and on EC2 the SDK chain falls back to the instance role —
            # wrong identity). Claude Code offers two hooks, which behave differently
            # and (verified empirically) are COMPLEMENTARY when both are set:
            #
            #   awsCredentialExport — output captured SILENTLY as credential JSON; the
            #                         primary resolver, re-invoked automatically ~5 min
            #                         before the Expiration we emit (Claude Code
            #                         >= 2.1.176; flat credential_process JSON accepted
            #                         >= 2.1.181). Drives the silent hourly STS refresh.
            #   awsAuthRefresh      — output is DISPLAYED to the user; fires on the FIRST
            #                         credential failure (not on every retry). This is the
            #                         only channel that surfaces our sign-in message —
            #                         awsCredentialExport discards stderr.
            #
            # IDC needs BOTH:
            #   - awsCredentialExport for the silent ~hourly role-credential refresh
            #     (SSO session still valid -> re-mint via STS, no browser); and
            #   - awsAuthRefresh (--login) so that when there is NO valid SSO session,
            #     the device-auth flow runs IN-SESSION: the credential-process --login
            #     gate now polls device authorization and surfaces the verification
            #     URL/code live (Claude Code streams the hook's stderr), so the ~8h
            #     SSO re-login can complete from inside a running `claude`. On headless
            #     hosts with discarded stderr (the silent awsCredentialExport path) the
            #     binary still fails fast rather than hanging. NOTE: Claude Code does
            #     not auto-invoke awsAuthRefresh on a 401 yet (anthropics/claude-code
            #     #67529) — until it does, the claude-bedrock launcher front-runs the
            #     sign-in so the user is authenticated before the first prompt.
            #
            # Non-IDC (OIDC) session profiles keep just awsAuthRefresh — their refresh
            # can be interactive (browser), which is exactly what that hook is for.
            if profile.effective_auth_type == "idc" and not is_idc_zero_binary:
                # IDC+quota path: credential-process binary handles STS refresh + quota.
                settings["awsCredentialExport"] = f"__CREDENTIAL_PROCESS_PATH__ --profile {profile_name}"
                settings["awsAuthRefresh"] = f"__CREDENTIAL_PROCESS_PATH__ --login --profile {profile_name}"
            elif profile.effective_auth_type == "idc" and is_idc_zero_binary:
                # IDC zero-binary: no credential-process. AWS SDK resolves via the
                # gip SSO profile in ~/.aws/config written by install.sh.
                # Session expiry is handled out-of-band via `aws sso login`.
                pass
            elif profile.credential_storage == "session":
                settings["awsAuthRefresh"] = f"__CREDENTIAL_PROCESS_PATH__ --profile {profile_name}"

            # Add ANTHROPIC_MODEL if user selected a model during init.
            # For managed-settings: only write when lock_default_model is True (admin opt-in).
            # For user-scope settings: always write (users can override via /model).
            settings_target = getattr(profile, "settings_target", "user")
            lock_model = getattr(profile, "lock_default_model", False)
            should_write_model = (
                hasattr(profile, "selected_model")
                and profile.selected_model
                and (settings_target != "managed" or lock_model)
            )
            if should_write_model:
                from governed_inference_platform.models import get_claude_code_alias, resolve_model_for_tier

                # Use a Claude Code alias (sonnet/opus/opusplan/haiku) so ANTHROPIC_MODEL
                # feeds through the DEFAULT_*_MODEL resolution chain for CRIS-aware routing.
                # model_alias is set during gip init (e.g. opus vs opusplan for Opus models).
                alias = getattr(profile, "model_alias", None) or get_claude_code_alias(profile.selected_model)
                settings["env"]["ANTHROPIC_MODEL"] = alias or profile.selected_model

                # Set all model tier env vars using the CRIS prefix from init.
                # Claude Code uses these to resolve the correct CRIS-prefixed
                # models for each tier (small/fast, default sonnet/opus/haiku).
                # This ensures all tiers respect the admin's routing geography
                # choice and works correctly with model aliases like 'opus', 'sonnet', 'haiku', 'opusplan'.
                cris_prefix = getattr(profile, "cross_region_profile", None) or "us"

                haiku_model = resolve_model_for_tier("haiku", cris_prefix)
                sonnet_model = resolve_model_for_tier("sonnet", cris_prefix)
                opus_model = resolve_model_for_tier("opus", cris_prefix)

                if haiku_model:
                    settings["env"]["ANTHROPIC_SMALL_FAST_MODEL"] = haiku_model
                    settings["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = haiku_model
                if sonnet_model:
                    settings["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] = sonnet_model
                if opus_model:
                    settings["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"] = opus_model

            # Application Inference Profile (AIP) overrides provide
            # admin-managed team/cost-center attribution. They replace CRIS
            # tier defaults, while ANTHROPIC_MODEL keeps its alias so Claude
            # Code resolves through the DEFAULT_*_MODEL chain.
            aip_overrides = {
                "inference_profile_haiku_arn": ("ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL"),
                "inference_profile_sonnet_arn": ("ANTHROPIC_DEFAULT_SONNET_MODEL",),
                "inference_profile_opus_arn": ("ANTHROPIC_DEFAULT_OPUS_MODEL",),
            }
            for profile_field, env_vars in aip_overrides.items():
                aip_arn = getattr(profile, profile_field, None)
                if aip_arn:
                    for env_var in env_vars:
                        settings["env"][env_var] = aip_arn

            # If monitoring is enabled, add telemetry configuration
            if profile.monitoring_enabled:
                _monitoring_mode = getattr(profile, "monitoring_mode", "central")

                # Sidecar mode: Claude Code always sends to the local otelcol on
                # localhost:4318. There is no central monitoring stack to read a
                # CollectorEndpoint from, so resolve the endpoint up front and skip
                # the profile/CloudFormation/prompt resolution below (which only
                # applies to central mode). Doing this here — rather than as an
                # override after resolution — is what actually configures telemetry
                # for sidecar packages: real sidecar deploys have no saved
                # otel_collector_endpoint, so the old post-resolution override never
                # ran and telemetry was silently left unconfigured.
                if _monitoring_mode == "sidecar":
                    endpoint = "http://localhost:4318"
                    verified_endpoint = None
                else:
                    # Central mode: try profile first (saved by gip deploy), then
                    # fall back to CloudFormation query.
                    endpoint = getattr(profile, "otel_collector_endpoint", None)
                    verified_endpoint = getattr(profile, "otel_verified_collector_endpoint", None)

                if not endpoint and _monitoring_mode != "sidecar":
                    verified_endpoint = None
                    # Fall back to reading from CloudFormation stack outputs
                    # Try multiple possible stack name patterns
                    possible_stacks = [
                        profile.stack_names.get("monitoring"),
                        f"{profile.identity_pool_name}-otel-collector"
                        if hasattr(profile, "identity_pool_name") and profile.identity_pool_name
                        else None,
                        f"{profile.stack_names.get('auth', '')}-otel-collector"
                        if profile.stack_names.get("auth")
                        else None,
                    ]
                    # Remove None/empty entries
                    possible_stacks = [s for s in possible_stacks if s]

                    for monitoring_stack in possible_stacks:
                        cmd = [
                            "aws",
                            "cloudformation",
                            "describe-stacks",
                            "--stack-name",
                            monitoring_stack,
                            "--region",
                            profile.aws_region,
                            "--query",
                            "Stacks[0].Outputs",
                            "--output",
                            "json",
                        ]

                        result = run_checked(cmd, capture_output=True, text=True)  # nosec B603 — fixed argv, no shell
                        if result.returncode == 0:
                            try:
                                outputs = json.loads(result.stdout)
                                for output in outputs:
                                    if output["OutputKey"] == "CollectorEndpoint":
                                        endpoint = output["OutputValue"]
                                    elif output["OutputKey"] == "VerifiedCollectorEndpoint":
                                        verified_endpoint = output["OutputValue"]
                            except (json.JSONDecodeError, TypeError):
                                pass

                        if endpoint:
                            # Save to profile for next time
                            profile.otel_collector_endpoint = endpoint
                            profile.otel_verified_collector_endpoint = (
                                verified_endpoint if verified_endpoint == endpoint else None
                            )
                            try:
                                from governed_inference_platform.config import Config

                                config = Config.load()
                                config.save_profile(profile)
                                console.print(
                                    f"[dim]Found endpoint from stack '{monitoring_stack}', saved to profile[/dim]"
                                )
                            except Exception:
                                pass
                            break

                if not endpoint:
                    # Monitoring stack not deployed or endpoint not found
                    console.print(
                        "[yellow]Warning: No OTel collector endpoint found in profile or CloudFormation.[/yellow]"
                    )
                    console.print(
                        "[yellow]Run 'gip deploy' to deploy the monitoring stack, or enter the endpoint manually.[/yellow]"
                    )
                    try:
                        import questionary

                        endpoint = questionary.text(
                            "OTel collector endpoint URL (leave blank to skip telemetry):",
                            default="",
                        ).ask()
                        if endpoint:
                            endpoint = endpoint.strip()
                        if endpoint:
                            # Save to profile so this is never asked again
                            profile.otel_collector_endpoint = endpoint
                            try:
                                from governed_inference_platform.config import Config

                                config = Config.load()
                                config.save_profile(profile)
                                console.print("[dim]Saved endpoint to profile[/dim]")
                            except Exception:
                                pass
                    except Exception:
                        pass

                if endpoint:
                    # Add monitoring configuration. In sidecar mode `endpoint` was
                    # already resolved to http://localhost:4318 above; in central
                    # mode it is the ALB address from the profile/CloudFormation.
                    resource_attrs = otel_resource_attributes or (
                        "department=default,team.id=default,cost_center=default,organization=default,project=default"
                    )
                    # Keep the package timestamp available for local diagnostics.
                    # Central collectors discard caller-supplied resource identity.
                    if settings_version:
                        resource_attrs += f",settings_version={settings_version}"

                    settings["env"].update(
                        {
                            "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
                            "OTEL_METRICS_EXPORTER": "otlp",
                            # The collector defines only a metrics pipeline, so /v1/logs is
                            # dropped (4xx). Explicitly disable logs export rather than deleting
                            # the key so a global/user default can't re-enable "otlp".
                            "OTEL_LOGS_EXPORTER": "none",
                            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
                            "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint,
                            "OTEL_RESOURCE_ATTRIBUTES": resource_attrs,
                        }
                    )

                    # The identity helper is safe only on the local sidecar path or on
                    # central OIDC ingress protected by TLS. Central IDC/no-auth routes
                    # and HTTP-only collectors are aggregate-only, so attaching the helper
                    # would expose credentials or imply attribution the collector cannot verify.
                    from governed_inference_platform.cli.utils.cowork_3p import (
                        telemetry_endpoint_allows_credentials,
                    )

                    _auth_type = getattr(profile, "effective_auth_type", profile.auth_type)
                    _is_idc = _auth_type == "idc"
                    _idc_zero_binary = _is_idc and not bool(getattr(profile, "quota_api_endpoint", None))
                    _identity_helper_allowed = (_monitoring_mode == "sidecar" and _auth_type in ("oidc", "idc")) or (
                        _auth_type == "oidc"
                        and verified_endpoint == endpoint
                        and telemetry_endpoint_allows_credentials(endpoint)
                    )
                    if not _idc_zero_binary and _identity_helper_allowed:
                        # Pass the profile explicitly (same as AWS_CREDENTIAL_PROCESS /
                        # awsAuthRefresh above) so the helper serves THIS profile even
                        # when AWS_PROFILE in the helper's environment points elsewhere.
                        settings["otelHeadersHelper"] = f"__OTEL_HELPER_PATH__ --profile {profile_name}"
                    elif _monitoring_mode != "sidecar" or _auth_type == "none":
                        console.print(
                            "[yellow]Central telemetry is aggregate-only for this auth/transport path; "
                            "the per-user OTEL identity helper and telemetry-derived quota are disabled.[/yellow]"
                        )

                    is_https = endpoint.startswith("https://")
                    console.print(f"[dim]Added monitoring with {'HTTPS' if is_https else 'HTTP'} endpoint[/dim]")
                    if not is_https and _monitoring_mode != "sidecar":
                        console.print(
                            "[dim]WARNING: Using HTTP endpoint - consider enabling HTTPS for production[/dim]"
                        )
                else:
                    console.print("[red]ERROR: Monitoring enabled but no OTel endpoint configured.[/red]")
                    console.print("[red]Run 'gip deploy' first or set otel_collector_endpoint in the profile.[/red]")

            # Determine output filename based on settings_target
            settings_target = getattr(profile, "settings_target", "user")

            if getattr(profile, "gateway_enabled", False):
                console.print(
                    "[yellow]Legacy gateway profile fields were ignored. Deliver Claude Code and Claude Desktop "
                    "managed configuration exactly as documented by the pinned AWS Samples gateway; GIP does "
                    "not generate or reinterpret those client keys.[/yellow]"
                )

            if settings_target == "managed":
                settings_filename = "managed-settings.json"
                console.print("[dim]Writing to managed-settings.json (OS-level enforcement)[/dim]")
            else:
                settings_filename = "settings.json"

            settings_path = claude_dir / settings_filename
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2)

            console.print("[dim]Created Claude Code settings for Bedrock configuration[/dim]")

        except Exception as e:
            console.print(f"[yellow]Warning: Could not create Claude Code settings: {e}[/yellow]")

    def _generate_cowork_3p_mdm_config(
        self,
        output_dir: Path,
        profile,
        profile_name: str = "gip",
    ) -> None:
        """Generate Claude Cowork 3P MDM configuration files.

        Delegates to shared utilities in cli/utils/cowork_3p.py to ensure
        consistency with the standalone 'gip cowork generate' command.
        """
        from governed_inference_platform.cli.utils.cowork_3p import (
            add_monitoring_config,
            add_websearch_mcp_config,
            build_mdm_config,
            derive_model_aliases,
            generate_all,
        )

        console = Console()

        try:
            bedrock_region = self._get_bedrock_region_for_profile(profile)
            model_aliases = derive_model_aliases()

            mdm_config = build_mdm_config(
                bedrock_region=bedrock_region,
                model_aliases=model_aliases,
                profile_name=profile_name,
                extra_keys=profile.cowork_3p_extra_keys or None,
                credential_mode=getattr(profile, "cowork_credential_mode", "helper"),
                credential_helper_ttl_sec=getattr(profile, "cowork_credential_helper_ttl_sec", 3500),
            )

            # Beta features (per-feature managed configuration keys)
            if getattr(profile, "cowork_chat_tab_enabled", False):
                mdm_config["chatTabEnabled"] = True
            if getattr(profile, "cowork_chat_advanced_file_analysis", False):
                mdm_config["chatAdvancedFileAnalysisEnabled"] = True
            if getattr(profile, "cowork_inference_session_lifetime_sec", None):
                mdm_config["inferenceSessionLifetimeSec"] = profile.cowork_inference_session_lifetime_sec

            add_monitoring_config(mdm_config, profile, console)
            add_websearch_mcp_config(mdm_config, profile, console)
            generate_all(output_dir, mdm_config, console)

        except Exception as e:
            console.print(f"[yellow]Warning: Could not generate CoWork 3P config: {e}[/yellow]")
