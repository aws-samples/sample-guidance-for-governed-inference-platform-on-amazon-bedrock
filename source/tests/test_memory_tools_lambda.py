# ABOUTME: Tests for the memory_tools Lambda — THE actorId security invariant (never from tool args)
# ABOUTME: Covers gate log-only (no data-plane calls), fail-closed identity, org write-group enforcement

"""Tests for the memory_tools Lambda (gateway MCP target).

The critical invariant under test (ADR-0016): the caller identity (actorId)
is derived ONLY from the gateway-forwarded Authorization context. A forged
identity smuggled into the tool arguments must never influence which
namespace is read or written, and when no identity can be derived the
identity-dependent tools refuse (fail closed). In the default 'log-only'
deploy gate, identity-dependent tools never touch the data plane at all.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

LAMBDA_PATH = (
    Path(__file__).resolve().parents[2]
    / "deployment"
    / "infrastructure"
    / "lambda-functions"
    / "memory_tools"
    / "index.py"
)


def _load_tools(env: dict) -> object:
    """Load the memory_tools Lambda module fresh with the given environment."""
    defaults = {
        "MEMORY_ID": "mem-test123",
        "DEPLOY_GATE": "active",
        "ENABLE_USER_MEMORY": "true",
        "ENABLE_ORG_MEMORY": "true",
        "ORG_WRITE_GROUPS": "",
    }
    defaults.update(env)
    for key, value in defaults.items():
        os.environ[key] = value

    module_name = f"memory_tools_index_{id(env)}"
    spec = importlib.util.spec_from_file_location(module_name, LAMBDA_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    module._client = MagicMock()
    return module


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _jwt(claims: dict) -> str:
    """Unsigned-but-well-formed JWT (the gateway already validated the real one)."""
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps(claims).encode())
    return f"{header}.{payload}.signature"


def _context(tool: str, authorization: str | None = None) -> SimpleNamespace:
    custom = {"bedrockAgentCoreToolName": f"w3-memory___{tool}"}
    if authorization:
        custom["Authorization"] = authorization
    return SimpleNamespace(client_context=SimpleNamespace(custom=custom))


ALICE = "alice@example.com"


def _actor_id_for_email(email: str = ALICE) -> str:
    return f"email:{hashlib.sha256(email.lower().encode('utf-8')).hexdigest()}"


def _bearer(email=ALICE, groups=None, **extra):
    claims = {"sub": "sub-123", "email": email}
    if groups is not None:
        claims["groups"] = groups
    claims.update(extra)
    return f"Bearer {_jwt(claims)}"


# --- THE critical tests: actorId is never taken from tool arguments ---


def test_forged_actor_id_argument_is_ignored_on_retrieve():
    """A client-smuggled actor_id must not change whose namespace is read."""
    module = _load_tools({})
    module._client.retrieve_memory_records.return_value = {"memoryRecordSummaries": []}
    event = {"query": "anything", "actor_id": "victim@example.com", "actorId": "victim@example.com"}

    result = module.lambda_handler(event, _context("memory_retrieve", _bearer()))

    assert result["status"] == "ok"
    namespaces = [call.kwargs["namespace"] for call in module._client.retrieve_memory_records.call_args_list]
    assert namespaces == [f"users/{_actor_id_for_email()}/facts", f"users/{_actor_id_for_email()}/preferences"]
    for namespace in namespaces:
        assert "victim" not in namespace


def test_forged_actor_id_argument_is_ignored_on_store():
    module = _load_tools({})
    event = {"content": "remember me", "actor_id": "victim@example.com"}

    result = module.lambda_handler(event, _context("memory_store", _bearer()))

    assert result["status"] == "ok"
    call = module._client.create_event.call_args
    assert call.kwargs["actorId"] == _actor_id_for_email()


def test_no_identity_refuses_user_tools_fail_closed():
    """Without a forwarded Authorization context the tools refuse — they never
    fall back to a client-supplied identity."""
    module = _load_tools({})
    event = {"query": "anything", "actor_id": "victim@example.com"}

    result = module.lambda_handler(event, _context("memory_retrieve", authorization=None))

    assert result["error"] == "identity_unavailable"
    module._client.retrieve_memory_records.assert_not_called()
    module._client.create_event.assert_not_called()


def test_malformed_token_refuses():
    module = _load_tools({})
    result = module.lambda_handler({"content": "x"}, _context("memory_store", "Bearer not-a-jwt"))
    assert result["error"] == "identity_unavailable"
    module._client.create_event.assert_not_called()


# --- Deploy gate (log-only default, ADR-0016) ---


@pytest.mark.parametrize("tool", ["memory_store", "memory_retrieve", "org_knowledge_add"])
def test_log_only_gate_blocks_identity_dependent_tools(tool):
    """In log-only mode no identity-dependent tool touches the data plane,
    even with a perfectly valid forwarded identity."""
    module = _load_tools({"DEPLOY_GATE": "log-only", "ORG_WRITE_GROUPS": "memory-admins"})
    event = {"content": "x", "query": "x"}

    result = module.lambda_handler(event, _context(tool, _bearer(groups=["memory-admins"])))

    assert result["status"] == "gated"
    module._client.create_event.assert_not_called()
    module._client.retrieve_memory_records.assert_not_called()
    module._client.batch_create_memory_records.assert_not_called()


def test_log_only_gate_still_serves_org_search():
    """org_knowledge_search is identity-independent (shared read-only) and is
    entitlement-gated at the gateway — it works in log-only mode."""
    module = _load_tools({"DEPLOY_GATE": "log-only"})
    module._client.retrieve_memory_records.return_value = {"memoryRecordSummaries": []}

    result = module.lambda_handler({"query": "handbook"}, _context("org_knowledge_search"))

    assert result["status"] == "ok"
    assert module._client.retrieve_memory_records.call_args.kwargs["namespace"] == "org/knowledge"


# --- Org write-group enforcement ---


def test_org_add_denied_when_no_write_groups_configured():
    """Empty OrgMemoryWriteGroups = admins only: the tool denies everyone."""
    module = _load_tools({"ORG_WRITE_GROUPS": ""})

    result = module.lambda_handler({"content": "x"}, _context("org_knowledge_add", _bearer(groups=["any-group"])))

    assert result["error"] == "forbidden"
    module._client.batch_create_memory_records.assert_not_called()


def test_org_add_denied_without_group_intersection():
    module = _load_tools({"ORG_WRITE_GROUPS": "memory-admins,kb-editors"})

    result = module.lambda_handler({"content": "x"}, _context("org_knowledge_add", _bearer(groups=["devs"])))

    assert result["error"] == "forbidden"
    module._client.batch_create_memory_records.assert_not_called()


def test_org_add_allowed_with_entitled_group():
    module = _load_tools({"ORG_WRITE_GROUPS": "memory-admins,kb-editors"})

    result = module.lambda_handler(
        {"content": "the vpn config", "title": "VPN"},
        _context("org_knowledge_add", _bearer(groups=["kb-editors"])),
    )

    assert result["status"] == "ok"
    record = module._client.batch_create_memory_records.call_args.kwargs["records"][0]
    assert record["namespaces"] == ["org/knowledge"]
    assert "the vpn config" in record["content"]["text"]


def test_org_add_accepts_cognito_groups_claim_variance():
    """Groups may arrive as cognito:groups (same variance as the quota authorizer)."""
    module = _load_tools({"ORG_WRITE_GROUPS": "memory-admins"})
    token = f"Bearer {_jwt({'email': ALICE, 'cognito:groups': ['memory-admins']})}"

    result = module.lambda_handler({"content": "x"}, _context("org_knowledge_add", token))

    assert result["status"] == "ok"


# --- Scope flags and dispatch ---


def test_disabled_user_memory_refuses_user_tools():
    module = _load_tools({"ENABLE_USER_MEMORY": "false"})
    result = module.lambda_handler({"content": "x"}, _context("memory_store", _bearer()))
    assert result["error"] == "disabled"
    module._client.create_event.assert_not_called()


def test_disabled_org_memory_refuses_org_tools():
    module = _load_tools({"ENABLE_ORG_MEMORY": "false"})
    assert module.lambda_handler({"query": "x"}, _context("org_knowledge_search"))["error"] == "disabled"
    assert module.lambda_handler({"content": "x"}, _context("org_knowledge_add", _bearer()))["error"] == "disabled"


def test_unknown_tool_rejected():
    module = _load_tools({})
    result = module.lambda_handler({}, _context("delete_everything", _bearer()))
    assert result["error"] == "unknown_tool"


def test_store_and_retrieve_happy_path_shapes():
    module = _load_tools({})
    module._client.retrieve_memory_records.return_value = {
        "memoryRecordSummaries": [
            {
                "content": {"text": "likes yaml"},
                "namespaces": [f"users/{_actor_id_for_email()}/preferences"],
                "score": 0.9,
            }
        ]
    }

    store = module.lambda_handler({"content": "I like yaml"}, _context("memory_store", _bearer()))
    assert store["status"] == "ok"
    create_kwargs = module._client.create_event.call_args.kwargs
    assert create_kwargs["memoryId"] == "mem-test123"
    assert create_kwargs["payload"][0]["conversational"]["content"]["text"] == "I like yaml"

    retrieve = module.lambda_handler({"query": "yaml", "top_k": 1}, _context("memory_retrieve", _bearer()))
    assert retrieve["status"] == "ok"
    assert retrieve["results"][0]["text"] == "likes yaml"


def test_actor_id_requires_email_claim():
    module = _load_tools({})
    token = f"Bearer {_jwt({'sub': 'sub-only-456'})}"
    result = module.lambda_handler({"content": "x"}, _context("memory_store", token))
    assert result["error"] == "identity_unavailable"
    module._client.create_event.assert_not_called()
