# ADR-0016: AgentCore Memory stack — server-derived actorId behind a deploy gate

Status: Accepted for log-only deployment; active mode rejected pending identity redesign · Date: 2026-07-09 · Live result: 2026-08-14

## Context

ADR-0012 Decision 4: memory ships opt-in with graduated modes, and "actorId
must derive from the validated JWT, never a tool argument" is a hard gate.
Research lane R9 (an internal research memo) verified the CFN
surface (`AWS::BedrockAgentCore::Memory`, second `GatewayTarget` from a
separate stack via plain-string `GatewayIdentifier` — F17) and found that a
plain Lambda gateway target receives **no JWT claims** (F8): the event carries
only the tool's schema arguments. R9's v1 sketch had clients send `actor_id`
as a tool argument bound by a Cedar `context.input.actor_id == principal.id`
policy (F4) — which conflicts with the ADR-0012 hard gate and rests on an
unverified assumption (Cedar `context.input` for Lambda targets, R9 risk 2).

## Decision

Separate stack `memory-stack.yaml` (opt-in, A3/A7): one Memory resource
(CMK-encrypted, stable underscore name from `identity_pool_name` — Name and
EncryptionKeyArn are replacement-on-update, R9 F10/risk 6/7), strategies
userPreference+semantic on `users/{actorId}/...` and semantic on
`org/knowledge`, four MCP tools on a second target of the existing websearch
gateway. **No tool schema declares an identity argument.** The Lambda derives
actorId exclusively from the Authorization header forwarded via the target's
`MetadataConfiguration.AllowedRequestHeaders` (R9 F19) — signature already
validated by the gateway's CUSTOM_JWT authorizer; function invokable only by
the gateway execution role. Identity-looking args are ignored; no identity →
refuse (fail closed). Defense-in-depth: `bedrock-agentcore:namespacePath` IAM
conditions (`users/*` / `org/*`, R9 F12); org writes gated on
`OrgMemoryWriteGroups` (empty = deny all, curate out-of-band); ADR-0014's
Cedar entitlement still gates gateway access. Whether Lambda targets surface
the forwarded header is undocumented (R9 Q1), so a **`DeployGate` parameter
(default `log-only`)** makes identity-dependent tools return a gated response
with zero data-plane calls until the live checklist (below) passes.
`extracted-only` default pins `EventExpiryDuration=3` (service minimum) with
a daily T+24h sweeper (`ListActors→ListSessions→ListEvents→DeleteEvent`);
erasure is `gip memory forget-user` (enumerate-and-delete + ≤100/batch
`BatchDeleteMemoryRecords`). Data classification: `assets/docs/MEMORY.md`.

## Alternatives considered

- **Cedar `context.input.actor_id` binding (R9 v1 sketch)** — requires
  `actor_id` as a tool argument, violating the ADR-0012 hard gate; binding
  behavior for Lambda targets is itself unverified (R9 Q2). Rejected.
- **Lambda REQUEST interceptor injecting actorId (R9 F7)** — documented
  identity-propagation path, but max one per gateway (shared surface with
  future lanes), adds a synchronous hop to every gateway call, custom code
  forever. Reserved as the fallback if the live test shows headers do not
  reach Lambda targets.
- **Kinesis-triggered delete-after-extraction** — `MemoryRecordCreated`
  events don't reference the source raw event (R9 F14); correlation
  impossible. Scheduled sweep chosen.

## Live-verification result

LV-9 resolved the header-forwarding path negatively at deployment on
2026-08-14: AgentCore rejects `Authorization` in
`MetadataConfiguration.AllowedRequestHeaders`. The memory stack remains
deployable in `log-only` mode, but identity-dependent tools cannot be activated
through this design. `DeployGate=active` is rejected until identity propagation
is redesigned. The checklist below is retained as the acceptance contract for
that future design, not as an available activation procedure.

## Acceptance checklist for a redesigned active mode

1. Header propagation: with a valid JWT, the Lambda logs show the forwarded
   Authorization value (client_context.custom or event headers).
2. Derived actorId equals `email:<sha256(lowercase-email)>` for the JWT email
   the gateway validated (raw email punctuation is not a valid AgentCore
   Memory actorId).
3. Forged-identity probe: `actor_id=victim@…` in args with alice's token
   touches only alice's namespaces.
4. Extraction timing supports the 24h sweep horizon (R9 Q4).
5. Second-target lifecycle: create/delete leaves web search intact (R9 Q5).

## Consequences

- User-memory tools remain visibly inert (gated responses) in the supported
  log-only deployment.
- If the header never propagates, the interceptor fallback changes the
  Lambda's derivation source but not the tool schemas or the CLI surface.
- Per-user memory-spend attribution remains open (same gap as web search).
