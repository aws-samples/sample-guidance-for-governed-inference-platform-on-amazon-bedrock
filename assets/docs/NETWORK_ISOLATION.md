# Network Isolation with AWS PrivateLink

This document is for organizations that require the platform's AWS traffic to
stay on private networks (VPC endpoints / PrivateLink) rather than the public
internet. It covers what can and cannot be made private today, a decision tree,
the full VPC-endpoint inventory, an example endpoint template snippet, and an
optional zero-trust IAM condition — with honest caveats about which client
environments this actually works for.

All AWS documentation cited was read **2026-07-08**; VPC endpoint service names
were verified live via `ec2:DescribeVpcEndpointServices` (us-east-1, 2026-07-08).

> **The honest summary:** every AWS-side network path of the platform can be made
> private today **except** the quota API (an API Gateway HTTP API — no private
> endpoint type exists for HTTP APIs) and the OIDC IdP exchange (public SaaS,
> unavoidable). Network isolation is a real option for VDI/VPN/cloud-workstation
> fleets and a foot-gun for laptop-off-VPN fleets — start with the decision tree.

## Decision tree: where do your developers' packets originate?

`aws:SourceVpce` conditions and VPC endpoints only apply when the request
actually traverses the endpoint:

| Client environment | Private path works? | Notes |
|---|---|---|
| WorkSpaces / VDI, EC2 or cloud dev environments, CI runners in a VPC | **YES** | Private DNS makes it zero-config for the credential-process binary — SDK default endpoints resolve to VPC-endpoint IPs |
| Laptop on corp network / VPN / Direct Connect | **YES, with DNS work** | Needs a Route 53 Resolver inbound endpoint or conditional forwarder so `bedrock-runtime.<region>.amazonaws.com` resolves to the endpoint's private IPs (pattern in the [Bedrock PrivateLink blog, 2023-10-30](https://aws.amazon.com/blogs/machine-learning/use-aws-privatelink-to-set-up-private-access-to-amazon-bedrock/)) |
| Laptop off VPN (home, cafe) | **NO** | Traffic hits public endpoints. A "require VPC endpoint" deny **hard-blocks these users**. If your fleet works from anywhere without always-on VPN, do not deploy the zero-trust condition below |

If your developers are not consistently on VDI/VPN, deploy VPC endpoints for the
server-side pieces you control (collector, gateway) and skip client-path
enforcement — you still get defense-in-depth without lockouts.

## VPC endpoint inventory

Every network path in the platform, and whether it can be private today:

