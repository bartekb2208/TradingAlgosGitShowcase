"""make_banner.py — regenerate docs/banner.svg.

Decorative only: the faint curves are seeded random walks, not backtest data.
Most drift down and end below their start; three are drawn in colour and end
above it. That is the ratio the README's ledger reports, nothing more precise.

    python tools/make_banner.py
"""
from __future__ import annotations

import random
from pathlib import Path

W, H = 1280, 340
X0, X1 = 770, 1240          # curve field on the right, clear of the text block
Y_START = 205
OUT = Path(__file__).resolve().parent.parent / "docs" / "banner.svg"
SURVIVOR_COLOURS = ["#3987e5", "#d95926", "#199e70"]


def walk(rng: random.Random, drift: float, n: int = 90) -> list[tuple[float, float]]:
    y, pts = Y_START, []
    for i in range(n):
        x = X0 + (X1 - X0) * i / (n - 1)
        pts.append((x, y))
        y += rng.gauss(drift, 5.2)
        y = min(max(y, 60), 315)
    return pts


def path(pts: list[tuple[float, float]]) -> str:
    return "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)


def main() -> None:
    rng = random.Random(19)
    faint, bold = [], []
    while len(faint) < 16:
        p = walk(rng, drift=0.9)
        if p[-1][1] > Y_START + 10:            # ended below the start line: a failure
            faint.append(path(p))
    while len(bold) < 3:
        p = walk(rng, drift=-0.75)
        if p[-1][1] < Y_START - 35:
            bold.append(path(p))

    font = "Inter, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
    mono = "'JetBrains Mono', 'SF Mono', Menlo, Consolas, monospace"
    chips = ["SHOWCASE ONLY", "NOT FINANCIAL ADVICE", "NOT FOR LIVE TRADING"]
    chip_svg, cx = [], 64
    for c in chips:
        w = 13 + len(c) * 8.1
        chip_svg.append(
            f'<rect x="{cx}" y="252" width="{w:.0f}" height="28" rx="6" fill="none" stroke="#5c5b56"/>'
            f'<text x="{cx + w / 2:.0f}" y="271" text-anchor="middle" font-family="{mono}" '
            f'font-size="12.5" letter-spacing="0.6" fill="#c3c2b7">{c}</text>')
        cx += w + 10

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="TradingAlgosGitShowcase: nineteen strategy ideas, three survivors. Showcase only, not financial advice, not for live trading.">
  <rect width="{W}" height="{H}" rx="14" fill="#1a1a19"/>
  <line x1="{X0}" y1="{Y_START}" x2="{X1}" y2="{Y_START}" stroke="#5c5b56" stroke-width="1" stroke-dasharray="4 4"/>
  <text x="{X1}" y="{Y_START - 8}" text-anchor="end" font-family="{mono}" font-size="11" fill="#8a8984">starting equity</text>
  <g fill="none" stroke="#c3c2b7" stroke-opacity="0.16" stroke-width="1.4" stroke-linejoin="round">
    {''.join(f'<path d="{d}"/>' for d in faint)}
  </g>
  <g fill="none" stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round">
    {''.join(f'<path d="{d}" stroke="{c}"/>' for d, c in zip(bold, SURVIVOR_COLOURS))}
  </g>
  <text x="64" y="112" font-family="{font}" font-size="46" font-weight="700" fill="#ffffff">TradingAlgosGitShowcase</text>
  <text x="64" y="160" font-family="{font}" font-size="22" fill="#c3c2b7">Nineteen strategy ideas. Three survived their own tests.</text>
  <text x="64" y="192" font-family="{font}" font-size="22" fill="#c3c2b7">Every failure is kept, with the reason it failed.</text>
  {''.join(chip_svg)}
</svg>
"""
    OUT.write_text(svg, encoding="utf-8")
    print(OUT.relative_to(OUT.parent.parent))


if __name__ == "__main__":
    main()
