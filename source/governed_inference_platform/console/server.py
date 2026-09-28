# ABOUTME: stdlib HTTP server + JSON router backing `gip console` (localhost wizard UI)
# ABOUTME: Reuses init_answers (validate/persist) and DeployCommand (deploy) — no logic duplication

"""HTTP backend for ``gip console``.

Design constraints (see assets/docs/CONSOLE.md):

- stdlib only (``http.server``) — this is an AWS guidance sample, no new
  runtime dependencies and no node/npm build chain.
- Binds 127.0.0.1 exclusively; every ``/api/*`` request must carry the
  per-session token embedded in the served page (drive-by localhost CSRF
  protection). No CORS headers are ever emitted, so a foreign origin cannot
  read the token off ``GET /``.
- Zero logic duplication: validation goes through
  ``init_answers.build_config_from_answers`` (the exact ``--from-file`` code
  path), profile persistence through ``InitCommand._save_configuration``, and
  deployment through ``DeployCommand`` via cleo's ``CommandTester``.
"""

import contextlib
import json
import re
import secrets
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any

import yaml

from governed_inference_platform.cli.commands.deploy import VALID_STACKS
from governed_inference_platform.cli.commands.init import COMMON_REGIONS
from governed_inference_platform.cli.commands.init_answers import (
    DEFAULT_MODEL_KEY,
    DEFAULTS,
    VALID_AUTH_TYPES,
    VALID_PROVIDER_TYPES,
    _prune_none,
    build_config_from_answers,
)
from governed_inference_platform.config import WEBSEARCH_SUPPORTED_REGIONS, Config
from governed_inference_platform.models import CLAUDE_MODELS, DEFAULT_REGIONS

MAX_BODY_BYTES = 1_000_000
TOKEN_HEADER = "X-Gip-Token"  # nosec B105 — HTTP header name, not a credential
_ALLOWED_HOSTS = ("127.0.0.1", "localhost")

# Wizard step-2 module catalog (UI metadata only — descriptions and dependency
# notes for the card grid; the enable/disable flags map 1:1 onto answers-file
# keys, and the actual rules are enforced server-side by init_answers).
MODULES: list[dict[str, Any]] = [
    {
        "id": "monitoring",
        "label": "Monitoring & dashboards",
        "answers_path": "monitoring.enabled",
        "description": "OTEL telemetry with CloudWatch dashboards (sidecar: local collector; central: ECS/ALB).",
        "requires": [],
        "stacks": ["dashboard"],
        "default": True,
    },
    {
        "id": "analytics",
        "label": "Analytics pipeline",
        "answers_path": "analytics.enabled",
        "description": "Kinesis Firehose + Athena SQL over usage telemetry.",
        "requires": [
            {
                "monitoring_mode": "central",
                "reason": "Needs the central OTEL collector pipeline (monitoring mode: central).",
            }
        ],
        "stacks": ["analytics"],
        "default": False,
    },
    {
        "id": "quota",
        "label": "Per-user quotas",
        "answers_path": "quota.enabled",
        "description": "Per-user cost or token budgets with alert/block enforcement.",
        "requires": [
            {"module": "monitoring", "reason": "Quota metering rides on monitoring telemetry."},
            {"auth_not": "none", "reason": "Quota enforcement requires per-user identity (OIDC or IDC)."},
        ],
        "stacks": ["quota"],
        "default": True,
    },
    {
        "id": "guardrails",
        "label": "Bedrock Guardrails",
        "answers_path": "guardrails.enabled",
        "description": "Account-level Guardrails enforcement in each configured Bedrock region.",
        "requires": [],
        "stacks": ["guardrails"],
        "default": False,
    },
    {
        "id": "model_lifecycle",
        "label": "Model lifecycle alerts",
        "answers_path": "model_lifecycle.enabled",
        "description": "Alerts when configured Bedrock models approach legacy/EOL status.",
        "requires": [],
        "stacks": ["model-lifecycle"],
        "default": False,
    },
    {
        "id": "web_search",
        "label": "Web search gateway",
        "answers_path": "web_search.enabled",
        "description": "AgentCore Gateway + Web Search connector (group-entitled).",
        "requires": [{"auth_not": "idc", "reason": "Web search is not available for IAM Identity Center deployments."}],
        "stacks": ["websearch"],
        "default": False,
    },
    {
        "id": "memory",
        "label": "AgentCore Memory",
        "answers_path": "memory.user_enabled",
        "description": "User/org memory tools attached to the web search gateway.",
        "requires": [{"module": "web_search", "reason": "Memory tools attach to the web search gateway (ADR-0016)."}],
        "stacks": ["memory"],
        "default": False,
    },
    {
        "id": "skills",
        "label": "Skills registry",
        "answers_path": "skills.enabled",
        "description": "Organization skills registry (Agent Registry + artifact bucket).",
        "requires": [],
        "stacks": ["skills"],
        "default": False,
    },
    {
        "id": "distribution",
        "label": "Distribution (presigned S3)",
        "answers_path": "distribution.enabled",
        "description": "S3 + IAM infrastructure for sharing packages via presigned URLs.",
        "requires": [],
        "stacks": ["distribution"],
        "default": False,
    },
    {
        "id": "codebuild",
        "label": "Windows CodeBuild",
        "answers_path": "codebuild.enabled",
        "description": "AWS CodeBuild project for Windows binary builds.",
        "requires": [],
        "stacks": ["codebuild"],
        "default": False,
    },
]


