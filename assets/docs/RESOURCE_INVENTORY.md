# AWS Resource Inventory Per Stack

> **Last updated:** 2026-09-02

> **Purpose:** Pre-deployment reference for customers under restrictive SCPs.
> Use this to plan IAM/SCP exemptions or decide which stacks to skip before running `gip deploy`.
> This is a static point-in-time list — verify against the actual templates if your deployment uses a newer version.

## Stack Deployment Order

```
auth → [guardrails] → [networking] → [s3bucket] → [monitoring] → [dashboard] → [analytics] → [quota] →
[metering] → [model-lifecycle] → [codebuild] → [distribution] → [websearch] → [memory] → [skills]
```

Stacks in `[]` are optional. Only `auth` is strictly required for basic Claude Code access.

---

## auth (bedrock-auth-{provider})

**Template:** `bedrock-auth-{cognito-pool|okta|azure|auth0|google|generic}.yaml`
**Required:** Yes (core authentication)

| Resource Type | Service Namespace | Count |
|---|---|---|
| `AWS::IAM::OIDCProvider` | `iam` | 1 in `direct` mode; in `cognito` mode, 1 for external-provider templates and 0 for `cognito-pool` |
| `AWS::IAM::ManagedPolicy` | `iam` | 1 |
| `AWS::IAM::Role` | `iam` | 1 in default `direct` mode; 2 in `cognito` mode |
| `AWS::Cognito::IdentityPool` | `cognito-identity` | 1 in `cognito` mode only |
| `AWS::Cognito::IdentityPoolRoleAttachment` | `cognito-identity` | 1 in `cognito` mode only |
| `AWS::Cognito::IdentityPoolPrincipalTag` | `cognito-identity` | 1 in `cognito` mode only |
| `AWS::Logs::LogGroup` | `logs` | 1 when monitoring is enabled |

`FederationType` defaults to `direct`; Cognito Identity Pool resources are not
created in that mode. External-provider templates retain their IAM OIDC
provider in either mode. The `cognito-pool` template creates its IAM OIDC
provider only in direct mode. The managed Bedrock policy exists in every mode.
All six managed templates expose optional `OidcThumbprintList` input. On an
update, `gip deploy` discovers the provider with
`cloudformation:DescribeStackResources`, reads its current list with
`iam:GetOpenIDConnectProvider`, and passes that list back unchanged.

**IAM actions required for deployment:**
- `cognito-identity:*`
- `iam:CreateRole`, `iam:PutRolePolicy`, `iam:AttachRolePolicy`, `iam:CreateOpenIDConnectProvider`, `iam:CreatePolicy`
- `iam:GetOpenIDConnectProvider` — `gip deploy` reads an existing provider's thumbprint list before updating the stack and passes it back (IAM rejects removing it)
- `logs:CreateLogGroup`, `logs:PutRetentionPolicy`

---

## auth (legacy cognito-identity-pool)

**Template:** `cognito-identity-pool.yaml`
**Required:** No (legacy, operator-managed; `gip deploy` does not deploy or update it)

| Resource Type | Service Namespace | Count |
|---|---|---|
| `AWS::Cognito::IdentityPool` | `cognito-identity` | 1 |
| `AWS::Cognito::IdentityPoolRoleAttachment` | `cognito-identity` | 1 |
| `AWS::Cognito::IdentityPoolPrincipalTag` | `cognito-identity` | 1 |
| `AWS::IAM::OIDCProvider` | `iam` | 1 in external-OIDC mode only |
| `AWS::IAM::ManagedPolicy` | `iam` | 1 |
| `AWS::IAM::Role` | `iam` | 2 + 1 when Bedrock tracking is enabled |
| `AWS::Logs::LogGroup` | `logs` | 1 when Bedrock tracking is enabled |
| `AWS::S3::Bucket` | `s3` | 1 when Bedrock tracking is enabled |
| `AWS::S3::BucketPolicy` | `s3` | 1 when Bedrock tracking is enabled |
| `AWS::CloudTrail::Trail` | `cloudtrail` | 1 when Bedrock tracking is enabled |

This seventh auth template has the same optional thumbprint shape under the
legacy parameter name `OIDCThumbprintList`. The operator must preserve its
current value on update. Its primary outputs are `IdentityPoolId` and
`BedrockRoleArn`; monitoring outputs are conditional on Bedrock tracking.

**IAM actions required for deployment:**
- `cognito-identity:*`
- `iam:CreateRole`, `iam:PutRolePolicy`, `iam:AttachRolePolicy`, `iam:CreateOpenIDConnectProvider`, `iam:CreatePolicy`
- `logs:CreateLogGroup`, `s3:CreateBucket`, `s3:PutBucketPolicy`, `cloudtrail:CreateTrail` when Bedrock tracking is enabled

---

## guardrails

**Template:** `guardrails-enforcement.yaml`
**Required:** No (optional, opt-in account-level Bedrock Guardrails enforcement)

| Resource Type | Service Namespace | Count |
|---|---|---|
| `AWS::Bedrock::Guardrail` | `bedrock` | 1 |
| `AWS::Bedrock::GuardrailVersion` | `bedrock` | 1 |
| `AWS::Bedrock::EnforcedGuardrailConfiguration` | `bedrock` | 1 |
| `AWS::CloudWatch::Dashboard` | `cloudwatch` | 1 |

