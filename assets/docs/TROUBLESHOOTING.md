# Troubleshooting

## Step 1: Run `gip doctor`

```bash
poetry run gip doctor           # Quick health check
poetry run gip doctor --verbose # Detailed config dump
poetry run gip doctor --json    # Machine-readable output
```

If checks fail, the command prints a pre-filled GitHub issue URL — just click and submit.

## Common Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| CloudWatch metrics stuck at 0 | otel-helper not installed or not spawning | Re-run `gip package` with Go installed, then re-install |
| "Cloud authentication" error in Claude Code | Credential refresh expired (IDC) or browser auth failed (OIDC) | Run `credential-process --profile <name>` manually to re-authenticate |
| Telemetry never reaches dashboard | `otel_collector_endpoint` missing from config | Run `gip deploy monitoring` then re-package |
| `gip package` reports "no binaries built" | Go not installed or wrong version | Install Go 1.24+ and re-run |
| Windows Defender blocks binaries | Unsigned Go executables trigger heuristic detection | Add install directory to exclusions (see [#649](https://github.com/aws-solutions-library-samples/guidance-for-claude-code-with-amazon-bedrock/issues/649)) |
| Region mismatch deployment failure | `aws_region` differs from Cognito/IdP region | Re-run `gip init` and verify region selection |
| Landing page returns "Forbidden" after signing in | Identity headers rejected: expired session, or the stack was redeployed behind a new load balancer | Sign in again to refresh the session; if it persists, check the landing-page log group for `AUTHZ_ERROR` and re-run `gip deploy distribution` |

## Debug Logging

For credential-process issues, enable debug output:

```bash
# Claude Code debug logs
CLAUDE_CODE_DEBUG_LOGS_DIR=~/.claude/debug claude --debug

# Direct credential-process test
~/gip/credential-process --profile gip --debug
```

## Filing a Bug

Run `gip doctor` — on failure it generates a pre-filled GitHub issue URL with:
- All check results
- OS, Python, auth type, monitoring mode (auto-detected)

Click the link, add any extra context, submit. That's it.

## Getting Help

- [Operational Runbooks](RUNBOOKS.md) — procedures for outages, rotations, quota failures, and upgrades
- [CLI Reference](CLI_REFERENCE.md) — full command documentation
- [GitHub Issues](https://github.com/aws-samples/sample-guidance-for-governed-inference-platform-on-amazon-bedrock/issues) — search existing issues
- [Monitoring Guide](MONITORING.md) — telemetry setup and dashboards