def build_bootstrap_catalog() -> dict[str, Any]:
    """Assemble the option catalogs the wizard needs, from the init sources of truth."""
    models = {}
    for model_key, model in CLAUDE_MODELS.items():
        models[model_key] = {
            "name": model.name,
            "profiles": {
                profile_key: {
                    "model_id": profile.model_id,
                    "description": profile.description,
                    "source_regions": list(profile.source_regions),
                    "destination_regions": list(profile.destination_regions),
                }
                for profile_key, profile in model.profiles.items()
            },
        }
    return {
        "defaults": {
            "auth_type": DEFAULTS["auth_type"],
            "region": DEFAULTS["aws"]["region"],
            "identity_pool_name": DEFAULTS["aws"]["identity_pool_name"],
            "model_key": DEFAULT_MODEL_KEY,
            "monitoring_mode": DEFAULTS["monitoring"]["mode"],
            "quota_limit_type": DEFAULTS["quota"]["limit_type"],
            "credential_storage": DEFAULTS["credential_storage"],
            "federation_type": DEFAULTS["federation_type"],
        },
        "auth_types": list(VALID_AUTH_TYPES),
        "provider_types": list(VALID_PROVIDER_TYPES),
        "regions": list(COMMON_REGIONS),
        "cris_default_regions": dict(DEFAULT_REGIONS),
        "websearch_supported_regions": list(WEBSEARCH_SUPPORTED_REGIONS),
        "models": models,
        "modules": MODULES,
        "valid_stacks": list(VALID_STACKS),
    }


_FIELD_PREFIX = re.compile(r"^([a-z0-9_.]+): (.*)$", re.DOTALL)


def errors_to_fields(errors: list[str]) -> list[dict[str, str]]:
    """Split init_answers error strings ('path: message') into field-level errors."""
    out = []
    for error in errors:
        match = _FIELD_PREFIX.match(error)
        if match:
            out.append({"field": match.group(1), "message": match.group(2)})
        else:
            out.append({"field": "", "message": error})
    return out


def _run_deploy_command(args: str, log_write) -> int:
    """Run one `gip deploy` invocation in-process via cleo's CommandTester.

    DeployCommand prints through a rich Console bound to sys.stdout, so the
    job's stdout is redirected into the log while it runs (one deploy job at a
    time — enforced by DeployJobManager).
    """
    from cleo.testers.command_tester import CommandTester

    from governed_inference_platform.cli.commands.deploy import DeployCommand

    tester = CommandTester(DeployCommand())
    writer = _LogStream(log_write)
    with contextlib.redirect_stdout(writer):
        exit_code = tester.execute(args)
    writer.close()
    cleo_output = tester.io.fetch_output()
    if cleo_output.strip():
        for line in cleo_output.splitlines():
            log_write(line)
    return exit_code if exit_code is not None else 0


class _LogStream:
    """File-like sink that forwards complete lines to the job log."""

    def __init__(self, log_write):
        self._log_write = log_write
        self._buffer = ""

    def write(self, text: str) -> int:
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                self._log_write(line)
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False

    def close(self) -> None:
        if self._buffer.strip():
            self._log_write(self._buffer)
        self._buffer = ""


