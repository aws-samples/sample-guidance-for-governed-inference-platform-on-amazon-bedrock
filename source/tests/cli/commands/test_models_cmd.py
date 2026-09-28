# ABOUTME: Tests for gip models command registration and CLI surface
# ABOUTME: Ensures 'models check' is registered, --help works, and bare 'models' shows subcommands

"""CLI registration tests for the models commands.

Drift-detection logic and exit codes are covered in tests/test_catalog_check.py;
this file follows the namespace-command test pattern (see test_namespace_commands.py).
"""

import pytest
from cleo.testers.application_tester import ApplicationTester

from governed_inference_platform.cli import create_application


class TestModelsCommandRegistration:
    @pytest.fixture
    def app_tester(self):
        return ApplicationTester(create_application())

    def test_models_check_command_registered(self):
        app = create_application()
        assert app.find("models check") is not None

    def test_models_check_help_works(self, app_tester):
        assert app_tester.execute("models check --help") == 0
        output = app_tester.io.fetch_output()
        assert "drift" in output.lower()
        assert "--region" in output
        assert "--json" in output

    def test_bare_models_namespace_shows_subcommands(self, app_tester):
        assert app_tester.execute("models") == 0
        output = app_tester.io.fetch_output()
        assert "does not exist" not in output
        assert "models check" in output

    def test_models_check_has_expected_options(self):
        app = create_application()
        command = app.find("models check")
        option_names = {opt.name for opt in command.definition.options}
        assert {"region", "json"} <= option_names
