# Exporting Telemetry to Your SIEM

This document gives working recipes for exporting the platform's usage telemetry
to Splunk, Datadog, or Elastic. Nothing in the platform hardcodes CloudWatch as
the only destination — export is standard AWS fan-out, and no platform code
change is required for any recipe below.

## What you are exporting: metadata only

The platform's telemetry invariant applies to exports exactly as it applies to
collection: **every exportable stream carries token counts, model IDs, cost, and
user identity — never prompt or completion content.** Wiring a SIEM to these
streams adds a new destination, not a new data class. State this in your data
classification review: the exported streams are usage metadata, not conversation
content.

Source log groups that exist once monitoring/analytics is deployed:

| Log group | Contents |
|---|---|
| `/aws/gip/metrics` | EMF usage metrics from the central collector (tokens, cost, model, user dimensions) |
| `/aws/gip/cowork-events` | Claude Desktop usage events (`deployment/infrastructure/cowork-dashboard.yaml:31`) |
| `/aws/bedrock/gip-*` | Bedrock model invocation logs when server-side metering is enabled (metadata only — text/image delivery disabled) |
| Lambda function logs | Operational logs of the quota/metering functions |

## Primary recipe: CloudWatch Logs subscription → Amazon Data Firehose → SIEM

This path works for **both** collector modes (central and sidecar-fed dashboards)
because it taps the log groups, not the collector. One subscription filter plus
one Firehose stream per destination; the platform stacks are untouched.

### Splunk

CloudWatch Logs subscription filter → Firehose → **native Splunk destination**
(HTTP Event Collector with indexer acknowledgment). Use Firehose's decompression
and message-extraction features so Splunk receives clean events. Officially
documented end-to-end:
[AWS Big Data blog, 2024-04-02](https://aws.amazon.com/blogs/big-data/deliver-decompressed-amazon-cloudwatch-logs-to-amazon-s3-and-splunk-using-amazon-data-firehose)
(retrieved 2026-07-08).

Steps:

1. Create a Firehose delivery stream with destination **Splunk** (HEC endpoint +
   token from your Splunk admin); enable CloudWatch decompression + message
   extraction.
2. Add a subscription filter on `/aws/gip/metrics` (and any other source
   group) targeting the Firehose stream, with an IAM role permitting
   `firehose:PutRecord`.
3. Verify events land in your Splunk index; the EMF JSON parses as structured
   fields.

### Datadog

Two officially supported options (choose one):

- **Firehose HTTP-endpoint destination** to
  `aws-kinesis-http-intake.logs.datadoghq.com` — Datadog is a supported Firehose
  partner destination
  ([Firehose destinations doc](https://docs.aws.amazon.com/firehose/latest/dev/what-is-this-service.html),
  retrieved 2026-07-08). Same subscription-filter wiring as Splunk, with your
  Datadog API key in the endpoint configuration.
- **Datadog Forwarder Lambda** subscribed directly to the log group
  (Datadog-official integration).

### Elastic

- **Firehose partner destination** (Elastic is listed in the same
  [Firehose destinations doc](https://docs.aws.amazon.com/firehose/latest/dev/what-is-this-service.html),
  retrieved 2026-07-08), or
- **Elastic Serverless Forwarder** subscribed to the log group.

### S3/Athena pull fallback

If the analytics pipeline is deployed, usage data already lands as Parquet in S3
(see [ANALYTICS.md](ANALYTICS.md)). Splunk DB Connect, Datadog, and Elastic can
all ingest from S3 on a schedule — a zero-change fallback when your SIEM team
prefers pull over push.

## Advanced path: exporter inside the collector (central mode only)

The central collector's configuration is pluggable by design: the OTEL config
lives in the SSM parameter `gip-otel-config`
(`deployment/infrastructure/otel-collector.yaml:676-688`) and the container image is
a template parameter (`CollectorImage`, default OpenTelemetry Collector Contrib
0.156.0 from Docker Hub, pinned by manifest-list digest,
`otel-collector.yaml:75-82`). You can add a vendor exporter (for example
Datadog) at the collector level by editing the SSM parameter and redeploying the
ECS service.

Two real caveats before you choose this path:

!!! warning "Do not switch back to the ADOT image for vendor exporters"
    The default Collector Contrib image includes vendor exporters such as
    Datadog, and unlike ADOT it ships the `transform` processor the pipeline's
    identity scrubbing depends on
    ([ADOT processors](https://aws-otel.github.io/docs/components/processors),
    retrieved 2026-09-02, lists no transform processor). ADOT ships a Datadog
    exporter today
    ([ADOT partner page](https://aws-otel.github.io/docs/partners/datadog),
    retrieved 2026-07-08), **but the ADOT collector has announced planned removal
    of the datadog/logzio/sapm/signalfx components**
    ([aws-otel-collector README notice](https://github.com/aws-observability/aws-otel-collector),
    retrieved 2026-07-08). If you override `CollectorImage`, use Contrib (from
    Docker Hub or mirrored into your own ECR, see
    [NETWORK_ISOLATION.md](NETWORK_ISOLATION.md)) or your vendor's OTel
    distribution rather than relying on ADOT keeping the exporter.

!!! warning "Stack updates clobber SSM config edits"
    The SSM parameter value is CloudFormation-managed
    (`otel-collector.yaml:676-688` and the surrounding `!If` blocks). Any manual edit
    to `gip-otel-config` is **overwritten on the next
    `gip deploy monitoring`**. Keep your exporter addition in your change
    records and re-apply after stack updates — or prefer the subscription-filter
    recipe above, which survives redeployments untouched.

**Sidecar mode:** the local collector binary is built from an OCB manifest that
includes only the `otlphttp` exporter by design
(`source/otel_helper/ocb-manifest.yaml`) — a minimal attack surface on end-user
machines. There is no supported way to add vendor exporters to the sidecar; use
the subscription-filter recipe, which covers sidecar deployments' CloudWatch
data identically.

## Related documents

- [MONITORING.md](MONITORING.md) — collector modes and dashboards
- [ANALYTICS.md](ANALYTICS.md) — the S3/Athena pipeline the fallback path reads
- [QUOTA_MONITORING.md](QUOTA_MONITORING.md) — server-side metering (source of `/aws/bedrock/gip-*`)
