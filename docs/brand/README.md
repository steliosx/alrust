# Alrust Convergence identity

The owner selected **01 / Convergence** on 1 October 2026. This is the
canonical identity for the project website, repository and future application.
Three incoming channels converge toward one output, reflecting the planned
alert aggregation workflow.

## Assets

| Asset | Use |
|---|---|
| `alrust-logo-light.svg` | Primary logo on ivory or white |
| `alrust-logo-dark.svg` | Primary logo on charcoal or another dark surface |
| `alrust-logo-mono.svg` | Single-color reproduction on a light surface |
| `alrust-mark.svg` | Rust-orange standalone symbol |
| `alrust-mark-dark.svg` | Ivory standalone symbol |
| `alrust-mark-mono.svg` | Charcoal standalone symbol |
| `favicon.svg` | Padded small icon; ivory mark on rust |
| `alrust-app-icon.svg` | 512 px application/avatar master |

The SVGs use explicit vector paths, including the lettering. They contain no
embedded bitmap, font dependency, script or external resource. `brand-spec.json`
records their palette, geometry and intended sizes. `generate_brand.py`
reproduces the assets using the Python standard library; optional preview
rendering uses a local Inkscape installation.

## Palette and placement

| Color | Hex |
|---|---|
| Rust | `#C74327` |
| Charcoal | `#20211E` |
| Ivory | `#FAF9F5` |

Keep the symbol proportions and its internal gaps intact. Use the supplied
small-icon composition at 16–32 px. The full logo should be at least 132 px
wide; 156–176 px is suitable for a website header. Reserve approximately one
incoming-channel thickness as clear space around a standalone logo.

The orange period is part of the primary wordmark; the product name remains
`alrust`. Canonical logo colors are fixed brand colors, not live alert severity
or health indicators.

## README illustrations

The assets in `../assets/` include a generated editorial hero derived from the
selected concept, a precise architecture SVG and a grouped roadmap SVG. The
hero is a brand illustration. The diagrams describe the planned system and
must not be used as evidence of implemented behavior, a passed phase gate or
a release. The README retains the detailed roadmap and editable Mermaid source.

All future illustrations should use this identity. Technical diagrams should
be edited as vectors so their component names and connections remain exact.

No project or trademark license is granted by this brand selection. The
repository's existing licensing section remains authoritative.
