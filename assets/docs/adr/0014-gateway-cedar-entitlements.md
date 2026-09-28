# ADR-0014: Gateway per-user entitlement via Cedar Policy engine

Status: Accepted; current entitlement design rejected after live validation · Date: 2026-07-08 · Live result: 2026-08-14

## Context

REVIEW.md finding #5: the websearch gateway's `CUSTOM_JWT` authorizer validates
only `aud == client_id` (`bedrock-agentcore-gateway.yaml`), so every user in the
IdP application — including users with no Bedrock entitlement — can invoke the
usage-billed search tool. There is also no per-user metering of search spend.
Research lane R9 (an internal research memo) ranked the available
authorization mechanisms for this gateway.

## Decision

Attach a Cedar policy engine to the gateway, expressed entirely in the existing
template (minimal diff, A1): new resources `AWS::BedrockAgentCore::PolicyEngine`
+ `AWS::BedrockAgentCore::Policy`, plus `PolicyEngineConfiguration` on
`AWS::BedrockAgentCore::Gateway` — all no-interruption updates (R9 F3). Gated on
a new optional `EntitledGroups` parameter (empty default = current behavior, A7)
AND `AuthType=oidc` (Cedar principals are JWT-derived; IDC/SigV4 unchanged). One
permit policy scoped to this gateway's ARN; Cedar's engine-level default-deny
supplies the deny posture. The groups check mirrors the quota authorizer's
claim-name variance (`groups` or `cognito:groups`,
`lambda-functions/quota_check/index.py:extract_groups_from_claims`) using
set-intersection (`containsAny`). Staging: `PolicyMode` parameter maps to the
engine's `LOG_ONLY` (default) | `ENFORCE` mode; the policy itself stays
`ACTIVE` so the engine mode is the single switch. Cost: $0.000025/authz request
(R9 F5); no standing charge.

## Alternatives considered

- **Authorizer claim narrowing (`CustomClaims`)** — zero cost/latency,
  pure-CFN, but gateway-wide: cannot allow web search while denying other
  (future) targets per group, and offers no LOG_ONLY staging or decision
  traces. R9 ranks it as a baseline layer, not the standalone fix. Rejected as
  the primary mechanism; may be layered on later.
- **Lambda REQUEST interceptor** — most expressive (can rewrite requests,
  inject server-derived identity into tool args) but max one per gateway, adds
  a synchronous Lambda hop to every call including `tools/list`, and is custom
  code owned forever. Reserved for the memory lane (E-G2) if Cedar's
  `context.input` binding proves insufficient for actor-id derivation (R9 F7/F8).

## Live-verification result

LV-8 resolved the assumption negatively for Cognito on 2026-08-14. AgentCore
exposes both `groups` and `cognito:groups` to the Cedar policy as `String`, not
as a set, so the proposed `containsAny` policy cannot be created. This is a
deployment-time incompatibility, not a safe entitlement implementation. The
product now rejects non-empty `EntitledGroups` and `PolicyMode=ENFORCE` with a
clear error until an exact string-representation design replaces this policy.
Okta representation remains untested and must not be inferred from Cognito.

## Consequences

- Per-user search-spend metering remains open. The rejected Cedar path does not
  provide interim per-user decision traces.
- Group entitlement is unavailable in the current implementation. Empty
  `EntitledGroups` preserves gateway deployment without this policy engine.
- New profile fields `websearch_entitled_groups` / `websearch_policy_mode`
  (defaults preserve old configs); answers-file keys
  `web_search.entitled_groups` / `web_search.policy_mode`.

## Evidence

- Template: `deployment/infrastructure/bedrock-agentcore-gateway.yaml`
  (EntitledGroups/PolicyMode params, HasEntitlement condition,
  GatewayPolicyEngine/GatewayEntitlementPolicy resources).
- Wiring: `source/governed_inference_platform/cli/commands/deploy.py:build_websearch_params`,
  `config.py` Profile fields, `init.py` wizard question, `init_answers.py`.
- Tests: `source/tests/test_websearch_gateway.py` (template contract + params),
  `source/tests/cli/commands/test_init_from_file.py` (non-interactive).
- CFN shapes verified in the CloudFormation Template Reference (retrieved
  2026-07-08): aws-resource-bedrockagentcore-policyengine, -policy,
  aws-properties-bedrockagentcore-gateway-gatewaypolicyengineconfiguration,
  -policy-policydefinition, -policy-cedarpolicy.
