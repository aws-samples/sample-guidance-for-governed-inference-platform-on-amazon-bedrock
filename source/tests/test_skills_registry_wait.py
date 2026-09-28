"""Tests for the Agent Registry readiness wait."""

import importlib.util
import inspect
import sys
from pathlib import Path

LAMBDA_DIR = (
    Path(__file__).resolve().parents[2] / "deployment" / "infrastructure" / "lambda-functions" / "skills_registry"
)
TEMPLATE = LAMBDA_DIR.parents[1] / "skills-registry.yaml"


def _load_index():
    sys.path.insert(0, str(LAMBDA_DIR))
    spec = importlib.util.spec_from_file_location("skills_registry_wait_index", LAMBDA_DIR / "index.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_registry_ready_default_wait_is_bounded_at_five_minutes():
    module = _load_index()
    defaults = inspect.signature(module._wait_registry_ready).parameters

    assert defaults["attempts"].default * defaults["delay_seconds"].default == 300


def test_registry_provisioner_lambda_timeout_exceeds_ready_wait():
    text = TEMPLATE.read_text(encoding="utf-8")
    provisioner = text.split("RegistryProvisionerFunction:", 1)[1].split("SkillsRegistry:", 1)[0]

    assert "Timeout: 360" in provisioner
