"""Diagram 3: websearch-mcp-flow (WEB_SEARCH.md). Content: DIAGRAM-SPECS.md section 3."""
from style import Diagram

NAME = "websearch-mcp-flow"
W, H = 1585, 1044
DEVR = 500                 # developer machine right edge; the IdP box shares it
CL0 = 720                  # AWS Cloud left edge
GAPX = (DEVR + CL0) / 2    # labels on lines crossing from the developer machine into AWS
CPX = 120                  # credential-process component centre
CLX = 380                  # client column
GX0, GX1 = 780, 940        # AgentCore Gateway component
TX, TLX = 1080, 1395       # gateway target, Web Search Tool
REG1, ACC1, AM0, AM1 = 1165, 1185, 1205, 1545
Y1, Y2, Y3 = 190, 350, 510  # Claude Code, Claude Desktop, OpenCode / Codex CLI
YT, YR, YF = 280, 180, 640  # target row, results row, fresh-Bearer row
IDP = (370, 780)            # OIDC IdP icon centre


def build(svg_dir, png_dir=None):
    d = Diagram(NAME, W, H)
    d.group(20, 58, DEVR - 20, 622, "plain", "Developer machine")
    d.group(240, 710, DEVR - 240, 160, "plain", "Your OIDC identity provider")
    d.group(CL0, 20, W - 20 - CL0, 884, "cloud", "AWS Cloud")
    d.group(CL0 + 20, 58, ACC1 - CL0 - 20, 826, "account", "AWS account")
    d.group(CL0 + 40, 96, REG1 - CL0 - 40, 584, "region", "AWS Region: us-east-1 (gip allow-list)")
    d.group(AM0, 58, AM1 - AM0, 352, "generic", "AWS-managed, outside your account")

    d.component("cp", CPX - 80, 110, 160, 490, "Res_Generic-Application_48_Light", "credential-process",
                ["auth header,", "MCP proxy"])
    d.node("cc", "Res_Client_48_Light", CLX, Y1, "Claude Code", ["MCP over HTTP"])
    d.node("cd", "Res_Client_48_Light", CLX, Y2, "Claude Desktop", ["managed MCP server"])
    d.node("oc", "Res_Client_48_Light", CLX, Y3, ["OpenCode /", "Codex CLI"], ["stdio MCP client"])
    d.node("idp", "Res_Server_48_Light", IDP[0], IDP[1], "OIDC IdP", ["issues ID token"])
    d.component("gw", GX0, 140, GX1 - GX0, YF + 20 - 140, "Arch_Amazon-Bedrock-AgentCore_48",
                ["Amazon Bedrock", "AgentCore", "Gateway"], ["MCP endpoint,", "CUSTOM_JWT", "inbound"])
    d.node("tgt", "Arch_Amazon-Bedrock-AgentCore_48", TX, YT, ["Gateway target:", "web-search"],
           ["managed connector", "config"])
    d.node("role", "Res_AWS-Identity-Access-Management_Role_48", TX, 790, "Gateway execution role", ["IAM role"])
    d.node("tool", "Arch_Amazon-Bedrock-AgentCore_48", TLX, YT, "Web Search Tool",
           ["AWS-managed search,", "Amazon-operated", "web index"])

    cpr = CPX + 80
    d.edge([(CLX - 32, Y1), (cpr, Y1)], ["headersHelper:", "auth header"], dashed=True)
    d.edge([(CLX - 32, Y2), (cpr, Y2)], ["headersHelper,", "every 900 s"], dashed=True)
    d.edge([(CLX - 32, Y3), (cpr, Y3)], "MCP over stdio", both=True)
    d.edge([(CLX + 32, Y1), (GX0, Y1)], "MCP HTTPS, Bearer token", at=GAPX)
    d.edge([(CLX + 32, Y2), (GX0, Y2)], "MCP HTTPS, Bearer token", at=GAPX)
    d.edge([(CPX + 30, 600), (CPX + 30, YF), (GX0, YF)], "MCP HTTPS, fresh Bearer", seg=1, at=GAPX)
    d.edge([(CPX - 30, 600), (CPX - 30, IDP[1] - 12), (IDP[0] - 32, IDP[1] - 12)], "refresh ID token", dashed=True,
           seg=1, at=165)
    d.edge([(GX0 + 40, YF + 20), (GX0 + 40, IDP[1] + 12), (IDP[0] + 32, IDP[1] + 12)], "OIDC discovery, JWKS",
           dashed=True, seg=1, at=(DEVR + CL0) / 2)
    d.edge([(GX1, YT), (TX - 32, YT)], ["route", "tools/call"])
    d.edge([(GX1 - 40, YF + 20), (GX1 - 40, 790), (TX - 32, 790)], "assumes role", dashed=True, seg=1)
    d.edge([(TX + 32, YT), (TLX - 32, YT)], ["InvokeWebSearch", "(query)"], at=(AM0 + TLX - 32) / 2)
    d.edge([(TLX, YT - 32), (TLX, YR), (GX1, YR)], "titles, URLs, snippets, dates", seg=1, at=(GX1 + REG1) / 2)

    d.caption(20, 944, ["OIDC only: gip deploy websearch requires an OIDC provider, and gip generates no MCP wiring "
                        "for AWS IAM Identity Center.",
                        "Any valid ID token for the IdP app client can call the usage-billed tool; group entitlement "
                        "cannot be deployed in this release.",
                        "Query text is processed in us-east-1."])
    d.legend(28, 1020, optional_box=False)
    return d.render(svg_dir, png_dir)
