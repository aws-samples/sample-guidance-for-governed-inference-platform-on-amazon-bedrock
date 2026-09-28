# CLAUDE.md — Lambda Functions

## Structure

Each Lambda lives in its own directory under `deployment/infrastructure/lambda-functions/`:
```
quota_check/            — Usage check + cost-based enforcement
metering_processor/     — Server-side invocation-log metering
skills_registry/        — Agent Registry provisioning and distribution
```

The retired GIP Claude Desktop bootstrap Lambdas have been removed from the
repository (not included in this sample; deployed stacks:
`aws cloudformation get-template` / `delete-stack`). New gateway/Desktop
deployments use the pinned AWS Samples CDK unchanged after verified fetch.

## Key Rules

### Security — Fail Closed
- **Never** accept auth without full cryptographic validation.
- If PyJWT is unavailable, return 500 — don't fall back to claims-only.
- Log `ERROR:` prefix for security failures (CloudWatch alerting).

### Response Format
- Always use a `_response(status_code, body)` helper for consistent JSON responses.
- Error responses: `{"error": "<code>", "message": "<human-readable>"}`.
- Success responses: domain-specific JSON (config, quota status, etc.).

### Identity Extraction
- **OIDC:** JWT claims (`sub`, `email`) from validated token.
- **IDC (IAM):** `requestContext.authorizer.iam.userArn` — parse session name for email.
- ARN format: `arn:aws:sts::ACCOUNT:assumed-role/Role/user@company.com`

### Dependencies
- Bundle all dependencies in the deployment package (don't rely on runtime having them).
- `requirements.txt` in each Lambda directory — deployed via Layer or inline.
- PyJWT, cryptography, requests — must be bundled for auth Lambdas.

### Logging
- Format: `print(f"ERROR|WARNING|INFO: <message>")` — no logging framework.
- Never log tokens, secrets, or full request bodies.
- Log: request path, auth mode used, user identifier (email/sub), error details.

### DynamoDB Patterns (device-code)
- TTL field: `expires_at` (epoch seconds) — DynamoDB auto-deletes expired items.
- Consistent reads for grant validation (eventual consistency = race condition).
- Condition expressions for atomic state transitions (`pending` → `authorized`).

## Common Pitfalls
- Don't import modules that aren't in the Lambda runtime or your bundled deps.
- Don't use `boto3` session caching across invocations for IAM-scoped clients.
- Don't return stack traces in error responses (log them, return generic message).
- Don't hardcode region — use `os.environ["AWS_REGION"]` (Lambda sets this).