**IAM actions required for deployment:**
- `bedrock:CreateGuardrail`, `bedrock:CreateGuardrailVersion`, plus the control-plane actions for `AWS::Bedrock::EnforcedGuardrailConfiguration` (account-level enforced guardrail configuration)
- `cloudwatch:PutDashboard`

**Scope warning:** the enforced configuration is **account-level per region**. With the default empty `ModelIncludeList`, the guardrail is enforced on every Bedrock model invocation in the account and region — not just Claude Code traffic.

---

## networking

**Template:** `networking.yaml`
**Required:** Only for central monitoring mode

| Resource Type | Service Namespace | Count |
|---|---|---|
| `AWS::EC2::VPC` | `ec2` | 1 |
| `AWS::EC2::InternetGateway` | `ec2` | 1 |
| `AWS::EC2::VPCGatewayAttachment` | `ec2` | 1 |
| `AWS::EC2::Subnet` | `ec2` | 2 |
| `AWS::EC2::RouteTable` | `ec2` | 1 |
| `AWS::EC2::Route` | `ec2` | 1 |
| `AWS::EC2::SubnetRouteTableAssociation` | `ec2` | 2 |

**IAM actions required for deployment:**
- `ec2:CreateVpc`, `ec2:CreateSubnet`, `ec2:CreateInternetGateway`, `ec2:AttachInternetGateway`
- `ec2:CreateRouteTable`, `ec2:CreateRoute`, `ec2:AssociateRouteTable`
- `ec2:DescribeVpcs`, `ec2:DescribeSubnets`, `ec2:DescribeRouteTables`

---

## s3bucket

**Template:** `s3bucket.yaml`
**Required:** For central monitoring + quota (Lambda packaging)

| Resource Type | Service Namespace | Count |
|---|---|---|
| `AWS::S3::Bucket` | `s3` | 2 (artifact + access-log destination) |
| `AWS::S3::BucketPolicy` | `s3` | 2 |

Both policies are unconditional so TLS-only and log-delivery controls always
apply. `OrganizationId` conditionally adds one org-wide read statement to
`CfnArtifactsBucketPolicy`; it does not condition the policy resource. The
artifact-bucket output is `CfnArtifactsBucket`.

**IAM actions required for deployment:**
- `s3:CreateBucket`, `s3:PutBucketPolicy`, `s3:PutEncryptionConfiguration`

---

## monitoring (otel-collector)

**Template:** `otel-collector.yaml`
**Required:** For usage tracking and dashboards

| Resource Type | Service Namespace | Count |
|---|---|---|
| `AWS::ECS::Cluster` | `ecs` | 1 |
| `AWS::ECS::Service` | `ecs` | 1 active (2 conditional variants) |
| `AWS::ECS::TaskDefinition` | `ecs` | 1 |
| `AWS::EC2::SecurityGroup` | `ec2` | 2 |
| `AWS::ElasticLoadBalancingV2::LoadBalancer` | `elasticloadbalancing` | 1 |
| `AWS::ElasticLoadBalancingV2::Listener` | `elasticloadbalancing` | 1 + 1 conditional |
| `AWS::ElasticLoadBalancingV2::ListenerRule` | `elasticloadbalancing` | 2 conditional |
| `AWS::ElasticLoadBalancingV2::TargetGroup` | `elasticloadbalancing` | 2 |
| `AWS::IAM::Role` | `iam` | 2 |
| `AWS::Logs::LogGroup` | `logs` | 2 + 1 conditional |
| `AWS::SSM::Parameter` | `ssm` | 1 |
| `AWS::CertificateManager::Certificate` | `acm` | 1 conditional |
| `AWS::Route53::RecordSet` | `route53` | 1 conditional |
| `AWS::CloudWatch::Alarm` | `cloudwatch` | 1 |

**IAM actions required for deployment:**
- `ecs:CreateCluster`, `ecs:CreateService`, `ecs:RegisterTaskDefinition`
- `ec2:CreateSecurityGroup`, `ec2:AuthorizeSecurityGroupIngress`
- `elasticloadbalancing:CreateLoadBalancer`, `elasticloadbalancing:CreateTargetGroup`, `elasticloadbalancing:CreateListener`
- `iam:CreateRole`, `iam:PutRolePolicy`, `iam:PassRole`
- `logs:CreateLogGroup`
- `ssm:PutParameter`
- `acm:RequestCertificate` (if custom domain)
- `route53:ChangeResourceRecordSets` (if custom domain)
- `cloudwatch:PutMetricAlarm`

Exactly one ECS service is created: the HTTP variant only when the explicit
internal local-development parameter `AllowInsecureHttpIngress=true` is set, or
the HTTPS variant when TLS is enabled. Plaintext is rejected by default and is
always rejected for internet-facing ALBs. Both target groups exist, but
the verified target group is registered with the service only when complete JWT
ingress parameters are supplied. The HTTPS listener, its two listener rules,
the certificate, and DNS record are conditional. The third log group exists
only when analytics is enabled. The template creates one collector-unhealthy
alarm. Autoscaling resources are commented out and are not deployed.
The collector runs as a single Fargate task (0.5 vCPU, 1 GB, `DesiredCount: 1`), so it has
no Availability Zone redundancy, and its capacity has not been load-tested. If it saturates,
dashboards and client-reported quota usage go stale; inference is unaffected because model
requests never pass through the collector. Before enabling the commented-out scaling target,
change its `ResourceId` to reference the deployed service (for example
`!Sub 'service/${ECSCluster}/${ECSServiceHTTPS.Name}'`): neither ECS service sets
`ServiceName`, so the hard-coded `otel-collector-service` does not match.

