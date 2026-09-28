# Model Lifecycle

Use the model-lifecycle component to detect models entering Legacy, public
extended access, premium pricing, or end of life before they break packages or
distort quota estimates.

## Prerequisites

- An SNS subscription for operator notifications.
- Bedrock control-plane read permissions in the selected region.
- A current model catalog (`gip models check`).

## Deploy and verify

```bash
poetry run gip deploy model-lifecycle
poetry run gip models check
```

The EventBridge schedule runs daily by default. Subscribe to the topic printed
after deployment. Treat `gip models check --propose` as a reviewed catalog
change, not an automatic production update.

The topic the stack creates (`gip-model-lifecycle-alerts`) is encrypted at rest
with a stack-owned KMS key whose policy grants EventBridge (the AWS Health
rule), CloudWatch alarms and the check Lambda the access they need to publish.
When quota monitoring is enabled, `gip deploy model-lifecycle` reuses the quota
stack's `gip-quota-alerts` topic instead and passes that topic's key ARN
(`AlertTopicKmsKeyArn`, read from the quota stack's `QuotaAlertTopicKmsKeyArn`
output) automatically, so the check Lambda can publish to it. If you deploy the
template with CloudFormation directly and point `AlertTopicArn` at an encrypted
topic, pass its key ARN as `AlertTopicKmsKeyArn` yourself; without it the check
Lambda cannot publish and `LifecycleCheckErrorAlarm` fires. An unencrypted
external topic needs no key parameter. The reused quota topic's access policy
and key policy both grant EventBridge, so AWS Health event passthrough works on
the same path as the daily poll.

## Upgrading

After updating the quota stack (`gip deploy quota`), redeploy model-lifecycle
(`gip deploy model-lifecycle`): the quota update encrypts `gip-quota-alerts`,
and an already-deployed check Lambda can publish to it again only once it
receives the key grant through `AlertTopicKmsKeyArn`. A full `gip deploy`
already orders quota before model-lifecycle; only partial redeploys hit this
window, and it is visible through `LifecycleCheckErrorAlarm`.

## Failure and cleanup

Lifecycle-check errors alarm through the stack topic. Follow [Runbook 9](RUNBOOKS.md#9-model-rotation-legacy-premium-pricing-end-of-life) for replacement and redistribution. Remove the component with:

```bash
poetry run gip destroy model-lifecycle
```
