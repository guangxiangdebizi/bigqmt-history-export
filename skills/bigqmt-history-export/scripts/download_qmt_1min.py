from __future__ import annotations

import argparse
import csv
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from qmt_native_download import NativeDownloadSession
from qmt_rpc_call import call as qmt_rpc_call


QMT_PERMISSION_FLOOR = "20250806"
FIELDS = ["time", "open", "high", "low", "close", "volume", "amount"]
RAW_COLUMNS = [
    "ts_code",
    "asset",
    "name",
    "page_offset",
    "trade_time",
    "open",
    "close",
    "high",
    "low",
    "vol",
    "amount",
    "trade_date",
]
PUBLIC_SCHEMA = pa.schema(
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
VALID_EXCHANGES = {"SH", "SZ", "BJ"}


@dataclass(frozen=True)
class Instrument:
    ts_code: str
    symbol: str
    exchange: str
    name: str = ""
    list_status: str = ""
    list_date: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download and export Big-QMT 1-minute history without GUI use. "
            "The output is compatible with the existing Tushare raw layout."
        )
    )
    parser.add_argument("--asset", choices=["stock", "etf"], default="stock")
    parser.add_argument("--universe-csv", type=Path)
    parser.add_argument("--codes", nargs="*")
    parser.add_argument(
        "--statuses",
        default="L",
        help="Comma-separated list_status filter for a universe CSV; empty keeps all.",
    )
    parser.add_argument("--start-date", default=QMT_PERMISSION_FLOOR)
    parser.add_argument(
        "--end-date",
        default=None,
        help="YYYYMMDD or YYYYMMDDhhmmss; default is the current local time.",
    )
    parser.add_argument("--out", type=Path, default=Path("data/raw/qmt"))
    parser.add_argument(
        "--publication-dir",
        type=Path,
        default=None,
        help=(
            "Existing neutral snapshot to update atomically per instrument. "
            "Defaults to the stock or ETF snapshot according to --asset."
        ),
    )
    parser.add_argument("--no-publication-overlay", action="store_true")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--retry-wait", type=float, default=2.0)
    parser.add_argument("--rpc-host", default="127.0.0.1")
    parser.add_argument("--rpc-port", type=int, default=58600)
    parser.add_argument("--rpc-timeout", type=float, default=120.0)
    parser.add_argument("--native-timeout-ms", type=int, default=120000)
    parser.add_argument("--pid", type=int)
    parser.add_argument("--num-shards", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--progress-every", type=int, default=10)
    return parser.parse_args()


def normalize_time(value: str, is_end: bool) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 8:
        return digits + ("235959" if is_end else "000000")
    if len(digits) == 14:
        return digits
    raise ValueError("time must be YYYYMMDD or YYYYMMDDhhmmss: %r" % value)


def default_universe(asset: str) -> Path:
    if asset == "stock":
        return Path("data/raw/tushare/meta/universe_stocks.csv")
    local_qmt_universe = Path("data/raw/qmt/meta/universe_etfs.csv")
    if local_qmt_universe.exists():
        return local_qmt_universe
    preferred = Path("data/raw/tushare/meta/universe_etfs.csv")
    if preferred.exists():
        return preferred
    # The QuantGo task manifest is also a complete ETF universe and does not
    # carry list_status; load_instruments treats such manifests as unfiltered.
    return Path("data/raw/tushare/meta/1min_tasks_quantgo_etf.csv")


def normalize_exchange(value: object) -> str:
    text = str(value or "").strip().upper()
    aliases = {"SSE": "SH", "SHSE": "SH", "SZSE": "SZ", "BSE": "BJ"}
    return aliases.get(text, text)


def parse_code(value: str) -> tuple[str, str, str]:
    text = str(value or "").strip().upper()
    if "." not in text:
        raise ValueError("QMT code requires a market suffix: %s" % text)
    symbol, exchange = text.rsplit(".", 1)
    exchange = normalize_exchange(exchange)
    if exchange not in VALID_EXCHANGES or not re.fullmatch(r"\d{6}", symbol):
        raise ValueError("unsupported A-share/ETF code: %s" % text)
    return "%s.%s" % (symbol, exchange), symbol, exchange


def load_instruments(args: argparse.Namespace) -> list[Instrument]:
    if args.codes:
        values = []
        for code in args.codes:
            ts_code, symbol, exchange = parse_code(code)
            values.append(Instrument(ts_code, symbol, exchange))
        return sorted(set(values), key=lambda item: item.ts_code)

    path = args.universe_csv or default_universe(args.asset)
    if not path.exists():
        raise FileNotFoundError("universe CSV does not exist: %s" % path)
    statuses = {
        item.strip().upper() for item in args.statuses.split(",") if item.strip()
    }
    values: dict[str, Instrument] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        has_status_column = bool(
            reader.fieldnames
            and any(name in reader.fieldnames for name in ("list_status", "status"))
        )
        if not has_status_column:
            statuses = set()
        for row in reader:
            status = str(row.get("list_status") or row.get("status") or "").strip().upper()
            if statuses and status not in statuses:
                continue
            raw_code = str(row.get("ts_code") or row.get("code") or "").strip()
            if not raw_code:
                symbol = str(row.get("symbol") or "").strip()
                exchange = normalize_exchange(row.get("exchange"))
                raw_code = "%s.%s" % (symbol, exchange)
            try:
                ts_code, symbol, exchange = parse_code(raw_code)
            except ValueError:
                continue
            values[ts_code] = Instrument(
                ts_code=ts_code,
                symbol=symbol,
                exchange=exchange,
                name=str(row.get("name") or "").strip(),
                list_status=status,
                list_date=str(row.get("list_date") or "").strip(),
            )
    return sorted(values.values(), key=lambda item: item.ts_code)


def apply_shard(
    instruments: list[Instrument], num_shards: int, shard_index: int
) -> list[Instrument]:
    if num_shards < 1:
        raise ValueError("--num-shards must be >= 1")
    if shard_index < 0 or shard_index >= num_shards:
        raise ValueError("--shard-index must satisfy 0 <= index < num-shards")
    return [
        item
        for index, item in enumerate(instruments)
        if index % num_shards == shard_index
    ]


def replace_with_retry(temporary: Path, path: Path) -> None:
    for attempt in range(12):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 11:
                raise
            time.sleep(0.1 * (attempt + 1))


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("%s.tmp.%d.%d" % (path.name, os.getpid(), threading.get_ident()))
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    replace_with_retry(temporary, path)


def atomic_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("%s.tmp.%d" % (path.name, os.getpid()))
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    replace_with_retry(temporary, path)


def instrument_dir(out: Path, asset: str, item: Instrument) -> Path:
    return out / "1min" / asset / ("%s_%s" % (item.symbol, item.exchange))


def range_label(start: str, end: str) -> str:
    return "%s_%s" % (start, end)


def part_path(out: Path, asset: str, item: Instrument, label: str) -> Path:
    return instrument_dir(out, asset, item) / ("part_qmt_%s.parquet" % label)


def receipt_path(out: Path, asset: str, item: Instrument, label: str) -> Path:
    return instrument_dir(out, asset, item) / ("_qmt_%s_complete.json" % label)


def valid_receipt(path: Path, parquet_path: Path) -> dict[str, Any] | None:
    if not path.exists() or not parquet_path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = int(pq.read_metadata(parquet_path).num_rows)
    except Exception:
        return None
    if payload.get("status") != "complete" or rows != int(payload.get("rows", -1)):
        return None
    return payload


def rpc_history(
    host: str,
    port: int,
    code: str,
    start: str,
    end: str,
    timeout: float,
) -> dict[str, Any]:
    params = {
        "fields": FIELDS,
        "stockCodes": [code],
        "startTime": start,
        "endTime": end,
        "period": "1m",
        "dividendType": "none",
        "count": -1,
    }
    response = qmt_rpc_call(host, port, "getMarketData", params, timeout)
    if isinstance(response, list):
        if len(response) != 1:
            raise RuntimeError("unexpected multipart getMarketData response")
        response = response[0]
    if int(response.get("status", -1)) != 0:
        raise RuntimeError("QMT getMarketData failed: %r" % response)
    result = response.get("params", {}).get("result", [])
    if len(result) != 2 or str(result[0]).upper() != code.upper():
        raise RuntimeError("unexpected QMT market-data result: %r" % (result[:1],))
    return {"code": result[0], "flat": result[1] or []}


def raw_frame(
    item: Instrument,
    asset: str,
    flat: list[Any],
    requested_start: str,
    requested_end: str,
) -> pd.DataFrame:
    if len(flat) % 2:
        raise ValueError("QMT flat history has an odd item count")
    records: list[dict[str, Any]] = []
    for index in range(0, len(flat), 2):
        timestamp = flat[index]
        values = flat[index + 1]
        if not isinstance(values, list) or len(values) % 2:
            raise ValueError("invalid QMT field/value vector at %r" % timestamp)
        record = {str(values[pos]): values[pos + 1] for pos in range(0, len(values), 2)}
        record["trade_time"] = timestamp
        records.append(record)
    if not records:
        return pd.DataFrame(columns=RAW_COLUMNS)

    frame = pd.DataFrame.from_records(records)
    for field in ("open", "high", "low", "close", "volume", "amount"):
        if field not in frame.columns:
            frame[field] = pd.NA
        frame[field] = pd.to_numeric(frame[field], errors="coerce")
    frame["trade_time"] = pd.to_datetime(frame["trade_time"], errors="raise")
    start_dt = pd.to_datetime(requested_start, format="%Y%m%d%H%M%S")
    end_dt = pd.to_datetime(requested_end, format="%Y%m%d%H%M%S")
    frame = frame.loc[
        (frame["trade_time"] >= start_dt) & (frame["trade_time"] <= end_dt)
    ].copy()
    frame.sort_values("trade_time", inplace=True, kind="stable")
    frame.drop_duplicates(subset=["trade_time"], keep="last", inplace=True)

    # Big-QMT reports Chinese stock/ETF volume in hands.  The existing raw and
    # public schemas store shares, so conversion by 100 is required.
    frame["vol"] = (frame["volume"].fillna(0) * 100).round().astype("int64")
    frame["amount"] = frame["amount"].fillna(0).round().astype("int64")
    frame.insert(0, "page_offset", 0)
    frame.insert(0, "name", item.name)
    frame.insert(0, "asset", asset)
    frame.insert(0, "ts_code", item.ts_code)
    frame["trade_date"] = frame["trade_time"].dt.strftime("%Y%m%d")
    return frame[RAW_COLUMNS].reset_index(drop=True)


def write_raw_part(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("%s.tmp.%d.%d" % (path.name, os.getpid(), threading.get_ident()))
    frame.to_parquet(temporary, index=False, compression="zstd")
    replace_with_retry(temporary, path)


def public_path(publication_dir: Path, asset: str, item: Instrument) -> Path:
    subdirectory = "stock_1m" if asset == "stock" else "etf_1m"
    return (
        publication_dir
        / "data"
        / subdirectory
        / item.exchange
        / (item.symbol + ".parquet")
    )


def publication_overlay(
    publication_dir: Path,
    asset: str,
    item: Instrument,
    raw: pd.DataFrame,
) -> dict[str, Any]:
    target = public_path(publication_dir, asset, item)
    incoming = pd.DataFrame(
        {
            "symbol": item.symbol,
            "exchange": item.exchange,
            "timestamp": raw["trade_time"],
            "open": raw["open"].astype("float64"),
            "high": raw["high"].astype("float64"),
            "low": raw["low"].astype("float64"),
            "close": raw["close"].astype("float64"),
            "volume": raw["vol"].astype("int64"),
            "turnover": raw["amount"].astype("float64"),
        }
    )
    existing_rows = 0
    overlap_rows = 0
    changed_overlap_rows = 0
    if target.exists():
        existing = pq.read_table(target).to_pandas()
        existing_rows = len(existing)
        overlap = existing.merge(
            incoming,
            on="timestamp",
            how="inner",
            suffixes=("_old", "_new"),
        )
        overlap_rows = len(overlap)
        if overlap_rows:
            changed = pd.Series(False, index=overlap.index)
            for field in ("open", "high", "low", "close", "turnover"):
                changed |= (
                    pd.to_numeric(overlap[field + "_old"], errors="coerce")
                    - pd.to_numeric(overlap[field + "_new"], errors="coerce")
                ).abs() > 1e-7
            changed |= overlap["volume_old"] != overlap["volume_new"]
            changed_overlap_rows = int(changed.sum())
        combined = pd.concat([existing, incoming], ignore_index=True, copy=False)
    else:
        combined = incoming

    combined["timestamp"] = pd.to_datetime(combined["timestamp"], errors="raise")
    combined.sort_values("timestamp", inplace=True, kind="stable")
    rows_before_deduplication = len(combined)
    combined.drop_duplicates(subset=["timestamp"], keep="last", inplace=True)
    combined = combined[[field.name for field in PUBLIC_SCHEMA]]
    table = pa.Table.from_pandas(
        combined,
        schema=PUBLIC_SCHEMA,
        preserve_index=False,
        safe=False,
    ).replace_schema_metadata({b"dataset_schema_version": b"1"})
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".parquet.partial")
    pq.write_table(
        table,
        temporary,
        compression="zstd",
        compression_level=7,
        use_dictionary=["symbol", "exchange"],
        write_statistics=True,
        row_group_size=250_000,
    )
    replace_with_retry(temporary, target)
    return {
        "publication_file": str(target),
        "publication_rows_before": existing_rows,
        "publication_rows_after": len(combined),
        "publication_rows_added": len(combined) - existing_rows,
        "publication_deduplicated": rows_before_deduplication - len(combined),
        "overlap_rows": overlap_rows,
        "changed_overlap_rows": changed_overlap_rows,
    }


def download_one(
    session: NativeDownloadSession,
    item: Instrument,
    args: argparse.Namespace,
    start: str,
    end: str,
    label: str,
) -> dict[str, Any]:
    started = time.time()
    parquet_path = part_path(args.out, args.asset, item, label)
    receipt = receipt_path(args.out, args.asset, item, label)
    if not args.no_resume:
        existing = valid_receipt(receipt, parquet_path)
        if existing:
            return {
                **existing,
                "status": "complete",
                "resume_action": "skipped_complete",
                "elapsed_seconds": round(time.time() - started, 3),
            }

    last_error: Exception | None = None
    frame = pd.DataFrame(columns=RAW_COLUMNS)
    for attempt in range(args.retries + 1):
        try:
            accepted = session.trigger(
                item.ts_code,
                "1m",
                start,
                end,
                timeout_ms=args.native_timeout_ms,
            )
            if accepted != 1:
                raise RuntimeError("native download helper returned %d" % accepted)
            response = rpc_history(
                args.rpc_host,
                args.rpc_port,
                item.ts_code,
                start,
                end,
                args.rpc_timeout,
            )
            frame = raw_frame(item, args.asset, response["flat"], start, end)
            if frame.empty:
                raise RuntimeError("QMT returned no bars for the requested range")
            last_error = None
            break
        except Exception as exc:
            last_error = exc
            if attempt >= args.retries:
                break
            time.sleep(args.retry_wait * (attempt + 1))
    if last_error is not None:
        return {
            "schema_version": 1,
            "source": "bigqmt_internal",
            "status": "failed",
            "ts_code": item.ts_code,
            "asset": args.asset,
            "name": item.name,
            "list_status": item.list_status,
            "requested_start": start,
            "requested_end": end,
            "error": "%s: %s" % (type(last_error).__name__, last_error),
            "elapsed_seconds": round(time.time() - started, 3),
        }

    write_raw_part(frame, parquet_path)
    overlay: dict[str, Any] = {}
    if not args.no_publication_overlay:
        overlay = publication_overlay(args.publication_dir, args.asset, item, frame)
    result = {
        "schema_version": 1,
        "source": "bigqmt_internal",
        "status": "complete",
        "ts_code": item.ts_code,
        "asset": args.asset,
        "name": item.name,
        "list_status": item.list_status,
        "list_date": item.list_date,
        "requested_start": start,
        "requested_end": end,
        "rows": len(frame),
        "first_trade_time": frame["trade_time"].iloc[0].isoformat(sep=" "),
        "last_trade_time": frame["trade_time"].iloc[-1].isoformat(sep=" "),
        "raw_file": str(parquet_path),
        "volume_source_unit": "hand",
        "volume_output_unit": "share",
        "volume_multiplier": 100,
        "completed_at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_seconds": round(time.time() - started, 3),
        **overlay,
    }
    atomic_json(receipt, result)
    return result


def main() -> None:
    args = parse_args()
    if args.concurrency < 1 or args.concurrency > 16:
        raise SystemExit("--concurrency must be in [1, 16]")
    start = normalize_time(args.start_date, is_end=False)
    end_value = args.end_date or datetime.now().strftime("%Y%m%d%H%M%S")
    end = normalize_time(end_value, is_end=True)
    if end < start:
        raise SystemExit("end date precedes start date")
    if args.publication_dir is None:
        args.publication_dir = Path(
            "data/public/china_a_share_1m_ohlcv"
            if args.asset == "stock"
            else "data/public/china_etf_1m_ohlcv"
        )
    label = range_label(start, end)

    instruments = load_instruments(args)
    total_universe = len(instruments)
    instruments = apply_shard(instruments, args.num_shards, args.shard_index)
    if args.limit is not None:
        instruments = instruments[: max(0, args.limit)]
    if not instruments:
        print("No instruments selected", flush=True)
        return

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "meta").mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    progress_path = args.out / "meta" / "qmt_progress.json"
    summary_path = args.out / "meta" / ("qmt_%s_symbols_%s.csv" % (args.asset, run_id))
    latest_path = args.out / "meta" / ("qmt_%s_symbols_latest.csv" % args.asset)
    print(
        "Big-QMT 1m: run=%s asset=%s selected=%d universe=%d concurrency=%d "
        "range=%s..%s shard=%d/%d"
        % (
            run_id,
            args.asset,
            len(instruments),
            total_universe,
            args.concurrency,
            start,
            end,
            args.shard_index,
            args.num_shards,
        ),
        flush=True,
    )

    results: list[dict[str, Any]] = []
    completed = failed = skipped = 0
    started_at = time.time()
    with NativeDownloadSession(args.pid) as session:
        print(
            "QMT native session: pid=%d helper_rva=0x%x"
            % (session.pid, session.helper_rva),
            flush=True,
        )
        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            futures = {
                executor.submit(
                    download_one,
                    session,
                    item,
                    args,
                    start,
                    end,
                    label,
                ): item
                for item in instruments
            }
            for index, future in enumerate(as_completed(futures), 1):
                item = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {
                        "schema_version": 1,
                        "source": "bigqmt_internal",
                        "status": "failed",
                        "ts_code": item.ts_code,
                        "asset": args.asset,
                        "name": item.name,
                        "error": "%s: %s" % (type(exc).__name__, exc),
                    }
                results.append(result)
                status = str(result.get("status") or "failed")
                if result.get("resume_action") == "skipped_complete":
                    skipped += 1
                elif status == "complete":
                    completed += 1
                else:
                    failed += 1
                progress = {
                    "run_id": run_id,
                    "asset": args.asset,
                    "status": "running",
                    "selected": len(instruments),
                    "processed": index,
                    "completed": completed,
                    "skipped": skipped,
                    "failed": failed,
                    "last_code": item.ts_code,
                    "requested_start": start,
                    "requested_end": end,
                    "concurrency": args.concurrency,
                    "elapsed_seconds": round(time.time() - started_at, 1),
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                }
                report_progress = (
                    status == "failed"
                    or index == 1
                    or index == len(instruments)
                    or index % max(1, args.progress_every) == 0
                )
                if report_progress:
                    atomic_json(progress_path, progress)
                    print(
                        "[%d/%d] ok=%d skip=%d fail=%d code=%s status=%s rows=%s"
                        % (
                            index,
                            len(instruments),
                            completed,
                            skipped,
                            failed,
                            item.ts_code,
                            status,
                            result.get("rows", 0),
                        ),
                        flush=True,
                    )

    results.sort(key=lambda row: str(row.get("ts_code") or ""))
    atomic_csv(summary_path, results)
    atomic_csv(latest_path, results)
    final_progress = {
        "run_id": run_id,
        "asset": args.asset,
        "status": "complete" if failed == 0 else "complete_with_failures",
        "selected": len(instruments),
        "processed": len(instruments),
        "completed": completed,
        "skipped": skipped,
        "failed": failed,
        "requested_start": start,
        "requested_end": end,
        "concurrency": args.concurrency,
        "elapsed_seconds": round(time.time() - started_at, 1),
        "summary": str(summary_path),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    atomic_json(progress_path, final_progress)
    print(
        "Done: completed=%d skipped=%d failed=%d summary=%s"
        % (completed, skipped, failed, summary_path),
        flush=True,
    )
    if failed:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
