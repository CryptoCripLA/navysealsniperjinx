from __future__ import annotations

import argparse
import csv
import json
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import pyarrow.parquet as pq


UTC = timezone.utc
RAW_ROOT = Path(r"D:\E盘备份_20260628\true项目\LR\MemeHunter\pumpfundata\raw")
DATASETS = ("pump_fun", "pump_amm")


@dataclass
class HourFile:
    dataset: str
    utc_hour: str
    beijing_hour: str
    path: str
    exists: bool
    size_bytes: int
    rows: int | None
    columns: list[str]


def beijing_day_to_utc_hours(day: str) -> list[datetime]:
    start_bj = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone(timedelta(hours=8)))
    start_utc = start_bj.astimezone(UTC)
    return [start_utc + timedelta(hours=i) for i in range(24)]


def hour_path(dataset: str, utc_dt: datetime) -> Path:
    date = utc_dt.strftime("%Y-%m-%d")
    hour = utc_dt.strftime("%H")
    return RAW_ROOT / dataset / f"date={date}" / f"{dataset}_{date}_{hour}.parquet"


def inspect_file(dataset: str, utc_dt: datetime) -> HourFile:
    path = hour_path(dataset, utc_dt)
    beijing_dt = utc_dt.astimezone(timezone(timedelta(hours=8)))
    if not path.exists():
        return HourFile(
            dataset=dataset,
            utc_hour=utc_dt.strftime("%Y-%m-%d %H:00:00"),
            beijing_hour=beijing_dt.strftime("%Y-%m-%d %H:00:00"),
            path=str(path),
            exists=False,
            size_bytes=0,
            rows=None,
            columns=[],
        )
    pf = pq.ParquetFile(path)
    return HourFile(
        dataset=dataset,
        utc_hour=utc_dt.strftime("%Y-%m-%d %H:00:00"),
        beijing_hour=beijing_dt.strftime("%Y-%m-%d %H:00:00"),
        path=str(path),
        exists=True,
        size_bytes=path.stat().st_size,
        rows=pf.metadata.num_rows,
        columns=pf.schema.names,
    )


def inspect_day(day: str) -> list[HourFile]:
    hours = beijing_day_to_utc_hours(day)
    return [inspect_file(dataset, utc_dt) for dataset in DATASETS for utc_dt in hours]


def write_manifest(rows: Iterable[HourFile], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    rows = list(rows)
    with output.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(asdict(rows[0]).keys()))
        writer.writeheader()
        for row in rows:
            item = asdict(row)
            item["columns"] = json.dumps(item["columns"], ensure_ascii=False)
            writer.writerow(item)


def copy_samples(rows: Iterable[HourFile], output_dir: Path, max_rows: int = 5000) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for dataset in DATASETS:
        first = next(row for row in rows if row.dataset == dataset and row.exists)
        target = output_dir / f"{dataset}_sample_1hour.parquet"
        table = pq.read_table(first.path)
        pq.write_table(table.slice(0, max_rows), target, compression="zstd")


def summarize(rows: list[HourFile]) -> dict:
    out = {}
    for dataset in DATASETS:
        part = [row for row in rows if row.dataset == dataset]
        present = [row for row in part if row.exists]
        out[dataset] = {
            "expected_hours": len(part),
            "found_hours": len(present),
            "missing_hours": len(part) - len(present),
            "size_bytes": sum(row.size_bytes for row in present),
            "rows": sum(row.rows or 0 for row in present),
            "columns": present[0].columns if present else [],
        }
    return out


def write_report(day: str, rows: list[HourFile], output: Path) -> None:
    summary = summarize(rows)
    output.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Pump.fun Market Lab - {day} Dataset Report",
        "",
        "This report describes one complete Beijing-time natural day of raw Pump.fun market data.",
        "",
        "## Coverage",
        "",
        f"- Beijing day: `{day} 00:00:00` to `{day} 23:59:59`",
        "- Included datasets: `pump_fun` bonding-curve market and `pump_amm` PumpSwap / AMM market",
        "- Hourly files per dataset: `24`",
        "",
        "## Summary",
        "",
        "| Dataset | Found hours | Rows | Size | Notes |",
        "|---|---:|---:|---:|---|",
    ]
    for dataset in DATASETS:
        item = summary[dataset]
        size_gb = item["size_bytes"] / (1024**3)
        note = "complete" if item["missing_hours"] == 0 else "missing files"
        lines.append(
            f"| `{dataset}` | {item['found_hours']}/24 | {item['rows']:,} | {size_gb:.2f} GiB | {note} |"
        )
    lines += [
        "",
        "## Safety Notes",
        "",
        "- This repository is a public research sample, not a trading signal service.",
        "- Raw market data is provided for reproducible analysis and tooling demos.",
        "- Do not infer live profitability from the sample without modeling slippage, priority fees, protocol fees, platform fees, failed transactions, and infrastructure cost.",
        "- No API keys, private wallets, or private strategy labels are included.",
        "",
        "## Files",
        "",
        "- `data_manifest/pumpfun_market_2026-07-31_manifest.csv`: hourly file inventory.",
        "- `reports/pumpfun_market_2026-07-31_report.md`: this generated report.",
        "- `data_sample/`: small parquet samples for quick local testing.",
        "- Full one-day raw data should be attached as GitHub Release assets because it is too large for normal Git history.",
        "",
    ]
    output.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--day", default="2026-07-31")
    parser.add_argument("--manifest", default="data_manifest/pumpfun_market_2026-07-31_manifest.csv")
    parser.add_argument("--report", default="reports/pumpfun_market_2026-07-31_report.md")
    parser.add_argument("--sample-dir", default="data_sample")
    parser.add_argument("--copy-samples", action="store_true")
    args = parser.parse_args()

    rows = inspect_day(args.day)
    write_manifest(rows, Path(args.manifest))
    write_report(args.day, rows, Path(args.report))
    if args.copy_samples:
        copy_samples(rows, Path(args.sample_dir))
    print(json.dumps(summarize(rows), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
