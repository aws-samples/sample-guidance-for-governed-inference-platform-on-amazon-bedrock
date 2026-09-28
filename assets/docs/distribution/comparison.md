# Distribution Platform Comparison

## Overview

The Governed Inference Platform supports two distribution methods for sharing packaged binaries and settings with end users:

1. **Presigned S3 URLs** - Simple, no authentication required
2. **Authenticated Landing Page** - Enterprise-grade with IdP integration

This guide helps you choose the right option for your organization.

---

## Quick Comparison

| Feature             | Presigned S3 URLs                  | Landing Page                            |
| ------------------- | ---------------------------------- | --------------------------------------- |
| **Best For**        | Small teams (< 20 users)           | Large teams (20-100 users)              |
| **Authentication**  | None (URLs shared via Slack/email) | IdP (Okta/Azure/Auth0/Cognito)          |
| **Setup Time**      | 5 minutes                          | 30 minutes                              |
| **Security**        | URL expiry (48 hours default, 7 days max) | IdP auth + URL expiry (1 hour)          |
| **Compliance**      | Basic                              | Enterprise-grade                        |
| **User Experience** | Copy/paste URL                     | Navigate to URL, authenticate, download |
| **Admin Overhead**  | Generate new URLs when needed      | Set up once, no maintenance             |
| **Access Control**  | Anyone with URL                    | IdP groups/users                        |

---

## Architecture Comparison

### Presigned S3 URLs

```
Admin Machine → S3 → Presigned URL (48h default) → User downloads directly
```

**How it works:**

1. Admin runs `poetry run gip distribute`
2. Package uploaded to S3
3. Presigned URL generated (expires in 48 hours by default; `--expiry-hours` up to 168 = 7 days)
4. Admin shares URL via Slack/email
5. Users download directly from S3 (no authentication)

**Pros:**

- Simple setup (no VPC, no IdP web app configuration)
- Works immediately after deployment
- No user authentication required

**Cons:**

- URL can be shared with anyone
- URLs expire after 48 hours by default, 7 days max (need to regenerate)
- No audit trail of who downloaded
- Not suitable for compliance requirements

---

### Authenticated Landing Page

```
Admin Machine → S3 → Lambda (generates presigned URLs) → User authenticates via IdP → Downloads from S3
                       ↑
                      ALB (OIDC)
```

**How it works:**

1. Admin runs `poetry run gip distribute`
2. Package uploaded to S3
3. Admin shares landing page URL via Slack/email
4. Users navigate to landing page
5. ALB redirects to IdP for authentication
6. After authentication, Lambda generates presigned URLs
7. Users download from S3 (authenticated)

**Pros:**

- Enterprise-grade security (IdP authentication)
- Access control via IdP groups
- Professional landing page UI
- Presigned URLs expire after 1 hour (limited sharing)
- Suitable for compliance requirements
- No need to regenerate URLs (landing page always available)

**Cons:**

- More complex setup (VPC, IdP web app configuration)
- Requires IdP web application configuration
- Requires networking stack (VPC, subnets)

---

## Decision Matrix

### Use Presigned S3 URLs when:

- ✅ Team size < 20 users
- ✅ Internal/trusted users only
- ✅ No compliance requirements
- ✅ Simple setup preferred
- ✅ Users can safely share URLs
- ✅ Cost is a primary concern
- ✅ No IdP infrastructure available

### Use Landing Page when:

- ✅ Team size 20-100 users
- ✅ External or untrusted users
- ✅ Compliance requirements (SOC2, audit trails)
- ✅ Already using IdP for other systems
- ✅ Need tight access control
- ✅ Professional UI preferred
- ✅ Want permanent distribution URL

---

## Setup Process Comparison

### Presigned S3 URLs Setup

1. Run `poetry run gip init`
2. Select "Presigned S3 URLs (simple, no authentication)"
3. Run `poetry run gip deploy distribution`
4. Wait 2-3 minutes for deployment
5. **Ready to use!**

### Landing Page Setup

1. Create web application in your IdP:
   - Okta: Create "Web Application"
   - Azure AD: Register application with "Web" platform
   - Auth0: Create "Regular Web Application"
   - Cognito: Create app client with "Authorization code" grant
