"""Audited subprocess chokepoint for tests.

All scanner-flagged test subprocess invocations route through `run_cmd`, which
delegates to the CLI's own audited choke point
(`governed_inference_platform.cli.utils.proc.run_checked`) so the package has a
single site that enforces list-form argv and `shell=False`. Commands under test
are repo-built binaries/scripts with test-controlled arguments.
"""

from governed_inference_platform.cli.utils.proc import run_checked


def run_cmd(argv, **kwargs):
    """Run a fixed-argv command with the shell disabled.

    Args:
        argv: Command as a list of strings (never a shell string).
        **kwargs: Passed through to subprocess.run (``shell=True`` is rejected).

    Returns:
        subprocess.CompletedProcess
    """
    return run_checked(argv, **kwargs)
