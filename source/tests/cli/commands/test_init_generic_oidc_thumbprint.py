# ABOUTME: Regression tests — `gip init` generic-OIDC step treats the IAM thumbprint prompt as optional
# ABOUTME: Blank keeps oidc_thumbprint unset, a value is normalized, Ctrl-C aborts, no TLS/hash computation

"""`gip init` generic OIDC: the JWKS TLS thumbprint prompt is optional.

The IAM OIDC provider's ThumbprintList is optional — IAM retrieves the CA thumbprint
itself when it is omitted — so the wizard must not compute one (it used to fingerprint
the JWKS leaf cert over a live TLS handshake) and must not require one. It only accepts an
operator-supplied override for private-CA JWKS hosts. These tests drive the OIDC step
of ``_gather_configuration`` with a generic provider and inspect the config captured at
the ``oidc_complete`` checkpoint.
"""

import socket
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ruff: noqa: E402
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

import questionary

from governed_inference_platform.cli.commands.init import InitCommand
from governed_inference_platform.cli.utils import oidc_discovery
from governed_inference_platform.config import IAM_OIDC_THUMBPRINT_GUIDE_URL

THUMBPRINT_PROMPT = "JWKS TLS cert thumbprint"


class _StopAtOidcComplete(Exception):
    """Raised from progress.save_step once the OIDC step finishes."""


def _run_generic_oidc_step(thumbprint_answer, existing_config=None):
    """Drive `_gather_configuration` through the OIDC step for a generic provider.

    Returns (config captured at ``oidc_complete`` or None if the wizard aborted,
    kwargs the thumbprint prompt was invoked with).
    """
    captured = {}
    prompts = {}

    progress = MagicMock()
    progress.get_last_step.return_value = None
    progress.get_saved_data.return_value = {}

    def save_step(step, data=None, *a, **k):
        if step == "oidc_complete":
            captured["config"] = dict(data)
            raise _StopAtOidcComplete()

    progress.save_step.side_effect = save_step

    def fake_select(message, *a, **k):
        m = MagicMock()
        text = str(message)
        if "authentication method" in text:
            m.ask.return_value = "oidc"
        elif "identity provider type" in text:
            m.ask.return_value = "generic"
        elif "credential" in text.lower() or "storage" in text.lower():
            m.ask.return_value = "keyring"
        elif "Federation type" in text:
            m.ask.return_value = "direct"
        elif "Bind session names" in text:
            m.ask.return_value = "none"
        else:
            m.ask.return_value = None
        return m

    def fake_text(message, *a, **k):
        m = MagicMock()
        text = str(message)
        if THUMBPRINT_PROMPT in text:
            prompts["thumbprint"] = k
            m.ask.return_value = thumbprint_answer
        elif "provider domain" in text:
            m.ask.return_value = "auth.example.com"
        elif "Client ID" in text:
            m.ask.return_value = "bedrock-cli-prod"
        else:
            m.ask.return_value = k.get("default", "")
        return m

    def fake_confirm(*a, **k):
        m = MagicMock()
        m.ask.return_value = False
        return m

    def no_network(*a, **k):
        raise AssertionError("gip init must not open TLS connections to compute a thumbprint")

    cmd = InitCommand()
    with (
        patch.object(questionary, "select", fake_select),
        patch.object(questionary, "text", fake_text),
        patch.object(questionary, "confirm", fake_confirm),
        # Discovery is a separate concern; make it fail fast so the manual-entry path runs.
        patch.object(
            oidc_discovery, "discover_oidc_endpoints", side_effect=oidc_discovery.OidcDiscoveryError("offline")
        ),
        patch.object(socket, "create_connection", no_network),
    ):
        try:
            result = cmd._gather_configuration(progress, existing_config=existing_config)
        except _StopAtOidcComplete:
            result = "stopped"

    assert "thumbprint" in prompts, "the optional thumbprint prompt never fired"
    if result is None:
        return None, prompts["thumbprint"]
    assert "config" in captured, "wizard never reached the oidc_complete checkpoint"
    return captured["config"], prompts["thumbprint"]


def test_blank_answer_leaves_thumbprint_unset():
    """Regression: pressing Enter must NOT abort the wizard and must store no thumbprint."""
    config, _ = _run_generic_oidc_step(thumbprint_answer="")

    assert config is not None
    assert config["provider_type"] == "generic"
    assert config["oidc_issuer_url"] == "https://auth.example.com"
    assert config["oidc_thumbprint"] is None


def test_prompt_explains_it_is_optional_and_links_the_iam_guide():
    _, kwargs = _run_generic_oidc_step(thumbprint_answer="")

    instruction = kwargs["instruction"]
    assert "Leave blank" in instruction
    assert "private CA" in instruction
    assert IAM_OIDC_THUMBPRINT_GUIDE_URL in instruction
    assert "openssl" not in instruction.lower()


@pytest.mark.parametrize(
    "answer",
    [
        "9E:99:A4:8A:99:60:B1:49:26:BB:7F:3B:02:E2:2D:A2:B0:AB:72:80",
        "  9e99a48a9960b14926bb7f3b02e22da2b0ab7280  ",
        "9E99A48A9960B14926BB7F3B02E22DA2B0AB7280",
    ],
)
def test_supplied_thumbprint_is_normalized(answer):
    """Private-CA override: colons stripped, whitespace trimmed, lower-cased."""
    config, _ = _run_generic_oidc_step(thumbprint_answer=answer)

    assert config["oidc_thumbprint"] == "9e99a48a9960b14926bb7f3b02e22da2b0ab7280"


def test_prompt_validator_accepts_blank_and_40_hex_only():
    _, kwargs = _run_generic_oidc_step(thumbprint_answer="")
    validate = kwargs["validate"]

    assert validate("") is True
    assert validate("   ") is True
    assert validate("9e99a48a9960b14926bb7f3b02e22da2b0ab7280") is True
    assert validate("9E:99:A4:8A:99:60:B1:49:26:BB:7F:3B:02:E2:2D:A2:B0:AB:72:80") is True
    assert isinstance(validate("not-a-thumbprint"), str)
    assert isinstance(validate("9e99a48a9960b14926bb7f3b02e22da2b0ab72"), str)  # 38 chars


def test_rerun_prefills_existing_thumbprint():
    """Re-running init on an older profile that carries a thumbprint offers it as the default."""
    existing = {
        "okta": {"domain": "auth.example.com", "client_id": "bedrock-cli-prod"},
        "provider_type": "generic",
        "oidc_issuer_url": "https://auth.example.com",
        "oidc_thumbprint": "9e99a48a9960b14926bb7f3b02e22da2b0ab7280",
    }
    _, kwargs = _run_generic_oidc_step(thumbprint_answer="", existing_config=existing)

    assert kwargs["default"] == "9e99a48a9960b14926bb7f3b02e22da2b0ab7280"


def test_ctrl_c_on_thumbprint_prompt_aborts_wizard():
    """questionary returns None on Ctrl-C; that must still abort, unlike a blank answer."""
    config, _ = _run_generic_oidc_step(thumbprint_answer=None)

    assert config is None