2. Run `poetry run gip init`
3. Select "Authenticated Landing Page (IdP + ALB)"
4. Enter IdP details (domain, client ID, client secret)
5. Run `poetry run gip deploy distribution`
6. Wait 5-10 minutes for deployment
7. Configure IdP redirect URI (displayed after deployment)
8. **Ready to use!**

---

## Security Comparison

### Presigned S3 URLs

**Security Features:**

- Presigned URL with time-based expiry (7 days max)
- S3 bucket not publicly accessible
- IAM user with read-only permissions
- Package integrity via SHA256 checksum

**Security Limitations:**

- No authentication required (anyone with URL can download)
- URLs can be shared/leaked
- No audit trail of downloads
- Need to regenerate URLs regularly

**Risk Level:** Medium (suitable for internal trusted users)

#### Presigning principal

The presigned-s3 stack supports two principals for URL signing, selected by the
`PresignPrincipalType` template parameter (set
`distribution_presign_principal_type` in your profile before
`gip deploy distribution`):

| | `iam-user` (default) | `role` |
|---|---|---|
| Credentials | Static IAM user access key in Secrets Manager | None stored — `gip distribute` assumes an IAM role via STS |
| Max URL lifetime | 168 hours (7 days) | 12 hours (1 hour if the admin runs on role-chained temporary credentials) |
| Why the limit | S3 SigV4 presigned URL ceiling | Presigned URLs signed with temporary credentials expire when the session expires |
| SCP compatibility | Blocked by SCPs that deny IAM user/access-key creation | SCP-friendly (no static credentials) |
| Key leak risk | Long-lived key must be rotated/guarded | No static key exists |
| Who can presign | Anyone who can read the secret | Principals matching `AdminPrincipalArnPattern` in this account |

**Recommendation:** use `role` mode unless you genuinely need URLs that live
longer than 12 hours. `gip distribute` caps `--expires-hours` at 12 in role
mode and prints the cap when applied; re-running the command mints a fresh
URL. Existing deployments keep `iam-user` mode by default (backwards
compatible).

### Landing Page

**Security Features:**

- IdP authentication required (corporate credentials)
- ALB OIDC integration (OAuth 2.0 standard)
- Presigned URLs with short expiry (1 hour)
- Access control via IdP groups
- S3 bucket not publicly accessible
- CloudWatch logging for troubleshooting

**Security Limitations:**

- Presigned URLs valid for 1 hour (limited window for sharing)
- Requires users to have IdP access

**Risk Level:** Low (suitable for enterprise compliance)

---

## Switching Between Types

You can switch between distribution types by:

1. Run `poetry run gip init` (reconfigure)
2. Select different distribution type
3. Run `poetry run gip deploy distribution`
4. CloudFormation will replace the stack with new type

**Note:** Same stack name used for both types, so you can't have both deployed simultaneously.

---

## Recommendations

### Start with Presigned S3 if:

- You're testing/prototyping
- Team is small and internal
- You want immediate setup
- Cost is critical

### Upgrade to Landing Page when:

- Team grows beyond 20 users
- You need compliance/audit trails
- You need access control
- You want professional UX

---

## FAQ

**Q: Can I have both types deployed at once?**
A: No, they use the same CloudFormation stack name. Choose one per deployment.

**Q: How do I switch from Presigned S3 to Landing Page?**
A: Run `gip init` to reconfigure, then `gip deploy distribution` to update the stack.

**Q: Do both types work with the same `gip distribute` command?**
A: Yes! The publish process is identical. Only the download method differs.

**Q: Can users download without authentication on the landing page?**
A: No, ALB requires IdP authentication before users can access the landing page.

**Q: What happens if presigned URLs expire?**
A: For presigned-s3: Generate new URLs with `gip distribute`. For landing-page: URLs regenerate automatically when users visit.

**Q: Can I use a custom domain?**
A: Landing page supports custom domains via Route53. Presigned-s3 uses S3 URLs directly.

---

## Next Steps

- For setup instructions, see the [Distribution Deployment Guide](deployment-guide.md)
- For the `gip distribute` command options, see the [CLI Reference](../CLI_REFERENCE.md)