`CollectorImage` defaults to OpenTelemetry Collector Contrib 0.156.0 from
Docker Hub, pinned to
`sha256:125bdbeb7590cc1952c5b3430ecf14063568980c2c93d5b38676cc0446ed8108`.
Contrib is not published to ECR Public. For isolated deployments, mirror that
digest into private ECR and override `CollectorImage`; see
[Network isolation](NETWORK_ISOLATION.md).

---

## dashboard

**Template:** `claude-code-dashboard.yaml`
**Required:** No (optional visualization)

| Resource Type | Service Namespace | Count |
|---|---|---|
| `AWS::CloudWatch::Dashboard` | `cloudwatch` | 1 |

**IAM actions required for deployment:**
- `cloudwatch:PutDashboard`

---

## cowork-dashboard

**Template:** `cowork-dashboard.yaml`
**Required:** No (optional, Claude Desktop-specific)

| Resource Type | Service Namespace | Count |
|---|---|---|
| `AWS::CloudWatch::Dashboard` | `cloudwatch` | 1 |
| `AWS::Logs::MetricFilter` | `logs` | 12 |

**IAM actions required for deployment:**
- `cloudwatch:PutDashboard`
- `logs:PutMetricFilter`

---

## analytics

**Template:** `analytics-pipeline.yaml`
**Required:** No (optional, for SQL queries on usage data)

| Resource Type | Service Namespace | Count |
|---|---|---|
| `AWS::S3::Bucket` | `s3` | 3 (log destination, analytics data, Athena results) |
| `AWS::S3::BucketPolicy` | `s3` | 1 |
| `AWS::IAM::Role` | `iam` | 3 |
| `AWS::Lambda::Function` | `lambda` | 1 |
| `AWS::KinesisFirehose::DeliveryStream` | `firehose` | 1 |
| `AWS::Logs::SubscriptionFilter` | `logs` | 1 |
| `AWS::Glue::Database` | `glue` | 1 |
| `AWS::Glue::Table` | `glue` | 1 |
| `AWS::Athena::WorkGroup` | `athena` | 1 |
| `AWS::Athena::NamedQuery` | `athena` | 14 |

**IAM actions required for deployment:**
- `s3:CreateBucket`, `s3:PutBucketPolicy`
- `iam:CreateRole`, `iam:PutRolePolicy`, `iam:PassRole`
- `lambda:CreateFunction`, `lambda:AddPermission`, `lambda:PutFunctionConcurrency`
- `firehose:CreateDeliveryStream`
- `logs:PutSubscriptionFilter`
- `glue:CreateDatabase`, `glue:CreateTable`
- `athena:CreateWorkGroup`, `athena:CreateNamedQuery`

---

## quota

**Template:** `quota-monitoring.yaml`
**Required:** No (optional, for per-user token limits)

| Resource Type | Service Namespace | Count |
|---|---|---|
| `AWS::DynamoDB::Table` | `dynamodb` | 2 |
| `AWS::Lambda::Function` | `lambda` | 2 + 1 when bypass detection is enabled |
| `AWS::Lambda::Permission` | `lambda` | 2 + 1 when bypass detection is enabled |
| `AWS::IAM::Role` | `iam` | 2 + 1 when bypass detection is enabled |
| `AWS::IAM::ManagedPolicy` | `iam` | 1 in IAM-auth mode; + 1 with an encrypted effective alert topic; + 1 when that topic is encrypted and bypass detection is enabled |
| `AWS::SQS::Queue` | `sqs` | 1 + 1 when bypass detection is enabled (dead-letter queues, `alias/aws/sqs`) |
| `AWS::SNS::Topic` | `sns` | 1 (conditional: only when `AlertTopicArn` is empty) |
| `AWS::SNS::TopicPolicy` | `sns` | 1 (conditional, with the topic) |
| `AWS::KMS::Key` | `kms` | 1 (conditional, `QuotaAlertTopicKey`) |
| `AWS::KMS::Alias` | `kms` | 1 (conditional, `QuotaAlertTopicKeyAlias`) |
| `AWS::Events::Rule` | `events` | 1 + 1 when bypass detection is enabled |
| `AWS::CloudWatch::Alarm` | `cloudwatch` | 3 |
| `AWS::ApiGatewayV2::Api` | `apigateway` | 1 |
| `AWS::ApiGatewayV2::Authorizer` | `apigateway` | 1 in JWT/OIDC mode only |
| `AWS::ApiGatewayV2::Integration` | `apigateway` | 1 |
| `AWS::ApiGatewayV2::Route` | `apigateway` | 1 |
| `AWS::ApiGatewayV2::Stage` | `apigateway` | 1 |

