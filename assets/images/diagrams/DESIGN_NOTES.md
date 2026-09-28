# Design notes: GIP architecture diagrams

Five hand-laid-out diagrams, one Python script each (`d1_`–`d5_*.py`), sharing the primitives and
build-failing checks in `style.py`. Content is bound to `DIAGRAM-SPECS.md` (node and edge tables).
These notes record the rules the diagrams satisfy, so a later edit does not undo them silently.

## Rules

- **Icons.** Official AWS Architecture Icons, package Q3 2026 (07.31.2026), in `icons/` byte-identical to the
  package and inlined unmodified as SVG symbols. Sizes: 64 px for nodes and hub components, 48 px inside module
  cards, 32 px for group icons. The package has one AgentCore icon, so every AgentCore and AWS Agent Registry node
  uses it and carries its exact name in the label.
- **Groups.** AWS Cloud `#232F3E`, AWS account `#E7157B` and Amazon S3 bucket `#7AA116` are solid with their group
  icons. AWS Region `#00A4A6` is dashed with the Region flag. Workstations and "Your OIDC identity provider" are
  plain `#7D8998` boxes. Dashed `#7D8998` boxes mark optional modules, explained by the legend swatch; the other
  dashed boxes state their meaning in the title ("Extracted-only mode (default)", "AWS-managed, outside your
  account"). Nesting is Cloud > account > Region. IAM sits outside the Region box, and AWS-managed processing sits
  inside AWS Cloud but outside the account.
- **Text.** Arial/Helvetica, with Menlo for commands. Node, component and group labels are 17 px (group and card
  titles bold). Card icon labels, commands, grey sub-captions, edge labels, captions, legend and step badges are
  15 px. The build fails unless min font × 980 / canvas width ≥ 9, which means text is at least 9 px at the 980 px
  README display width.
- **Connectors.** Orthogonal only, 1.6 px `#232F3E`. Solid lines are request or data flows. Dashed lines are auth
  or validation, async or scheduled triggers, or flows that exist only when an optional module is on. Numbered
  badges appear only in the credential-flow diagram, 1:1 with the README list.
- **Glyph snapping.** `icons/glyph-extents.json` holds the opaque extents (alpha ≥ 128) of every row and column of
  each icon at 128 px. `edge()` moves each end that sits on a node icon's 64 px box onto the visible glyph along its
  row or column. When that row or column is empty, it uses the nearest non-empty one within 6 %. Side edges on a
  person attach below the neck, and EventBridge rule edges from above attach at the top ray.
- **Component boxes** for hub processes (credential-process, AgentCore Gateway, AgentCore Memory, AWS Agent Registry,
  the distributor): icon and label at the top, ports along the border.
- **No in-image titles.** The embedding document's heading and the alt text (`ALT-TEXT.md`) carry the title.
- **Caption block.** Caveats go in grey sentences under the drawing. The one fact a diagram exists to show also
  sits on its element, for example "log-only" on memory-tools.
- **Build-failing checks**, run on every build: diagonal segments; a connector through text, an icon, a badge or
  another connector; overlapping text; text within 3 px of a border; anything off the canvas; an edge end more than
  3 px from an opaque glyph pixel that is not on a card, component or group border; the readability rule.

## Rebuild

```
python3 build.py                        # all five; exits 1 on any check error
python3 build.py memory-architecture    # one diagram
python3 build.py --svg-dir DIR --png-dir DIR
```

The build uses only the Python 3 standard library and needs `rsvg-convert` (librsvg) on PATH. Copied to
`assets/images/diagrams/`, the build writes each `<name>.svg` beside the scripts and a 2× `<name>.png` to
`assets/images/`. Anywhere else, both go to `out/`. Text widths come from Helvetica metrics plus a 4 % margin, so the
checks do not depend on the installed fonts. Regenerate the glyph table only when `icons/` changes:
`uv run --with pillow python3 measure_glyphs.py`.

## Self-check

| Diagram | Canvas (px) | Min font → px at 980 | Nodes vs spec | Edges vs spec | Checks |
|---|---|---|---|---|---|
| Platform overview (README hero) | 1584 × 1378 | 15 → 9.28 | 12/12 | 11/11 | all pass |
| Direct IAM Federation credential flow | 1629 × 912 | 15 → 9.02 | 12/12 | 13/13 | all pass; badges 1–6 = CF-E1, E3, E5, E8, E11, E12 |
| Web search over MCP | 1585 × 1044 | 15 → 9.27 | 9/9 | 12/12 | all pass |
| Memory architecture | 1624 × 1026 | 15 → 9.05 | 11/11 | 11/11 | all pass |
| Skills registry flow | 1606 × 1090 | 15 → 9.15 | 12/12 | 14/14 | all pass |

`build.py` prints the counts, canvas and readability figures for each diagram. Each PNG was also inspected visually
after rendering with rsvg-convert 2.62.3.

## Review response (DIAGRAM-REVIEW.md, 2026-09-25)

| # | Response |
|---|---|
| 1 | Fixed as above. All 14 listed ends now touch their glyphs. Alarm edges land on the circle over the tall bar and on the base. |
| 2 | Fixed. The minimum font is 15 px and canvases are 1584–1629 px wide, so text renders at 9.02–9.28 px at 980 px; the readability check is part of the build. Edge labels are 15 px, the brief's minimum, not the review's suggested 16 px. |
| 3 | Fixed: two-line "Amazon Bedrock / AgentCore Gateway" and "… / AgentCore Memory" labels. The MCP card grew to fit. |
| 4, 5 | Fixed: "Amazon Cognito user pools"; "no long-lived AWS keys". |
| 6 | Fixed: the memory-tools sub-caption reads "log-only: only org_knowledge_search runs". |
| 7 | Fixed: the memory legend has no optional-module swatch, and both AWS-managed groups read "AWS-managed, outside your account". |
| 8, 9 | Fixed: badge 6 sits at the Amazon Bedrock end, and the invoke edge has the sub-caption "all clients except Codex CLI". |
| 10, 11 | Fixed with the review's wording. Item 10 was reworded in a second pass so PutMetricData reads as allowed: "Allows (default): Anthropic models in allowed Regions; PutMetricData with monitoring. Denies bedrock-mantle:*". |
| 12 | Fixed: the quota label clears the IdP box by 20 px, and the IdP caption moved beside its icon. |
| 13, 15 | Fixed: "internals per its README"; "internal apps (any SDK host)"; "Sidecar: no Fargate, no analytics"; "Athena over S3 data lake". |
| 14 | Resolved: the Web Search Tool is AWS-managed and outside your account (diagram 3 draws that boundary), so the platform overview no longer draws it inside the account; its platform-tools card notes "Calls the AWS-managed Web Search Tool". |
| 16 | Fixed: Amazon Bedrock's caption moved above its icon and renders 19.5 px from the account border. The Region box sits at least 20 px outside the MCP card. |
| 17 | Fixed: the IdP box shares the developer box's right edge, and its two arrows enter 24 px apart. |
| 18 | Fixed for web search (the AWS-managed box ends at y 410) and memory (the Memory box is 220 px tall). Partly fixed for the credential flow: the AWS boxes now start just above the Amazon Bedrock row, and the quota and STS rows moved up about 130 px. A band of about 130 px remains between the Bedrock row and the quota group, because the IdP box and the PKCE row have to sit between them. |
| 19 | Fixed: "Extracted-only mode (default)". The alarm label sits about 78 px below the memory-tools caption. |
| 20 | Fixed: the legend is bottom-left, and the upload corridor moved to y 106 with its label below the line. |
