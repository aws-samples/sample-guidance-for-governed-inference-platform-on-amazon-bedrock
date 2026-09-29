# ABOUTME: Tests for `gip console` — localhost visual deployment console (server + command)
# ABOUTME: Covers bootstrap catalog shape, validate, profile parity with --from-file, deploy job, auth token, localhost bind

"""Tests for the `gip console` command and its stdlib HTTP backend.

The console is a pure front-end over the existing answers-file machinery:
- /api/validate and /api/profile go through init_answers.build_config_from_answers
  and InitCommand._save_configuration (the exact `init --from-file` code path) —
  verified here by byte-comparing the resulting profile JSON against one
  produced by `gip init --from-file`.
- /api/deploy drives DeployCommand; the runner is mocked in these tests
  (no real AWS calls), mirroring how test_deploy_*.py mocks the engine.
"""

import http.client
import json
import re
import threading
import time
from unittest.mock import patch

import pytest
import yaml

from governed_inference_platform.cli.commands.console import ConsoleCommand
from governed_inference_platform.cli.commands.deploy import VALID_STACKS
from governed_inference_platform.cli.commands.init import COMMON_REGIONS, InitCommand
from governed_inference_platform.cli.commands.init_answers import (
    DEFAULT_MODEL_KEY,
    VALID_AUTH_TYPES,
    VALID_PROVIDER_TYPES,
)
from governed_inference_platform.config import Config
from governed_inference_platform.console import server as console_server
from governed_inference_platform.console.server import MAX_BODY_BYTES, create_console_server

EXPECTED_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)

MINIMAL_ANSWERS = {
    "okta": {"domain": "company.okta.com", "client_id": "0oa0000000000000000"},
}

MINIMAL_YAML = """
okta:
  domain: company.okta.com
  client_id: 0oa0000000000000000
"""


@pytest.fixture
def config_paths(tmp_path):
    """Patch Config storage paths into tmp_path and stub out AWS stack checks."""
    config_dir = tmp_path / ".gip"
    config_dir.mkdir()
    profiles_dir = config_dir / "profiles"
    profiles_dir.mkdir()
    config_file = config_dir / "config.json"
    config_file.write_text(json.dumps({"schema_version": "2.0", "active_profile": None}))

    with (
        patch.object(Config, "CONFIG_DIR", config_dir),
        patch.object(Config, "CONFIG_FILE", config_file),
        patch.object(Config, "PROFILES_DIR", profiles_dir),
        patch.object(InitCommand, "_stack_exists", side_effect=Exception("no creds")),
    ):
        yield profiles_dir


