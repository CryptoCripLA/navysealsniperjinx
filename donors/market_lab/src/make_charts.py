from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "data_manifest" / "pumpfun_market_2026-07-31_manifest.csv"
OUT_DIR = ROOT / "docs" / "images"

COLORS = {
    "pump_fun": "#2f80ed",
    "pump_amm": "#27ae60",
}


def load_rows() -> list[dict]:
    with MANIFEST.open("r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    for row in rows:
        row["rows"] = int(row["rows"])
        row["size_bytes"] = int(row["size_bytes"])
        row["hour"] = int(row["beijing_hour"][11:13])
    return rows


def svg_header(width: int, height: int, title: str) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="{title}">',
        "<style>",
        "text{font-family:Arial,Helvetica,sans-serif;fill:#24292f}",
        ".title{font-size:22px;font-weight:700}",
        ".axis{font-size:12px;fill:#57606a}",
        ".label{font-size:13px;fill:#24292f}",
        ".grid{stroke:#d0d7de;stroke-width:1}",
        "</style>",
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
        f'<text class="title" x="28" y="36">{title}</text>',
    ]


def save_svg(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def bar_chart(rows: list[dict], metric: str, title: str, filename: str, unit: str) -> None:
    width, height = 980, 420
    left, right, top, bottom = 72, 28, 64, 64
    plot_w = width - left - right
    plot_h = height - top - bottom

    by_dataset = defaultdict(dict)
    for row in rows:
        value = row[metric]
        if metric == "size_bytes":
            value = value / (1024 * 1024)
        by_dataset[row["dataset"]][row["hour"]] = value

    max_value = max(max(values.values()) for values in by_dataset.values())
    step = plot_w / 24
    bar_w = step * 0.32

    lines = svg_header(width, height, title)
    for i in range(5):
        y = top + plot_h - plot_h * i / 4
        value = max_value * i / 4
        lines.append(f'<line class="grid" x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}"/>')
        lines.append(f'<text class="axis" x="12" y="{y+4:.1f}">{value:,.0f}</text>')

    for hour in range(24):
        x0 = left + hour * step + step * 0.18
        for j, dataset in enumerate(("pump_fun", "pump_amm")):
            value = by_dataset[dataset].get(hour, 0)
            h = 0 if max_value == 0 else plot_h * value / max_value
            x = x0 + j * (bar_w + 3)
            y = top + plot_h - h
            lines.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}" '
                f'rx="2" fill="{COLORS[dataset]}"/>'
            )
        if hour % 3 == 0:
            lines.append(f'<text class="axis" x="{left + hour * step + 8:.1f}" y="{height-34}">{hour:02d}</text>')

    lines.append(f'<text class="axis" x="{width/2-48:.1f}" y="{height-12}">Beijing hour</text>')
    lines.append(f'<text class="axis" x="20" y="{top-10}">{unit}</text>')
    lines.append(f'<rect x="{width-220}" y="22" width="14" height="14" fill="{COLORS["pump_fun"]}"/>')
    lines.append(f'<text class="label" x="{width-200}" y="34">pump_fun</text>')
    lines.append(f'<rect x="{width-120}" y="22" width="14" height="14" fill="{COLORS["pump_amm"]}"/>')
    lines.append(f'<text class="label" x="{width-100}" y="34">pump_amm</text>')
    lines.append("</svg>")
    save_svg(OUT_DIR / filename, lines)


def totals_chart(rows: list[dict]) -> None:
    width, height = 760, 360
    left, top = 220, 78
    bar_h, gap = 46, 34
    totals = defaultdict(int)
    for row in rows:
        totals[row["dataset"]] += row["rows"]
    max_total = max(totals.values())

    lines = svg_header(width, height, "Total raw rows by market")
    for i, dataset in enumerate(("pump_fun", "pump_amm")):
        y = top + i * (bar_h + gap)
        w = 430 * totals[dataset] / max_total
        lines.append(f'<text class="label" x="40" y="{y+30}">{dataset}</text>')
        lines.append(f'<rect x="{left}" y="{y}" width="{w:.1f}" height="{bar_h}" rx="6" fill="{COLORS[dataset]}"/>')
        lines.append(f'<text class="label" x="{left+w+14:.1f}" y="{y+30}">{totals[dataset]:,}</text>')
    lines.append('<text class="axis" x="40" y="302">One complete Beijing-time day: 2026-07-31</text>')
    lines.append("</svg>")
    save_svg(OUT_DIR / "total_rows_by_market.svg", lines)


def main() -> None:
    rows = load_rows()
    totals_chart(rows)
    bar_chart(rows, "rows", "Hourly raw rows by market", "hourly_rows_by_market.svg", "rows")
    bar_chart(rows, "size_bytes", "Hourly parquet size by market", "hourly_size_by_market.svg", "MiB")
    print(f"Wrote charts to {OUT_DIR}")


if __name__ == "__main__":
    main()
