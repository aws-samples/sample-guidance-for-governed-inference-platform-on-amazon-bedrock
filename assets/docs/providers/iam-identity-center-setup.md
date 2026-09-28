# AWS IAM Identity Center Setup Guide

This guide covers setting up the Governed Inference Platform using AWS IAM Identity Center (formerly AWS SSO) as the authentication method.

## How It Works

IDC uses your existing AWS SSO credentials — no external IdP or JWT tokens needed.

- **Authentication:** Developers run `aws sso login` — Claude Code picks up ambient AWS credentials automatically via the standard credential chain. When quota enforcement is enabled, the credential-process binary resolves the permission-set credentials instead, so the quota decision runs before credentials are returned.
- **Monitoring:** The local OTEL sidecar collector sends metrics to CloudWatch's native OTLP endpoint using the developer's SSO session credentials (SigV4). User identity is baked as a static resource attribute in the collector config at distribution time.
- **Attribution:** User email is resolved from the IAM Identity Center ARN session name (e.g. `user@company.com`) during `gip init` or `gip package` and embedded directly in the collector configuration.

The developer bundle depends on whether quota monitoring is enabled:

| Quota monitoring | Bundle contents | Quota enforcement |
|---|---|---|
| Disabled | Collector binary only — no credential-process, no otel-helper | None. Ambient SSO credentials reach Bedrock without a quota decision. |
| Enabled | Collector binary and credential-process | SigV4 quota check runs before credentials are returned. |

## When to Choose IAM Identity Center vs OIDC

### Choose IAM Identity Center (IDC) when:
- Your organization already uses AWS SSO/Identity Center for AWS access management
- You want to leverage existing AWS permission sets and user groups
- You need native AWS authentication without external identity providers
- You want simplified setup for AWS-first environments

### Choose OIDC when:
- Your organization uses external identity providers (Okta, Azure AD, etc.)
- You need JWT-based ALB authorization for the OTEL proxy
- You want token-based session management with refresh tokens

## What's NOT Supported with IAM Identity Center

⚠️ **Differences from OIDC:**

