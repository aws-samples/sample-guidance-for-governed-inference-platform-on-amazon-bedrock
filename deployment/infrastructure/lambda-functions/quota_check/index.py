# ABOUTME: Lambda function for real-time quota checking before credential issuance
# ABOUTME: Returns allowed/blocked status based on user quota policy and current usage
# ABOUTME: Extracts user identity from JWT claims (OIDC) or IAM caller ARN (IDC) for quota enforcement

import json
import os
from datetime import datetime, timezone
from decimal import Decimal

import boto3

# Initialize clients
dynamodb = boto3.resource("dynamodb")


def _validated_env_enum(name: str, default: str, allowed: set[str]) -> str:
    value = os.environ.get(name, default)
    if value not in allowed:
        print(f"ERROR: Invalid {name} value; using fail-closed default '{default}'")
        return default
    return value


# Configuration from environment
QUOTA_TABLE = os.environ.get("QUOTA_TABLE", "UserQuotaMetrics")
POLICIES_TABLE = os.environ.get("POLICIES_TABLE", "QuotaPolicies")
# Security: Control fail behavior when email claim is missing or errors occur
# Default to fail-closed values; invalid values also resolve to these defaults.
MISSING_EMAIL_ENFORCEMENT = _validated_env_enum("MISSING_EMAIL_ENFORCEMENT", "block", {"block", "warn"})
ERROR_HANDLING_MODE = _validated_env_enum("ERROR_HANDLING_MODE", "fail_closed", {"fail_closed", "fail_open"})
# Server-side metering mode (quota-metering stack): "shadow" (default) keeps
# today's client-telemetry enforcement; "max" enforces on max(client, server_*)
# so a stopped sidecar no longer reduces enforced usage. "strict" is Phase 3
# and deliberately not accepted yet (see the server-side metering design doc).
METERING_MODE = os.environ.get("METERING_MODE", "shadow")

# Default limits from environment (used when fine-grained quotas are disabled)
ENABLE_FINEGRAINED_QUOTAS = os.environ.get("ENABLE_FINEGRAINED_QUOTAS", "false").lower() == "true"
QUOTA_MODE = _validated_env_enum("QUOTA_MODE", "token", {"token", "cost"})
MONTHLY_TOKEN_LIMIT = int(os.environ.get("MONTHLY_TOKEN_LIMIT", "0"))
DAILY_TOKEN_LIMIT = int(os.environ.get("DAILY_TOKEN_LIMIT", "0"))
MONTHLY_COST_LIMIT_USD = float(os.environ.get("MONTHLY_COST_LIMIT_USD", "0"))
DAILY_COST_LIMIT_USD = float(os.environ.get("DAILY_COST_LIMIT_USD", "0"))
MONTHLY_ENFORCEMENT_MODE = os.environ.get("MONTHLY_ENFORCEMENT_MODE", "block")
DAILY_ENFORCEMENT_MODE = os.environ.get("DAILY_ENFORCEMENT_MODE", "alert")
WARNING_THRESHOLD_80 = int(os.environ.get("WARNING_THRESHOLD_80", "240000000"))
WARNING_THRESHOLD_90 = int(os.environ.get("WARNING_THRESHOLD_90", "270000000"))

# DynamoDB tables
quota_table = dynamodb.Table(QUOTA_TABLE)
policies_table = dynamodb.Table(POLICIES_TABLE)


class DecimalEncoder(json.JSONEncoder):
    """JSON encoder that handles Decimal types."""

    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