The API route always exists. Supplying `OidcIssuerUrl` selects a JWT authorizer;
leaving it empty selects `AWS_IAM` and creates `QuotaApiInvokePolicy` instead.
The topic, `QuotaAlertTopicKey`, `QuotaAlertTopicKeyAlias`, and topic policy
exist only when `AlertTopicArn` is empty. Lambda KMS grants target either the
created key or the customer-supplied `AlertTopicKmsKeyArn`; no grant exists for
an unencrypted external topic. Customer-owned topic and key resource policies
remain customer responsibilities, including cross-account access.
`QuotaAlertTopicArn` always exports the effective topic, while conditional
output `QuotaAlertTopicKmsKeyArn` exports the created or supplied key for
model-lifecycle's `AlertTopicKmsKeyArn` input.

This stack creates at most one billable customer-managed key; its alias is not a
second key. `QuotaMonitorDLQ`/`SidecarMonitorDLQ` use `alias/aws/sqs`, and both
DynamoDB tables use `alias/aws/dynamodb`; those AWS-managed aliases do not add a
monthly customer-managed-key charge.

**IAM actions required for deployment:**
- `dynamodb:CreateTable`, `dynamodb:DescribeTable`
- `lambda:CreateFunction`, `lambda:AddPermission`
- `iam:CreateRole`, `iam:PutRolePolicy`, `iam:PassRole`, `iam:CreatePolicy`, `iam:AttachRolePolicy`
- `sqs:CreateQueue`
- `sns:CreateTopic`, `sns:SetTopicAttributes`
- `kms:CreateKey`, `kms:CreateAlias`, `kms:PutKeyPolicy`, `kms:EnableKeyRotation` (if no existing topic is supplied)
- `events:PutRule`, `events:PutTargets`
- `apigateway:CreateApi`, `apigateway:CreateRoute`, `apigateway:CreateIntegration`, `apigateway:CreateStage`, `apigateway:CreateAuthorizer`
- `cloudwatch:PutMetricAlarm`

---

## metering (quota-metering)

**Template:** `quota-metering.yaml`
**Required:** No (optional, server-side usage metering from Bedrock model invocation logs; deployed once per allowed Bedrock region)

| Resource Type | Service Namespace | Count |
|---|---|---|
| `AWS::Logs::LogGroup` | `logs` | 1 |
| `AWS::Logs::SubscriptionFilter` | `logs` | 1 |
| `AWS::IAM::Role` | `iam` | 3 |
| `AWS::Lambda::Function` | `lambda` | 2 |
| `AWS::Lambda::Permission` | `lambda` | 1 |
| `AWS::SQS::Queue` | `sqs` | 1 (dead-letter queue, `alias/aws/sqs`) |
| `AWS::CloudWatch::Alarm` | `cloudwatch` | 5 |
| `Custom::BedrockInvocationLoggingConfig` | `cloudformation` | 1 |

**IAM actions required for deployment:**
- `logs:CreateLogGroup`, `logs:PutRetentionPolicy`, `logs:PutSubscriptionFilter`
- `iam:CreateRole`, `iam:PutRolePolicy`, `iam:AttachRolePolicy`, `iam:PassRole`
- `lambda:CreateFunction`, `lambda:AddPermission`
- `sqs:CreateQueue`
- `cloudwatch:PutMetricAlarm`

**Runtime/custom-resource note:** the logging-config custom resource Lambda calls `bedrock:GetModelInvocationLoggingConfiguration`, `bedrock:PutModelInvocationLoggingConfiguration`, and `bedrock:DeleteModelInvocationLoggingConfiguration` during stack create/update/delete — the `bedrock` namespace must be allowed. The Bedrock invocation logging configuration is a per-account, per-region **singleton**; if one already exists the stack fails closed unless `AdoptExistingConfig=true` (adopt mode only attaches a subscription filter to the existing log group). The processor Lambda performs cross-region DynamoDB `GetItem` plus transactional writes authorized by `PutItem`/`UpdateItem` against the quota stack's `UserQuotaMetrics` table. One of the five alarms (`MantleEndpointUsageAlarm`) is a Metrics Insights query alarm over the `AWS/BedrockMantle` namespace — a governance tripwire for unmetered bedrock-mantle endpoint usage. The DLQ's AWS-managed `alias/aws/sqs` reference creates no customer-managed key.

---

## model-lifecycle

**Template:** `model-lifecycle.yaml`
**Required:** No (optional, daily model lifecycle alerts — legacy / provider-set premium pricing / end-of-life countdown)

| Resource Type | Service Namespace | Count |
|---|---|---|
| `AWS::SNS::Topic` | `sns` | 1 (conditional) |
| `AWS::SNS::TopicPolicy` | `sns` | 1 (conditional) |
| `AWS::KMS::Key` | `kms` | 1 (conditional, `LifecycleAlertTopicKey`) |
| `AWS::KMS::Alias` | `kms` | 1 (conditional, `LifecycleAlertTopicKeyAlias`) |
| `AWS::SSM::Parameter` | `ssm` | 2 |
| `AWS::IAM::Role` | `iam` | 1 |
| `AWS::IAM::ManagedPolicy` | `iam` | Up to 1 active (2 mutually exclusive conditional variants) |
| `AWS::Lambda::Function` | `lambda` | 1 |
| `AWS::Lambda::Permission` | `lambda` | 1 |
| `AWS::SQS::Queue` | `sqs` | 1 (dead-letter queue, `alias/aws/sqs`) |
| `AWS::Events::Rule` | `events` | 2 |
| `AWS::CloudWatch::Alarm` | `cloudwatch` | 1 |

