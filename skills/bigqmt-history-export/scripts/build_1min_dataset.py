from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


TARGET_SCHEMA = pa.schema(
    [
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("exchange", pa.string(), nullable=False),
        pa.field("timestamp", pa.timestamp("us"), nullable=False),
        pa.field("open", pa.float64()),
        pa.field("high", pa.float64()),
        pa.field("low", pa.float64()),
        pa.field("close", pa.float64()),
        pa.field("volume", pa.int64()),
        pa.field("turnover", pa.float64()),
    ]
)

FORBIDDEN_MARKERS = ("tushare",)
DATE_RE = re.compile(r"^\d{8}$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a neutral, publication-ready stock or ETF 1-minute Parquet snapshot."
    )
    parser.add_argument("--asset-kind", choices=["stock", "etf"], default="stock")
    parser.add_argument("--input-root", required=True, type=Path, help="Directory containing one directory per instrument.")
    parser.add_argument("--output-dir", required=True, type=Path, help="Publication snapshot directory.")
    parser.add_argument("--universe-csv", type=Path, help="Optional local instrument universe used only to calculate coverage.")
    parser.add_argument("--calendar-json", type=Path, help="Optional local trading-calendar JSON used only to calculate gaps.")
    parser.add_argument("--history-floor", default="2005-01-01", help="Earliest expected date in YYYY-MM-DD form.")
    parser.add_argument("--workers", type=int, default=min(3, max(1, os.cpu_count() or 1)), help="Parallel instrument conversions.")
    parser.add_argument("--overwrite", action="store_true", help="Rebuild existing normalized files.")
    parser.add_argument("--limit", type=int, help="Build at most this many instruments; intended for smoke tests.")
    return parser.parse_args()


def normalize_exchange(value: object) -> str:
    text = str(value or "").strip().upper()
    aliases = {"SSE": "SH", "SHSE": "SH", "SZSE": "SZ", "BSE": "BJ"}
    return aliases.get(text, text)


def parse_symbol_dir(path: Path) -> tuple[str, str] | None:
    match = re.fullmatch(r"(.+)_([A-Za-z]+)", path.name)
    if not match:
        return None
    return match.group(1), normalize_exchange(match.group(2))


