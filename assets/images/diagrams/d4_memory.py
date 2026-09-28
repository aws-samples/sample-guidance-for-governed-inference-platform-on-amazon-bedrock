"""Diagram 4: memory-architecture (MEMORY.md). Content: DIAGRAM-SPECS.md section 4."""
from style import Diagram

NAME = "memory-architecture"
W, H = 1624, 1026
LX = 122                          # client / operator column
CL0 = 250                         # AWS Cloud left edge; account +20, Region +40
GWX, TGX, FNX = 445, 620, 800     # request row: gateway, target, memory-tools Lambda
MX0, MX1 = 1026, 1186             # AgentCore Memory component
MCX = (MX0 + MX1) / 2
REG1, ACC1, AM0, AM1 = 1206, 1226, 1246, 1584
EX = 1412                         # built-in extraction
YA, YB, YC = 300, 660, 786        # request row, sweeper row, operator row
AX, AY = 930, 510                 # CloudWatch alarms
RX = 620                          # EventBridge rule


def build(svg_dir, png_dir=None):
    d = Diagram(NAME, W, H)
    d.group(20, 190, 204, 250, "plain", "Developer machines")
    d.group(20, 700, 204, 190, "plain", "Operator workstation")
    d.group(CL0, 20, W - 20 - CL0, 870, "cloud", "AWS Cloud")
    d.group(CL0 + 20, 58, ACC1 - CL0 - 20, 812, "account", "AWS account")
    d.group(CL0 + 40, 96, REG1 - CL0 - 40, 754, "region", "AWS Region: us-east-1 (web search gateway Region)")
    d.group(520, YB - 90, 365, 205, "generic", "Extracted-only mode (default)")
    d.group(AM0, 58, AM1 - AM0, 372, "generic", "AWS-managed, outside your account")

    d.node("cl", "Res_Client_48_Light", LX, YA, "MCP clients", ["Claude Code,", "Claude Desktop,", "OpenCode, Codex CLI"])
    d.node("op", "Res_User_48_Light", LX, YC, "Operator", ["gip memory", "forget-user"])
    d.node("gw", "Arch_Amazon-Bedrock-AgentCore_48", GWX, YA, ["Amazon Bedrock", "AgentCore Gateway"],
           ["shared with", "web search"])
    d.node("tgt", "Arch_Amazon-Bedrock-AgentCore_48", TGX, YA, ["Gateway target:", "memory tools"],
           ["up to 4 MCP tools"])
    d.node("fn", "Arch_AWS-Lambda_48", FNX, YA, ["AWS Lambda:", "memory-tools"],
           ["log-only: only", "org_knowledge_search runs"])
    d.node("alarm", "Res_Amazon-CloudWatch_Alarm_48", AX, AY, ["Amazon CloudWatch", "alarms"], [], pos="left")
    d.node("rule", "Res_Amazon-EventBridge_Rule_48", RX, YB, ["Amazon EventBridge", "rule"], ["daily schedule"])
    d.node("sw", "Arch_AWS-Lambda_48", FNX, YB, ["AWS Lambda:", "sweeper"], ["raw-event purge"])
    d.node("kms", "Arch_AWS-Key-Management-Service_48", MCX, 170, "AWS KMS key", ["customer managed"], pos="left")
    d.component("mem", MX0, 240, MX1 - MX0, 220, "Arch_Amazon-Bedrock-AgentCore_48",
                ["Amazon Bedrock", "AgentCore", "Memory"], ["memory store"])
    d.node("ext", "Arch_Amazon-Bedrock-AgentCore_48", EX, YA, "Built-in extraction",
           ["async inference; US", "geography: us-east-1,", "us-east-2, us-west-2"])

    d.edge([(LX + 32, YA), (GWX - 32, YA)], ["MCP tools/call,", "Bearer"], at=(CL0 + 40 + GWX - 32) / 2)
    d.edge([(GWX + 32, YA), (TGX - 32, YA)], ["route", "memory tools"])
    d.edge([(TGX + 32, YA), (FNX - 32, YA)], ["Lambda", "invoke"])
    d.edge([(FNX + 32, YA - 10), (MX0, YA - 10)], "org_knowledge_search", dashed=True, sub=["org/knowledge only"])
    d.edge([(FNX + 32, YA + 14), (AX + 18, YA + 14), (AX + 18, AY - 32)], ["Errors", "metric"], dashed=True, seg=1,
           side="right", at=AY - 100)
    d.edge([(RX + 32, YB), (FNX - 32, YB)], "rate(1 day)", dashed=True)
    d.edge([(FNX + 32, YB + 14), (MX0 + 30, YB + 14), (MX0 + 30, 460)], ["DeleteEvent", "older than 24 h"],
           dashed=True, seg=0, side="below", at=(885 + MX0 + 30) / 2)
    d.edge([(FNX + 32, YB - 14), (AX, YB - 14), (AX, AY + 32)], ["Errors,", "Invocations"], dashed=True, seg=1,
           side="right")
    d.edge([(MCX, 202), (MCX, 240)], "encrypts", dashed=True, side="right")
    d.edge([(MX1, YA), (EX - 32, YA)], "async extraction", dashed=True, both=True, at=(AM0 + EX - 32) / 2)
    d.edge([(LX + 32, YC + 14), (MX1 - 30, YC + 14), (MX1 - 30, 460)], "forget-user, IAM-signed", seg=0,
           at=(CL0 + 40 + 520) / 2)

    d.caption(20, 934, ["DeployGate = log-only: memory_store, memory_retrieve and org_knowledge_add return a gated "
                         "response and read or write nothing; only org_knowledge_search reads the store.",
                         "Extracted-only mode: the sweeper deletes raw events older than 24 hours once a day, and raw "
                         "events expire after 3 days, so raw text can exist for up to about 48 hours.",
                         "Stored only in us-east-1. Alarm notifications only if AlarmTopicArn is set (gip deploy memory "
                         "never sets it). Key and Memory name are replacement-on-update."])
    d.legend(28, 1006, optional_box=False)
    return d.render(svg_dir, png_dir)