class DeployJobManager:
    """Runs deploy requests sequentially in a background thread, tracks status."""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.reset()

    def reset(self) -> None:
        self.stacks: list[dict[str, Any]] = []
        self.events: deque[str] = deque(maxlen=500)
        self.dry_run = False
        self.profile: str | None = None
        self.error: str | None = None
        self.started_at: float | None = None
        self.finished_at: float | None = None

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, stacks: list[str], dry_run: bool, profile: str | None) -> tuple[bool, str]:
        with self._lock:
            if self.active:
                return False, "A deployment is already running"
            self.reset()
            self.dry_run = dry_run
            self.profile = profile
            self.stacks = [{"stack": s, "status": "pending", "exit_code": None} for s in stacks]
            self.started_at = time.time()
            self._thread = threading.Thread(target=self._run, name="gip-console-deploy", daemon=True)
            self._thread.start()
            return True, "started"

    def _log(self, line: str) -> None:
        self.events.append(line)

    def _run(self) -> None:
        try:
            for entry in self.stacks:
                entry["status"] = "running"
                args = "" if entry["stack"] == "all" else entry["stack"]
                if self.dry_run:
                    args += " --dry-run"
                if self.profile:
                    args += f" --profile {self.profile}"
                self._log(f"── gip deploy {args.strip()} ──")
                exit_code = _run_deploy_command(args.strip(), self._log)
                entry["exit_code"] = exit_code
                entry["status"] = "succeeded" if exit_code == 0 else "failed"
                if exit_code != 0:
                    for remaining in self.stacks:
                        if remaining["status"] == "pending":
                            remaining["status"] = "skipped"
                    break
        except Exception as e:  # surface, never crash the server thread
            self.error = str(e)
            for entry in self.stacks:
                if entry["status"] in ("pending", "running"):
                    entry["status"] = "failed"
        finally:
            self.finished_at = time.time()

    def status(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "dry_run": self.dry_run,
            "profile": self.profile,
            "stacks": self.stacks,
            "events": list(self.events),
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": None if self.active else self.finished_at,
        }


class ConsoleServer(ThreadingHTTPServer):
    """127.0.0.1-only server carrying per-session state (token, deploy job, answers)."""

    daemon_threads = True

    def __init__(self, host: str = "127.0.0.1", port: int = 8321):
        super().__init__((host, port), ConsoleRequestHandler)
        self.token = secrets.token_urlsafe(32)
        self.deploy_manager = DeployJobManager()
        self.last_answers: dict[str, Any] | None = None
        self.answers_lock = threading.Lock()

    @property
    def url(self) -> str:
        return f"http://{self.server_address[0]}:{self.server_address[1]}/"


def load_index_html() -> str:
    """Read the single-file SPA shipped as package data."""
    return (resources.files("governed_inference_platform.console") / "static" / "index.html").read_text(
        encoding="utf-8"
    )


