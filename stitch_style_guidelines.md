## Brand & Style

This design system is engineered for distributed systems engineers, SREs, and platform operators who navigate complex, high-throughput topologies under high-stress conditions. It operates as precision instrumentation: zero fluff, zero ornamental visual noise, and absolute fidelity to data hierarchy.

The aesthetic fuses industrial utilitarianism with high-density technical dashboards. Every interface element exists solely to convey system state, network health, latency distribution, or service dependency. Visual affordances are mechanical and discrete, prioritizing scan speed, high typographic contrast, and instant recognition of operational anomalies. Surfaces do not rely on skeuomorphic bevels or soft atmospheric gradients; instead, structural gridlines, 1px rules, calibrated operational tints, and strict tabular alignment establish spatial hierarchy.

## Layout & Spacing

The layout is structured around an ultra-compact 4px baseline sub-grid combined with an 8px module rule. Screen real estate is treated as high-value workspace: padding is reserved strictly for legibility barriers, avoiding excessive decorative whitespace.

### Grid and Pane Orchestration
- **Root Shell:** Fluid-width workstation layout composed of dockable, collapsible panels:
  - Global Command Rail: 48px fixed width (icon-only, high-contrast states).
  - Primary Tree / Hierarchy Rail: 240px default width (resizable down to 180px, collapsible to 0px).
  - Telemetry Canvas: Fully fluid multi-row layout using CSS Grid or Split.js horizontal panes.
  - Inspection Drawer: 360px–480px slide-over or dock-right inspector.
- **Density Rhythms:**
  - Table and List rows: 28px standard height, 22px ultra-compact density mode.
  - Card Internal Padding: `space-sm` (8px) on dense topological monitors; `space-md` (12px) on analytic chart cards.
  - Node Gaps: 8px horizontal and vertical inter-card spacing across metric dashboards.

### Responsive & Viewport Adaptation
- **Desktop Primary (>= 1440px):** Full multi-pane workflow; simultaneous rendering of topology DAG (Directed Acyclic Graph), streaming log tails, and metric correlation drawers.
- **Medium / Laptop (1024px – 1439px):** Inspector defaults to floating overlay; hierarchy rail collapses automatically into an icon menu.
- **Small / Field Responder (< 1024px):** Single-column stacked mode; tabs substitute split horizontal panes; topology graph swaps to serialized list view of nodes grouped by severity.

## Elevation & Depth

This design system rejects diffuse, deep, multi-layered drop shadows. Elevation is defined through **structural layering, border boundaries, and z-index surface nesting**.

### Surface Elevation Levels
- **Layer 0 (Canvas):** `#F8FAFC`. Base root surface containing graph nodes, split tracks, and main gutters.
- **Layer 1 (Card / Node / Row Surface):** `#FFFFFF` paired with an explicit `1px solid #E2E8F0` border. No drop shadow. Hover state switches border to `1px solid #CBD5E1`.
- **Layer 2 (Floating Popover / Metric Tooltip / Context Menu):** `#FFFFFF` with a crisp structural border `1px solid #94A3B8` and a non-diffuse micro-shadow: `0 1px 3px rgba(15, 23, 42, 0.08), 0 1px 2px rgba(15, 23, 42, 0.04)`.
- **Layer 3 (Modal / Severe Diagnostic Focus):** `#FFFFFF` with `1px solid #64748B`, backed by an unblurred, low-opacity backdrop scrim: `rgba(15, 23, 42, 0.4)`.

### State Interaction Overlays
Active selections, cursor crosshairs, and focus rings never use blurs. They are communicated strictly via crisp, high-contrast outlines: `outline: 2px solid #2563EB; outline-offset: -1px;`.

## Components

### Buttons & Action Triggers
- **Height & Padding:** 24px (compact) or 28px (standard). Padding: 0 8px (compact), 0 12px (standard).
- **Primary Action:** Solid `#0F172A` background, white text, 4px radius. Hover: `#1E293B`. Active: `#334155`.
- **Secondary Action:** `#FFFFFF` background, `1px solid #CBD5E1` border, `#334155` text. Hover: `#F8FAFC` background with `#0F172A` border.
- **Destructive Action:** `#FFFFFF` background, `1px solid #E11D48` border, `#BE123C` text. Hover: `#FFE4E6`.
- **Ghost Action / Tool Bar Icon:** No border, transparent background, `#64748B` icon. Hover: `#F1F5F9` background, `#0F172A` icon.

### Status Indicators & Micro-Badges
- **Status Dot (Pip):** 6px × 6px circle with a subtle 1px ring overlay matching the container background to guarantee contrast across varying surfaces.
  - Healthy: `#16A34A`
  - Warning: `#D97706`
  - Danger: `#E11D48` (Pulsing animation enabled only during unacknowledged triage)
  - Muted: `#94A3B8`
- **Micro-Badge:** Height 18px. Font: `JetBrains Mono` 10px / line-height 12px. Internal padding: 1px 4px.
  - Warning badge: `#FEF3C7` background, `1px solid #FCD34D`, `#92400E` text.
  - Error badge: `#FFE4E6` background, `1px solid #FECDD3`, `#9F1239` text.

### Form Inputs & Query Terminals
- **Height:** 28px standard input height.
- **Styling:** `#FFFFFF` fill, `1px solid #CBD5E1` border, `#0F172A` text, 4px radius. Focus: `1px solid #2563EB` with `box-shadow: 0 0 0 1px #2563EB`.
- **Query Filter / PromQL / Lucene Bar:** Uses `JetBrains Mono` 12px, integrates syntax tokens directly (e.g., metric keys in blue, operators in slate, strings in emerald), with an embedded clear button and inline execution latency indicator.

### Data Tables & Log Streams
- **Density:** Row heights locked to 24px (dense) or 28px (default). Cell padding: 0 8px.
- **Dividers:** `1px solid #F1F5F9` row borders. Header row: sticky, `#F8FAFC` background, `1px solid #E2E8F0` bottom border, 11px uppercase Inter label with `#64748B` ink.
- **Row States:** Hover triggers `#F8FAFC`. Selected row highlights with `#EFF6FF` background and a 2px vertical accent bar on the left edge (`#2563EB`).

### Topology Graph Nodes
- **Geometry:** Rectangular cards (160px × 56px default) with a 4px radius and `1px solid #E2E8F0` border on `#FFFFFF` base.
- **Left Health Rail:** 3px wide vertical border on the node's left edge mapped to operational color (Green, Amber, Rose, Gray).
- **Node Content:** Two rows:
  - Row 1: Service slug (`JetBrains Mono` 11px bold, truncated) + status pip.
  - Row 2: Throughput (`req/s`) and P99 latency (`ms`) in tabular monospace font (`#64748B`).

### Checkboxes & Segmented Controls
- **Checkbox:** 14px × 14px square, 2px radius, `1px solid #CBD5E1`. Checked state: `#0F172A` fill with a crisp white check vector.
- **Segmented Time Range Switcher (15m, 1h, 24h, 7d):** Enclosed 24px height container in `#F1F5F9` with a 1px border. Active button: `#FFFFFF` fill, 2px radius, `1px solid #CBD5E1`, `#0F172A` bold text. Inactive button: transparent, `#64748B` text.