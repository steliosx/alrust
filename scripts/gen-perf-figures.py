#!/usr/bin/env python3
"""Draw the charts of docs/performance/README.md as SVG files.

Every value drawn is a number quoted in one of the reports in docs/performance/, written here as
the exact text the report shows. `--check` proves that: each quoted text must occur in its report,
and the committed SVG files must equal what this script writes now. The script uses only the
Python standard library and writes the same bytes on every run.

Each chart is written twice, for a light page and for a dark page:
docs/performance/assets/<name>-light.svg and <name>-dark.svg. The README shows the right one with
a <picture> element.

    python3 scripts/gen-perf-figures.py           # write the SVG files
    python3 scripts/gen-perf-figures.py --check   # quotes and files, exit 1 on a difference
    python3 scripts/gen-perf-figures.py --self-test
"""

import argparse
import html
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PERF = os.path.join(ROOT, "docs", "performance")
ASSETS = os.path.join(PERF, "assets")

# Colour tokens per page theme (categorical slots 1 and 2, validated for both surfaces).
THEMES = {
    "light": {
        "surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781",
        "grid": "#e1e0d9", "axis": "#c3c2b7", "s1": "#2a78d6", "s2": "#eb6834",
        "base": "#a9a8a2",
    },
    "dark": {
        "surface": "#1a1a19", "ink": "#f0efec", "ink2": "#c3c2b7", "muted": "#898781",
        "grid": "#2c2c2a", "axis": "#383835", "s1": "#3987e5", "s2": "#d95926",
        "base": "#6b6a65",
    },
}
FONT = 'system-ui, -apple-system, &quot;Segoe UI&quot;, sans-serif'

# A bar is (series, low, high, label). `low` and `high` are quoted texts from the report; a
# single value has high None. Series "base" is the reference arm of a chart.
FIGURES = [
    {
        "name": "two-site-placement",
        "report": "2026-11-22-two-site-placement.md",
        "title": "Site B waits far less when its requests go to the primary's site",
        "subtitle": ("Alerta at two sites, 50 ms emulated link, site A primary. Site B median per "
                     "request; each bar spans the medians of three blocks."),
        "unit": "ms",
        "axis": "Site B median time per request (ms), lower is better",
        "ticks": [0, 250, 500, 750, 1000],
        "series": [("s2", "L: site B's own Alerta, SQL crosses the link"),
                   ("s1", "P: HAProxy sends the request to the primary's site")],
        "panels": [{
            "groups": [
                ("Alert list", [("s2", "459.3", "464.0", "459–464 ms"),
                                ("s1", "103.1", "104.4", "103–104 ms")]),
                ("Alert count", [("s2", "409.8", "413.7", "410–414 ms"),
                                 ("s1", "56.1", "56.5", "56 ms")]),
                ("Duplicate alert (write)", [("s2", "971.1", "976.9", "971–977 ms"),
                                             ("s1", "61.3", "62.8", "61–63 ms")]),
            ],
        }],
        "source": ("Run 45037764779, one GitHub-hosted runner. Report: "
                   "docs/performance/2026-11-22-two-site-placement.md"),
    },
    {
        "name": "duplicate-writes",
        "report": "2026-11-21-fastpath-bench.md",
        "title": "Two of the three row updates of a duplicate alert are an echo; leave them out",
        "subtitle": ("Alerta 9.1.0, one site, 1,000 alerts, 4 workers of 4 threads. Duplicate "
                     "writes per second, median of three repetitions."),
        "unit": "/s",
        "axis": "Duplicate writes per second, higher is better",
        "ticks": [0, 250, 500, 750, 1000, 1250],
        "series": [],
        "panels": [
            {"heading": "8 clients", "groups": [
                ("Default plugins (baseline)", [("base", "737.7", None, "738/s")]),
                ("forwarder removed", [("s1", "1047.6", None, "1048/s, +39 to +45%")]),
                ("+ noecho", [("s1", "930.9", None, "931/s, +21 to +31%")]),
                ("+ noecho and hot", [("s1", "1108.4", None, "1108/s, +26 to +51%")]),
                ("+ id prefix index", [("s1", "731.0", None, "731/s, -3 to 0%")]),
            ]},
            {"heading": "1 client", "groups": [
                ("Default plugins (baseline)", [("base", "309.3", None, "309/s")]),
                ("forwarder removed", [("s1", "393.7", None, "394/s, +27 to +31%")]),
                ("+ noecho", [("s1", "341.4", None, "341/s, +10 to +13%")]),
                ("+ noecho and hot", [("s1", "415.1", None, "415/s, +27 to +37%")]),
                ("+ id prefix index", [("s1", "319.3", None, "319/s, +3 to +5%")]),
            ]},
        ],
        "quotes": ["+44.9 %", "+38.8 %", "+21.4 %", "+30.5 %", "+25.5 %", "+50.6 %",
                   "-3.1 %", "+0.4 %", "+26.9 %", "+30.6 %", "+10.1 %", "+12.9 %",
                   "+26.5 %", "+37.2 %", "+3.0 %", "+4.6 %"],
        "source": ("Run 44934944933, one GitHub-hosted runner; % is the change against the "
                   "baseline in each repetition. Report: "
                   "docs/performance/2026-11-21-fastpath-bench.md"),
    },
    {
        "name": "savedq-cpu",
        "report": "2026-11-22-savedq-load.md",
        "title": "Repeated searches cost much less CPU with savedq; a new one costs a little more",
        "subtitle": ("Alerta 9.1.0 under gunicorn, 40 requests a second. Server CPU per request, "
                     "median of four repetitions."),
        "unit": "ms",
        "axis": "Server CPU per request (ms), lower is better",
        "ticks": [0, 3, 6, 9, 12],
        "series": [("base", "safe (no savedq)"), ("s1", "safe,savedq")],
        "panels": [{
            "groups": [
                ("One search, again and again", [("base", "10.29", None, "10.29 ms"),
                                                 ("s1", "2.76", None, "2.76 ms, -73.2%")]),
                ("Sixteen searches in turn", [("base", "3.84", None, "3.84 ms"),
                                              ("s1", "2.09", None, "2.09 ms, -46.9%")]),
                ("A new search every time", [("base", "11.16", None, "11.16 ms"),
                                             ("s1", "11.41", None, "11.41 ms, +2.2%")]),
                ("No search (control)", [("base", "3.33", None, "3.33 ms"),
                                         ("s1", "3.24", None, "3.24 ms, -3.4%")]),
            ],
        }],
        "quotes": ["-73.2%", "-46.9%", "+2.2%", "-3.4%"],
        "source": ("Run 45054378079, one GitHub-hosted runner; % is the median change in the "
                   "same repetition. Report: docs/performance/2026-11-22-savedq-load.md"),
    },
]