def lambda_handler(event, context):
    """
    Real-time quota check for credential issuance.

    Authentication:
        JWT token required in Authorization header. API Gateway JWT Authorizer
        validates the token and passes claims to Lambda via requestContext.

    Returns:
        JSON response with allowed status and usage details
    """
    try:
        # Extract user identity from either JWT claims (OIDC) or IAM caller identity (IDC)
        email = None
        groups = []

        # Path 1: JWT Authorizer (OIDC users)
        authorizer_context = event.get("requestContext", {}).get("authorizer", {})
        jwt_claims = authorizer_context.get("jwt", {}).get("claims", {})

        if jwt_claims:
            email = jwt_claims.get("email")
            groups = extract_groups_from_claims(jwt_claims)

        # Path 2: IAM identity (IDC users) — extract identity from caller ARN
        # HTTP API payload format 2.0 with AWS_IAM authorization places the caller
        # identity at requestContext.authorizer.iam.*. Payload format 1.0 (and REST
        # APIs) use requestContext.identity.*. The quota route is deployed as an
        # HTTP API v2 route (quota-monitoring.yaml PayloadFormatVersion '2.0'), so
        # read the v2 location first and keep v1 as a backwards-compatible fallback.
        # ARN format: arn:aws:sts::ACCOUNT:assumed-role/AWSReservedSSO_.../user@company.com
        # OR:         arn:aws:sts::ACCOUNT:assumed-role/AWSReservedSSO_.../username (non-email IDC usernames)
        if not email:
            iam_context = authorizer_context.get("iam", {}) or {}
            identity = event.get("requestContext", {}).get("identity", {}) or {}
            caller_arn = (
                iam_context.get("userArn")
                or identity.get("caller")
                or identity.get("userArn")
                or ""
            )
            identity_source = "authorizer.iam" if iam_context.get("userArn") else "identity"
            if "/" in caller_arn:
                session_name = caller_arn.split("/")[-1]
                if "@" in session_name:
                    # Standard case: IDC username is an email address
                    email = session_name
                    print(f"Identity resolved from IAM ARN ({identity_source}, email): {email}")
                elif session_name and "AWSReservedSSO" in caller_arn:
                    # IDC username without @ (e.g. "akshaya.claude" instead of "user@company.com")
                    # Use the raw username as the identity — policies can be set by username
                    email = session_name
                    print(f"Identity resolved from IAM ARN ({identity_source}, IDC username): {email}")

        if not email:
            # Neither JWT nor IAM identity resolved
            print(f"No user identity found. JWT claims: {list(jwt_claims.keys())}")
            allow_missing_email = MISSING_EMAIL_ENFORCEMENT == "warn"
            return build_response(
                200,
                {
                    "error": "No user identity found (no JWT email claim or IAM session name)",
                    "allowed": allow_missing_email,
                    "reason": "missing_identity",
                    "message": "Could not resolve user identity"
                    + (" - quota check skipped" if allow_missing_email else " - access denied for security"),
                },
            )

        # 1. Resolve the effective quota policy for this user
        policy = resolve_quota_for_user(email, groups)

        if policy is None:
            # No policy = unlimited (quota monitoring disabled)
            return build_response(
                200,
                {
                    "allowed": True,
                    "reason": "no_policy",
                    "enforcement_mode": None,
                    "usage": None,
                    "policy": None,
                    "unblock_status": None,
                    "message": "No quota policy configured - unlimited access",
                },
            )

        # 2. Check for active unblock override
        unblock_status = get_unblock_status(email)
        if unblock_status and unblock_status.get("is_unblocked"):
            return build_response(
                200,
                {
                    "allowed": True,
                    "reason": "unblocked",
                    "enforcement_mode": policy.get("enforcement_mode", "alert"),
                    "usage": get_user_usage_summary(email, policy),
                    "policy": {
                        "type": policy.get("policy_type"),
                        "identifier": policy.get("identifier"),
                    },
                    "unblock_status": unblock_status,
                    "message": f"Access granted - temporarily unblocked until {unblock_status.get('expires_at')}",
                },
            )

        # 3. Get current usage
        usage = get_user_usage(email)
        usage_summary = build_usage_summary(usage, policy)

        # 4. Monthly and daily enforcement modes are independent.
        enforcement_mode = policy.get("enforcement_mode", "alert")
        daily_mode = policy.get("daily_enforcement_mode", "alert")

        # 5. Check limits (monthly, daily) — supports both token and cost modes
        monthly_tokens = usage.get("total_tokens", 0)
        daily_tokens = usage.get("daily_tokens", 0)
        monthly_cost = float(usage.get("estimated_cost", 0))
        daily_cost = float(usage.get("daily_cost_usd", 0))

        monthly_limit = policy.get("monthly_token_limit", 0)
        daily_limit = policy.get("daily_token_limit")
        monthly_cost_limit = float(policy.get("monthly_cost_limit", 0))
        daily_cost_limit = float(policy.get("daily_cost_limit", 0))

        # Cost-based enforcement (takes precedence when configured)
        if enforcement_mode == "block" and monthly_cost_limit > 0 and monthly_cost >= monthly_cost_limit:
            return build_response(
                200,
                {
                    "allowed": False,
                    "reason": "monthly_cost_exceeded",
                    "enforcement_mode": enforcement_mode,
                    "usage": usage_summary,
                    "policy": {
                        "type": policy.get("policy_type"),
                        "identifier": policy.get("identifier"),
                    },
                    "unblock_status": {"is_unblocked": False},
                    "message": f"Monthly spend limit exceeded: ${monthly_cost:.2f} / ${monthly_cost_limit:.2f} ({monthly_cost / monthly_cost_limit * 100:.1f}%). Contact your administrator.",
                },
            )

        if daily_cost_limit > 0 and daily_cost >= daily_cost_limit:
            if daily_mode == "block":
                return build_response(
                    200,
                    {
                        "allowed": False,
                        "reason": "daily_cost_exceeded",
                        "enforcement_mode": enforcement_mode,
                        "usage": usage_summary,
                        "policy": {
                            "type": policy.get("policy_type"),
                            "identifier": policy.get("identifier"),
                        },
                        "unblock_status": {"is_unblocked": False},
                        "message": f"Daily spend limit exceeded: ${daily_cost:.2f} / ${daily_cost_limit:.2f}. Resets at UTC midnight.",
                    },
                )

        # Token-based enforcement (existing behavior)
        # Check monthly token limit
        if enforcement_mode == "block" and monthly_limit > 0 and monthly_tokens >= monthly_limit:
            return build_response(
                200,
                {
                    "allowed": False,
                    "reason": "monthly_exceeded",
                    "enforcement_mode": enforcement_mode,
                    "usage": usage_summary,
                    "policy": {
                        "type": policy.get("policy_type"),
                        "identifier": policy.get("identifier"),
                    },
                    "unblock_status": {"is_unblocked": False},
                    "message": f"Monthly quota exceeded: {int(monthly_tokens):,} / {int(monthly_limit):,} tokens ({monthly_tokens / monthly_limit * 100:.1f}%). Contact your administrator for assistance.",
                },
            )

        # Check daily token limit (if configured)
        if daily_limit and daily_limit > 0 and daily_tokens >= daily_limit:
            if daily_mode == "block":
                return build_response(
                    200,
                    {
                        "allowed": False,
                        "reason": "daily_exceeded",
                        "enforcement_mode": enforcement_mode,
                        "usage": usage_summary,
                        "policy": {
                            "type": policy.get("policy_type"),
                            "identifier": policy.get("identifier"),
                        },
                        "unblock_status": {"is_unblocked": False},
                        "message": f"Daily quota exceeded: {int(daily_tokens):,} / {int(daily_limit):,} tokens ({daily_tokens / daily_limit * 100:.1f}%). Quota resets at UTC midnight.",
                    },
                )

        # All checks passed - access allowed
        return build_response(
            200,
            {
                "allowed": True,
                "reason": "within_quota",
                "enforcement_mode": enforcement_mode,
                "usage": usage_summary,
                "policy": {
                    "type": policy.get("policy_type"),
                    "identifier": policy.get("identifier"),
                },
                "unblock_status": {"is_unblocked": False},
                "message": "Access granted - within quota limits",
            },
        )

    except Exception as e:
        print(f"ERROR: quota check failed (mode={ERROR_HANDLING_MODE}): {str(e)}")
        print(
            json.dumps(
                {
                    "_aws": {
                        "Timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
                        "CloudWatchMetrics": [
                            {
                                "Namespace": "GIP/Quota",
                                "Dimensions": [[]],
                                "Metrics": [{"Name": "CheckFailures"}],
                            }
                        ],
                    },
                    "CheckFailures": 1,
                }
            )
        )
        import traceback

        traceback.print_exc()

        # Security: Honor error handling mode - default to fail-closed for security
        allow_on_error = ERROR_HANDLING_MODE == "fail_open"
        return build_response(
            200,
            {
                "allowed": allow_on_error,
                "reason": "check_failed",
                "enforcement_mode": None,
                "usage": None,
                "policy": None,
                "unblock_status": None,
                "message": f"Quota check failed ({ERROR_HANDLING_MODE})",
            },
        )