@pytest.fixture
def server():
    """Start a console server on an ephemeral 127.0.0.1 port."""
    srv = create_console_server(port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _request(srv, method, path, body=None, token="__default__", extra_headers=None):  # nosec B107
    """One-shot HTTP request against the test server. token=None omits the header."""
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    headers = {"Content-Type": "application/json"}
    if token == "__default__":  # nosec B105 -- sentinel value, not a credential
        token = srv.token
    if token is not None:
        headers["X-Gip-Token"] = token
    if extra_headers:
        headers.update(extra_headers)
    payload = json.dumps(body) if isinstance(body, dict) else body
    conn.request(method, path, body=payload, headers=headers)
    response = conn.getresponse()
    raw = response.read()
    conn.close()
    content_type = response.getheader("Content-Type") or ""
    parsed = json.loads(raw) if "json" in content_type else raw.decode("utf-8")
    return response.status, parsed, dict(response.getheaders())


def _wait_for_deploy(srv, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, body, _ = _request(srv, "GET", "/api/deploy/status")
        assert status == 200
        if not body["active"] and body["started_at"]:
            return body
        time.sleep(0.05)
    pytest.fail("deploy job did not finish in time")


class TestServerSecurity:
    def test_binds_localhost_only(self, server):
        assert server.server_address[0] == "127.0.0.1"

    def test_root_serves_spa_with_embedded_token(self, server):
        status, html, _ = _request(server, "GET", "/", token=None)
        assert status == 200
        assert "Deployment Console" in html
        assert 'id="stepnav"' in html  # wizard markup
        assert server.token in html  # session token injected
        assert "__GIP_SESSION_TOKEN__" not in html

    def test_api_rejects_missing_token(self, server):
        status, body, _ = _request(server, "GET", "/api/bootstrap", token=None)
        assert status == 401
        assert "X-Gip-Token" in body["error"]

    def test_api_rejects_wrong_token(self, server):
        status, _, _ = _request(server, "GET", "/api/bootstrap", token="wrong-token")  # nosec B106
        assert status == 401

    def test_post_rejects_wrong_token(self, server):
        status, _, _ = _request(server, "POST", "/api/validate", body={"answers": {}}, token="wrong-token")  # nosec B106  # fmt: skip
        assert status == 401

    def test_rejects_foreign_host_header(self, server):
        """DNS-rebinding guard: only 127.0.0.1/localhost Host headers are served."""
        status, body, _ = _request(server, "GET", "/", token=None, extra_headers={"Host": "evil.example.com"})
        assert status == 403

    def test_no_cors_headers_emitted(self, server):
        _, _, headers = _request(server, "GET", "/", token=None)
        assert "Access-Control-Allow-Origin" not in headers

    @pytest.mark.parametrize("path, token", [("/", None), ("/api/bootstrap", "__default__"), ("/api/nope", None)])
    def test_anti_framing_and_sniffing_headers_on_every_response(self, server, path, token):
        """The console can start deploys, so no response may be framed or MIME-sniffed."""
        _, _, headers = _request(server, "GET", path, token=token)
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Content-Security-Policy"] == EXPECTED_CSP

    @pytest.mark.parametrize("method", ["HEAD", "PUT", "DELETE"])
    def test_stdlib_error_responses_carry_security_headers(self, server, method):
        """send_error() replies (501 for methods without a do_* handler) are not exempt."""
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        conn.request(method, "/")
        response = conn.getresponse()
        response.read()
        conn.close()
        assert response.status == 501
        assert response.getheader("X-Frame-Options") == "DENY"
        assert response.getheader("X-Content-Type-Options") == "nosniff"
        assert response.getheader("Content-Security-Policy") == EXPECTED_CSP

    def test_security_headers_sent_exactly_once(self, server):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        conn.request("GET", "/")
        response = conn.getresponse()
        response.read()
        conn.close()
        for name in ("X-Frame-Options", "X-Content-Type-Options", "Content-Security-Policy", "Referrer-Policy"):
            assert len(response.msg.get_all(name)) == 1, name

    def test_spa_needs_nothing_the_csp_blocks(self, server):
        """The CSP allows only inline script/style and same-origin fetch; the SPA must stay within that."""
        _, html, _ = _request(server, "GET", "/", token=None)
        lowered = html.lower()
        for blocked in ("<script src", "<link", "<iframe", "<form", "<img", "@import", "eval("):
            assert blocked not in lowered, blocked
        assert not re.search(r"(?<![A-Za-z])url\(", html)  # CSS url(); createObjectURL( is fine
        assert re.findall(r"fetch\((\S+?)[,)]", html) == ["path", '"/api/answers.yaml"']

    def test_rejects_host_header_with_wrong_port(self, server):
        """DNS-rebinding guard is port-aware: a local name on another port is refused."""
        other_port = server.server_address[1] + 1
        for host in (f"127.0.0.1:{other_port}", f"localhost:{other_port}", "localhost", "127.0.0.1"):
            status, _, _ = _request(server, "GET", "/", token=None, extra_headers={"Host": host})
            assert status == 403, host

    def test_bare_host_accepted_only_when_serving_port_80(self, server, monkeypatch):
        """Browsers omit :80, so only a port-80 console accepts a Host without a port."""
        real_port = server.server_address[1]
        monkeypatch.setattr(server, "server_address", ("127.0.0.1", 80))

        def status_for(host):
            conn = http.client.HTTPConnection("127.0.0.1", real_port, timeout=10)
            conn.request("GET", "/", headers={"Host": host})
            status = conn.getresponse().status
            conn.close()
            return status

        for host in ("localhost", "127.0.0.1", "localhost:80", "127.0.0.1:80"):
            assert status_for(host) == 200, host
        assert status_for(f"localhost:{real_port}") == 403

    def test_accepts_localhost_name_on_own_port(self, server):
        port = server.server_address[1]
        status, _, _ = _request(server, "GET", "/", token=None, extra_headers={"Host": f"LocalHost:{port}"})
        assert status == 200

    def test_token_compared_in_constant_time(self, server):
        with patch.object(console_server.hmac, "compare_digest", wraps=console_server.hmac.compare_digest) as spy:
            status, _, _ = _request(server, "GET", "/api/bootstrap")
        assert status == 200
        spy.assert_called()

    def test_non_ascii_token_rejected_not_crashed(self, server):
        status, _, _ = _request(server, "GET", "/api/bootstrap", token="caf\u00e9")  # nosec B106
        assert status == 401

    def test_body_size_limit(self, server):
        """Oversized POST bodies are refused (413) without being read."""
        oversized = '{"answers": {"x": "' + "a" * (MAX_BODY_BYTES + 100) + '"}}'
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        try:
            conn.request(
                "POST",
                "/api/validate",
                body=oversized,
                headers={"Content-Type": "application/json", "X-Gip-Token": server.token},
            )
            status = conn.getresponse().status
        except (BrokenPipeError, ConnectionResetError):
            # Server rejected and closed the socket before the body finished
            # uploading — the request was refused either way.
            return
        finally:
            conn.close()
        assert status == 413

    def test_unknown_routes_404(self, server):
        status, _, _ = _request(server, "GET", "/api/nope")
        assert status == 404
        status, _, _ = _request(server, "POST", "/api/nope", body={})
        assert status == 404


class TestBootstrapCatalog:
    def test_catalog_shape_matches_init_sources(self, server):
        status, body, _ = _request(server, "GET", "/api/bootstrap")
        assert status == 200
        assert body["auth_types"] == list(VALID_AUTH_TYPES)
        assert body["provider_types"] == list(VALID_PROVIDER_TYPES)
        assert body["regions"] == list(COMMON_REGIONS)
        assert body["valid_stacks"] == list(VALID_STACKS)
        assert body["defaults"]["model_key"] == DEFAULT_MODEL_KEY
        # Model catalog carries per-CRIS-profile IDs and regions
        default_model = body["models"][DEFAULT_MODEL_KEY]
        assert default_model["name"]
        assert "us" in default_model["profiles"]
        us_profile = default_model["profiles"]["us"]
        assert us_profile["model_id"].startswith("us.")
        assert us_profile["destination_regions"]
        # Module cards have everything the wizard grid needs
        assert body["modules"], "module catalog must not be empty"
        for module in body["modules"]:
            assert module["id"] and module["label"] and module["description"]
            assert "answers_path" in module and "requires" in module
        memory = next(m for m in body["modules"] if m["id"] == "memory")
        assert any(r.get("module") == "web_search" for r in memory["requires"])


class TestValidate:
    def test_happy_path(self, server, config_paths):
        status, body, _ = _request(server, "POST", "/api/validate", body={"answers": MINIMAL_ANSWERS})
        assert status == 200
        assert body["valid"] is True
        assert body["errors"] == []
        assert "company.okta.com" in body["answers_yaml"]
        assert body["resolved"]["selected_model"]
        assert body["resolved"]["cross_region_profile"] == "us"

    def test_field_level_errors(self, server, config_paths):
        status, body, _ = _request(server, "POST", "/api/validate", body={"answers": {"auth_type": "oidc"}})
        assert status == 200
        assert body["valid"] is False
        fields = {e["field"] for e in body["errors"]}
        assert "okta.domain" in fields
        assert "okta.client_id" in fields

    def test_unknown_key_rejected(self, server, config_paths):
        status, body, _ = _request(server, "POST", "/api/validate", body={"answers": {"bogus_key": 1}})
        assert status == 200
        assert body["valid"] is False
        assert any("unknown key: bogus_key" in e["message"] for e in body["errors"])

    def test_answers_must_be_object(self, server):
        status, _, _ = _request(server, "POST", "/api/validate", body={"answers": "nope"})
        assert status == 400


class TestProfileCreation:
    def test_profile_written_to_config_home(self, server, config_paths):
        status, body, _ = _request(
            server, "POST", "/api/profile", body={"answers": MINIMAL_ANSWERS, "profile_name": "console-prof"}
        )
        assert status == 200
        assert body["ok"] is True
        profile_file = config_paths / "console-prof.json"
        assert profile_file.exists()
        saved = json.loads(profile_file.read_text())
        assert saved["provider_domain"] == "company.okta.com"
        assert saved["client_id"] == "0oa0000000000000000"
        # Active profile set, same as run_from_file via _save_configuration
        assert json.loads((config_paths.parent / "config.json").read_text())["active_profile"] == "console-prof"

    def test_profile_identical_to_init_from_file(self, server, config_paths, tmp_path):
        """The console MUST persist exactly what `gip init --from-file` persists."""
        from tests.support.cli import CliTester

        answers_file = tmp_path / "answers.yaml"
        answers_file.write_text(MINIMAL_YAML)
        tester = CliTester(InitCommand())
        assert tester.run(f"--from-file {answers_file} --profile-name cli-prof") == 0

        status, _, _ = _request(
            server, "POST", "/api/profile", body={"answers": MINIMAL_ANSWERS, "profile_name": "console-prof"}
        )
        assert status == 200

        cli_profile = json.loads((config_paths / "cli-prof.json").read_text())
        console_profile = json.loads((config_paths / "console-prof.json").read_text())
        for volatile in ("name", "created_at", "updated_at"):
            cli_profile.pop(volatile, None)
            console_profile.pop(volatile, None)
        assert console_profile == cli_profile

    def test_invalid_answers_rejected_with_field_errors(self, server, config_paths):
        status, body, _ = _request(
            server, "POST", "/api/profile", body={"answers": {"auth_type": "oidc"}, "profile_name": "bad"}
        )
        assert status == 400
        assert any(e["field"] == "okta.domain" for e in body["errors"])
        assert not (config_paths / "bad.json").exists()

    def test_invalid_profile_name_rejected(self, server, config_paths):
        status, body, _ = _request(
            server, "POST", "/api/profile", body={"answers": MINIMAL_ANSWERS, "profile_name": "bad name!"}
        )
        assert status == 400
        assert "Invalid profile name" in body["error"]

    def test_existing_profile_requires_force(self, server, config_paths):
        payload = {"answers": MINIMAL_ANSWERS, "profile_name": "dupe"}
        status, _, _ = _request(server, "POST", "/api/profile", body=payload)
        assert status == 200
        status, body, _ = _request(server, "POST", "/api/profile", body=payload)
        assert status == 409
        assert "already exists" in body["error"]
        status, _, _ = _request(server, "POST", "/api/profile", body={**payload, "force": True})
        assert status == 200


class TestAnswersDownload:
    def test_404_before_any_answers(self, server):
        status, _, _ = _request(server, "GET", "/api/answers.yaml")
        assert status == 404

    def test_download_after_validate(self, server, config_paths):
        _request(server, "POST", "/api/validate", body={"answers": MINIMAL_ANSWERS})
        status, text, headers = _request(server, "GET", "/api/answers.yaml")
        assert status == 200
        assert "attachment" in headers.get("Content-Disposition", "")
        parsed = yaml.safe_load(text)
        assert parsed["okta"]["domain"] == "company.okta.com"


class TestDeployEndpoint:
    def test_deploy_runs_stacks_via_mocked_engine(self, server, config_paths):
        calls = []

        def fake_run(args, log_write):
            calls.append(args)
            log_write(f"deployed {args}")
            return 0

        with patch.object(console_server, "_run_deploy_command", side_effect=fake_run):
            status, body, _ = _request(
                server, "POST", "/api/deploy", body={"stacks": ["auth", "dashboard"], "dry_run": False}
            )
            assert status == 202
            final = _wait_for_deploy(server)

        assert [s["status"] for s in final["stacks"]] == ["succeeded", "succeeded"]
        assert calls == ["auth", "dashboard"]
        assert any("deployed auth" in e for e in final["events"])

    def test_deploy_all_and_dry_run_flags(self, server, config_paths):
        calls = []

        def fake_run(args, log_write):
            calls.append(args)
            return 0

        with patch.object(console_server, "_run_deploy_command", side_effect=fake_run):
            status, _, _ = _request(
                server, "POST", "/api/deploy", body={"stacks": [], "dry_run": True, "profile": "my-prof"}
            )
            assert status == 202
            _wait_for_deploy(server)

        assert calls == ["--dry-run --profile my-prof"]

    def test_failed_stack_marks_rest_skipped(self, server, config_paths):
        def fake_run(args, log_write):
            return 1 if args.startswith("auth") else 0

        with patch.object(console_server, "_run_deploy_command", side_effect=fake_run):
            status, _, _ = _request(server, "POST", "/api/deploy", body={"stacks": ["auth", "dashboard", "quota"]})
            assert status == 202
            final = _wait_for_deploy(server)

        assert [s["status"] for s in final["stacks"]] == ["failed", "skipped", "skipped"]
        assert final["stacks"][0]["exit_code"] == 1

    def test_unknown_stack_rejected(self, server):
        status, body, _ = _request(server, "POST", "/api/deploy", body={"stacks": ["not-a-stack"]})
        assert status == 400
        assert "not-a-stack" in body["error"]

    def test_invalid_profile_name_rejected(self, server):
        status, _, _ = _request(server, "POST", "/api/deploy", body={"stacks": ["auth"], "profile": "bad name!"})
        assert status == 400

    def test_concurrent_deploy_conflicts(self, server, config_paths):
        release = threading.Event()

        def slow_run(args, log_write):
            release.wait(timeout=5)
            return 0

        with patch.object(console_server, "_run_deploy_command", side_effect=slow_run):
            status, _, _ = _request(server, "POST", "/api/deploy", body={"stacks": ["auth"]})
            assert status == 202
            status, body, _ = _request(server, "POST", "/api/deploy", body={"stacks": ["auth"]})
            assert status == 409
            assert "already running" in body["error"]
            release.set()
            _wait_for_deploy(server)

    def test_real_runner_reuses_deploy_command(self, config_paths, capsys):
        """The unmocked runner drives the real DeployCommand: a missing profile
        exits non-zero through deploy.py's own error path (no AWS calls)."""
        lines = []
        exit_code = console_server._run_deploy_command("--profile does-not-exist", lines.append)
        assert exit_code == 1
        assert any("not found" in line for line in lines)


class TestConsoleCommand:
    def test_command_metadata(self):
        command = ConsoleCommand()
        assert command.name == "console"
        option_names = {o.name for o in command.options}
        assert option_names == {"port", "no-browser"}

    def test_invalid_port_rejected(self):
        from cleo.testers.command_tester import CommandTester

        tester = CommandTester(ConsoleCommand())
        assert tester.execute("--port not-a-number") == 1

    def test_registered_in_application(self):
        from governed_inference_platform.cli import create_application

        app = create_application()
        assert app.find("console") is not None

    def test_no_browser_flag_respected(self, config_paths):
        """--no-browser must not open a browser; server starts and is shut down."""
        import webbrowser

        started = threading.Event()

        def fake_serve(self_server):
            started.set()

        with (
            patch.object(webbrowser, "open_new_tab") as mock_open,
            patch.object(console_server.ConsoleServer, "serve_forever", autospec=True, side_effect=fake_serve),
        ):
            from cleo.testers.command_tester import CommandTester

            tester = CommandTester(ConsoleCommand())
            exit_code = tester.execute("--port 0 --no-browser")

        assert exit_code == 0
        assert started.is_set()
        mock_open.assert_not_called()