def iso_date(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.lower() in {"nan", "nat", "none"}:
        return ""
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 8:
        try:
            return datetime.strptime(digits[:8], "%Y%m%d").date().isoformat()
        except ValueError:
            return ""
    try:
        return datetime.fromisoformat(text).date().isoformat()
    except ValueError:
        return ""


def read_calendar(path: Path | None) -> list[str]:
    if not path:
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    candidates: list[list[str]] = []

    def visit(value: Any) -> None:
        if isinstance(value, list):
            dates = [iso_date(item) for item in value]
            dates = [item for item in dates if item]
            if dates:
                candidates.append(dates)
            for item in value:
                if isinstance(item, (dict, list)):
                    visit(item)
        elif isinstance(value, dict):
            for item in value.values():
                visit(item)

    visit(payload)
    if not candidates:
        raise ValueError(f"No calendar dates found in {path}")
    return sorted(set(max(candidates, key=len)))


def read_universe(path: Path | None) -> dict[tuple[str, str], dict[str, str]]:
    if not path:
        return {}
    result: dict[tuple[str, str], dict[str, str]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            symbol = str(row.get("symbol") or "").strip().upper()
            if not symbol:
                raw = str(row.get("code") or row.get("instrument") or "").strip().upper()
                symbol = raw.split(".", 1)[0]
            exchange = normalize_exchange(row.get("exchange"))
            if not exchange:
                raw = str(row.get("code") or row.get("instrument") or "").strip().upper()
                if "." in raw:
                    exchange = normalize_exchange(raw.rsplit(".", 1)[1])
            if not symbol or not exchange:
                continue
            result[(symbol, exchange)] = {
                "listing_date": iso_date(row.get("list_date") or row.get("listing_date")),
                "delisting_date": iso_date(row.get("delist_date") or row.get("delisting_date")),
                "status": str(row.get("list_status") or row.get("status") or "").strip(),
            }
    return result


def output_file(
    output_dir: Path,
    symbol: str,
    exchange: str,
    asset_kind: str = "stock",
) -> Path:
    return output_dir / "data" / f"{asset_kind}_1m" / exchange / f"{symbol}.parquet"


def source_files(symbol_dir: Path) -> list[Path]:
    return sorted(path for path in symbol_dir.rglob("*.parquet") if path.is_file())


def summarize_existing(target: Path, symbol: str, exchange: str, input_files: int) -> dict[str, Any]:
    table = pq.read_table(target, columns=["timestamp"])
    timestamps = pd.to_datetime(table.column("timestamp").to_pandas())
    if timestamps.empty:
        raise ValueError(f"Normalized file is empty: {target}")
    return {
        "symbol": symbol,
        "exchange": exchange,
        "file": target.relative_to(target.parents[3]).as_posix(),
        "input_files": input_files,
        "records": len(timestamps),
        "duplicate_records_removed": 0,
        "first_timestamp": timestamps.min().isoformat(sep=" "),
        "last_timestamp": timestamps.max().isoformat(sep=" "),
        "observed_dates": sorted({item.date().isoformat() for item in timestamps}),
        "reused": True,
    }


def convert_instrument(
    symbol_dir_text: str,
    output_dir_text: str,
    overwrite: bool,
    asset_kind: str = "stock",
) -> dict[str, Any]:
    symbol_dir = Path(symbol_dir_text)
    output_dir = Path(output_dir_text)
    parsed = parse_symbol_dir(symbol_dir)
    if not parsed:
        raise ValueError(f"Cannot determine symbol and exchange from directory name: {symbol_dir.name}")
    symbol, exchange = parsed
    inputs = source_files(symbol_dir)
    if not inputs:
        raise ValueError(f"No Parquet files found for {symbol_dir}")

    target = output_file(output_dir, symbol, exchange, asset_kind)
    if target.exists() and not overwrite:
        return summarize_existing(target, symbol, exchange, len(inputs))

    frames: list[pd.DataFrame] = []
    required = {"trade_time", "open", "high", "low", "close", "vol", "amount"}
    for path in inputs:
        parquet_file = pq.ParquetFile(path)
        available = set(parquet_file.schema_arrow.names)
        missing = required - available
        if missing:
            raise ValueError(f"{path} is missing required columns: {sorted(missing)}")
        table = pq.read_table(path, columns=["trade_time", "open", "high", "low", "close", "vol", "amount"])
        frame = table.to_pandas()
        frame.rename(columns={"trade_time": "timestamp", "vol": "volume", "amount": "turnover"}, inplace=True)
        frames.append(frame)

    combined = pd.concat(frames, ignore_index=True, copy=False)
    if combined.empty:
        raise ValueError(f"No records found for {symbol}.{exchange}")
    combined["timestamp"] = pd.to_datetime(combined["timestamp"], errors="raise")
    combined.sort_values("timestamp", inplace=True, kind="stable")
    rows_before_deduplication = len(combined)
    combined.drop_duplicates(subset=["timestamp"], keep="last", inplace=True)
    combined.insert(0, "exchange", exchange)
    combined.insert(0, "symbol", symbol)
    combined = combined[[field.name for field in TARGET_SCHEMA]]

    output_table = pa.Table.from_pandas(combined, schema=TARGET_SCHEMA, preserve_index=False, safe=False)
    output_table = output_table.replace_schema_metadata({b"dataset_schema_version": b"1"})
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".parquet.partial")
    pq.write_table(
        output_table,
        temporary,
        compression="zstd",
        compression_level=7,
        use_dictionary=["symbol", "exchange"],
        write_statistics=True,
        row_group_size=250_000,
    )
    temporary.replace(target)

    timestamps = combined["timestamp"]
    return {
        "symbol": symbol,
        "exchange": exchange,
        "file": target.relative_to(output_dir).as_posix(),
        "input_files": len(inputs),
        "records": len(combined),
        "duplicate_records_removed": rows_before_deduplication - len(combined),
        "first_timestamp": timestamps.iloc[0].isoformat(sep=" "),
        "last_timestamp": timestamps.iloc[-1].isoformat(sep=" "),
        "observed_dates": sorted({item.date().isoformat() for item in timestamps}),
        "reused": False,
    }


def date_ranges(dates: Iterable[str], calendar_positions: dict[str, int]) -> list[tuple[str, str, int]]:
    values = sorted(set(dates), key=lambda item: calendar_positions[item])
    if not values:
        return []
    ranges: list[tuple[str, str, int]] = []
    start = previous = values[0]
    count = 1
    for value in values[1:]:
        if calendar_positions[value] == calendar_positions[previous] + 1:
            previous = value
            count += 1
            continue
        ranges.append((start, previous, count))
        start = previous = value
        count = 1
    ranges.append((start, previous, count))
    return ranges


def write_csv(path: Path, fieldnames: list[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_readme(
    output_dir: Path,
    summary: dict[str, Any],
    asset_kind: str = "stock",
) -> None:
    total_size_gb = summary["normalized_bytes"] / 1_000_000_000
    if asset_kind == "etf":
        pretty_name = "China Exchange-Traded Funds 1-Minute OHLCV"
        title = "China Exchange-Traded Funds 1-Minute OHLCV"
        description = "Minute-level OHLCV bars for exchange-listed Chinese ETFs."
        asset_tag = "exchange-traded-funds"
    else:
        pretty_name = "China A-Share Equities 1-Minute OHLCV"
        title = "China A-Share Equities 1-Minute OHLCV"
        description = "Minute-level OHLCV bars for exchange-listed Chinese equities."
        asset_tag = "stock-market"
    content = f"""---
license: other
task_categories:
- time-series-forecasting
tags:
- finance
- {asset_tag}
- ohlcv
- minute-bars
- china
pretty_name: {pretty_name}
size_categories:
- 10B<n<100B
---

# {title}

{description} This is a fixed snapshot with normalized fields, one Parquet file per instrument, and machine-readable coverage reports.

## Snapshot

- Instruments with bars: {summary["instruments_with_bars"]:,}
- Records: {summary["records"]:,}
- First timestamp: {summary["first_timestamp"]}
- Last timestamp: {summary["last_timestamp"]}
- Normalized data size: {total_size_gb:.2f} GB
- Snapshot build time: {summary["built_at"]}

## Layout

```text
data/{asset_kind}_1m/{{exchange}}/{{symbol}}.parquet
metadata/coverage_by_instrument.csv
metadata/missing_intervals.csv
metadata/absent_instruments.csv
metadata/summary.json
```

`coverage_by_instrument.csv` lists every published instrument, its observed time range, record count, and trading-day coverage. `missing_intervals.csv` lists runs of trading-calendar dates that have no observed bars. `absent_instruments.csv` lists eligible instruments in the supplied local universe that have no published file.

## Schema

| Field | Type | Description |
| --- | --- | --- |
| `symbol` | string | Security identifier without exchange suffix. |
| `exchange` | string | Exchange code: `SH`, `SZ`, or `BJ`. |
| `timestamp` | timestamp | Local market timestamp, Asia/Shanghai. |
| `open` | float64 | Open price for the minute bar. |
| `high` | float64 | High price for the minute bar. |
| `low` | float64 | Low price for the minute bar. |
| `close` | float64 | Close price for the minute bar. |
| `volume` | int64 | Reported minute volume. |
| `turnover` | float64 | Reported minute turnover. |

## Coverage Notes

Coverage is evaluated at the trading-day level using the supplied local trading calendar. A missing interval means that no bar was observed for that instrument on one or more eligible trading days. It does not by itself distinguish data collection gaps from instrument suspensions or other market-status events. Dates before listing and dates after delisting are excluded from the expected range.
"""
    (output_dir / "README.md").write_text(content, encoding="utf-8")


def assert_public_tree_clean(output_dir: Path) -> None:
    hits: list[str] = []
    for path in output_dir.rglob("*"):
        if any(marker in path.as_posix().lower() for marker in FORBIDDEN_MARKERS):
            hits.append(path.relative_to(output_dir).as_posix())
            continue
        if not path.is_file():
            continue
        if path.suffix.lower() == ".parquet":
            metadata = pq.ParquetFile(path).metadata.metadata or {}
            payload = b" ".join(metadata.keys()) + b" " + b" ".join(metadata.values())
            if any(marker.encode("utf-8") in payload.lower() for marker in FORBIDDEN_MARKERS):
                hits.append(path.relative_to(output_dir).as_posix())
            continue
        if path.stat().st_size > 20_000_000:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        if any(marker in text for marker in FORBIDDEN_MARKERS):
            hits.append(path.relative_to(output_dir).as_posix())
    if hits:
        raise RuntimeError("Forbidden publication markers found: " + ", ".join(hits[:20]))


def build_reports(
    output_dir: Path,
    results: list[dict[str, Any]],
    universe: dict[tuple[str, str], dict[str, str]],
    calendar: list[str],
    history_floor: str,
    asset_kind: str = "stock",
) -> dict[str, Any]:
    if not results:
        raise RuntimeError("No instrument files were built")
    snapshot_end = max(item["last_timestamp"][:10] for item in results)
    snapshot_start = min(item["first_timestamp"] for item in results)
    history_floor = iso_date(history_floor)
    if not history_floor:
        raise ValueError("--history-floor must be a valid date")

    if calendar:
        observed_calendar_dates = {
            observed_date
            for item in results
            for observed_date in item["observed_dates"]
        }
        calendar = sorted(set(calendar) | observed_calendar_dates)
        calendar = [item for item in calendar if item <= snapshot_end]
        if not calendar:
            raise ValueError("Trading calendar has no dates at or before the snapshot end")
    calendar_positions = {item: index for index, item in enumerate(calendar)}

    coverage_rows: list[dict[str, Any]] = []
    missing_rows: list[dict[str, Any]] = []
    published_keys: set[tuple[str, str]] = set()
    total_missing_days = 0

    for item in sorted(results, key=lambda row: (row["exchange"], row["symbol"])):
        key = (item["symbol"], item["exchange"])
        published_keys.add(key)
        listing = universe.get(key, {}).get("listing_date", "")
        delisting = universe.get(key, {}).get("delisting_date", "")
        universe_status = universe.get(key, {}).get("status", "")
        expected_start = max(history_floor, listing) if listing else item["first_timestamp"][:10]
        if delisting:
            expected_end = min(snapshot_end, delisting)
        elif universe_status.upper() == "D":
            expected_end = item["last_timestamp"][:10]
        else:
            expected_end = snapshot_end
        observed_dates = set(item.pop("observed_dates"))
        if calendar and expected_start <= expected_end:
            expected_dates = [value for value in calendar if expected_start <= value <= expected_end]
            expected_set = set(expected_dates)
            missing_dates = [value for value in expected_dates if value not in observed_dates]
            expected_count = len(expected_dates)
            missing_count = len(missing_dates)
            observed_count = len(expected_set & observed_dates)
            for start, end, count in date_ranges(missing_dates, calendar_positions):
                missing_rows.append(
                    {
                        "symbol": item["symbol"],
                        "exchange": item["exchange"],
                        "start_date": start,
                        "end_date": end,
                        "trading_days_without_bars": count,
                    }
                )
        else:
            expected_count = 0
            missing_count = 0
            observed_count = len(observed_dates)
        total_missing_days += missing_count
        coverage_rows.append(
            {
                **item,
                "listing_date": listing,
                "delisting_date": delisting,
                "universe_status": universe_status,
                "expected_start_date": expected_start,
                "expected_end_date": expected_end,
                "expected_trading_days": expected_count,
                "observed_trading_days": observed_count,
                "missing_trading_days": missing_count,
                "coverage_status": "complete" if missing_count == 0 else "partial",
            }
        )

    absent_rows: list[dict[str, Any]] = []
    for (symbol, exchange), details in sorted(universe.items(), key=lambda item: (item[0][1], item[0][0])):
        listing = details["listing_date"]
        if (symbol, exchange) in published_keys or not listing or listing > snapshot_end:
            continue
        delisting = details["delisting_date"]
        if delisting and delisting < history_floor:
            continue
        absent_rows.append(
            {
                "symbol": symbol,
                "exchange": exchange,
                "listing_date": listing,
                "delisting_date": delisting,
                "status": details["status"],
            }
        )

    metadata_dir = output_dir / "metadata"
    write_csv(
        metadata_dir / "coverage_by_instrument.csv",
        [
            "symbol", "exchange", "file", "input_files", "records", "duplicate_records_removed",
            "first_timestamp", "last_timestamp", "reused", "listing_date", "delisting_date",
            "universe_status", "expected_start_date", "expected_end_date", "expected_trading_days", "observed_trading_days",
            "missing_trading_days", "coverage_status",
        ],
        coverage_rows,
    )
    write_csv(
        metadata_dir / "missing_intervals.csv",
        ["symbol", "exchange", "start_date", "end_date", "trading_days_without_bars"],
        missing_rows,
    )
    write_csv(
        metadata_dir / "absent_instruments.csv",
        ["symbol", "exchange", "listing_date", "delisting_date", "status"],
        absent_rows,
    )

    normalized_files = list(
        (output_dir / "data" / f"{asset_kind}_1m").rglob("*.parquet")
    )
    normalized_bytes = sum(path.stat().st_size for path in normalized_files)
    summary = {
        "dataset_schema_version": 1,
        "asset_kind": asset_kind,
        "built_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "instruments_with_bars": len(coverage_rows),
        "records": sum(int(row["records"]) for row in coverage_rows),
        "input_files": sum(int(row["input_files"]) for row in coverage_rows),
        "duplicate_records_removed": sum(int(row["duplicate_records_removed"]) for row in coverage_rows),
        "first_timestamp": snapshot_start,
        "last_timestamp": max(item["last_timestamp"] for item in results),
        "calendar_start": calendar[0] if calendar else "",
        "calendar_end": calendar[-1] if calendar else "",
        "instruments_with_missing_trading_days": sum(1 for row in coverage_rows if row["missing_trading_days"]),
        "missing_trading_days": total_missing_days,
        "missing_intervals": len(missing_rows),
        "eligible_instruments_without_bars": len(absent_rows),
        "normalized_files": len(normalized_files),
        "normalized_bytes": normalized_bytes,
    }
    (metadata_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / ".gitattributes").write_text(
        "*.parquet filter=lfs diff=lfs merge=lfs -text\n", encoding="ascii"
    )
    write_readme(output_dir, summary, asset_kind)
    return summary


def main() -> int:
    args = parse_args()
    input_root = args.input_root.resolve()
    output_dir = args.output_dir.resolve()
    if not input_root.is_dir():
        raise SystemExit(f"Input root does not exist: {input_root}")
    if input_root == output_dir or input_root in output_dir.parents:
        raise SystemExit("Output directory must not be inside the input directory")
    if args.overwrite and output_dir.exists():
        data_root = output_dir / "data" / f"{args.asset_kind}_1m"
        if data_root.exists():
            shutil.rmtree(data_root)

    candidate_dirs = [path for path in input_root.iterdir() if path.is_dir() and parse_symbol_dir(path)]
    empty_dirs = [path for path in candidate_dirs if not any(path.glob("*.parquet"))]
    instrument_dirs = [path for path in candidate_dirs if path not in empty_dirs]
    instrument_dirs.sort(key=lambda path: parse_symbol_dir(path) or ("", ""))
    if args.limit:
        instrument_dirs = instrument_dirs[: args.limit]
    if not instrument_dirs:
        raise SystemExit(f"No instrument directories found in {input_root}")

    universe = read_universe(args.universe_csv.resolve() if args.universe_csv else None)
    calendar = read_calendar(args.calendar_json.resolve() if args.calendar_json else None)
    output_dir.mkdir(parents=True, exist_ok=True)
    print(
        f"Building {len(instrument_dirs):,} instruments with {args.workers} worker(s); "
        f"{len(empty_dirs):,} directories have no bars.",
        flush=True,
    )

    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    with ProcessPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(
                convert_instrument,
                str(path),
                str(output_dir),
                args.overwrite,
                args.asset_kind,
            ): path
            for path in instrument_dirs
        }
        for index, future in enumerate(as_completed(futures), start=1):
            path = futures[future]
            try:
                result = future.result()
                results.append(result)
            except Exception as exc:
                failures.append({"instrument_directory": path.name, "error": str(exc)})
            if index % 25 == 0 or index == len(futures):
                print(f"Processed {index:,}/{len(futures):,}; failures={len(failures):,}", flush=True)

    if failures:
        write_csv(output_dir / "metadata" / "build_failures.csv", ["instrument_directory", "error"], failures)
        print(f"Build stopped with {len(failures)} failure(s); see metadata/build_failures.csv", file=sys.stderr)
        return 1

    summary = build_reports(
        output_dir,
        results,
        universe,
        calendar,
        args.history_floor,
        args.asset_kind,
    )
    assert_public_tree_clean(output_dir)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
