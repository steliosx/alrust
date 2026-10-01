from pathlib import Path
from html import escape
import subprocess
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent
IVORY = '#FAF9F5'
CHARCOAL = '#20211E'
RUST = '#C74327'
BORDER = '#DDDCD4'
# Exact geometry from the approved 01 / Convergence canonical mark.
MARK_PATHS = [
    'M6 0H114C166 0 187 25 213 65C232 94 249 114 301 114H377Q384 114 384 121V192Q384 199 377 199H290C246 199 226 218 204 245C180 278 159 294 109 294H6Q0 294 0 288V222Q0 215 6 215H101C139 215 158 205 181 182C198 165 212 156 228 151C207 144 190 133 173 113C151 85 133 74 101 74H6Q0 74 0 68V6Q0 0 6 0Z',
    'M6 103H109C132 103 149 113 160 130C167 140 167 145 161 155C148 173 131 180 109 180H6Q0 180 0 174V109Q0 103 6 103Z',
]


def begin(height, title, description):
    return [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="{height}" viewBox="0 0 1200 {height}" role="img" aria-labelledby="diagram-title diagram-desc">',
        f'<title id="diagram-title">{escape(title)}</title>',
        f'<desc id="diagram-desc">{escape(description)}</desc>',
        '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse" markerUnits="userSpaceOnUse"><path d="M0 0L10 5L0 10Z" fill="#20211E"/></marker></defs>',
        f'<rect width="1200" height="{height}" fill="{IVORY}"/>',
        f'<g font-family="Arial, Helvetica, sans-serif" fill="{CHARCOAL}">',
    ]


