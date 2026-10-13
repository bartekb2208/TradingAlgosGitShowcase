"""make_charts.py — regenerate the figures in docs/img/ (light + dark variants).

Every number below is copied from a results document in this repo or, for the
Larry Williams ablation, read off the cTrader "Trade statistics" screenshots of
the original runs (see research/orb-larry-williams-ablation/README.md). Nothing
here is recomputed from price data; this script only draws.

    pip install matplotlib
    python tools/make_charts.py
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "docs" / "img"

THEMES = {
    "light": dict(surface="#fcfcfb", text="#0b0b0b", text2="#52514e", grid="#e4e3df",
                  series=["#2a78d6", "#eb6834", "#1baf7a"], pos="#2a78d6", neg="#e34948"),
    "dark": dict(surface="#1a1a19", text="#ffffff", text2="#c3c2b7", grid="#383835",
                 series=["#3987e5", "#d95926", "#199e70"], pos="#3987e5", neg="#e66767"),
}

# --- Larry Williams ablation: net P&L on a $10,000 account, Jan 2022 -> Jun 2026, m5 ---
LW_VARIANTS = ["ORB baseline", "+ Bailout exit", "+ Extension filter",
               "+ Greatest Swing Value", "+ Trade-day-of-week"]
LW_NET = {  # None = no clean record survives (see the study README)
    "NAS100": [14944.90, None, 13690.60, -298.77, 8015.46],
    "US500":  [10287.72, -2010.41, 9617.70, 108.77, 4532.55],
    "XAUUSD": [-984.21, -2033.91, -1712.51, -153.82, -1222.79],
}
LW_FOOTNOTE = {("NAS100", 4): "*"}  # probable attribution, see README

# --- Survivors: profit factor in-sample vs the pre-registered out-of-sample window ---
SURVIVORS = [  # name, IS PF, OOS PF
    ("DailyDipSwing", 2.03, 1.88),
    ("UsdJpyCarryTrend", 1.70, 1.23),
    ("ORB 1.1", 1.53, 1.34),
]


def _style(ax, t):
    ax.set_facecolor(t["surface"])
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(t["grid"])
    ax.tick_params(colors=t["text2"], labelsize=9, length=0)


def lw_ablation(theme: str) -> Path:
    t = THEMES[theme]
    fig, axes = plt.subplots(1, 3, figsize=(12, 3.5), sharey=True, facecolor=t["surface"])
    lim = 17500
    for ax, (sym, vals) in zip(axes, LW_NET.items()):
        _style(ax, t)
        ys = list(range(len(LW_VARIANTS)))[::-1]
        for i, (y, v) in enumerate(zip(ys, vals)):
            if v is None:
                ax.text(0, y, "  no clean record", va="center", ha="left",
                        color=t["text2"], fontsize=8.5, style="italic")
                continue
            ax.barh(y, v, height=0.55, color=t["pos"] if v >= 0 else t["neg"],
                    edgecolor=t["surface"], linewidth=2)
            label = f"{v:+,.0f}{LW_FOOTNOTE.get((sym, i), '')}"
            ax.text(v + (350 if v >= 0 else -350), y, label, va="center",
                    ha="left" if v >= 0 else "right", color=t["text"], fontsize=8.5)
        ax.axvline(0, color=t["text2"], linewidth=1)
        ax.set_xlim(-lim * 0.42, lim)
        ax.set_title(sym, color=t["text"], fontsize=11, loc="left", pad=8)
        ax.xaxis.grid(True, color=t["grid"], linewidth=0.8)
        ax.set_axisbelow(True)
        ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda x, _: f"{x/1000:+.0f}k" if x else "0"))
    axes[0].set_yticks(list(range(len(LW_VARIANTS)))[::-1])
    axes[0].set_yticklabels(LW_VARIANTS, color=t["text"], fontsize=9.5)
    fig.suptitle("Larry Williams additions vs the ORB baseline — net P&L on $10k, Jan 2022 – Jun 2026",
                 x=0.01, ha="left", color=t["text"], fontsize=12.5, y=0.975)
    fig.text(0.01, 0.015, "Single in-sample cTrader runs, m5 bars, commissions and swaps included.  "
             "* 390-trade result saved under three variant folders; attributed to TDW by trade count.",
             color=t["text2"], fontsize=8)
    fig.tight_layout(rect=(0, 0.05, 1, 0.95), w_pad=2.5)
    p = OUT / f"lw-ablation-{theme}.png"
    fig.savefig(p, dpi=160, facecolor=t["surface"])
    plt.close(fig)
    return p


def is_vs_oos(theme: str) -> Path:
    t = THEMES[theme]
    fig, ax = plt.subplots(figsize=(7.2, 4.3), facecolor=t["surface"])
    _style(ax, t)
    ax.axhline(1.0, color=t["text2"], linewidth=1, linestyle=(0, (4, 3)))
    ax.text(-0.33, 1.025, "break-even (PF 1.0)", va="bottom", ha="left", color=t["text2"], fontsize=8.5)
    for (name, a, b), c in zip(SURVIVORS, t["series"]):
        ax.plot([0, 1], [a, b], color=c, linewidth=2, marker="o", markersize=8,
                markeredgecolor=t["surface"], markeredgewidth=2, solid_capstyle="round")
        ax.text(-0.04, a, f"{a:.2f}", ha="right", va="center", color=t["text"], fontsize=9)
        ax.text(1.04, b, f"{b:.2f}  {name}", ha="left", va="center", color=t["text"], fontsize=9.5)
    ax.set_xlim(-0.35, 1.75)
    ax.set_ylim(0.85, 2.2)
    ax.set_xticks([0, 1])
    ax.set_xticklabels(["in-sample\n(window used to choose)", "out-of-sample\n(sealed, read once)"],
                       color=t["text"], fontsize=9.5)
    ax.set_yticks([])
    ax.spines["bottom"].set_visible(False)
    ax.set_title("The three survivors: profit factor before and after the seal",
                 color=t["text"], fontsize=12, loc="left", pad=12)
    fig.text(0.01, 0.015, "Every survivor got worse out of sample. ORB 1.1's Calmar fell 5.76 → 1.35 over the same split.",
             color=t["text2"], fontsize=8)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    p = OUT / f"is-vs-oos-{theme}.png"
    fig.savefig(p, dpi=160, facecolor=t["surface"])
    plt.close(fig)
    return p


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for th in THEMES:
        for fn in (lw_ablation, is_vs_oos):
            print(fn(th).relative_to(OUT.parent.parent))
