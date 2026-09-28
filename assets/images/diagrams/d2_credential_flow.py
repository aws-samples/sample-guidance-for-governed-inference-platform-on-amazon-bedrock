"""Diagram 2: credential-flow-direct-diagram (README, Direct IAM Federation). Content: DIAGRAM-SPECS.md section 2."""
from style import Diagram, GREY

NAME = "credential-flow-direct-diagram"
W, H = 1629, 912
CAX, CAY = 112, 560          # credential cache
BX0, BX1 = 236, 406          # credential-process component
AX = (BX0 + BX1) / 2         # developer / app column
BADGE_X = 434                # step badges column (steps 2-5)
DEVR = 460                   # developer machine right edge
IX0, IX1 = 632, 905          # your OIDC identity provider box
IPX = IX0 + 44               # OIDC IdP icon; its label sits to the right
CL0 = 931                    # AWS Cloud left edge
MIDX = (DEVR + CL0) / 2      # label centre for lines crossing from the developer machine into AWS
C1, C2, C3 = 1066, 1266, 1460  # AWS columns
YI, YR = 246, 276            # invoke model / model response
Y2, Y3, YS = 399, 498, 684   # PKCE row, quota row, STS row
Y4, Y5 = YS - 15, YS + 15    # AssumeRoleWithWebIdentity / temporary credentials


def build(svg_dir, png_dir=None):
    d = Diagram(NAME, W, H)
    d.group(20, 20, DEVR - 20, 750, "plain", "Developer machine")
    d.group(IX0, 318, IX1 - IX0, 140, "plain", "Your OIDC identity provider")
    d.group(CL0, 142, W - 20 - CL0, 730, "cloud", "AWS Cloud")
    d.group(CL0 + 20, 180, W - 60 - CL0, 672, "account", "AWS account")
    d.group(CL0 + 35, Y3 - 72, C3 + 95 - CL0 - 35, 160, "generic", "Quota check API (optional)")

    d.node("dev", "Res_User_48_Light", AX, 104, "Developer", ["signs in"], pos="left")
    d.node("app", "Res_Client_48_Light", AX, 260, ["Claude Code /", "Claude Desktop"], ["calls Amazon Bedrock"], pos="left")
    d.component("cp", BX0, 360, BX1 - BX0, 380, "Res_Generic-Application_48_Light", "credential-process",
                ["local credential", "helper"])
    d.node("cache", "Res_Credentials_48_Light", CAX, CAY, "Credential cache", ["file or keyring;", "no long-lived AWS keys"])
    d.node("idp", "Res_Server_48_Light", IPX, Y2, "OIDC IdP",
           ["Okta, Microsoft Entra ID,", "Auth0, Google,", "Amazon Cognito", "user pools, or other OIDC"], pos="right")

    d.node("apigw", "Arch_Amazon-API-Gateway_48", C1, Y3, "Amazon API Gateway", ["quota check API"])
    d.node("fn", "Arch_AWS-Lambda_48", C2, Y3, "AWS Lambda", ["quota decision"])
    d.node("ddb", "Arch_Amazon-DynamoDB_48", C3, Y3, "Amazon DynamoDB", ["policies, usage"])
    d.node("sts", "Res_AWS-Identity-Access-Management_AWS-STS_48", C1, YS, "AWS STS", ["token exchange"])
    d.node("oidcp", "Arch_AWS-Identity-and-Access-Management_48", C2, YS, ["IAM OIDC", "identity provider"],
           ["trusts your IdP"])
    d.node("role", "Res_AWS-Identity-Access-Management_Role_48", C3, YS, "Federated IAM role",
           ["Bedrock-scoped.", "Allows (default): Anthropic", "models in allowed Regions;", "PutMetricData with monitoring.",
            "Denies bedrock-mantle:*"])
    d.node("bedrock", "Arch_Amazon-Bedrock_48", C2, (YI + YR) / 2, "Amazon Bedrock",
           ["Claude models. Cross-Region", "inference may process in other", "Regions of the geography"], pos="right")

    d.edge([(AX, 136), (AX, 228)], ["requests", "Bedrock access"], side="left", at=194, badge=(1, 0), badge_at=153)
    d.edge([(AX, 292), (AX, 360)], ["credential", "request / response"], both=True, side="left")
    d.edge([(CAX + 32, CAY), (BX0, CAY)], ["read / write", "cache"], both=True, at=190)
    d.edge([(BX1, Y2), (IPX - 32, Y2)], "PKCE sign-in, ID token", both=True, at=(DEVR + IX0) / 2,
           sub=["browser on first sign-in"], badge=(2, 0), badge_at=BADGE_X)
    d.edge([(BX1, Y3), (C1 - 32, Y3)], "quota check: allow / deny", dashed=True, both=True, at=MIDX,
           badge=(3, 0), badge_at=BADGE_X)
    d.text(MIDX, Y3 + 24, ["only when a quota endpoint is configured;", "a deny, or an error in fail-closed mode",
                           "(default), stops issuance"], 15, GREY, lh=18)
    d.edge([(C1 + 32, Y3), (C2 - 32, Y3)], "invoke")
    d.edge([(C2 + 32, Y3), (C3 - 32, Y3)], ["read policy,", "usage"])
    d.edge([(BX1, Y4), (C1 - 32, Y4)], "AssumeRoleWithWebIdentity", at=MIDX, sub=["with the ID token"],
           badge=(4, 0), badge_at=BADGE_X)
    d.edge([(C1 - 32, Y5), (BX1, Y5)], "temporary credentials", side="below", at=MIDX,
           sub=["up to 12 hours; session name", "from the email or sub claim"], badge=(5, 0), badge_at=BADGE_X)
    d.edge([(C1 + 32, YS), (C2 - 32, YS)], ["check issuer,", "audience"], dashed=True)
    d.edge([(C1, YS - 32), (C1, YS - 62), (C3, YS - 62), (C3, YS - 32)], "evaluate trust policy", dashed=True,
           seg=1, t=0.6)
    d.edge([(AX + 32, YI), (C2 - 32, YI)], "invoke model", at=MIDX,
           sub=["Claude Code: SigV4 · Claude Desktop: bearer token"], badge=(6, 0), badge_at=C2 - 32 - 42)
    d.edge([(C2 - 32, YR), (AX + 32, YR)], "model response", side="below", at=MIDX)
    d.legend(28, 890, optional_box=True)
    return d.render(svg_dir, png_dir)
