#!/usr/bin/env python3
"""Reproduce the approved Alrust 01 / Convergence vector asset system.

The two canonical mark paths and six custom outlined letters below are a clean
geometric reconstruction of the user-selected concept. This script does not
trace raster noise, embed an image, depend on a font, or fetch any resource.
It writes the final SVGs and a concise machine-readable brand specification.
Optional --render uses a local Inkscape installation for visual QA previews.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
RUST = "#C74327"
CHARCOAL = "#20211E"
IVORY = "#FAF9F5"

# Coordinates are in the canonical 384 × 294 mark coordinate system.
# The upper/lower routes are one silhouette; the middle input is detached.
MARK_OUTER = (
    "M6 0H114C166 0 187 25 213 65"
    "C232 94 249 114 301 114H377"
    "Q384 114 384 121V192Q384 199 377 199H290"
    "C246 199 226 218 204 245C180 278 159 294 109 294"
    "H6Q0 294 0 288V222Q0 215 6 215H101"
    "C139 215 158 205 181 182C198 165 212 156 228 151"
    "C207 144 190 133 173 113C151 85 133 74 101 74"
    "H6Q0 74 0 68V6Q0 0 6 0Z"
)
MARK_CENTER = (
    "M6 103H109C132 103 149 113 160 130"
    "C167 140 167 145 161 155C148 173 131 180 109 180"
    "H6Q0 180 0 174V109Q0 103 6 103Z"
)

# A purpose-drawn wordmark, kept as curves so it renders identically everywhere.
# This retains the reference's single-storey a, substantial strokes, round bowls,
# short t ascender and circular rust-orange punctuation.
LETTER_PATHS = {
    "a": (
        "M71 70C90 70 104 77 116 89V73H158V221H116V207"
        "C104 219 89 224 70 224C29 224 0 193 0 148"
        "C0 104 29 70 71 70Z"
        "M80 109C59 109 43 125 43 148C43 171 59 185 80 185"
        "C102 185 117 170 117 148C117 125 102 109 80 109Z"
    ),
    "l": "M183 0H226V221H183Z",
    "r": (
        "M255 73H298V94C308 78 321 70 339 70"
        "C344 70 348 71 352 72V113"
        "C346 111 340 111 335 111C311 111 298 125 298 149"
        "V221H255Z"
    ),
    "u": (
        "M369 73H412V156C412 174 422 185 440 185"
        "C457 185 467 174 467 156V73H509V157"
        "C509 199 482 224 440 224C396 224 369 198 369 157Z"
    ),
    "s": (
        "M588 70C612 70 631 77 644 90L622 118"
        "C611 109 599 104 587 104C576 104 571 109 571 115"
        "C571 123 581 127 598 132C630 142 647 152 647 177"
        "C647 207 622 224 588 224C561 224 540 215 523 201"
        "L545 173C558 185 574 190 587 190C598 190 604 186 604 179"
        "C604 171 594 167 577 162C546 152 529 142 529 117"
        "C529 89 553 70 588 70Z"
    ),
    "t": (
        "M665 25H708V73H747V112H708V163"
        "C708 179 716 186 731 186C737 186 742 185 747 184V221"
        "C740 223 732 224 724 224C684 224 665 202 665 162Z"
    ),
}


def paths(fill: str) -> str:
    return (
        f'<g fill="{fill}"><path d="{MARK_OUTER}"/>'
        f'<path d="{MARK_CENTER}"/></g>'
    )


def svg(title: str, viewbox: str, width: int, height: int, body: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{viewbox}" '
        f'width="{width}" height="{height}" role="img" '
        'aria-labelledby="title">\n'
        f'<title id="title">{title}</title>\n{body}\n</svg>\n'
    )


def write_mark(name: str, fill: str, title: str) -> None:
    (ROOT / name).write_text(svg(title, "0 0 384 294", 384, 294, paths(fill)))


def write_logo(name: str, mark_fill: str, type_fill: str, dot_fill: str, title: str) -> None:
    letters = "".join(f'<path d="{d}"/>' for d in LETTER_PATHS.values())
    body = (
        paths(mark_fill)
        + f'\n<g transform="translate(434 45)" fill="{type_fill}" fill-rule="evenodd">'
        + letters
        + '</g>\n'
        + f'<circle cx="1214" cy="245" r="26" fill="{dot_fill}"/>'
    )
    (ROOT / name).write_text(svg(title, "0 0 1240 294", 1240, 294, body))


def write_assets() -> None:
    write_mark("alrust-mark.svg", RUST, "Alrust — Convergence mark, rust orange")
    write_mark("alrust-mark-dark.svg", IVORY, "Alrust — Convergence mark, ivory")
    write_mark("alrust-mark-mono.svg", CHARCOAL, "Alrust — Convergence mark, charcoal")
    write_logo("alrust-logo-light.svg", RUST, CHARCOAL, RUST, "alrust — for light backgrounds")
    write_logo("alrust-logo-dark.svg", RUST, IVORY, RUST, "alrust — for dark backgrounds")
    write_logo("alrust-logo-mono.svg", CHARCOAL, CHARCOAL, CHARCOAL, "alrust — monochrome")
    favicon = (
        f'<rect width="64" height="64" rx="14" fill="{RUST}"/>'
        '<g transform="translate(8 13.625) scale(.125)">'
        + paths(IVORY) + '</g>'
    )
    (ROOT / "favicon.svg").write_text(svg("Alrust", "0 0 64 64", 64, 64, favicon))
    icon = (
        f'<rect width="512" height="512" rx="112" fill="{RUST}"/>'
        '<g transform="translate(64 109)">'
        + paths(IVORY) + '</g>'
    )
    (ROOT / "alrust-app-icon.svg").write_text(svg("Alrust application icon", "0 0 512 512", 512, 512, icon))

    spec = {
        "name": "alrust",
        "concept": "01 / Convergence",
        "version": "1.0",
        "date": "2026-10-01",
        "source": "User-selected 01 / Convergence concept image; manually reconstructed clean vector geometry.",
        "palette": {"rust": RUST, "charcoal": CHARCOAL, "ivory": IVORY},
        "canonical_mark_viewbox": [0, 0, 384, 294],
        "canonical_logo_viewbox": [0, 0, 1240, 294],
        "construction": {
            "mark": "Two filled paths: joined upper/lower routes and a detached rounded central input.",
            "wordmark": "Six custom outlined lowercase letterforms and a circular period; no font dependency.",
            "logo_wordmark_origin": [434, 45],
            "logo_gap_units": 50,
            "backgrounds": "Mark and logo variants have transparent backgrounds; favicon and app icon have opaque rust backgrounds."
        },
        "clear_space": {
            "minimum": "74 mark units on every side outside the asset bounding box (one incoming channel thickness).",
            "compact_ui_exception": "At 16–32 px, use the padded favicon/app icon composition supplied."
        },
        "minimum_sizes": {
            "standalone_mark_width_px": 24,
            "full_wordmark_width_px": 132,
            "favicon_px": 16,
            "recommended_header_wordmark_width_px": 156
        },
        "variants": {
            "alrust-mark.svg": "Rust mark on light backgrounds.",
            "alrust-mark-dark.svg": "Ivory mark on dark backgrounds.",
            "alrust-mark-mono.svg": "Charcoal mark on light backgrounds.",
            "alrust-logo-light.svg": "Rust mark and period, charcoal letters.",
            "alrust-logo-dark.svg": "Rust mark and period, ivory letters.",
            "alrust-logo-mono.svg": "All charcoal, including period.",
            "favicon.svg": "Ivory mark on a rust rounded square, optimized composition for 16–32 px.",
            "alrust-app-icon.svg": "512 px master with the same padded icon composition."
        },
        "usage": [
            "Keep all proportions, channels and the negative-space convergence point intact.",
            "Use the light wordmark on ivory/white and the dark wordmark on charcoal.",
            "Use one mark consistently across website, README, repository avatar and application assets.",
            "Do not add gradients, shadows, outlines or textures to the canonical logo.",
            "The period is part of the supplied primary wordmark; the product name remains alrust."
        ],
        "checks": [
            "All glyphs are paths, not SVG text.",
            "No embedded raster images, scripts, external resources, filters or fonts.",
            "All mark variants share the exact same two canonical paths."
        ],
        "project_note": "Brand assets do not imply a released or working alrust server."
    }
    (ROOT / "brand-spec.json").write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n")


def render() -> None:
    from PIL import Image, ImageDraw, ImageFont

    previews = ROOT / "previews"
    previews.mkdir(exist_ok=True)

    def png(name: str, width: int, destination: str | None = None) -> Path:
        output = previews / (destination or f"{Path(name).stem}-{width}.png")
        subprocess.run([
            "inkscape", str(ROOT / name), "--export-type=png",
            f"--export-width={width}", f"--export-filename={output}"
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return output

    for name in ["alrust-logo-light.svg", "alrust-logo-dark.svg", "alrust-logo-mono.svg"]:
        png(name, 1240)
    for size in [16, 24, 32]:
        png("favicon.svg", size, f"favicon-{size}.png")
        png("alrust-mark.svg", size, f"mark-{size}.png")
    png("alrust-app-icon.svg", 512)
    png("alrust-mark.svg", 384)
    png("alrust-mark-dark.svg", 384)
    png("alrust-mark-mono.svg", 384)

    sheet = Image.new("RGB", (1600, 1120), IVORY)
    draw = ImageDraw.Draw(sheet)
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    title = ImageFont.truetype(font_path, 25)
    label = ImageFont.truetype(font_path, 17)
    small = ImageFont.truetype(font_path, 14)
    draw.text((58, 37), "ALRUST  /  01 CONVERGENCE", fill=CHARCOAL, font=title)
    draw.text((58, 76), "Final vector system · 2026-10-01", fill="#62635D", font=label)
    light = Image.open(previews / "alrust-logo-light-1240.png").convert("RGBA")
    sheet.paste(light, (180, 132), light)
    draw.text((58, 448), "PRIMARY · IVORY BACKGROUND", fill="#62635D", font=small)
    draw.rectangle((0, 484, 1599, 821), fill=CHARCOAL)
    dark = Image.open(previews / "alrust-logo-dark-1240.png").convert("RGBA")
    dark.thumbnail((1054, 250), Image.Resampling.LANCZOS)
    sheet.paste(dark, (273, 524), dark)
    draw.text((58, 793), "PRIMARY · CHARCOAL BACKGROUND", fill=IVORY, font=small)

    draw.text((58, 850), "SMALL-SIZE ICONS", fill=CHARCOAL, font=label)
    x = 64
    for size in [16, 24, 32]:
        im = Image.open(previews / f"favicon-{size}.png").convert("RGBA")
        sheet.paste(im, (x, 891 + (32 - size)//2), im)
        draw.text((x - 1, 940), f"{size} px", fill=CHARCOAL, font=small)
        enlarged = im.resize((size * 3, size * 3), Image.Resampling.NEAREST)
        sheet.paste(enlarged, (x, 984), enlarged)
        x += 132
    draw.text((464, 850), "MONOCHROME", fill=CHARCOAL, font=label)
    mono = Image.open(previews / "alrust-logo-mono-1240.png").convert("RGBA")
    mono.thumbnail((600, 144), Image.Resampling.LANCZOS)
    sheet.paste(mono, (464, 910), mono)
    icon = Image.open(previews / "alrust-app-icon-512.png").convert("RGBA")
    icon.thumbnail((194, 194), Image.Resampling.LANCZOS)
    sheet.paste(icon, (1334, 870), icon)
    draw.text((1295, 1082), "APPLICATION / AVATAR", fill=CHARCOAL, font=small)
    sheet.save(previews / "alrust-brand-contact-sheet.png")


def validate() -> None:
    for path in ROOT.glob("*.svg"):
        root = ET.fromstring(path.read_text())
        tags = {node.tag.rsplit("}", 1)[-1] for node in root.iter()}
        assert not tags & {"image", "script", "foreignObject", "text", "style"}, (path, tags)
        assert "href=" not in path.read_text(), path
        assert root.attrib["viewBox"], path
    print("8 SVG assets written; XML and path-only checks passed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render", action="store_true", help="Generate QA PNGs with Inkscape and Pillow.")
    args = parser.parse_args()
    write_assets()
    validate()
    if args.render:
        render()
        print("Visual QA previews written to previews/.")
