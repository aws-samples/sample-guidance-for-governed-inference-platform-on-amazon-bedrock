"""Thin wrappers around cleo's testers used by CLI tests.

`run()` is a drop-in alias for cleo's `execute()` so tests drive commands
through a single, clearly-named entry point.
"""

from cleo.testers.application_tester import ApplicationTester
from cleo.testers.command_tester import CommandTester


class CliTester(CommandTester):
    """CommandTester with a `run` alias for `execute`."""

    def run(self, args: str = "", **kwargs) -> int:
        return self.execute(args, **kwargs)


class CliAppTester(ApplicationTester):
    """ApplicationTester with a `run` alias for `execute`."""

    def run(self, args: str = "", **kwargs) -> int:
        return self.execute(args, **kwargs)
