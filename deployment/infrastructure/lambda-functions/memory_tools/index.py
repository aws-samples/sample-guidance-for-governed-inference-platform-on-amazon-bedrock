# ABOUTME: MCP tool implementations for AgentCore Memory (memory_store/memory_retrieve/org_knowledge_search/org_knowledge_add)
# ABOUTME: actorId is NEVER a tool argument — derived only from the gateway-forwarded Authorization context (ADR-0016 gate)

"""AgentCore Memory tools Lambda (gateway MCP target).

SECURITY MODEL (ADR-0016, R9 F7/F8): a plain Lambda gateway target receives
NO JWT claims — the event carries only the tool's inputSchema arguments and
the client context carries only gateway metadata. The caller's identity
(actorId) therefore:

1. is NEVER accepted from tool arguments — any identity-looking argument a
   client smuggles in is ignored;
2. is derived exclusively from the Authorization header the gateway forwards
   to this target (``MetadataConfiguration.AllowedRequestHeaders`` — R9 Q1).
   The token's signature was already validated by the gateway's CUSTOM_JWT
   authorizer, and this function is only invokable by the gateway's execution
   role (Lambda resource policy), so the payload is decoded without a second
   signature check;
3. is gated: whether Lambda targets actually surface the forwarded header is
   UNDOCUMENTED until proven in a live deploy. Until then DEPLOY_GATE stays
   'log-only' and every identity-dependent tool answers with a gated response
   and performs no data-plane call. Flipping to 'active' requires the
   ADR-0016 live checklist.

Fail-closed: in 'active' mode, if no identity can be derived the
identity-dependent tools refuse (they never fall back to a client-supplied
value or a shared namespace).
"""

import base64
import binascii
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

MEMORY_ID = os.environ.get("MEMORY_ID", "")
DEPLOY_GATE = os.environ.get("DEPLOY_GATE", "log-only")
ENABLE_USER_MEMORY = os.environ.get("ENABLE_USER_MEMORY", "false").lower() == "true"
ENABLE_ORG_MEMORY = os.environ.get("ENABLE_ORG_MEMORY", "false").lower() == "true"
ORG_WRITE_GROUPS = [g.strip() for g in os.environ.get("ORG_WRITE_GROUPS", "").split(",") if g.strip()]

ORG_NAMESPACE = "org/knowledge"
USER_NAMESPACES = ("facts", "preferences")
DEFAULT_TOP_K = 5
LEGACY_OK_ERRORS = {"ValidationException", "ResourceNotFoundException"}

_client = None


def _actor_id_for_identity(kind, value):
    normalized = value.strip().lower() if kind == "email" else value.strip()
    return f"{kind}:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()}"


def _user_actor_ids(actor_id, legacy_actor_id=None):
    actor_ids = [actor_id]
    if legacy_actor_id and legacy_actor_id != actor_id:
        actor_ids.append(legacy_actor_id)
    return actor_ids


def _legacy_namespace_allowed(candidate):
    return all(ch.isalnum() or ch in "-_/:" for ch in candidate)


def _memory_client():
    """Lazily create the bedrock-agentcore data-plane client."""
    global _client
    if _client is None:
        _client = boto3.client("bedrock-agentcore")
    return _client


def _error(code, message):
    return {"error": code, "message": message}


def _gated(tool_name, detail):
    """log-only response: says what would have happened, touches nothing."""
    print(f"INFO: DEPLOY_GATE=log-only — {tool_name} gated ({detail})")
    return {
        "status": "gated",
        "message": (
            f"{tool_name} is deployed in log-only mode: the actorId-binding "
            "security gate (ADR-0016) has not been verified for this "
            "deployment yet, so no memory was read or written."
        ),
    }