The topic, policy, `LifecycleAlertTopicKey`, and
`LifecycleAlertTopicKeyAlias` are created only when no `AlertTopicArn` is
supplied. The topic policy grants EventBridge and CloudWatch publish access.
Exactly one Lambda KMS policy is active for either the stack key or an encrypted
external topic named by `AlertTopicKmsKeyArn`; neither policy exists for an
unencrypted external topic. `LifecycleAlertTopicArn` outputs the effective
created or reused topic. With quota enabled, `gip deploy` passes both
`QuotaAlertTopicArn` and conditional `QuotaAlertTopicKmsKeyArn`, so this stack
normally creates no second alert-topic key. The DLQ uses AWS-managed
`alias/aws/sqs`, not another billable customer-managed key. The two EventBridge
rules are the daily poll and AWS Health `scheduledChange` passthrough.

**IAM actions required for deployment:**
- `sns:CreateTopic`, `sns:SetTopicAttributes`
- `kms:CreateKey`, `kms:CreateAlias`, `kms:PutKeyPolicy`, `kms:EnableKeyRotation` (if no existing topic is supplied)
- `sqs:CreateQueue`
- `ssm:PutParameter`
- `iam:CreateRole`, `iam:PutRolePolicy`, `iam:AttachRolePolicy`, `iam:PassRole`, `iam:CreatePolicy`
- `lambda:CreateFunction`, `lambda:AddPermission`
- `events:PutRule`, `events:PutTargets`
- `cloudwatch:PutMetricAlarm`

**Runtime note:** the check Lambda calls `bedrock:ListFoundationModels` and `bedrock:GetFoundationModel` — the `bedrock` namespace must be allowed at runtime.

---

## codebuild

**Template:** `codebuild-windows.yaml`
**Required:** No (optional, for Windows binary builds)
**Region restriction:** us-east-1, us-east-2, us-west-2, eu-west-1, ap-southeast-2 only

| Resource Type | Service Namespace | Count |
|---|---|---|
| `AWS::S3::Bucket` | `s3` | 2 |
| `AWS::S3::BucketPolicy` | `s3` | 1 (access-log bucket: log-delivery grant + TLS-only) |
| `AWS::IAM::Role` | `iam` | 1 |
| `AWS::Logs::LogGroup` | `logs` | 3 |
| `AWS::CodeBuild::Project` | `codebuild` | 3 (Windows, Linux x64, Linux ARM64) |

Build artifacts are written with SSE-KMS under the AWS-managed `aws/s3` key
(no key cost; no extra IAM for same-account readers).

**IAM actions required for deployment:**
- `s3:CreateBucket`, `s3:PutBucketPolicy`
- `iam:CreateRole`, `iam:PutRolePolicy`, `iam:PassRole`
- `logs:CreateLogGroup`
- `codebuild:CreateProject`

---

## distribution (legacy IAM-user)

**Template:** `distribution.yaml`
**Required:** No (legacy manual/quick-create template; `gip deploy distribution` uses `presigned-s3-distribution.yaml`)

| Resource Type | Service Namespace | Count |
|---|---|---|
| `AWS::S3::Bucket` | `s3` | 2 |
| `AWS::S3::BucketPolicy` | `s3` | 1 (access-log delivery + TLS-only) |
| `AWS::IAM::Group` | `iam` | 1 |
| `AWS::IAM::User` | `iam` | 1 in IAM-user mode |
| `AWS::IAM::AccessKey` | `iam` | 1 in IAM-user mode |
| `AWS::IAM::Policy` | `iam` | 1 in IAM-user mode |
| `AWS::SecretsManager::Secret` | `secretsmanager` | 1 in IAM-user mode |

The single `IdentityPoolName` parameter names the resources. Outputs expose the
distribution bucket, credential secret, and access-log bucket. This template
always creates static IAM credentials; use the presigned template's role mode
where SCPs prohibit access keys.

**IAM actions required for deployment:**
- `s3:CreateBucket`, `s3:PutBucketPolicy`
- `iam:CreateGroup`, `iam:CreateUser`, `iam:AddUserToGroup`, `iam:CreateAccessKey`, `iam:PutUserPolicy`
- `secretsmanager:CreateSecret`

---

## distribution (presigned-s3)

**Template:** `presigned-s3-distribution.yaml`
**Required:** No (optional, for binary distribution via presigned URLs)

| Resource Type | Service Namespace | Count |
|---|---|---|
| `AWS::S3::Bucket` | `s3` | 2 |
| `AWS::S3::BucketPolicy` | `s3` | 1 (access-log bucket: log-delivery grant + TLS-only) |
| `AWS::IAM::Group` | `iam` | 1 in IAM-user mode |
| `AWS::IAM::User` | `iam` | 1 |
| `AWS::IAM::AccessKey` | `iam` | 1 |
| `AWS::IAM::Policy` | `iam` | 1 |
| `AWS::SecretsManager::Secret` | `secretsmanager` | 1 |
| `AWS::IAM::Role` | `iam` | 1 in role mode instead of the five IAM-user resources above |

`PresignPrincipalType` defaults to `iam-user`. In `role` mode the IAM user,
group, access key, inline policy, and secret are not created; only the assumable
presigning role is created. `DistributionSecretArn`/`DistributionSecretName`
are IAM-user-only outputs; `PresignRoleArn` is role-only.

