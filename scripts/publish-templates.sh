#!/usr/bin/env bash
#
# publish-templates.sh — publish the Governed Inference Platform CloudFormation
# templates to an S3 bucket and print AWS console quick-create links.
#
# Usage:
#   scripts/publish-templates.sh <bucket> [prefix] [region]
#
#   bucket  S3 bucket you own (must already exist; you need s3:PutObject).
#   prefix  Key prefix for the uploaded templates (default: gip-templates).
#   region  Region for the console links and S3 URLs
#           (default: the AWS CLI's configured region).
#
# Templates that bundle local Lambda source (Code: ./lambda-functions/...) are
# run through `aws cloudformation package` first, so every printed link points
# at a template that deploys as-is from the console.
#
# The printed links follow the CloudFormation quick-create convention. If you
# publish them on an internal wiki you can dress them up as buttons with the
# standard launch-stack image (the cloudformation-launch-stack.png convention):
#   [![Launch Stack](https://s3.amazonaws.com/cloudformation-examples/cloudformation-launch-stack.png)](<quick-create-url>)
# Plain markdown links work everywhere and are what this script emits.

set -euo pipefail

usage() {
  echo "Usage: $0 <bucket> [prefix] [region]" >&2
  exit 64
}

[ $# -ge 1 ] || usage

BUCKET="$1"
PREFIX="${2:-gip-templates}"
REGION="${3:-$(aws configure get region || true)}"

if [ -z "$REGION" ]; then
  echo "ERROR: no region given and none configured for the AWS CLI." >&2
  echo "       Pass one: $0 <bucket> [prefix] [region]" >&2
  exit 64
fi

# Strip any trailing slash so key joins stay clean.
PREFIX="${PREFIX%/}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_DIR="$SCRIPT_DIR/../deployment/infrastructure"

if [ ! -d "$TEMPLATE_DIR" ]; then
  echo "ERROR: template directory not found: $TEMPLATE_DIR" >&2
  exit 66
fi

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

SKILLS_BUILD_DIR="$WORK_DIR/skills-registry-build"
mkdir -p "$SKILLS_BUILD_DIR/lambda-functions/skills_registry"
cp "$TEMPLATE_DIR/skills-registry.yaml" "$SKILLS_BUILD_DIR/skills-registry.yaml"
cp "$TEMPLATE_DIR/lambda-functions/skills_registry/"*.py \
  "$TEMPLATE_DIR/lambda-functions/skills_registry/requirements.txt" \
  "$SKILLS_BUILD_DIR/lambda-functions/skills_registry/"
python3 -m pip install \
  --disable-pip-version-check \
  --no-compile \
  --requirement "$TEMPLATE_DIR/lambda-functions/skills_registry/requirements.txt" \
  --target "$SKILLS_BUILD_DIR/lambda-functions/skills_registry"

# Standalone templates that make sense as a per-stack console deployment,
# with a suggested stack name for each. Templates not on this list are still
# uploaded, but they are driven by the gip CLI (packaging pipeline,
# distribution buckets, bootstrap servers, memory add-on) and get no link.
STANDALONE_TEMPLATES="
bedrock-auth-okta.yaml:gip-auth-okta
bedrock-auth-azure.yaml:gip-auth-azure
bedrock-auth-auth0.yaml:gip-auth-auth0
bedrock-auth-google.yaml:gip-auth-google
bedrock-auth-generic.yaml:gip-auth-generic
bedrock-auth-cognito-pool.yaml:gip-auth-cognito-pool
bedrock-auth-idc.yaml:gip-auth-idc
cognito-user-pool-setup.yaml:gip-user-pool
cognito-custom-domain-cert.yaml:gip-cognito-domain-cert
networking.yaml:gip-networking
otel-collector.yaml:gip-monitoring
claude-code-dashboard.yaml:gip-dashboard
cowork-dashboard.yaml:gip-cowork-dashboard
logs-insights-queries.yaml:gip-logs-insights
analytics-pipeline.yaml:gip-analytics
quota-monitoring.yaml:gip-quota
quota-metering.yaml:gip-metering
guardrails-enforcement.yaml:gip-guardrails
model-lifecycle.yaml:gip-model-lifecycle
landing-page-distribution.yaml:gip-landing-page
skills-registry.yaml:gip-skills-registry
bedrock-agentcore-gateway.yaml:gip-websearch-gateway
"

echo "Publishing templates from $TEMPLATE_DIR"
echo "  bucket: s3://$BUCKET/$PREFIX/"
echo "  region: $REGION"
echo

for template in "$TEMPLATE_DIR"/*.yaml; do
  file="$(basename "$template")"
  upload_source="$template"
  package_source="$template"

  if [ "$file" = "skills-registry.yaml" ]; then
    package_source="$SKILLS_BUILD_DIR/$file"
  fi

  # Templates that reference local Lambda source must be packaged first so the
  # uploaded copy is deployable straight from the console.
  if grep -q 'Code: \./lambda-functions/' "$template"; then
    echo "  packaging $file (bundles local Lambda source) ..."
    aws cloudformation package \
      --template-file "$package_source" \
      --s3-bucket "$BUCKET" \
      --s3-prefix "$PREFIX/artifacts" \
      --output-template-file "$WORK_DIR/$file" \
      --region "$REGION" > /dev/null
    upload_source="$WORK_DIR/$file"
  fi

  echo "  uploading $file"
  aws s3 cp "$upload_source" "s3://$BUCKET/$PREFIX/$file" \
    --region "$REGION" --only-show-errors
done

echo
echo "Done. Ready-to-paste markdown for the standalone templates:"
echo
echo "---------------------------------------------------------------------"

echo "$STANDALONE_TEMPLATES" | while IFS=: read -r file stack_name; do
  [ -n "$file" ] || continue
  template_url="https://$BUCKET.s3.$REGION.amazonaws.com/$PREFIX/$file"
  quick_create="https://$REGION.console.aws.amazon.com/cloudformation/home?region=$REGION#/stacks/create/review?templateURL=$template_url&stackName=$stack_name"
  cat <<EOF

### $file

[Launch stack: $stack_name]($quick_create)

CLI fallback:

    aws cloudformation create-stack \\
      --stack-name $stack_name \\
      --template-url $template_url \\
      --capabilities CAPABILITY_NAMED_IAM \\
      --region $REGION

EOF
done

echo "---------------------------------------------------------------------"
echo
echo "Notes:"
echo "  - cognito-custom-domain-cert and bedrock-agentcore-gateway must be"
echo "    deployed in us-east-1; re-run with region us-east-1 for those links."
echo "  - The console prompts for every parameter; see"
echo "    assets/docs/LAUNCH_STACKS.md for the dependency order and caveats."
