# Component Map

Use this page after choosing a path in [Deployment Paths](DEPLOYMENT_PATHS.md).
Every GIP module is optional except identity/inference access. The profile
created by `gip init` is the source of truth for GIP CloudFormation modules.
Claude Apps Gateway and its Desktop companion are externally owned AWS Samples
CDK applications pinned under `vendor/` and materialized by
`scripts/fetch-claude-apps-gateway.sh`; their upstream configuration is
authoritative. For how each GIP control behaves at runtime — enforcement point,
AWS primitive, and per-user versus account scope — see
[How the Controls Actually Work](HOW_IT_WORKS.md).

| Component | Use it when | Prerequisites | Deploy | Verify / operate | Cleanup |
|---|---|---|---|---|---|
| Identity | Users need temporary Bedrock credentials | IdP app or IAM Identity Center; model access | `gip deploy auth` | `gip test`; [providers](providers/README.md) | `gip destroy auth` |
| Networking | Central services need a VPC | CIDR and two AZs | `gip deploy networking` | [Network Isolation](NETWORK_ISOLATION.md) | `gip destroy networking` |
| Monitoring | Per-user usage dashboards are required | Identity; central or sidecar choice | `gip deploy monitoring`; `gip deploy dashboard` | [Monitoring](MONITORING.md), [Failure Posture](FAILURE_POSTURE.md) | Run `gip destroy dashboard`, then `gip destroy monitoring` |
| Quota | Budgets must alert or block | Verified identity and monitoring | `gip deploy quota` | `gip quota usage`; [Quota](QUOTA_MONITORING.md), Runbook 3 | `gip destroy quota` |
| Server metering | Client telemetry needs tamper resistance | Quota stack; Bedrock invocation logging | `gip deploy metering` | Drift/invalid-record alarms; [Metering](QUOTA_MONITORING.md#server-side-metering-tamper-proof-usage) | `gip destroy metering` |
| Analytics | Historical SQL analysis is needed | Central monitoring with analytics enabled | `gip deploy analytics` | Athena named queries; [Analytics](ANALYTICS.md) | `gip destroy analytics` |
| Apps gateway | Claude apps need a central data plane | Follow upstream DNS/TLS, OIDC, binary, and CDK prerequisites | Pinned upstream `claude-apps-gateway/cdk` (fetched) | Upstream tests and acceptance steps; [Apps Gateway](APPS_GATEWAY.md) | Upstream CDK teardown; `gip destroy gateway` is legacy-only |
| Web search | Governed MCP web search is required | OIDC/IDC and supported AgentCore region | `gip deploy websearch` | [Web Search](WEB_SEARCH.md), live checklist | `gip destroy websearch` |
| Memory | User/org memory is approved | Web-search gateway; KMS; deploy gate | `gip deploy memory` | [Memory](MEMORY.md), erasure and sweeper alarms | `gip destroy memory` |
| Skills registry | Skills need publish/approve/distribute governance | S3 artifacts and Agent Registry GA | `gip deploy skills` | `gip skills list`; [Skills](SKILLS_REGISTRY.md) | `gip destroy skills` plus retained-registry procedure |
| Guardrails | Account-level Bedrock Guardrails are required | Guardrail policy and target regions | `gip deploy guardrails` | [Guardrails](GUARDRAILS.md) | `gip destroy guardrails` |
| Model lifecycle | Operators need legacy/EOL alerts | SNS subscription | `gip deploy model-lifecycle` | `gip models check`; [Model Lifecycle](MODEL_LIFECYCLE.md) | `gip destroy model-lifecycle` |
| Desktop bootstrap | Desktop needs gateway-native or PKCE config delivery | Deployed upstream gateway; follow the selected upstream contract | Gateway `desktop` policy or pinned upstream companion CDK | [Desktop Bootstrap](BOOTSTRAP_SERVER.md) | Upstream CDK teardown; `gip destroy bootstrap` is legacy-only |
| Distribution | IT needs packages or a landing page | Built package; optional IdP web app | `gip deploy distribution`; `gip distribute` | [Distribution](distribution/comparison.md) | `gip destroy distribution` |

For all components, start incident response at [Failure Posture](FAILURE_POSTURE.md)
and [Runbooks](RUNBOOKS.md). `gip status` gives deployment state; `gip doctor`
checks local package/config health.