**IAM actions required for deployment:**
- `s3:CreateBucket`, `s3:PutBucketPolicy`
- `iam:CreateGroup`, `iam:CreateUser`, `iam:AddUserToGroup`, `iam:CreateAccessKey`, `iam:PutUserPolicy` (IAM-user mode)
- `iam:CreateRole`, `iam:PutRolePolicy` (role mode)
- `secretsmanager:CreateSecret` (IAM-user mode)

---

## distribution (landing-page)

**Template:** `landing-page-distribution.yaml`
**Required:** No (optional, ALB-based landing page with IdP auth)

| Resource Type | Service Namespace | Count |
|---|---|---|
| `AWS::S3::Bucket` | `s3` | 2 |
| `AWS::S3::BucketPolicy` | `s3` | 2 |
| `AWS::EC2::SecurityGroup` | `ec2` | 1 |
| `AWS::ElasticLoadBalancingV2::LoadBalancer` | `elasticloadbalancing` | 1 |
| `AWS::ElasticLoadBalancingV2::Listener` | `elasticloadbalancing` | 1 |
| `AWS::ElasticLoadBalancingV2::TargetGroup` | `elasticloadbalancing` | 1 |
| `AWS::Lambda::Function` | `lambda` | 1 + 1 in Cognito mode |
| `AWS::Lambda::Permission` | `lambda` | 1 |
| `AWS::IAM::Role` | `iam` | 1 + 1 in Cognito mode |
| `AWS::Logs::LogGroup` | `logs` | 1 |
| `AWS::CertificateManager::Certificate` | `acm` | 1 |
| `AWS::Route53::RecordSet` | `route53` | 1 when `HostedZoneId` is set |
| `AWS::CloudFormation::CustomResource` | `cloudformation` | 1 in Cognito mode |

The Cognito-only role, function, and custom resource update the distribution
web client's callback URLs. `DistributionURL` and `IdPRedirectURI` are always
output; `CognitoCallbackStatus` is conditional.

**IAM actions required for deployment:**
- `s3:CreateBucket`, `s3:PutBucketPolicy`
- `ec2:CreateSecurityGroup`, `ec2:AuthorizeSecurityGroupIngress`
- `elasticloadbalancing:CreateLoadBalancer`, `elasticloadbalancing:CreateTargetGroup`, `elasticloadbalancing:CreateListener`
- `lambda:CreateFunction`, `lambda:AddPermission`
- `iam:CreateRole`, `iam:PutRolePolicy`, `iam:PassRole`
- `logs:CreateLogGroup`
- `acm:RequestCertificate`
- `route53:ChangeResourceRecordSets`

---

## logs-insights-queries

**Template:** `logs-insights-queries.yaml`
**Required:** No (optional, pre-built CloudWatch Insights queries)

| Resource Type | Service Namespace | Count |
|---|---|---|
| `AWS::Logs::QueryDefinition` | `logs` | 19 |

**IAM actions required for deployment:**
- `logs:PutQueryDefinition`

---

## Claude Desktop bootstrap (external upstream CDK)

GIP no longer owns a bootstrap stack or static resource inventory. Use the
gateway-native Desktop overlay or the exact AWS Samples companion CDK pinned in
`UPSTREAM.json` and materialized by `scripts/fetch-claude-apps-gateway.sh`
under `vendor/aws-samples/anthropic-on-aws/claude-apps-gateway-bootstrap`.
Review its synthesized template at the pinned commit in `UPSTREAM.json` before
granting IAM/SCP exemptions. Only `UPSTREAM.json`, `LICENSE`, and `README.md`
remain committed under the vendor directory; both materialized subtrees are
gitignored and verified by the fetch script (ADR-0036).

---

## websearch

**Template:** `bedrock-agentcore-gateway.yaml`
**Required:** No (optional, for MCP web search tool via Bedrock AgentCore)
**Region restriction:** us-east-1 only (managed connector availability)

| Resource Type | Service Namespace | Count |
|---|---|---|
| `AWS::BedrockAgentCore::Gateway` | `bedrock-agentcore` | 1 |
| `AWS::BedrockAgentCore::GatewayTarget` | `bedrock-agentcore` | 1 |
| `AWS::IAM::Role` | `iam` | 1 |
| `AWS::BedrockAgentCore::PolicyEngine` | `bedrock-agentcore` | 0 deployable (1 `HasEntitlement` declaration) |
| `AWS::BedrockAgentCore::Policy` | `bedrock-agentcore` | 0 deployable (1 `HasEntitlement` declaration) |

**IAM actions required for deployment:**
- `bedrock-agentcore:CreateGateway`, `bedrock-agentcore:CreateGatewayTarget`
- `iam:CreateRole`, `iam:PutRolePolicy`, `iam:PassRole`

Non-empty `EntitledGroups` and `ENFORCE` are rejected in this release. Live
Cognito validation showed AgentCore represents group claims as strings, so the
set-based Cedar policy cannot be created safely.

---

## memory

**Template:** `memory-stack.yaml`
**Required:** No (optional, opt-in AgentCore Memory MCP tools attached as a second target on the existing websearch gateway)
**Region restriction:** same region as the websearch gateway (us-east-1 today)