def quotes(fig):
    """Every text of `fig` that must occur in its report."""
    out = list(fig.get("quotes", []))
    for panel in fig["panels"]:
        for _, bars in panel["groups"]:
            for _, low, high, _ in bars:
                out.append(low)
                if high is not None:
                    out.append(high)
    return out


def missing_quotes(fig, text):
    return [q for q in quotes(fig) if q not in text]


def esc(s):
    return html.escape(s, quote=True)


def n(x):
    """A coordinate with one decimal and no trailing zero, so output is stable."""
    s = f"{x:.1f}"
    return s[:-2] if s.endswith(".0") else s


def bar_path(x, y, w, h, r=4.0):
    """A bar from the baseline at x: square at the baseline, rounded at the data end."""
    if w <= r:
        return f"M{n(x)} {n(y)}h{n(w)}v{n(h)}h{n(-w)}z"
    return (f"M{n(x)} {n(y)}h{n(w - r)}a{n(r)} {n(r)} 0 0 1 {n(r)} {n(r)}v{n(h - 2 * r)}"
            f"a{n(r)} {n(r)} 0 0 1 {n(-r)} {n(r)}h{n(-(w - r))}z")


def wrap(text, width):
    """Split `text` into lines of at most `width` characters, at spaces."""
    lines, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}" if line else word
    if line:
        lines.append(line)
    return lines


