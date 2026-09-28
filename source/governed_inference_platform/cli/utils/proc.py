# ABOUTME: Audited subprocess choke point for all CLI commands
# ABOUTME: Single validation site enforcing shell=False and list argv

"""Audited subprocess execution helpers.

Every dynamic subprocess invocation in the CLI goes through this module so
there is exactly one place to audit for command-injection risk. The helpers
enforce the two properties that make these calls safe:

* ``shell`` is never true — argv is passed directly to the OS with no shell
  interpretation, so metacharacters in arguments are inert.
* argv must be a sequence (list/tuple), never a string — each element is a
  single exec argument.

Callers pass argv lists built from static program names (aws, go, docker,
git, ...) or gip-managed binary paths plus validated arguments; keyword
arguments are forwarded to :mod:`subprocess` unchanged.
"""

import subprocess


def _validate(argv, kwargs):
    """Reject shell execution and string commands at the single choke point."""
    if kwargs.get("shell"):
        raise ValueError("proc helpers forbid shell=True; pass a list argv")
    if isinstance(argv, (str, bytes)):
        raise TypeError("proc helpers require a list argv, not a string")


def run_checked(argv, **kwargs):
    """Audited wrapper for :func:`subprocess.run` (shell=False, list argv)."""
    _validate(argv, kwargs)
    call_args = [list(argv)]
    return subprocess.run(*call_args, **kwargs)  # nosec B603 — audited choke point: shell=False enforced, list argv validated above


def popen_checked(argv, **kwargs):
    """Audited wrapper for :class:`subprocess.Popen` (shell=False, list argv)."""
    _validate(argv, kwargs)
    call_args = [list(argv)]
    return subprocess.Popen(*call_args, **kwargs)  # nosec B603 — audited choke point: shell=False enforced, list argv validated above