| Resource Type | Service Namespace | Count |
|---|---|---|
| `AWS::BedrockAgentCore::Memory` | `bedrock-agentcore` | 1 |
| `AWS::BedrockAgentCore::GatewayTarget` | `bedrock-agentcore` | 1 |
| `AWS::KMS::Key` | `kms` | 1 (conditional) |
| `AWS::KMS::Alias` | `kms` | 1 (conditional) |
| `AWS::IAM::Role` | `iam` | 2 (1 conditional) |
| `AWS::Lambda::Function` | `lambda` | 2 (1 conditional) |
| `AWS::Lambda::Permission` | `lambda` | 2 (1 conditional) |
| `AWS::SQS::Queue` | `sqs` | 1 in `extracted-only` mode |
| `AWS::Events::Rule` | `events` | 1 (conditional) |
| `AWS::CloudWatch::Alarm` | `cloudwatch` | 3 (2 conditional) |

`MemoryKmsKey`/`MemoryKmsKeyAlias` are created only when no customer-managed
key ARN is supplied; that is at most one billable key, and the alias is not a
second key. The sweeper resources (second role/function/permission, the
`MemorySweeperDLQ`, EventBridge rule, and two alarms) exist only in the default
`extracted-only` mode. Its DLQ uses AWS-managed `alias/aws/sqs`, not the memory
key. `EffectiveKmsKeyArn` outputs the supplied or created memory key.

**IAM actions required for deployment:**
- `bedrock-agentcore:CreateMemory`, `bedrock-agentcore:CreateGatewayTarget`
- `kms:CreateKey`, `kms:CreateAlias`, `kms:PutKeyPolicy`, `kms:EnableKeyRotation` (if no existing key is supplied)
- `iam:CreateRole`, `iam:PutRolePolicy`, `iam:AttachRolePolicy`, `iam:PassRole`
- `lambda:CreateFunction`, `lambda:AddPermission`
- `sqs:CreateQueue` (in `extracted-only` mode)
- `events:PutRule`, `events:PutTargets`
- `cloudwatch:PutMetricAlarm`

**Content warning:** this stack stores conversation-derived content (encrypted with a customer-managed KMS key). See [Memory](MEMORY.md) for data classification and erasure procedures before enabling.

---

## skills (skills-registry)

**Template:** `skills-registry.yaml`
**Required:** No (optional, governed skills registry — S3 artifact store + Agent Registry with publisher/curator personas and a distributor Lambda)

| Resource Type | Service Namespace | Count |
|---|---|---|
| `AWS::S3::Bucket` | `s3` | 2 |
| `AWS::S3::BucketPolicy` | `s3` | 2 |
| `AWS::KMS::Key` | `kms` | 1 (unconditional, `CuratorTopicKey`) |
| `AWS::KMS::Alias` | `kms` | 1 (unconditional, `CuratorTopicKeyAlias`) |
| `AWS::IAM::Role` | `iam` | 4 |
| `AWS::Lambda::Function` | `lambda` | 2 |
| `AWS::Lambda::Permission` | `lambda` | 1 |
| `AWS::SQS::Queue` | `sqs` | 1 (dead-letter queue) |
| `AWS::SQS::QueuePolicy` | `sqs` | 1 |
| `AWS::SNS::Topic` | `sns` | 1 (conditional: only when `AlertTopicArn` is empty) |
| `AWS::SNS::TopicPolicy` | `sns` | 1 (conditional, with the topic) |
| `AWS::Events::Rule` | `events` | 2 |
| `Custom::AgentRegistry` | `cloudformation` | 1 |

Exactly one customer-managed key, `CuratorTopicKey`, is created even when an
external `AlertTopicArn` is supplied because it always encrypts
`DistributorDeadLetterQueue`; the pending-approval topic shares it when the
stack creates that topic. `CuratorTopicKeyAlias` is an alias, not a second key,
and there is no `SkillsRegistryKey` logical resource. The topic policy grants
EventBridge publish access to the created topic; the queue policy grants the
distributor schedule access to the encrypted DLQ. `CuratorTopicArn` outputs the
created or reused topic.

**IAM actions required for deployment:**
- `s3:CreateBucket`, `s3:PutBucketPolicy`
- `kms:CreateKey`, `kms:CreateAlias`, `kms:PutKeyPolicy`, `kms:EnableKeyRotation`
- `iam:CreateRole`, `iam:PutRolePolicy`, `iam:PassRole`
- `lambda:CreateFunction`, `lambda:AddPermission`
- `sqs:CreateQueue`, `sqs:SetQueueAttributes`
- `sns:CreateTopic`, `sns:SetTopicAttributes`
- `events:PutRule`, `events:PutTargets`

**Runtime/custom-resource note:** no native Agent Registry CloudFormation type exists. A custom-resource Lambda calls the selected `agent-registry` GA or `bedrock-agentcore` preview surface. `CreateRegistry` and `ListRegistries` require account-level permissions; get/update/delete remain registry-scoped. First creation also requires the documented AgentCore workload-identity actions and `iam:CreateServiceLinkedRole` for `agent-registry.amazonaws.com`. Publisher, curator, and distributor permissions are record-scoped. Both namespaces remain granted until the preview endpoint shuts down on 2026-09-17. If `OrganizationId` is set, the artifact bucket policy grants org-wide read via `aws:PrincipalOrgID`. By default the registry is **retained on stack delete** (approval state lives only in the registry; manual cleanup required).

---

## Claude Apps Gateway (external upstream CDK)