def render(fig, theme):
    t = THEMES[theme]
    width = 860
    pad = 24
    label_w = 220
    x0 = pad + label_w
    x1 = width - pad - 220
    top_ticks = fig["ticks"]
    vmax = top_ticks[-1]

    def sx(v):
        return x0 + (x1 - x0) * v / vmax

    out = []
    y = pad
    title_lines = wrap(fig["title"], 70)
    for line in title_lines:
        y += 22
        out.append(f'<text x="{pad}" y="{y}" font-size="18" font-weight="600" '
                   f'fill="{t["ink"]}">{esc(line)}</text>')
    for line in wrap(fig["subtitle"], 105):
        y += 19
        out.append(f'<text x="{pad}" y="{y}" font-size="13" fill="{t["ink2"]}">{esc(line)}</text>')
    if fig["series"]:
        y += 26
        lx = pad
        for key, name in fig["series"]:
            out.append(f'<rect x="{lx}" y="{y - 10}" width="12" height="12" rx="3" '
                       f'fill="{t[key]}"/>')
            out.append(f'<text x="{lx + 18}" y="{y}" font-size="13" fill="{t["ink2"]}">'
                       f'{esc(name)}</text>')
            lx += 18 + int(len(name) * 7.0) + 28
    bar_h, gap, group_gap = 20, 2, 16
    for panel in fig["panels"]:
        y += 22
        if "heading" in panel:
            y += 6
            out.append(f'<text x="{pad}" y="{y}" font-size="14" font-weight="600" '
                       f'fill="{t["ink"]}">{esc(panel["heading"])}</text>')
            y += 10
        plot_top = y
        rows = []
        for label, bars in panel["groups"]:
            gy = y
            for key, low, high, text in bars:
                lo = float(low)
                hi = float(high) if high is not None else None
                rows.append((key, y, lo, hi, text))
                y += bar_h + gap
            y -= gap
            mid = (gy + y) / 2
            for i, line in enumerate(wrap(label, 30)):
                out.append(f'<text x="{x0 - 12}" y="{n(mid + 4 + 16 * i)}" font-size="13" '
                           f'text-anchor="end" fill="{t["ink"]}">{esc(line)}</text>')
            y += group_gap
        plot_bottom = y - group_gap + 6
        grid = []
        for v in top_ticks:
            gx = sx(v)
            color = t["axis"] if v == 0 else t["grid"]
            grid.append(f'<line x1="{n(gx)}" y1="{n(plot_top - 6)}" x2="{n(gx)}" '
                        f'y2="{n(plot_bottom)}" stroke="{color}" stroke-width="1"/>')
            grid.append(f'<text x="{n(gx)}" y="{n(plot_bottom + 16)}" font-size="11" '
                        f'text-anchor="middle" fill="{t["muted"]}">{v:,}</text>')
        out.extend(grid)
        for key, by, lo, hi, text in rows:
            w = sx(lo) - x0
            out.append(f'<path d="{bar_path(x0, by, w, bar_h)}" fill="{t[key]}"/>')
            end = sx(lo)
            if hi is not None and hi > lo:
                hx = sx(hi)
                cy = by + bar_h / 2
                out.append(f'<line x1="{n(end)}" y1="{n(cy)}" x2="{n(hx)}" y2="{n(cy)}" '
                           f'stroke="{t["ink2"]}" stroke-width="2"/>')
                out.append(f'<line x1="{n(hx)}" y1="{n(by + 3)}" x2="{n(hx)}" '
                           f'y2="{n(by + bar_h - 3)}" stroke="{t["ink2"]}" stroke-width="2"/>')
                end = hx
            out.append(f'<text x="{n(end + 8)}" y="{n(by + bar_h / 2 + 4)}" font-size="12" '
                       f'fill="{t["ink2"]}">{esc(text)}</text>')
        y = plot_bottom + 16
    y += 22
    out.append(f'<text x="{n((x0 + x1) / 2)}" y="{y}" font-size="12" text-anchor="middle" '
               f'fill="{t["ink2"]}">{esc(fig["axis"])}</text>')
    for line in wrap(fig["source"], 120):
        y += 18
        out.append(f'<text x="{pad}" y="{y}" font-size="11" fill="{t["muted"]}">'
                   f'{esc(line)}</text>')
    height = y + pad
    head = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" font-family="{FONT}" role="img" '
            f'aria-label="{esc(fig["title"])}">\n'
            f'<title>{esc(fig["title"])}</title>\n'
            f'<rect width="{width}" height="{height}" rx="8" fill="{t["surface"]}"/>\n')
    return head + "\n".join(out) + "\n</svg>\n"


def outputs():
    """Each (file name, SVG text) that the script writes."""
    for fig in FIGURES:
        for theme in ("light", "dark"):
            yield f'{fig["name"]}-{theme}.svg', render(fig, theme)


def check():
    problems = []
    for fig in FIGURES:
        with open(os.path.join(PERF, fig["report"]), encoding="utf-8") as fh:
            text = fh.read()
        for q in missing_quotes(fig, text):
            problems.append(f'{fig["name"]}: "{q}" is not in {fig["report"]}')
    for name, svg in outputs():
        path = os.path.join(ASSETS, name)
        try:
            with open(path, encoding="utf-8") as fh:
                have = fh.read()
        except FileNotFoundError:
            have = None
        if have != svg:
            problems.append(f"{name}: differs from this script's output (run it again)")
    for p in problems:
        print(p, file=sys.stderr)
    return 1 if problems else 0


def self_test():
    fig = {"panels": [{"groups": [("g", [("s1", "1.5", "2.5", "x"), ("s2", "3", None, "y")])]}],
           "quotes": ["+1 %"]}
    assert quotes(fig) == ["+1 %", "1.5", "2.5", "3"]
    assert missing_quotes(fig, "a 1.5 b 2.5 c 3 d +1 %") == []
    assert missing_quotes(fig, "1.5 2.5 +1 %") == ["3"]
    assert n(2.0) == "2" and n(2.5) == "2.5"
    assert bar_path(0, 0, 2, 10) == "M0 0h2v10h-2z"
    assert bar_path(0, 0, 10, 20).startswith("M0 0h6a4 4 0 0 1 4 4v12")
    assert wrap("aa bb cc", 5) == ["aa bb", "cc"]
    first = dict(outputs())
    assert first == dict(outputs()), "output is not deterministic"
    assert len(first) == 2 * len(FIGURES)
    for name, svg in first.items():
        assert svg.startswith("<svg ") and svg.endswith("</svg>\n"), name
        assert "<script" not in svg and "href" not in svg, name
    with tempfile.TemporaryDirectory() as tmp:
        for name, svg in first.items():
            with open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
                fh.write(svg)
    print("self-test passed")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if args.check:
        return check()
    os.makedirs(ASSETS, exist_ok=True)
    for name, svg in outputs():
        with open(os.path.join(ASSETS, name), "w", encoding="utf-8") as fh:
            fh.write(svg)
        print(os.path.relpath(os.path.join(ASSETS, name), ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