def _b64url_decode(segment):
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def _decode_jwt_claims(token):
    """Decode a JWT payload WITHOUT signature verification.

    Acceptable only because (a) the gateway's CUSTOM_JWT authorizer already
    validated the signature/audience before forwarding, and (b) this Lambda
    is only invokable by the gateway execution role. Returns {} on any
    malformed input.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        claims = json.loads(_b64url_decode(parts[1]))
        return claims if isinstance(claims, dict) else {}
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return {}


def _find_authorization(event, context):
    """Locate the gateway-forwarded Authorization value (R9 Q1).

    Where a Lambda target surfaces AllowedRequestHeaders is undocumented, so
    every plausible location is checked: the client context custom map and a
    top-level ``headers`` map on the event. Returns the raw header value or
    None.
    """
    candidates = []
    custom = getattr(getattr(context, "client_context", None), "custom", None)
    if isinstance(custom, dict):
        candidates.append(custom)
    if isinstance(event, dict) and isinstance(event.get("headers"), dict):
        candidates.append(event["headers"])
    for source in candidates:
        for key, value in source.items():
            if key.lower() == "authorization" and isinstance(value, str) and value:
                return value
    return None


def _derive_identity(event, context):
    """Derive (actor_id, groups, legacy_actor_id) from the forwarded JWT.

    actor_id is derived from the email claim (the platform's user identity
    everywhere else: quota, offboarding, forget-user). The raw claim is hashed
    into AgentCore Memory's allowed actorId pattern. Groups mirror the quota
    authorizer's claim-name variance (``groups`` or ``cognito:groups``).
    """
    header = _find_authorization(event, context)
    if not header:
        return None, [], None
    token = header[7:] if header.lower().startswith("bearer ") else header
    claims = _decode_jwt_claims(token)
    email = claims.get("email")
    if isinstance(email, str) and email.strip():
        legacy_actor_id = email.strip().lower()
        actor_id = _actor_id_for_identity("email", legacy_actor_id)
    else:
        return None, [], None
    raw_groups = claims.get("groups") or claims.get("cognito:groups") or []
    if isinstance(raw_groups, str):
        raw_groups = [raw_groups]
    groups = [g for g in raw_groups if isinstance(g, str)]
    return actor_id, groups, legacy_actor_id


def _tool_name(context):
    """Extract the bare tool name from the gateway context.

    The gateway delivers '<target_name>___<tool_name>' in
    client_context.custom['bedrockAgentCoreToolName'] (R9 F8).
    """
    custom = getattr(getattr(context, "client_context", None), "custom", None)
    full = (custom or {}).get("bedrockAgentCoreToolName", "") if isinstance(custom, dict) else ""
    return full.split("___")[-1] if full else ""


def _top_k(event):
    try:
        value = int(event.get("top_k", DEFAULT_TOP_K))
    except (TypeError, ValueError):
        return DEFAULT_TOP_K
    return max(1, min(value, 25))


def _retrieve(namespace, query, top_k):
    """Paginated-enough retrieval from one namespace (topK bounds results)."""
    response = _memory_client().retrieve_memory_records(
        memoryId=MEMORY_ID,
        namespace=namespace,
        searchCriteria={"searchQuery": query, "topK": top_k},
    )
    results = []
    for summary in response.get("memoryRecordSummaries", []):
        results.append(
            {
                "text": (summary.get("content") or {}).get("text", ""),
                "namespaces": summary.get("namespaces", []),
                "score": summary.get("score"),
            }
        )
    return results


def memory_store(event, actor_id):
    content = event.get("content")
    if not content or not isinstance(content, str):
        return _error("invalid_argument", "content (string) is required")
    now = datetime.now(timezone.utc)
    _memory_client().create_event(
        memoryId=MEMORY_ID,
        actorId=actor_id,
        sessionId=f"memory-tools-{now:%Y%m%d}",
        eventTimestamp=now,
        payload=[{"conversational": {"content": {"text": content}, "role": "USER"}}],
    )
    print(f"INFO: memory_store actor={actor_id} bytes={len(content)}")
    return {
        "status": "ok",
        "message": (
            "Stored. Durable facts/preferences are extracted asynchronously; "
            "the raw note is deleted after extraction (extracted-only mode)."
        ),
    }


def memory_retrieve(event, actor_id, legacy_actor_id=None):
    query = event.get("query")
    if not query or not isinstance(query, str):
        return _error("invalid_argument", "query (string) is required")
    top_k = _top_k(event)
    results = []
    warnings = []
    for suffix in USER_NAMESPACES:
        for candidate in _user_actor_ids(actor_id, legacy_actor_id):
            if candidate != actor_id and not _legacy_namespace_allowed(candidate):
                continue
            try:
                results.extend(_retrieve(f"users/{candidate}/{suffix}", query, top_k))
            except ClientError as e:
                if candidate == actor_id:
                    raise
                code = e.response.get("Error", {}).get("Code", "")
                if code not in LEGACY_OK_ERRORS:
                    raise
                warning = f"Legacy memory namespace users/{candidate}/{suffix} unavailable: {code}"
                warnings.append(warning)
                print(f"WARNING: {warning}")
    results.sort(key=lambda r: r.get("score") or 0, reverse=True)
    print(f"INFO: memory_retrieve actor={actor_id} results={len(results)}")
    response = {"status": "partial" if warnings else "ok", "results": results[:top_k]}
    if warnings:
        response["warnings"] = warnings
    return response


def org_knowledge_search(event):
    query = event.get("query")
    if not query or not isinstance(query, str):
        return _error("invalid_argument", "query (string) is required")
    top_k = _top_k(event)
    results = _retrieve(ORG_NAMESPACE, query, top_k)
    print(f"INFO: org_knowledge_search results={len(results)}")
    return {"status": "ok", "results": results[:top_k]}


def org_knowledge_add(event, actor_id, groups):
    content = event.get("content")
    if not content or not isinstance(content, str):
        return _error("invalid_argument", "content (string) is required")
    if not ORG_WRITE_GROUPS:
        # Empty allow-list = admins only: curated writes happen out-of-band
        # via the data plane, never through the tool.
        print(f"ERROR: org_knowledge_add denied for {actor_id} — no OrgMemoryWriteGroups configured (admins only)")
        return _error(
            "forbidden",
            "Publishing to org knowledge is restricted to administrators for this deployment.",
        )
    if not set(groups) & set(ORG_WRITE_GROUPS):
        print(f"ERROR: org_knowledge_add denied for {actor_id} — caller groups do not intersect OrgMemoryWriteGroups")
        return _error("forbidden", "You are not in a group entitled to publish org knowledge.")
    title = event.get("title")
    text = f"{title}\n\n{content}" if title and isinstance(title, str) else content
    _memory_client().batch_create_memory_records(
        memoryId=MEMORY_ID,
        records=[
            {
                "requestIdentifier": str(uuid.uuid4()),
                "namespaces": [ORG_NAMESPACE],
                "content": {"text": text},
                "timestamp": datetime.now(timezone.utc),
            }
        ],
    )
    print(f"INFO: org_knowledge_add actor={actor_id} bytes={len(text)}")
    return {"status": "ok", "message": "Published to org knowledge."}


def lambda_handler(event, context):
    """Dispatch one gateway tool call.

    ``event`` contains ONLY the tool's schema arguments (plus, possibly, a
    forwarded ``headers`` map — R9 Q1). Any identity-looking argument in the
    event is deliberately ignored.
    """
    tool = _tool_name(context)
    print(f"INFO: tool call {tool or '<unknown>'} gate={DEPLOY_GATE}")

    if tool == "org_knowledge_search":
        # Shared, read-only, identity-independent: entitlement to reach the
        # gateway at all is enforced upstream (Cedar EntitledGroups).
        if not ENABLE_ORG_MEMORY:
            return _error("disabled", "Org memory is not enabled for this deployment.")
        return org_knowledge_search(event)

    if tool in ("memory_store", "memory_retrieve"):
        if not ENABLE_USER_MEMORY:
            return _error("disabled", "User memory is not enabled for this deployment.")
    elif tool == "org_knowledge_add":
        if not ENABLE_ORG_MEMORY:
            return _error("disabled", "Org memory is not enabled for this deployment.")
    else:
        return _error("unknown_tool", f"Unknown tool '{tool}'.")

    # Identity-dependent tools below: gate first, then derive, then fail
    # closed when identity cannot be established.
    if DEPLOY_GATE != "active":
        return _gated(tool, "identity-dependent tool")

    actor_id, groups, legacy_actor_id = _derive_identity(event, context)
    if not actor_id:
        print(f"ERROR: {tool} refused — no verifiable caller identity in the gateway-forwarded context")
        return _error(
            "identity_unavailable",
            "The gateway did not forward a verifiable caller identity; refusing to touch user memory.",
        )

    if tool == "memory_store":
        return memory_store(event, actor_id)
    if tool == "memory_retrieve":
        return memory_retrieve(event, actor_id, legacy_actor_id)
    return org_knowledge_add(event, actor_id, groups)