def text(parts, x, y, value, size=24, weight=400, fill=CHARCOAL, spacing=None):
    extra = f' letter-spacing="{spacing}"' if spacing is not None else ''
    parts.append(f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" fill="{fill}"{extra}>{escape(value)}</text>')


def rect(parts, x, y, w, h, stroke=BORDER, width=1.5, fill=IVORY, radius=8):
    parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{fill}" stroke="{stroke}" stroke-width="{width}"/>')


def line(parts, path, arrow=True, width=2.4, color=CHARCOAL):
    marker = ' marker-end="url(#arrow)"' if arrow else ''
    parts.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="{width}" stroke-linejoin="round" stroke-linecap="round"{marker}/>')


def brand(parts):
    # Embed the canonical outlined logo, so diagrams use the same identity.
    logo = ET.parse(ROOT.parent / 'brand' / 'alrust-logo-light.svg').getroot()
    parts.append('<g transform="translate(48 36) scale(0.145)" aria-hidden="true">')
    for child in logo:
        if child.tag.rsplit('}', 1)[-1] not in {'title', 'desc', 'metadata'}:
            parts.append(ET.tostring(child, encoding='unicode'))
    parts.append('</g>')


def save(parts, name):
    parts.extend(['</g>', '</svg>'])
    path = ROOT / name
    path.write_text('\n'.join(parts) + '\n', encoding='utf-8')
    return path


architecture = begin(
    1474,
    'Alrust planned architecture — no working alrust runtime yet',
    'Planned architecture for an internal NOC. Zabbix version 7.0.25 or newer uses a native webhook. Veeam ONE and other sources use a generic webhook mapper. Both feed alrust-receivers, which feeds alrust-api. The API uses axum and tokio and owns orchestration and I/O. It separately calls alrust-engine, a pure Rust library for deduplication, correlation and lifecycle with no I/O, and reads and writes alrust-store-pg, using SQLx and PostgreSQL only. There is no engine-to-store edge. The vendored Alerta web UI calls the API using the consumed Alerta HTTP contract. Tauri on Windows and macOS wraps the same UI and receives API SSE for native notifications. The API also sends outbound webhooks to notification targets. Customer grouping and routing are not a tenant security boundary. All components and flows shown are planned.',
)
brand(architecture)
text(architecture, 48, 154, 'Planned architecture', 52, 700)
text(architecture, 48, 201, 'No working alrust runtime yet', 28, 600, RUST)
text(architecture, 48, 251, 'Internal NOC only', 24, 600)
text(architecture, 48, 283, 'Customer grouping / routing, not a tenant security boundary.', 24)
line(architecture, 'M48 312H1152', False, 1.5, BORDER)

# Every dependency below starts at its owner. The engine and store have no connecting edge.
line(architecture, 'M308 435H348')
line(architecture, 'M308 635H348')
line(architecture, 'M590 546H630')
line(architecture, 'M874 455H914')
line(architecture, 'M874 647H914')
# Consumed HTTP contract: UI -> API.
line(architecture, 'M428 970V874H692V682')
# API SSE: API -> Tauri wrapper.
line(architecture, 'M785 682V970')
# Outbound delivery: a separate API path outside all client boxes.
line(architecture, 'M846 682V808H1174V1345H1152')
# The desktop wrapper uses the same vendored interface.
line(architecture, 'M658 1075H598')

# Sources.
rect(architecture, 48, 365, 260, 138)
text(architecture, 70, 407, 'Zabbix ≥7.0.25', 26, 700)
text(architecture, 70, 453, 'Native webhook', 23)
rect(architecture, 48, 541, 260, 188)
text(architecture, 70, 583, 'Veeam ONE', 26, 700)
text(architecture, 70, 616, '+ other sources', 23)
text(architecture, 70, 667, 'Generic webhook', 22)
text(architecture, 70, 697, 'mapper', 22)

# Intake and orchestration.
rect(architecture, 348, 410, 242, 272)
text(architecture, 370, 452, 'RECEIVE', 20, 700, spacing=1.4)
text(architecture, 370, 503, 'alrust-receivers', 24, 700)
text(architecture, 370, 563, 'Native + mapped', 22)
text(architecture, 370, 595, 'webhooks', 22)
rect(architecture, 630, 410, 244, 272, stroke=RUST, width=2.4)
text(architecture, 652, 452, 'ORCHESTRATE', 20, 700, fill=RUST, spacing=0.5)
text(architecture, 652, 503, 'alrust-api', 28, 700)
text(architecture, 652, 553, 'axum / tokio', 24)
text(architecture, 652, 592, 'Orchestration', 23)
text(architecture, 652, 625, 'I/O ownership', 22)

# Two separate API dependencies; labels explicitly express the direction.
rect(architecture, 914, 341, 238, 223)
text(architecture, 936, 381, 'API CALLS', 20, 700, spacing=1.2)
text(architecture, 936, 424, 'alrust-engine', 25, 700)
text(architecture, 936, 461, 'Pure Rust library', 22)
text(architecture, 936, 502, 'Dedup / correlation', 21)
text(architecture, 936, 533, 'Lifecycle · NO I/O', 21, 600)
rect(architecture, 914, 596, 238, 174)
text(architecture, 936, 634, 'READS / WRITES', 20, 700, spacing=0.3)
text(architecture, 936, 675, 'alrust-store-pg', 24, 700)
text(architecture, 936, 713, 'SQLx', 23)
text(architecture, 936, 746, 'PostgreSQL only', 22)

# Client and delivery connector labels sit clear of all connecting paths.
text(architecture, 48, 841, 'Calls the consumed', 23)
text(architecture, 48, 873, 'Alerta HTTP contract', 23, 600)
text(architecture, 805, 900, 'SSE', 24, 700)
text(architecture, 805, 932, 'notifications', 22)
text(architecture, 917, 849, 'Outbound webhooks', 22)

rect(architecture, 48, 970, 550, 214)
text(architecture, 72, 1010, 'WEB INTERFACE', 20, 700, spacing=1.2)
text(architecture, 72, 1056, 'Alerta web UI', 31, 700)
text(architecture, 72, 1102, 'Vendored', 24)
text(architecture, 72, 1143, 'Uses the consumed Alerta HTTP contract', 22)
rect(architecture, 658, 970, 494, 214)
text(architecture, 682, 1010, 'DESKTOP WRAPPER', 20, 700, spacing=1.2)
text(architecture, 682, 1056, 'Tauri · Windows / macOS', 29, 700)
text(architecture, 682, 1102, 'Wraps the same UI', 24)
text(architecture, 682, 1143, 'Native notifications from API SSE', 22)

text(architecture, 48, 1314, 'I/O stays in the API', 26, 700)
text(architecture, 48, 1358, 'The engine is a pure Rust library.', 23)
text(architecture, 48, 1392, 'The API calls it and accesses storage.', 23)
rect(architecture, 658, 1274, 494, 142)
text(architecture, 682, 1328, 'Notification targets', 29, 700)
text(architecture, 682, 1375, 'Receive outbound webhooks', 23)
line(architecture, 'M48 1452H1152', False, 1.5, BORDER)
architecture_path = save(architecture, 'architecture-planned.svg')

roadmap = begin(
    890,
    'Alrust planned roadmap — Phase 0 is current',
    'Supplementary grouping of the exact per-phase roadmap in the README. Phase 0, Specify and prove, is current: source-derived contracts; the reference conformance CI gate is still open. Phases 1 and 2, Build and compare, are planned: the pure engine and PostgreSQL first, then HTTP and SSE with differential parity. Phases 3 through 5, Connect and qualify, are planned: authentication, receivers, operations and a shadow run. Phase 6, Desktop, is planned: a Windows and macOS wrapper with native notifications. No working alrust runtime exists yet. No completed gates or phases are implied.',
)
brand(roadmap)
text(roadmap, 48, 154, 'Delivery roadmap', 52, 700)
text(roadmap, 48, 203, 'Phase 0 is current. The remaining groups are planned.', 26)
line(roadmap, 'M48 239H1152', False, 1.5, BORDER)

stages = [
    dict(x=48, phase='PHASE 0', current=True, title=['Specify', '& prove'],
         body=[(534, 'Source-derived'), (566, 'contracts'), (628, 'Reference'), (660, 'conformance CI'), (692, 'gate still open.')]),
    dict(x=332, phase='PHASES 1–2', current=False, title=['Build &', 'compare'],
         body=[(534, 'Pure engine +'), (566, 'PostgreSQL'), (628, 'Then HTTP / SSE'), (660, '+ differential'), (692, 'parity')]),
    dict(x=616, phase='PHASES 3–5', current=False, title=['Connect &', 'qualify'],
         body=[(534, 'Auth + receivers'), (566, 'Operations'), (628, 'Shadow run')]),
    dict(x=900, phase='PHASE 6', current=False, title=['Desktop'],
         body=[(534, 'Windows / macOS'), (566, 'wrapper'), (628, 'Native'), (660, 'notifications')]),
]
for stage in stages:
    x = stage['x']
    rect(roadmap, x, 275, 252, 480, stroke=RUST if stage['current'] else BORDER, width=2.5 if stage['current'] else 1.5)
    text(roadmap, x+22, 318, stage['phase'], 22, 700, spacing=0.5)
    if stage['current']:
        rect(roadmap, x+22, 340, 128, 34, stroke=RUST, width=0, fill=RUST, radius=4)
        text(roadmap, x+34, 364, 'CURRENT', 20, 700, IVORY, spacing=0.5)
    else:
        text(roadmap, x+22, 364, 'PLANNED', 20, 600, spacing=0.5)
    for i, title in enumerate(stage['title']):
        text(roadmap, x+22, 430 + 39*i, title, 32, 700)
    for y, body in stage['body']:
        text(roadmap, x+22, y, body, 22)

text(roadmap, 48, 807, 'Visual grouping only. The README retains the exact per-phase table.', 24)
text(roadmap, 48, 854, 'No working alrust runtime yet.', 25, 600, RUST)
roadmap_path = save(roadmap, 'roadmap-planned.svg')

# Previews reflect the maximum intended README display width.
for svg in [architecture_path, roadmap_path]:
    output = svg.with_name(svg.stem + '-850.png')
    subprocess.run([
        'inkscape', str(svg.relative_to(ROOT)),
        '--export-type=png', '--export-width=850',
        '--export-filename=' + str(output.relative_to(ROOT)),
    ], cwd=ROOT, check=True)
    print(svg.name, '->', output.name)
