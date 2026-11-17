"""Animated GIF of the single-client results in docs/performance/2026-11-15-bench-compare.md.

A replay of measured median rates, not a recording: each bar grows at the
requests per second that run 43792492613 measured with one client, over a
simulated five seconds. Every number comes from
docs/performance/2026-11-15-bench-compare-results.json.

Standard library only; needs ImageMagick (`magick`, with an SVG renderer)
and `ffmpeg` on PATH. Usage, from the repository root:
    python3 -I scripts/bench/make_race_gif.py docs/performance/assets/bench-race.gif [preview-dir]
"""

import json
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "docs/performance/2026-11-15-bench-compare-results.json"
RUN = "43792492613"
W, H = 880, 480
FPS = 12
RACE_S = 5.0
INTRO_FRAMES, HOLD_FRAMES = 18, 42
ARMS = [("alrust", "alrust", "#f97316"),
        ("alerta-1x4", "Alerta, 1 worker", "#60a5fa"),
        ("alerta-4x4-pgb", "Alerta, pooler + 4 workers", "#3b82f6")]
CASES = [("list", "List the 50 newest alerts"),
         ("count", "Count alerts by severity and status"),
         ("write-dup", "Receive a duplicate alert")]
FONT = "Liberation Sans, DejaVu Sans, sans-serif"


def medians():
    cells = json.loads(RESULTS.read_text())["cells"]
    out = {}
    for case, _ in CASES:
        for arm, _, _ in ARMS:
            xs = [c for c in cells if c["case"] == case and c["arm"] == arm and c["c"] == 1 and c["valid"]]
            out[(case, arm)] = (statistics.median(c["rps"] for c in xs),
                                statistics.median(c["p50_ms"] for c in xs))
    return out


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def text(x, y, s, size, weight="normal", fill="#e5e7eb", anchor="start"):
    return (f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" '
            f'font-weight="{weight}" fill="{fill}" text-anchor="{anchor}">{esc(s)}</text>')


def frame(m, t, final):
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}">',
             f'<rect width="{W}" height="{H}" fill="#111827"/>',
             text(24, 36, "alrust and Alerta 9.1.0: requests answered by one client", 22, "bold"),
             text(24, 60, f"Same 4-vCPU GitHub runner · PostgreSQL 18.7 · 1,000 alerts · "
                          f"t = {t:4.1f} s of {RACE_S:.0f} s", 14, fill="#9ca3af")]
    x_label, x_bar, bar_w = 24, 250, 380
    for i, (case, title) in enumerate(CASES):
        y0 = 92 + i * 122
        parts.append(text(x_label, y0 + 14, title, 16, "bold"))
        top = max(m[(case, arm)][0] for arm, _, _ in ARMS) * RACE_S
        for j, (arm, label, color) in enumerate(ARMS):
            rps, p50 = m[(case, arm)]
            done = rps * t
            y = y0 + 26 + j * 28
            width = max(2.0, bar_w * done / top)
            parts.append(text(x_label, y + 15, label, 14, fill="#d1d5db"))
            parts.append(f'<rect x="{x_bar}" y="{y}" width="{bar_w}" height="20" rx="3" fill="#1f2937"/>')
            parts.append(f'<rect x="{x_bar}" y="{y}" width="{width:.1f}" height="20" rx="3" fill="{color}"/>')
            note = (f"{rps:,.0f} req/s · {p50:.1f} ms each" if final else f"{done:,.0f} answered")
            parts.append(text(x_bar + bar_w + 12, y + 15, note, 14,
                              "bold" if arm == "alrust" else "normal"))
    parts.append(text(24, H - 16, f"Replay of the median single-client rates measured in run {RUN}; "
                                  "not a live recording.", 12, fill="#9ca3af"))
    parts.append("</svg>")
    return "\n".join(parts)


def main():
    out = Path(sys.argv[1]).resolve()
    for tool in ("magick", "ffmpeg"):
        if not shutil.which(tool):
            sys.exit(f"{tool} not found")
    m = medians()
    race_frames = int(RACE_S * FPS)
    frames = ([frame(m, 0.0, False)] * INTRO_FRAMES
              + [frame(m, RACE_S * (k + 1) / race_frames, False) for k in range(race_frames)]
              + [frame(m, RACE_S, True)] * HOLD_FRAMES)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for k, svg in enumerate(frames):
            (tmp / f"f{k:03d}.svg").write_text(svg)
            subprocess.run(["magick", str(tmp / f"f{k:03d}.svg"), str(tmp / f"f{k:03d}.png")], check=True)
        pal = tmp / "palette.png"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-framerate", str(FPS), "-i", str(tmp / "f%03d.png"),
                        "-vf", "palettegen=max_colors=64:stats_mode=full", str(pal)], check=True)
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-framerate", str(FPS), "-i", str(tmp / "f%03d.png"),
                        "-i", str(pal), "-lavfi", "paletteuse=dither=none", "-loop", "0", str(out)], check=True)
        if len(sys.argv) > 2:  # a directory for two preview frames, for checking by eye
            previews = Path(sys.argv[2])
            previews.mkdir(parents=True, exist_ok=True)
            shutil.copy(tmp / f"f{INTRO_FRAMES + race_frames // 2:03d}.png", previews / "mid.png")
            shutil.copy(tmp / f"f{len(frames) - 1:03d}.png", previews / "final.png")
    print(f"{out}: {out.stat().st_size} bytes, {len(frames)} frames at {FPS} fps")


if __name__ == "__main__":
    main()