1. **Quota via SigV4**: Per-user quota enforcement works via IAM SigV4 authentication (not JWT). The quota Lambda extracts user email from the assumed-role ARN session name. Requires the quota-monitoring stack to be deployed, and switches the developer bundle from collector-only to collector plus credential-process (see [How It Works](#how-it-works)).
2. **Per-User OTEL Attribution**: Works automatically — credential-process writes user email to the OTEL cache from the STS caller ARN. Requires email as the IDC session name (the default).
3. **No ALB JWT Authorization**: The OTEL proxy (central mode) cannot validate requests via JWT. Use sidecar mode, or accept unauthenticated OTEL ingestion in proxy mode.

## Prerequisites

Before starting, ensure you have:

- AWS IAM Identity Center enabled in your AWS account
- A permission set created with appropriate Bedrock access (recommended: `BedrockDeveloperAccess`)
- Users assigned to the permission set
- AWS CLI v2 installed and configured

## Step-by-Step Deployment

### 1. Initialize Configuration

Run the Claude Code setup wizard and select IAM Identity Center:

```bash
poetry run gip init
```

When prompted for authentication method, choose:
```
❯ AWS IAM Identity Center (SSO)
```

### 2. Provide Identity Center Details

You'll be prompted for:

- **Start URL**: Your Identity Center portal URL (e.g., `https://company.awsapps.com/start`)
- **SSO Region**: The AWS region where Identity Center is configured
- **Account ID**: Your 12-digit AWS account number
- **Permission Set**: The name of your permission set (default: `BedrockDeveloperAccess`)

### 3. AWS Configuration

The wizard will generate an AWS config block and offer to append it to `~/.aws/config`:

```ini
[profile gip]
sso_session = gip-MyPool
sso_account_id = 123456789012
sso_role_name = BedrockDeveloperAccess
region = us-east-1

[sso-session gip-MyPool]
sso_start_url = https://company.awsapps.com/start
sso_region = us-east-1
sso_registration_scopes = sso:account:access
```

### 4. Authenticate with AWS SSO

Before deploying, authenticate using the AWS CLI:

```bash
aws sso login --profile gip
```

Verify your identity:

```bash
aws sts get-caller-identity --profile gip
```

### 5. Deploy Infrastructure

Deploy the Claude Code infrastructure:

```bash
poetry run gip deploy
```

This will:
- Deploy the quota monitoring stack when quota enforcement is enabled (skipped when it is not)
- Deploy the IAM role and Bedrock access policy
- Deploy monitoring and dashboard stacks (if enabled)

## Extending SSO Session Duration

By default, AWS SSO sessions expire after 8-12 hours. To extend this:

1. **In AWS Console**: Go to IAM Identity Center → Settings → Session settings
2. **Update Session Duration**: Set to maximum allowed (up to 7 days for programmatic access)
3. **Apply to Permission Sets**: Ensure your permission set inherits these settings

Example session settings:
- **Programmatic access**: 7 days
- **AWS Management Console access**: 12 hours

## Per-User Cost Attribution

IDC users get per-user OTEL attribution automatically. The user email is baked into the collector configuration at distribution time (`gip package` resolves it from `aws sts get-caller-identity`). Dashboard widgets (Token Usage by User, Active Users) work without additional configuration.

**Requirement:** IAM Identity Center must use email as the session name (the default). The ARN format must be:
`arn:aws:sts::ACCOUNT:assumed-role/RoleName/user@company.com`

> **Note:** Unlike the OIDC path (which extracts identity dynamically from JWT tokens at runtime), the IDC path uses a static email set at distribution time. This works well for single-user machines. If a user's email changes, regenerate their package with `gip package`.

### Verifying Attribution

```bash
# Confirm email is in your ARN
aws sts get-caller-identity --profile gip
# Look for: "Arn": "arn:aws:sts::123456789012:assumed-role/.../user@company.com"

# Check the OTEL cache was written (after first credential-process invocation)
cat ~/.gip/otel-cache/*.json
# Should show: {"x-user-email": "user@company.com", ...}
```

If your ARN shows a session ID instead of email, update your IDC session name configuration in the IAM Identity Center console under Settings → Session settings.

For additional cost tracking via CloudTrail:

### 1. Enable CloudTrail

```yaml
# Add to your monitoring configuration
CloudTrailEnabled: true
CloudTrailS3Bucket: my-company-cloudtrail-bucket
```

### 2. Query CloudTrail Logs

Use Athena or CloudWatch Logs Insights to query Bedrock API calls:

```sql
-- Athena query for user-specific Bedrock usage
SELECT 
    useridentity.sessioncontext.sessionissuer.principalid as user_id,
    eventname,
    COUNT(*) as api_calls,
    DATE_TRUNC('day', eventtime) as date
FROM cloudtrail_logs
WHERE 
    eventsource = 'bedrock.amazonaws.com'
    AND eventname LIKE 'InvokeModel%'
    AND eventtime >= current_timestamp - interval '30' day
GROUP BY 1,2,4
ORDER BY date DESC, api_calls DESC;
```

### 3. Create Custom Dashboards

Use the user identity from CloudTrail to create cost allocation reports and usage dashboards.

## Troubleshooting

### Common Issues

1. **"Profile not found" errors**
   ```bash
   # Verify your profile exists
   aws configure list-profiles
   
   # Test SSO authentication
   aws sso login --profile gip
   ```

2. **"Access denied" for Bedrock**
   - Verify your permission set includes Bedrock permissions
   - Check that the deployed IAM role has the correct policies attached
   - Ensure you're using the correct AWS region for Bedrock access

3. **CloudFormation deployment failures**
   ```bash
   # Check stack status
   aws cloudformation describe-stacks --stack-name YourStackName
   
   # View stack events for errors
   aws cloudformation describe-stack-events --stack-name YourStackName
   ```

4. **SSO session expired**
   ```bash
   # Re-authenticate
   aws sso login --profile gip
   
   # Verify credentials are refreshed
   aws sts get-caller-identity --profile gip
   ```

### IAM Role Trust Policy Issues

If you encounter trust policy errors, verify the CloudFormation template deployed correctly:

```bash
# Check the federated role
aws iam get-role --role-name BedrockIDCFederatedRole

# Verify trust policy allows SSO principals
aws iam get-role --role-name BedrockIDCFederatedRole --query 'Role.AssumeRolePolicyDocument'
```

### Permission Set Configuration

Ensure your Identity Center permission set includes:

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "bedrock:InvokeModel*",
                "bedrock:GetFoundationModel",
                "bedrock:ListFoundationModels"
            ],
            "Resource": "*"
        }
    ]
}
```

## Migration from OIDC to IDC

To switch an existing OIDC profile to IDC:

1. **Backup current configuration**:
   ```bash
   cp ~/.gip/profiles/myprofile.json ~/.gip/profiles/myprofile-backup.json
   ```

2. **Re-run init with IDC**:
   ```bash
   poetry run gip init --profile myprofile
   ```
   Select "AWS IAM Identity Center (SSO)" when prompted.

3. **Redeploy infrastructure**:
   ```bash
   poetry run gip deploy
   ```
   The auth stack will be updated to use the IDC template instead of OIDC.

## Next Steps

After successful deployment:

- **Test Authentication**: Create and run a simple Claude Code script
- **Monitor Usage**: Use CloudWatch dashboards for system monitoring
- **Set Up Alerts**: Configure SNS notifications for system health
- **Train Users**: Share SSO login instructions with your team

For quota monitoring and per-user controls, consider using the OIDC authentication method instead.