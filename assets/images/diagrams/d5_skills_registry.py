"""Diagram 5: skills-registry-flow (SKILLS_REGISTRY.md). Content: DIAGRAM-SPECS.md section 5."""
from style import Diagram

NAME = "skills-registry-flow"
W, H = 1606, 1090
WX = 125                          # publisher / curator column
CL0 = 250                         # AWS Cloud left edge (account +20)
RG0, RG1 = 400, 590               # AWS Agent Registry component
PX = (RG0 + RG1) / 2              # EventBridge rule and SNS topic column
DX0, DX1 = 710, 870               # distributor component
BK0, BK1 = 1000, 1280             # artifact bucket group
SX = 1125                         # S3 object column
MIDB = (DX1 + BK0) / 2            # labels between the distributor and the bucket
MIDR = (SX + 32 + BK1) / 2        # labels inside the bucket, right of the objects
ACC1 = 1350                       # account right edge
DVX = 1488                        # developer machines client
Y1, Y2, Y3 = 210, 410, 620        # skills/, distribution/, approved/
YC = 800                          # curator
CORR = 106                        # top corridor for the create-only upload
RAY = 7                           # EventBridge rule glyph: top ray sits 7 px left of centre


def build(svg_dir, png_dir=None):
    d = Diagram(NAME, W, H)
    d.group(20, 58, 210, 242, "plain", "Publisher workstation")
    d.group(20, 690, 210, 210, "plain", "Curator workstation")
    d.group(CL0, 20, ACC1 + 20 - CL0, 940, "cloud", "AWS Cloud")
    d.group(CL0 + 20, 58, ACC1 - CL0 - 20, 882, "account", "AWS account")
    d.group(BK0, 130, BK1 - BK0, Y3 + 90 - 130, "bucket", "Amazon S3 artifact bucket")
    d.group(1390, 350, 196, 410, "plain", "Developer machines")

    d.node("pub", "Res_User_48_Light", WX, 190, "Publisher", ["gip skills publish"])
    d.node("cur", "Res_User_48_Light", WX, YC, "Curator", ["gip skills approve"])
    d.component("reg", RG0, 130, RG1 - RG0, 150, "Arch_Amazon-Bedrock-AgentCore_48", "AWS Agent Registry",
                ["approval state"])
    d.node("pend", "Res_Amazon-EventBridge_Rule_48", PX, 420, ["Amazon EventBridge", "rule"],
           ["pending-approval", "event"], pos="left")
    d.node("sns", "Res_Amazon-Simple-Notification-Service_Topic_48", PX, Y3, "Amazon SNS topic",
           ["curator", "notifications"], pos="right")
    d.component("dist", DX0, 330, DX1 - DX0, 400, "Arch_AWS-Lambda_48", ["AWS Lambda:", "distributor"],
                ["approve,", "promote, render"])
    d.node("sched", "Res_Amazon-EventBridge_Rule_48", 850, 830, ["Amazon EventBridge", "rule"],
           ["15-minute schedule"], pos="right")
    d.node("src", "Res_Amazon-Simple-Storage-Service_Object_48", SX, Y1, "skills/<name>/<version>/", ["review source"])
    d.node("lock", "Res_Amazon-Simple-Storage-Service_Object_48", SX, Y2, "distribution/",
           ["skills-lock.json,", "marketplace.json"])
    d.node("appr", "Res_Amazon-Simple-Storage-Service_Object_48", SX, Y3, "approved/sha256/<digest>",
           ["immutable artifacts"])
    d.node("dev", "Res_Client_48_Light", DVX, Y3, ["Claude Code,", "OpenCode,", "Codex CLI"], ["gip skills sync"])
    d.node("harness", "Arch_Amazon-Bedrock-AgentCore_48", 1250, 830, "AgentCore harness",
           ["optional; this account or", "another org account"])

    d.edge([(WX, 158), (WX, CORR), (ACC1 - 25, CORR), (ACC1 - 25, Y1 - 12), (SX + 32, Y1 - 12)],
           "create-only upload", seg=1, at=700, side="below")
    d.edge([(WX + 32, 204), (RG0, 204)], ["create record,", "submit"], at=(CL0 + 20 + RG0) / 2)
    d.edge([(PX - RAY, 280), (PX - RAY, 388)], ["Pending Approval", "event"], dashed=True, side="right")
    d.edge([(PX, 452), (PX, Y3 - 32)], "publish", dashed=True, side="right")
    d.edge([(PX - 32, Y3), (210, Y3), (210, YC + 8), (WX + 32, YC + 8)], "notify curator", dashed=True, seg=0,
           at=(CL0 + 20 + PX - 32) / 2)
    d.edge([(WX + 32, YC + 24), (790, YC + 24), (790, 730)], "synchronous invoke: approve", seg=0, at=500)
    d.edge([(850 - RAY, 798), (850 - RAY, 730)], ["every", "15 minutes"], dashed=True, side="right")
    d.edge([(730, 330), (730, 250), (RG1, 250)], ["update URI,", "approve"], seg=1)
    d.edge([(850, 330), (850, Y1 + 12), (SX - 32, Y1 + 12)], ["read version,", "verify"], seg=1, at=MIDB)
    d.edge([(DX1, Y2 + 10), (SX - 32, Y2 + 10)], ["write outputs,", "lock last"], at=MIDB)
    d.edge([(DX1, Y3), (SX - 32, Y3)], ["promote", "(If-None-Match)"], at=MIDB)
    d.edge([(DVX - 32, Y3 - 16), (SX + 32, Y3 - 16)], ["fetch version,", "verify SHA-256"], at=MIDR)
    d.edge([(DVX, Y3 - 32), (DVX, Y2 - 10), (SX + 32, Y2 - 10)], ["read", "skills-lock.json"], seg=1, at=MIDR)
    d.edge([(1250, 798), (1250, Y3 + 20), (SX + 32, Y3 + 20)], ["s3:// approved", "directory"], seg=0,
           side="left", at=744)

    d.caption(20, 1012, ["One versioned, SSE-S3, TLS-only, access-logged bucket; writes to approved/* and skills/* "
                         "must use If-None-Match, and approved/* cannot be deleted.",
                         "Publisher and curator act through IAM-signed role sessions. Readers need s3:GetObject on "
                         "the bucket; installing from marketplace.json is unverified."])
    d.legend(28, 1068, optional_box=False)
    return d.render(svg_dir, png_dir)