GIP no longer publishes a gateway CloudFormation template or a derived static
resource count. Review and synthesize the exact AWS Samples CDK materialized by
`scripts/fetch-claude-apps-gateway.sh` under
`vendor/aws-samples/anthropic-on-aws/claude-apps-gateway` at the commit in
`UPSTREAM.json`. Its README and synthesized template are the source of truth for
resources and IAM/SCP pre-clearance.

---

## auth (idc)

**Template:** `bedrock-auth-idc.yaml`
**Required:** Alternative to OIDC auth (for IAM Identity Center users)

| Resource Type | Service Namespace | Count |
|---|---|---|
| `AWS::IAM::ManagedPolicy` | `iam` | 1 |
| `AWS::IAM::Role` | `iam` | 1 |

IAM Identity Center handles user authentication, while this stack provisions
the Bedrock access policy and federated IAM role used by IDC sessions.

**IAM actions required for deployment:**
- `iam:CreatePolicy`, `iam:CreateRole`, `iam:AttachRolePolicy`, `iam:PutRolePolicy`

---

## cognito-user-pool

**Template:** `cognito-user-pool-setup.yaml`
**Required:** No (optional, built-in user directory)

| Resource Type | Service Namespace | Count |
|---|---|---|
| `AWS::Cognito::UserPool` | `cognito-idp` | 1 |
| `AWS::Cognito::UserPoolClient` | `cognito-idp` | 2 |
| `AWS::Cognito::UserPoolDomain` | `cognito-idp` | 1 |
| `AWS::Cognito::UserPoolIdentityProvider` | `cognito-idp` | 1 in Federate mode |
| `AWS::IAM::Role` | `iam` | 1 + 1 in Federate mode + 1 when custom-claim injection is enabled |
| `AWS::Lambda::Function` | `lambda` | 1 + 1 in Federate mode + 1 when custom-claim injection is enabled |
| `AWS::Lambda::Permission` | `lambda` | 1 when custom-claim injection is enabled |
| `AWS::CloudFormation::CustomResource` | `cloudformation` | 1 + 1 in Federate mode |

**IAM actions required for deployment:**
- `cognito-idp:CreateUserPool`, `cognito-idp:CreateUserPoolClient`, `cognito-idp:CreateUserPoolDomain`
- `cognito-idp:CreateIdentityProvider` (Federate mode)
- `iam:CreateRole`, `iam:PutRolePolicy`, `iam:PassRole`
- `lambda:CreateFunction`, `lambda:AddPermission`

**Runtime/custom-resource note:** the always-present secret-store custom
resource reads the confidential web client's secret and creates or updates one
Secrets Manager secret. Federate mode adds a second custom resource that reads
and updates the public client. Their roles grant only the corresponding
`cognito-idp:DescribeUserPoolClient`/`UpdateUserPoolClient` and
`secretsmanager:CreateSecret`/`UpdateSecret`/`DescribeSecret`/`TagResource`
calls.

---

## cognito-custom-domain-cert

**Template:** `cognito-custom-domain-cert.yaml`
**Required:** No (manual Cognito custom-domain prerequisite; us-east-1 only)

| Resource Type | Service Namespace | Count |
|---|---|---|
| `AWS::CertificateManager::Certificate` | `acm` | 1 |
| `AWS::Route53::RecordSet` | `route53` | 1 when `CreateParentDomainRecord=true` |

Parameters identify the custom/parent domains and Route 53 hosted zone. Outputs
are `CertificateArn`, `CustomDomainName`, and `UsageInstructions`.

**IAM actions required for deployment:**
- `acm:RequestCertificate`
- `route53:ChangeResourceRecordSets` when the parent record is enabled

---

## KMS key-count and cost boundary

| Stack | Customer-managed keys created by the template |
|---|---|
| quota | 0 or 1: `QuotaAlertTopicKey` only when `AlertTopicArn` is empty |
| model-lifecycle | 0 or 1: `LifecycleAlertTopicKey` only when `AlertTopicArn` is empty |
| memory | 0 or 1: `MemoryKmsKey` only when `KmsKeyArn` is empty |
| skills | Exactly 1: unconditional `CuratorTopicKey`, shared by its DLQ and created topic |

The release-note USD 1/month base charge applies once per created
`AWS::KMS::Key`, not per alias or encrypted resource. `gip deploy` reuses the
quota topic and key for model-lifecycle when quota is enabled. References to
AWS-managed `alias/aws/sqs` (five possible DLQs), `alias/aws/dynamodb` (two
tables), and `alias/aws/s3` (three CodeBuild artifact configurations) do not
create customer-managed keys or incur that monthly key-storage charge.

---

## SCP Service Namespace Summary

Minimum services required for **basic deployment** (auth only):
```
cognito-identity, iam, logs, cloudformation, sts
```

Full deployment adds:
```
ec2, ecs, elasticloadbalancing, s3, ssm, acm, route53,
cloudwatch, cloudtrail, dynamodb, lambda,
sns, events, apigateway, athena, glue, firehose, codebuild,
secretsmanager, bedrock-agentcore, agent-registry, cognito-idp,
bedrock, sqs, kms, execute-api
```

**CloudFormation itself** always requires:
```
cloudformation:CreateStack, cloudformation:UpdateStack, cloudformation:DescribeStacks,
cloudformation:DescribeStackResources, cloudformation:DescribeStackEvents, cloudformation:GetTemplate
```