def build_response(status_code: int, body: dict) -> dict:
    """Build API Gateway response with CORS headers."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            # Mirrors the API-level CORS config (CorsAllowedOrigins parameter in
            # quota-monitoring.yaml); callers are CLI binaries, not browsers.
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization",
        },
        "body": json.dumps(body, cls=DecimalEncoder),
    }


def extract_groups_from_claims(claims: dict) -> list:
    """
    Extract group memberships from JWT token claims.

    Supports multiple claim formats:
    - groups: Standard groups claim (array or comma-separated string)
    - cognito:groups: Amazon Cognito groups claim
    - custom:department: Custom department claim (treated as a group)

    Args:
        claims: JWT claims dictionary from API Gateway JWT Authorizer

    Returns:
        List of group names
    """
    groups = []

    # Standard groups claim
    if "groups" in claims:
        claim_groups = claims["groups"]
        if isinstance(claim_groups, list):
            groups.extend(claim_groups)
        elif isinstance(claim_groups, str):
            # Could be comma-separated or single value
            groups.extend([g.strip() for g in claim_groups.split(",") if g.strip()])

    # Cognito groups claim
    if "cognito:groups" in claims:
        claim_groups = claims["cognito:groups"]
        if isinstance(claim_groups, list):
            groups.extend(claim_groups)
        elif isinstance(claim_groups, str):
            groups.extend([g.strip() for g in claim_groups.split(",") if g.strip()])

    # Custom department claim (treated as a group for policy matching)
    if "custom:department" in claims:
        department = claims["custom:department"]
        if department:
            groups.append(f"department:{department}")

    return list(set(groups))  # Remove duplicates


def resolve_quota_for_user(email: str, groups: list) -> dict | None:
    """
    Resolve the effective quota policy for a user.
    Precedence: user-specific > group (most restrictive) > default

    Returns:
        Policy dict or None if no policy applies (unlimited).
    """
    if not ENABLE_FINEGRAINED_QUOTAS and (
        MONTHLY_TOKEN_LIMIT > 0 or MONTHLY_COST_LIMIT_USD > 0 or DAILY_COST_LIMIT_USD > 0
    ):
        # F-004: Return default limits from environment. Cost limits are carried
        # alongside token limits so cost-mode deployments enforce without any
        # fine-grained DynamoDB policy (regression: cost env defaults were
        # previously dropped here, so cost mode never blocked anyone).
        return {
            "policy_type": "default",
            "identifier": "environment",
            "quota_mode": QUOTA_MODE,
            "monthly_token_limit": MONTHLY_TOKEN_LIMIT,
            "daily_token_limit": DAILY_TOKEN_LIMIT if DAILY_TOKEN_LIMIT > 0 else None,
            "monthly_cost_limit": MONTHLY_COST_LIMIT_USD,
            "daily_cost_limit": DAILY_COST_LIMIT_USD,
            "warning_threshold_80": WARNING_THRESHOLD_80,
            "warning_threshold_90": WARNING_THRESHOLD_90,
            "enforcement_mode": MONTHLY_ENFORCEMENT_MODE,
            "daily_enforcement_mode": DAILY_ENFORCEMENT_MODE,
            "enabled": True,
        }

    # 1. Check for user-specific policy
    user_policy = get_policy("user", email)
    if user_policy and user_policy.get("enabled", True):
        return user_policy

    # 2. Check for group policies (apply most restrictive)
    if groups:
        group_policies = []
        for group in groups:
            group_policy = get_policy("group", group)
            if group_policy and group_policy.get("enabled", True):
                group_policies.append(group_policy)

        if group_policies:
            return min(group_policies, key=_policy_limit_sort_key)

    # 3. Fall back to default policy
    default_policy = get_policy("default", "default")
    if default_policy and default_policy.get("enabled", True):
        return default_policy

    # 4. No policy = unlimited
    return None


def get_policy(policy_type: str, identifier: str) -> dict | None:
    """Get a policy from DynamoDB.

    Returns None only when the lookup succeeds and no item exists ("no policy
    configured"). Infrastructure failures (throttling, IAM, endpoint errors)
    propagate to the top-level handler so ERROR_HANDLING_MODE decides —
    swallowing them here would convert a DynamoDB outage into "no policy =
    unlimited access", defeating fail-closed enforcement (review F1).
    """
    pk = f"POLICY#{policy_type}#{identifier}"

    try:
        response = policies_table.get_item(Key={"pk": pk, "sk": "CURRENT"})
    except Exception as e:
        print(f"ERROR: policy lookup failed for {policy_type}:{identifier}: {e}")
        raise

    item = response.get("Item")

    if not item:
        return None

    stored_mode = item.get("quota_mode")
    has_cost_limit = any(float(item.get(field, 0) or 0) > 0 for field in ("monthly_cost_limit", "daily_cost_limit"))
    quota_mode = stored_mode or ("cost" if has_cost_limit else "token")
    # F-004: Policies written before quota_mode had no meaningful token cap.
    legacy_cost_only = stored_mode is None and quota_mode == "cost"
    return {
        "policy_type": item.get("policy_type"),
        "identifier": item.get("identifier"),
        "quota_mode": quota_mode,
        "monthly_token_limit": 0 if legacy_cost_only else int(item.get("monthly_token_limit", 0)),
        "daily_token_limit": (
            None
            if legacy_cost_only
            else int(item.get("daily_token_limit", 0))
            if item.get("daily_token_limit")
            else None
        ),
        "monthly_cost_limit": float(item.get("monthly_cost_limit", 0)),
        "daily_cost_limit": float(item.get("daily_cost_limit", 0)),
        "warning_threshold_80": int(item.get("warning_threshold_80", 0)),
        "warning_threshold_90": int(item.get("warning_threshold_90", 0)),
        "enforcement_mode": item.get("enforcement_mode", "alert"),
        "daily_enforcement_mode": item.get("daily_enforcement_mode", "alert"),
        "enabled": item.get("enabled", True),
    }


def _policy_limit_sort_key(policy: dict) -> tuple[float, float]:
    """Order group policies by their primary configured limit."""
    monthly_cost = float(policy.get("monthly_cost_limit", 0) or 0)
    monthly_tokens = float(policy.get("monthly_token_limit", 0) or 0)
    if policy.get("quota_mode") == "cost":
        return (
            monthly_cost if monthly_cost > 0 else float("inf"),
            monthly_tokens or float("inf"),
        )
    return (
        monthly_tokens if monthly_tokens > 0 else float("inf"),
        monthly_cost or float("inf"),
    )


def get_unblock_status(email: str) -> dict:
    """Check if user has an active unblock override."""
    pk = f"USER#{email}"
    sk = "UNBLOCK#CURRENT"

    try:
        response = quota_table.get_item(Key={"pk": pk, "sk": sk})
        item = response.get("Item")

        if not item:
            return {"is_unblocked": False}

        # Check if unblock has expired
        expires_at = item.get("expires_at")
        if expires_at:
            expires_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) > expires_dt:
                return {"is_unblocked": False, "expired": True}

        return {
            "is_unblocked": True,
            "expires_at": expires_at,
            "unblocked_by": item.get("unblocked_by"),
            "unblocked_at": item.get("unblocked_at"),
            "reason": item.get("reason"),
            "duration_type": item.get("duration_type"),
        }
    except Exception as e:
        # Deliberate swallow: the failure direction is "deny the override",
        # which is fail-closed (the user stays blocked). Contrast get_policy /
        # get_user_usage, where swallowing would fail open (review F1/F2).
        print(f"Error checking unblock status for {email}: {e}")
        return {"is_unblocked": False, "error": str(e)}


def get_user_usage(email: str) -> dict:
    """Get current usage for a user in the current month.

    Returns all-zeros only when the lookup succeeds and no month item exists
    (genuinely no usage yet). Infrastructure failures propagate to the
    top-level handler so ERROR_HANDLING_MODE decides — swallowing them here
    would read an over-quota user as zero usage during a DynamoDB outage or
    throttling event, exactly when heavy usage makes throttling most likely
    (review F2).
    """
    now = datetime.now(timezone.utc)
    month_prefix = now.strftime("%Y-%m")
    current_date = now.strftime("%Y-%m-%d")

    pk = f"USER#{email}"
    sk = f"MONTH#{month_prefix}"

    try:
        response = quota_table.get_item(Key={"pk": pk, "sk": sk})
    except Exception as e:
        print(f"ERROR: usage lookup failed for {email}: {e}")
        raise

    item = response.get("Item")

    if not item:
        return {
            "total_tokens": 0,
            "daily_tokens": 0,
            "daily_date": current_date,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_tokens": 0,
            "estimated_cost": 0,
            "daily_cost_usd": 0,
        }

    # Check if daily tokens need to be reset (different day)
    daily_date = item.get("daily_date")
    daily_tokens = float(item.get("daily_tokens", 0))

    if daily_date != current_date:
        # Day has changed, daily tokens should be 0 for the new day
        daily_tokens = 0

    total_tokens = float(item.get("total_tokens", 0))
    estimated_cost = float(item.get("estimated_cost", 0))
    daily_cost_usd = float(item.get("daily_cost_usd", 0)) if daily_date == current_date else 0

    # Server-side metering (quota-metering stack): server_* attributes are
    # additive on the same item and read as 0 when the stack is absent, so
    # shadow mode is byte-identical to today's behavior. In "max" mode the
    # enforced figure is max(client, server) — a stopped sidecar cannot
    # reduce it, while client figures still cover server collection gaps.
    if METERING_MODE == "max":
        server_daily_date = item.get("server_daily_date")
        server_daily_current = server_daily_date == current_date
        server_total = float(item.get("server_total_tokens", 0))
        server_daily = float(item.get("server_daily_tokens", 0)) if server_daily_current else 0
        server_cost = float(item.get("server_estimated_cost", 0))
        server_daily_cost = float(item.get("server_daily_cost_usd", 0)) if server_daily_current else 0
        total_tokens = max(total_tokens, server_total)
        daily_tokens = max(daily_tokens, server_daily)
        estimated_cost = max(estimated_cost, server_cost)
        daily_cost_usd = max(daily_cost_usd, server_daily_cost)

    return {
        "total_tokens": total_tokens,
        "daily_tokens": daily_tokens,
        "daily_date": daily_date,
        "input_tokens": float(item.get("input_tokens", 0)),
        "output_tokens": float(item.get("output_tokens", 0)),
        "cache_tokens": float(item.get("cache_tokens", 0)),
        "estimated_cost": estimated_cost,
        "daily_cost_usd": daily_cost_usd,
    }


def build_usage_summary(usage: dict, policy: dict) -> dict:
    """Build usage summary with percentages."""
    monthly_tokens = usage.get("total_tokens", 0)
    daily_tokens = usage.get("daily_tokens", 0)

    monthly_limit = policy.get("monthly_token_limit", 0)
    daily_limit = policy.get("daily_token_limit")

    summary = {
        "monthly_tokens": int(monthly_tokens),
        "monthly_limit": monthly_limit,
        "monthly_percent": round(monthly_tokens / monthly_limit * 100, 1) if monthly_limit > 0 else 0,
        "daily_tokens": int(daily_tokens),
    }

    if daily_limit:
        summary["daily_limit"] = daily_limit
        summary["daily_percent"] = round(daily_tokens / daily_limit * 100, 1) if daily_limit > 0 else 0

    return summary


def get_user_usage_summary(email: str, policy: dict) -> dict:
    """Get user usage and build summary in one call."""
    usage = get_user_usage(email)
    return build_usage_summary(usage, policy)
