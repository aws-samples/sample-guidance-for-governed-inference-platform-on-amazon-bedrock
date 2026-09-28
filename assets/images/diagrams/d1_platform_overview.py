"""Diagram 1: platform-overview-v2 (README hero). Content: DIAGRAM-SPECS.md section 1."""
from style import Diagram

NAME = "platform-overview-v2"
W, H = 1584, 1378
DEV_R = 240                   # developer machines right edge
CLOUD_X = 480                 # AWS Cloud left edge; cross-boundary labels sit in DEV_R..CLOUD_X
CX0, CW = 540, 744            # module card column
CX1 = CX0 + CW                # card right edge
ICON_X0 = CX0 + 262           # icons start after the text column
# card top and height per module
ROW = {"auth": (100, 118), "gw": (290, 116), "gov": (424, 106), "quota": (548, 106), "tel": (672, 106),
       "mcp": (844, 122), "skills": (1006, 106), "dist": (1130, 106)}
REG = (CX0 - 22, 800, CW + 44, 186)  # us-east-1 Region box around the MCP card (22 px inset)
BED = (1460, 262)             # Amazon Bedrock icon centre; its label sits above the icon
MID = (DEV_R + CLOUD_X) / 2   # centre of the cross-boundary edge labels


def cy(r):
    return ROW[r][0] + ROW[r][1] / 2


def card(d, cid, title, cmds, icons, notes=(), dashed=True):
    y, h = ROW[cid]
    d.card(cid, CX0, y, CW, h, title, cmds, icons, dashed=dashed, icon_x0=ICON_X0, slots=3, notes=notes)


