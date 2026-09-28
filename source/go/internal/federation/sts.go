package federation

import (
	"context"
	"fmt"
	"os"
	"regexp"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	awsconfig "github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/sts"
	"gip-go/internal/jwt"
)

// AWSCredentials is the credential_process output format.
type AWSCredentials struct {
	Version         int    `json:"Version"`
	AccessKeyID     string `json:"AccessKeyId"`
	SecretAccessKey string `json:"SecretAccessKey"`
	SessionToken    string `json:"SessionToken"`
	Expiration      string `json:"Expiration"`
}

var sanitizeRe = regexp.MustCompile(`[^\w+=,.@\-]`)

// sessionNameRe is the full STS RoleSessionName constraint ([\w+=,.@-], 2-64
// chars). Bound modes validate against it and fail closed instead of
// sanitizing: the trust policy compares the session name to the raw claim
// with StringEquals, so any local rewrite would guarantee an AccessDenied.
var sessionNameRe = regexp.MustCompile(`^[\w+=,.@-]{2,64}$`)

// AssumeRoleWithWebIdentity exchanges an OIDC token for AWS credentials via direct STS.
//
// sessionNameBinding mirrors the auth stack's SessionNameBinding parameter
// (""/"none"/"legacy" = client-chosen legacy name, "email"/"sub" = raw claim).
func AssumeRoleWithWebIdentity(region, roleARN, idToken string, claims jwt.Claims, maxDuration int, sessionNameBinding string) (*AWSCredentials, error) {
	// Clear AWS env vars to prevent recursive credential resolution
	savedEnv := clearAWSEnv()
	defer restoreEnv(savedEnv)

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	cfg, err := awsconfig.LoadDefaultConfig(ctx,
		awsconfig.WithRegion(region),
		awsconfig.WithCredentialsProvider(aws.AnonymousCredentials{}),
	)
	if err != nil {
		return nil, fmt.Errorf("loading AWS config: %w", err)
	}

	client := sts.NewFromConfig(cfg)

	// Build session name
	sessionName, err := buildSessionName(claims, sessionNameBinding)
	if err != nil {
		return nil, err
	}

	input := &sts.AssumeRoleWithWebIdentityInput{
		RoleArn:          aws.String(roleARN),
		RoleSessionName:  aws.String(sessionName),
		WebIdentityToken: aws.String(idToken),
		DurationSeconds:  aws.Int32(int32(maxDuration)),
	}

	result, err := client.AssumeRoleWithWebIdentity(ctx, input)
	if err != nil {
		if isBoundMode(sessionNameBinding) {
			return nil, fmt.Errorf(
				"AssumeRoleWithWebIdentity failed: %w\n"+
					"Note: this deployment binds session names to the %q token claim "+
					"(session_name_binding=%s). If the error is AccessDenied, the role trust "+
					"policy's sts:RoleSessionName condition likely rejected the session name — "+
					"make sure the stack's SessionNameBinding parameter matches this client's "+
					"config.json and that your ID token carries the claim.",
				err, sessionNameBinding, sessionNameBinding)
		}
		return nil, fmt.Errorf("AssumeRoleWithWebIdentity failed: %w", err)
	}

	creds := result.Credentials
	expiration := ""
	if creds.Expiration != nil {
		expiration = creds.Expiration.Format(time.RFC3339)
	}

	return &AWSCredentials{
		Version:         1,
		AccessKeyID:     aws.ToString(creds.AccessKeyId),
		SecretAccessKey: aws.ToString(creds.SecretAccessKey),
		SessionToken:    aws.ToString(creds.SessionToken),
		Expiration:      expiration,
	}, nil
}

// buildSessionName derives an STS RoleSessionName from OIDC claims.
//
// The resulting principal ARN (assumed-role/RoleName/<session-name>) is what
// appears in the CUR 2.0 line_item_iam_principal column, enabling per-user
// Bedrock cost visibility without IdP-side session tag configuration.
//
// binding selects the naming contract (mirrors the auth stack's
// SessionNameBinding parameter):
//
//   - ""/"none"/"legacy": best-effort readable name — full email when
//     available (sanitized, truncated to 64), else a prefixed sanitized sub.
//   - "email"/"sub": the session name must be EXACTLY the raw claim, because
//     the role trust policy enforces
//     StringEquals sts:RoleSessionName == ${<provider>:email|sub}. No
//     sanitizing, no truncation, no prefix — any mismatch means STS denies
//     the call, so invalid or missing claims fail closed with a clear error.
//
// AWS STS RoleSessionName regex: [\w+=,.@-]*, 2-64 chars.
func buildSessionName(claims jwt.Claims, binding string) (string, error) {
	switch binding {
	case "", "none", "legacy":
		return legacySessionName(claims), nil
	case "email", "sub":
		value := claims.GetString(binding)
		if value == "" {
			return "", fmt.Errorf(
				"session_name_binding is %q but the ID token has no %q claim.\n"+
					"STS would deny the bound AssumeRoleWithWebIdentity call anyway (the trust "+
					"policy requires the session name to equal that claim). Ask your admin to "+
					"redeploy the auth stack with SessionNameBinding=%s, or fix the IdP to emit "+
					"the claim.",
				binding, binding, alternativeBinding(binding))
		}
		if !sessionNameRe.MatchString(value) {
			return "", fmt.Errorf(
				"session_name_binding is %q but the token's %s claim %q cannot be an STS "+
					"session name (allowed: 2-64 characters from [A-Za-z0-9_+=,.@-]).\n"+
					"Ask your admin to set SessionNameBinding=%s on the auth stack, or use "+
					"Cognito federation mode.",
				binding, binding, value, alternativeBinding(binding))
		}
		return value, nil
	default:
		return "", fmt.Errorf(
			"invalid session_name_binding %q in config.json (expected none, email, or sub)", binding)
	}
}

// alternativeBinding suggests the other binding mode in error messages.
func alternativeBinding(binding string) string {
	if binding == "email" {
		return "sub"
	}
	return "email"
}

// isBoundMode reports whether the binding mode enforces exact claim equality.
func isBoundMode(binding string) bool {
	return binding == "email" || binding == "sub"
}

// legacySessionName is the historical client-chosen naming: readable,
// sanitized, truncated — appropriate only when the trust policy does not
// constrain sts:RoleSessionName.
func legacySessionName(claims jwt.Claims) string {
	if email := claims.GetString("email"); email != "" {
		sanitized := sanitizeRe.ReplaceAllString(email, "-")
		if len(sanitized) > 64 {
			sanitized = sanitized[:64]
		}
		return sanitized
	}
	if sub := claims.GetString("sub"); sub != "" {
		// Auth0 often uses pipe-delimited sub (e.g. auth0|12345); sanitize first.
		sanitized := sanitizeRe.ReplaceAllString(sub, "-")
		if len(sanitized) > 32 {
			sanitized = sanitized[:32]
		}
		return "gip-" + sanitized
	}
	return "gip"
}

func clearAWSEnv() map[string]string {
	vars := []string{"AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"}
	saved := make(map[string]string)
	for _, v := range vars {
		if val, ok := os.LookupEnv(v); ok {
			saved[v] = val
			os.Unsetenv(v)
		}
	}
	return saved
}

func restoreEnv(saved map[string]string) {
	for k, v := range saved {
		os.Setenv(k, v)
	}
}