class ConsoleRequestHandler(BaseHTTPRequestHandler):
    server: ConsoleServer
    protocol_version = "HTTP/1.1"

    # -- plumbing ----------------------------------------------------------

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass  # keep the gip console terminal quiet

    def _send(self, status: int, body: bytes, content_type: str, extra_headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # One request per connection: responses (e.g. 401) may be sent before
        # the request body was read, which would poison a kept-alive socket.
        self.send_header("Connection", "close")
        self.close_connection = True
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def _reject_bad_host(self) -> bool:
        """DNS-rebinding guard: only accept Host headers naming this machine."""
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        if host not in _ALLOWED_HOSTS:
            self._send_json(403, {"error": "forbidden host"})
            return True
        return False

    def _check_token(self) -> bool:
        if self.headers.get(TOKEN_HEADER) == self.server.token:
            return True
        self._send_json(401, {"error": f"missing or invalid {TOKEN_HEADER} header"})
        return False

    def _read_json_body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length <= 0:
            self._send_json(400, {"error": "request body required"})
            return None
        if length > MAX_BODY_BYTES:
            self._send_json(413, {"error": f"request body exceeds {MAX_BODY_BYTES} bytes"})
            return None
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"error": "invalid JSON body"})
            return None
        if not isinstance(data, dict):
            self._send_json(400, {"error": "JSON body must be an object"})
            return None
        return data

    # -- routes ------------------------------------------------------------

    def do_GET(self):  # noqa: N802 - stdlib naming
        if self._reject_bad_host():
            return
        path = self.path.split("?")[0]
        if path == "/":
            html = load_index_html().replace("__GIP_SESSION_TOKEN__", self.server.token)
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/bootstrap":
            if self._check_token():
                self._send_json(200, build_bootstrap_catalog())
        elif path == "/api/deploy/status":
            if self._check_token():
                self._send_json(200, self.server.deploy_manager.status())
        elif path == "/api/answers.yaml":
            if self._check_token():
                self._handle_answers_download()
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802 - stdlib naming
        if self._reject_bad_host():
            return
        path = self.path.split("?")[0]
        if not self._check_token():
            return
        body = self._read_json_body()
        if body is None:
            return
        if path == "/api/validate":
            self._handle_validate(body)
        elif path == "/api/profile":
            self._handle_profile(body)
        elif path == "/api/deploy":
            self._handle_deploy(body)
        else:
            self._send_json(404, {"error": "not found"})

    # -- handlers ----------------------------------------------------------

    def _handle_validate(self, body: dict) -> None:
        answers = body.get("answers")
        if not isinstance(answers, dict):
            self._send_json(400, {"error": "'answers' must be an object"})
            return
        config, errors = build_config_from_answers(answers)
        pruned = _prune_none(answers)
        answers_yaml = yaml.safe_dump(pruned, default_flow_style=False, sort_keys=False)
        if not errors:
            with self.server.answers_lock:
                self.server.last_answers = pruned
        self._send_json(
            200,
            {
                "valid": not errors,
                "errors": errors_to_fields(errors),
                "answers_yaml": answers_yaml,
                "resolved": {
                    "selected_model": config.get("aws", {}).get("selected_model") if config else None,
                    "cross_region_profile": config.get("aws", {}).get("cross_region_profile") if config else None,
                    "allowed_bedrock_regions": config.get("aws", {}).get("allowed_bedrock_regions") if config else None,
                    "stacks": config.get("aws", {}).get("stacks") if config else None,
                },
            },
        )

    def _handle_profile(self, body: dict) -> None:
        """Persist the profile exactly as `gip init --from-file` does.

        Mirrors init_answers.run_from_file step for step: profile-name check,
        build_config_from_answers, existing-profile/--force check, then
        InitCommand._save_configuration (the shared persistence code path).
        """
        answers = body.get("answers")
        if not isinstance(answers, dict):
            self._send_json(400, {"error": "'answers' must be an object"})
            return
        profile_name = body.get("profile_name") or "default"
        force = bool(body.get("force"))

        if not Config._is_valid_profile_name(profile_name):
            self._send_json(
                400,
                {
                    "error": f"Invalid profile name '{profile_name}': must be alphanumeric with hyphens only, "
                    "max 64 characters."
                },
            )
            return

        config, errors = build_config_from_answers(answers)
        if errors:
            self._send_json(400, {"error": "answers are invalid", "errors": errors_to_fields(errors)})
            return

        existing = Config.load().get_profile(profile_name)
        if existing and not force:
            self._send_json(409, {"error": f"Profile '{profile_name}' already exists. Set force=true to overwrite it."})
            return

        from governed_inference_platform.cli.commands.init import InitCommand

        InitCommand()._save_configuration(config, profile_name)
        with self.server.answers_lock:
            self.server.last_answers = _prune_none(answers)

        self._send_json(
            200,
            {
                "ok": True,
                "profile": profile_name,
                "path": str(Config.PROFILES_DIR / f"{profile_name}.json"),
                "stacks": config["aws"]["stacks"],
                "region": config["aws"]["region"],
                "selected_model": config["aws"].get("selected_model"),
            },
        )

    def _handle_deploy(self, body: dict) -> None:
        stacks = body.get("stacks") or ["all"]
        if not isinstance(stacks, list) or not all(isinstance(s, str) for s in stacks):
            self._send_json(400, {"error": "'stacks' must be a list of stack names"})
            return
        invalid = [s for s in stacks if s != "all" and s not in VALID_STACKS]
        if invalid:
            self._send_json(
                400, {"error": f"unknown stacks: {', '.join(invalid)} (valid: all, {', '.join(VALID_STACKS)})"}
            )
            return
        profile = body.get("profile")
        if profile is not None and not Config._is_valid_profile_name(str(profile)):
            self._send_json(400, {"error": f"invalid profile name '{profile}'"})
            return
        started, message = self.server.deploy_manager.start(stacks, bool(body.get("dry_run")), profile)
        if not started:
            self._send_json(409, {"error": message})
            return
        self._send_json(202, {"ok": True, "stacks": stacks, "dry_run": bool(body.get("dry_run"))})

    def _handle_answers_download(self) -> None:
        with self.server.answers_lock:
            answers = self.server.last_answers
        if answers is None:
            self._send_json(404, {"error": "no answers generated yet — validate or create a profile first"})
            return
        body = yaml.safe_dump(answers, default_flow_style=False, sort_keys=False).encode("utf-8")
        self._send(
            200,
            body,
            "application/x-yaml; charset=utf-8",
            {"Content-Disposition": 'attachment; filename="answers.yaml"'},
        )


def create_console_server(port: int = 8321, host: str = "127.0.0.1") -> ConsoleServer:
    """Create (but do not start) the console server. Port 0 picks an ephemeral port."""
    return ConsoleServer(host=host, port=port)