def build(svg_dir, png_dir=None):
    d = Diagram(NAME, W, H)
    d.group(CLOUD_X, 20, W - 20 - CLOUD_X, 1300, "cloud", "AWS Cloud")
    d.group(CLOUD_X + 20, 58, W - 60 - CLOUD_X, 1242, "account", "AWS account")
    d.group(*REG, "region", "AWS Region: us-east-1 (gip allow-list)")
    d.group(20, 58, DEV_R - 20, 1242, "plain", "Developer machines")

    d.node("cc", "Res_Client_48_Light", 130, 166, "Claude Code", ["CLI harness"])
    d.node("cd", "Res_Client_48_Light", 130, 470, "Claude Desktop", ["Chat, Cowork, Code"])
    d.node("sdk", "Res_Client_48_Light", 130, 820, ["Other AWS SDK", "clients"],
           ["OpenCode, Pi, Aider,", "internal apps (any SDK host),", "Codex CLI: MCP tools only"])
    d.text(130, 1236, ["credential-process and", "otel-helper, installed", "by gip package"], 15, "#545B64", lh=18)

    card(d, "auth", "Authentication (core)", ["gip deploy auth"],
         [("Arch_AWS-Identity-and-Access-Management_48", ["IAM OIDC provider,", "federated role"]),
          ("Res_AWS-Identity-Access-Management_AWS-STS_48", "AWS STS")],
         ["Federates your OIDC IdP or", "AWS IAM Identity Center;", "default-on Anthropic-only scoping"], dashed=False)
    card(d, "gw", "Claude apps gateway", [],
         [("Arch_Elastic-Load-Balancing_48", "internal ALB"), ("Arch_AWS-Fargate_48", "gateway container"),
          ("Arch_Amazon-RDS_48", ["PostgreSQL", "sign-in state"])],
         ["Recommended for Claude apps.", "Pinned upstream AWS Samples", "CDK, not gip deploy;",
          "internals per its README"])
    card(d, "gov", "Model governance", ["gip deploy guardrails", "gip deploy model-lifecycle"],
         [("Arch_Amazon-Bedrock_48", ["Amazon Bedrock", "Guardrails"]), ("Arch_AWS-Lambda_48", "lifecycle check")])
    card(d, "quota", "Quota + metering", ["gip deploy quota", "gip deploy metering"],
         [("Arch_Amazon-API-Gateway_48", "quota check API"), ("Arch_AWS-Lambda_48", ["check, monitor,", "metering"]),
          ("Arch_Amazon-DynamoDB_48", "policies, usage")])
    card(d, "tel", "Telemetry + analytics", ["gip deploy monitoring", "gip deploy analytics"],
         [("Arch_AWS-Fargate_48", ["OTEL collector", "(central mode)"]), ("Arch_Amazon-CloudWatch_48", "dashboards"),
          ("Arch_Amazon-Athena_48", ["Athena over", "S3 data lake"])],
         ["Sidecar: no Fargate, no analytics"])
    card(d, "mcp", "Platform tools over MCP", ["gip deploy websearch", "gip deploy memory"],
         [("Arch_Amazon-Bedrock-AgentCore_48", ["Amazon Bedrock", "AgentCore Gateway"], ["MCP endpoint"]),
          ("Arch_Amazon-Bedrock-AgentCore_48", ["Amazon Bedrock", "AgentCore Memory"], ["log-only"])],
         ["Calls the AWS-managed", "Web Search Tool"])
    card(d, "skills", "Skills registry", ["gip deploy skills"],
         [("Arch_Amazon-Bedrock-AgentCore_48", "AWS Agent Registry"),
          ("Arch_Amazon-Simple-Storage-Service_48", ["immutable", "artifacts"]), ("Arch_AWS-Lambda_48", "distributor")])
    card(d, "dist", "Distribution", ["gip deploy distribution"],
         [("Arch_Amazon-Simple-Storage-Service_48", "packages")],
         ["Presigned URLs or a landing", "page behind IdP sign-in"])

    d.node("bedrock", "Arch_Amazon-Bedrock_48", BED[0], BED[1], "Amazon Bedrock",
           ["Claude models", "AWS service, not", "deployed by GIP"], pos="above")

    d.edge([(DEV_R, cy("auth")), (CX0, cy("auth"))], "credential-process federation", dashed=True, at=MID)
    d.edge([(DEV_R, BED[1]), (BED[0] - 32, BED[1])], "invoke, temporary credentials", at=MID,
           sub=["all clients except Codex CLI"])
    d.edge([(DEV_R, cy("gw")), (CX0, cy("gw"))], "gateway sign-in (OIDC)", dashed=True, at=MID,
           sub=["Claude Code, Claude Desktop"])
    d.edge([(DEV_R, cy("quota")), (CX0, cy("quota"))], "quota check before STS", dashed=True, at=MID)
    d.edge([(DEV_R, cy("tel")), (CX0, cy("tel"))], "OTLP telemetry", dashed=True, at=MID,
           sub=["Claude Code, Claude Desktop", "(Desktop: aggregate only)"])
    d.edge([(DEV_R, cy("mcp")), (CX0, cy("mcp"))], "MCP, Bearer ID token", dashed=True, at=MID,
           sub=["Claude Code, Claude Desktop,", "OpenCode, Codex CLI"])
    d.edge([(CX0, cy("skills")), (DEV_R, cy("skills"))], "gip skills sync", dashed=True, at=MID,
           sub=["Claude Code, OpenCode,", "Codex CLI"])
    d.edge([(CX0, cy("dist")), (DEV_R, cy("dist"))], "installer download", dashed=True, at=MID)

    bb = BED[1] + 32
    d.edge([(CX1, cy("gw")), (BED[0] - 24, cy("gw")), (BED[0] - 24, bb)], "inference, task role", dashed=True, seg=0)
    d.edge([(CX1, cy("gov")), (BED[0], cy("gov")), (BED[0], bb)], ["enforced guardrail", "configuration"], dashed=True,
           seg=0)
    d.edge([(BED[0] + 24, bb), (BED[0] + 24, cy("quota")), (CX1, cy("quota"))], "invocation-log metering",
           dashed=True, seg=1)

    d.caption(CX0, 1262, ["Other modules deploy to the Region you choose in gip init.",
                          "Guardrails and metering deploy one stack per allowed Amazon Bedrock Region."])
    d.legend(28, 1352)
    return d.render(svg_dir, png_dir)