| Platform path | VPC endpoint service | Private? | Evidence |
|---|---|---|---|
| Harness → Bedrock runtime (`InvokeModel`, `Converse`, streaming) | `com.amazonaws.<region>.bedrock-runtime` (plus `-fips` variants in us-east-1/2, us-west-2, ca-central-1, GovCloud) | **YES** — SigV4 InvokeModel/Converse explicitly supported through the endpoint | [Bedrock VPC endpoint docs](https://docs.aws.amazon.com/bedrock/latest/userguide/vpc-interface-endpoints.html) (read 2026-07-08) |
| Bearer-token (`bedrock:CallWithBearerToken`) through that endpoint | same | **YES, verified 2026-08-14 in us-east-1.** A bearer Converse request returned `pong` through an endpoint with private DNS; the CloudTrail event recorded `callWithBearerToken=true` and the matching `vpcEndpointId`. Bearer callers still require `Principal: "*"` in the endpoint policy because they have no IAM principal | Live LV-12 result |
| credential-process → STS (`AssumeRoleWithWebIdentity`, `source/go/internal/federation/sts.go:37-61`) | `com.amazonaws.<region>.sts` (plus fips) | **YES** — long-standing; SDK regional endpoints + private DNS = zero code change | [STS VPC endpoint docs](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_credentials_sts_vpce.html) |
| credential-process → Cognito identity pools (`GetId`/`GetCredentialsForIdentity`, `source/go/internal/federation/cognito.go:37-55`) | `com.amazonaws.<region>.cognito-identity` (plus fips) | **YES — new since 2025-12-11** ([What's New](https://aws.amazon.com/about-aws/whats-new/2025/12/amazon-cognito-identity-pools-private-connectivity-aws-privatelink/)). **Not available in GovCloud or China** | [Cognito VPC endpoint docs](https://docs.aws.amazon.com/cognito/latest/developerguide/vpc-interface-endpoints.html) |
| Cognito user pool SDK APIs | `com.amazonaws.<region>.cognito-idp` | **PARTIAL — new since 2025-11-07** ([What's New](https://aws.amazon.com/about-aws/whats-new/2025/11/amazon-cognito-user-pools-private-connectivity-aws-privatelink)): SDK/admin APIs yes; **the OAuth authorization-code flow, managed login / hosted UI, and SAML/OIDC federated sign-in are NOT supported over the endpoint.** Any flow using the hosted UI stays public | same What's New |
| Quota API (API Gateway v2 **HTTP API**, `deployment/infrastructure/quota-monitoring.yaml:840-846`) | `com.amazonaws.<region>.execute-api` exists, **but the private endpoint type is REST-API-only** | **NO (today)** — HTTP APIs cannot be private ([private REST APIs](https://docs.aws.amazon.com/apigateway/latest/developerguide/apigateway-private-apis.html), read 2026-07-08). See the hazard callout below | AWS docs + repost knowledge center |
| OTEL → collector ALB | n/a — self-hosted ALB; the `ALBScheme` parameter already supports `internal` (`deployment/infrastructure/otel-collector.yaml:143-153`) | **YES (existing hook)** — requires VPN/DX/VDI reachability. If the collector's own subnets go private, it additionally needs `com.amazonaws.<region>.logs` and `.monitoring` endpoints for its exports ([CW Logs VPCE](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/cloudwatch-logs-and-interface-VPC.html), [CW VPCE](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/cloudwatch-and-interface-VPC.html)), and its image (Collector Contrib 0.156.0 from Docker Hub, pinned by digest in the `CollectorImage` parameter, `otel-collector.yaml:75-82`) must be mirrored to a private ECR (plus `ecr.api`/`ecr.dkr`/S3-gateway endpoints) | template + AWS docs |
| Claude apps gateway | n/a — the pinned AWS Samples CDK uses an IPv4-only internal ALB | **YES (already private)** — clients need the private hostname and network path documented upstream. Gateway/bootstrap source is not committed: first run `scripts/fetch-claude-apps-gateway.sh`; the gitignored trees are verified against `vendor/aws-samples/anthropic-on-aws/UPSTREAM.json` | [Apps Gateway](APPS_GATEWAY.md) and [ADR-0036](adr/0036-verified-fetch-claude-apps-gateway.md) |
| AgentCore Gateway MCP (web search) | `com.amazonaws.<region>.bedrock-agentcore.gateway` (gateway), `.bedrock-agentcore` (data plane), `.bedrock-agentcore-control` (control plane) | **YES** — the data plane supports both SigV4 and Bearer/OAuth ([AgentCore VPC endpoint docs](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/vpc-interface-endpoints.html), read 2026-07-08). **Caveat:** this platform's OIDC-mode gateway uses a `CUSTOM_JWT` authorizer (`deployment/infrastructure/bedrock-agentcore-gateway.yaml:259-261`), and endpoint policies match IAM principals only — the endpoint policy **must set `Principal: "*"`** or JWT callers get 403. Region caveat: AgentCore endpoints exist in a subset of regions (us-east-1, us-west-2, eu-central-1, ap-southeast-2 per the [starter-toolkit page](https://aws.github.io/bedrock-agentcore-starter-toolkit/user-guide/security/vpc-interface-endpoints.html), read 2026-07-08). Ingress walkthrough: [AWS blog, 2025-10-03](https://aws.amazon.com/jp/blogs/machine-learning/secure-ingress-connectivity-to-amazon-bedrock-agentcore-gateway-using-interface-vpc-endpoints/) | AWS docs |
| S3 package distribution (presigned URLs) | `com.amazonaws.<region>.s3` — **gateway** endpoint (in-VPC, free, transparent to presigned URLs) or **interface** endpoint (on-prem via DX/VPN; the presigned URL must be generated against the endpoint-specific DNS name) | **YES** | [S3 PrivateLink docs](https://docs.aws.amazon.com/AmazonS3/latest/userguide/privatelink-interface-endpoints.html) |
| `bedrock-mantle` (not used by this platform; governance note) | `com.amazonaws.<region>.bedrock-mantle` **exists** (verified live 2026-07-08; listed in the [Bedrock VPC endpoint docs](https://docs.aws.amazon.com/bedrock/latest/userguide/vpc-interface-endpoints.html) with a mantle endpoint-policy example) | YES | Governance impact: a permissive Bedrock endpoint policy also opens a private path to the unmetered mantle endpoint. The platform's explicit `bedrock-mantle:*` IAM deny remains the control that matters; the example endpoint policy below allowlists `bedrock:InvokeModel*` actions only |

!!! warning "Hazard: private-DNS `execute-api` endpoints break the quota API"
    The quota API is an API Gateway **HTTP API**, which has no private endpoint
    type. Worse: if you create an `execute-api` VPC endpoint **with private DNS
    enabled** for some other REST API, all *public* API Gateway calls from that
    VPC — including this platform's quota checks — start failing with 403
    ([repost knowledge center](https://repost.aws/knowledge-center/api-gateway-vpc-connections),
    updated 2025-10-06; [companion article](https://repost.aws/knowledge-center/api-gateway-endpoint-vpc),
    2026-02-10). A customer deploying VPC endpoints for Bedrock can accidentally
    break quota enforcement this way. If you need an `execute-api` endpoint for
    other workloads, disable private DNS on it or move those APIs to custom
    domains. The example template below deliberately ships **no** `execute-api`
    endpoint. Because the quota path is fail-closed, a 403 here blocks
    credential vending — this misconfiguration presents as "developers can't
    get credentials from inside the VPC."

## Residual public surface (cannot be made private)

1. **OIDC IdP traffic** (Okta/Entra/Auth0/Google): the browser authorization flow
   and token/refresh endpoints are public SaaS. Unavoidable for OIDC mode; also
   applies to Cognito **hosted-UI / managed-login / federated** flows, which are
   explicitly excluded from Cognito PrivateLink (What's New, 2025-11-07).
   Mitigation is IdP-side (network zones / Conditional Access), not platform-side.
2. **Container image pulls:** the collector image default is OpenTelemetry
   Collector Contrib 0.156.0 from Docker Hub, pinned by manifest-list digest as
   `otel/opentelemetry-collector-contrib@sha256:125bdbeb7590cc1952c5b3430ecf14063568980c2c93d5b38676cc0446ed8108`
   (`CollectorImage` parameter, `otel-collector.yaml:75-82`). It is not on ECR
   Public because the collector config depends on the Contrib-only `transform`
   processor (OTTL JWT parsing, SHA-256 of `sub`, `keep_keys` scrubbing;
   `otel-collector.yaml:709-796` and `879-966`), which the ADOT image on ECR
   Public (`public.ecr.aws/aws-observability/aws-otel-collector`) does not ship
   ([ADOT processors](https://aws-otel.github.io/docs/components/processors),
   read 2026-09-02), and the OpenTelemetry project does not publish Contrib to
   ECR Public (ECR Public Gallery search, 2026-09-02). Availability: Docker Hub
   allows unauthenticated users 100 pulls per 6 hours per IPv4 address
   ([Docker Hub usage and limits](https://docs.docker.com/docker-hub/usage/),
   read 2026-09-02), and every collector task start is an anonymous pull from
   the task's public or NAT IP. Mitigation: mirror the pinned digest into your
   own ECR and set `CollectorImage`; the task execution role already carries
   `AmazonECSTaskExecutionRolePolicy` (`otel-collector.yaml:602`), which covers
   pulls from a private ECR repository in the same account, so no Docker Hub
   dependency remains.
3. **Gateway/bootstrap source and images:** this is separate from the collector
   image above. The source and its image build/download paths are owned by the
   pinned AWS Samples CDK and are not committed here. Run
   `scripts/fetch-claude-apps-gateway.sh` before following those paths; it needs
   `github.com` access and materializes gitignored trees verified against
   `vendor/aws-samples/anthropic-on-aws/UPSTREAM.json`. Follow
   [APPS_GATEWAY.md](APPS_GATEWAY.md) and
   [ADR-0036](adr/0036-verified-fetch-claude-apps-gateway.md), not the removed
   GIP `GatewayImage` parameter.
4. **Build-time Go/OCB dependencies** (`proxy.golang.org`, GitHub releases):
   already solved for offline machines by
   `scripts/prepare-offline-go-bundle.sh` (pre-seeds the pinned OCB binary and
   `GOMODCACHE`, then builds with `GOPROXY=off`).
5. **Package distribution to developer machines outside the private network**
   (presigned S3 URLs): public HTTPS by design; private only for in-VPC/DX
   clients (see the S3 row above).
6. **GovCloud/China:** the `cognito-identity` endpoint is not offered there
   (What's New, 2025-12-11).

## Example VPC endpoint template

The platform does not ship or manage a VPC endpoint stack this wave — most
isolation-minded enterprises bring their own VPC and endpoint management, and
[QUICK_START.md](../../QUICK_START.md) already points BYO-VPC customers at the
existing-VPC wizard option. The snippet below is a **copy-paste starting point**,
not a managed stack (see
[ADR-0019](adr/0019-multi-account-network-isolation-doc-first.md)):

```yaml
AWSTemplateFormatVersion: '2010-09-09'
Description: 'Example - VPC endpoints for private access to the governed Bedrock platform'
Parameters:
  VpcId: {Type: AWS::EC2::VPC::Id}
  SubnetIds: {Type: List<AWS::EC2::Subnet::Id>}
  VpcCidr: {Type: String, Default: 10.0.0.0/16}
  EnableAgentCore: {Type: String, Default: 'false', AllowedValues: ['true', 'false']}
Conditions:
  HasAgentCore: !Equals [!Ref EnableAgentCore, 'true']
Resources:
  VpceSecurityGroup:
    Type: AWS::EC2::SecurityGroup
    Properties:
      GroupDescription: HTTPS from VPC to interface endpoints
      VpcId: !Ref VpcId
      SecurityGroupIngress:
        - {IpProtocol: tcp, FromPort: 443, ToPort: 443, CidrIp: !Ref VpcCidr}
  BedrockRuntimeEndpoint:
    Type: AWS::EC2::VPCEndpoint
    Properties:
      ServiceName: !Sub 'com.amazonaws.${AWS::Region}.bedrock-runtime'
      VpcEndpointType: Interface
      VpcId: !Ref VpcId
      SubnetIds: !Ref SubnetIds
      SecurityGroupIds: [!Ref VpceSecurityGroup]
      PrivateDnsEnabled: true
      PolicyDocument:
        Version: '2012-10-17'
        Statement:
          - Effect: Allow
            Principal: '*'   # bearer-token callers have no IAM principal
            Action: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream',
                     'bedrock:Converse', 'bedrock:ConverseStream', 'bedrock:CallWithBearerToken']
            Resource: '*'    # model access is enforced in the role policy, not here
  # StsEndpoint:             com.amazonaws.${AWS::Region}.sts              (same shape)
  # CognitoIdentityEndpoint: com.amazonaws.${AWS::Region}.cognito-identity (same shape; skip on GovCloud)
  # LogsEndpoint:            com.amazonaws.${AWS::Region}.logs             (collector-VPC only)
  # MonitoringEndpoint:      com.amazonaws.${AWS::Region}.monitoring       (collector-VPC only)
  # AgentCoreGatewayEndpoint: Condition HasAgentCore -
  #   com.amazonaws.${AWS::Region}.bedrock-agentcore.gateway, Principal '*' (CUSTOM_JWT ingress)
  S3GatewayEndpoint:
    Type: AWS::EC2::VPCEndpoint
    Properties:
      ServiceName: !Sub 'com.amazonaws.${AWS::Region}.s3'
      VpcEndpointType: Gateway
      VpcId: !Ref VpcId
      # RouteTableIds: [...]
# Deliberately NO execute-api endpoint: the quota API is an HTTP API; a private-DNS
# execute-api endpoint would 403 quota checks from inside the VPC (see hazard above).
```

The `Principal: '*'` on the Bedrock endpoint is intentional and safe here: the
endpoint policy is a **network** control, not the authorization boundary — model
and region access are enforced by the assumed role's IAM policy, and the action
list excludes `bedrock-mantle:*` entirely.

## Optional zero-trust pattern: `aws:SourceVpce` deny

The standard network-perimeter pattern
([data-perimeter blog, 2023-09-05](https://aws.amazon.com/blogs/security/establishing-a-data-perimeter-on-aws-allow-access-to-company-data-only-from-expected-networks/);
[Bedrock-specific prescriptive guidance](https://docs.aws.amazon.com/prescriptive-guidance/latest/data-perimeter-for-amazon-bedrock/network-perimeter.html),
read 2026-07-08) — add to the federated role policy:

```json
{
  "Sid": "DenyBedrockOutsideVpce",
  "Effect": "Deny",
  "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream",
             "bedrock:Converse", "bedrock:ConverseStream"],
  "Resource": "*",
  "Condition": {
    "StringNotEqualsIfExists": { "aws:SourceVpce": "vpce-EXAMPLE1234567890" },
    "BoolIfExists": { "aws:ViaAWSService": "false" }
  }
}
```

Notes:

- The `aws:ViaAWSService` exception keeps AWS-on-your-behalf calls (for example
  guardrail sub-calls) working; the `IfExists` operators are the documented
  pattern so requests without the key are not over-denied.
- Scope the deny to Bedrock runtime actions only — the quota API (execute-api)
  and OTEL (ALB) do not traverse the Bedrock VPC endpoint.
- This ships as documented JSON this wave, not a template parameter
  ([ADR-0019](adr/0019-multi-account-network-isolation-doc-first.md)). Apply it
  by editing the deployed auth stack's role policy through your change process.

!!! danger "Off-VPN lockout"
    With this deny in place, any developer whose traffic does not traverse the
    named VPC endpoint — most commonly a laptop off VPN — is **hard-blocked from
    all Bedrock inference**, even with valid credentials and quota. Deploy it
    only for fleets that are always on VDI/VPN/DX, and test with one pilot group
    first. This is why the platform does not enable it by default.

## Related documents

- [MULTI_ACCOUNT.md](MULTI_ACCOUNT.md) — account topologies; combine per-account endpoints with per-account residency scoping
- [MONITORING.md](MONITORING.md) — collector deployment modes and the `ALBScheme` parameter
- [APPS_GATEWAY.md](APPS_GATEWAY.md) — verified fetch and upstream gateway deployment path
- [ADR-0019](adr/0019-multi-account-network-isolation-doc-first.md) — doc-first rationale and deferred items
- [ADR-0036](adr/0036-verified-fetch-claude-apps-gateway.md) — why gateway/bootstrap source is materialized instead of committed
