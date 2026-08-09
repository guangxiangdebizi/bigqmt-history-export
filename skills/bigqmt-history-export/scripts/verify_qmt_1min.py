from __future__ import annotations

import argparse
import csv
import json
import os
import time
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate a Big-QMT 1-minute batch manifest.")
    parser.add_argument("--out", type=Path, default=Path("data/raw/qmt"))
    parser.add_argument("--asset", choices=["stock", "etf"], default="stock")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--sample-rows", type=int, default=10000)
    return parser.parse_args()


def latest_manifest(out: Path, asset: str) -> Path:
    path = out / "meta" / ("qmt_%s_symbols_latest.csv" % asset)
    if not path.exists():
        raise FileNotFoundError(path)
    return path


def validate_file(path: Path, sample_rows: int) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(path)
    parquet = pq.ParquetFile(path)
    metadata = parquet.metadata
    rows = int(metadata.num_rows)
    names = set(parquet.schema_arrow.names)
    required = {
        "ts_code",
        "asset",
        "trade_time",
        "open",
        "close",
        "high",
        "low",
        "vol",
        "amount",
        "trade_date",
    }
    missing = sorted(required - names)
    if missing:
        raise ValueError("missing columns %s" % missing)
    if rows < 1:
        raise ValueError("empty parquet")
    frame = parquet.read(
        columns=["trade_time", "vol", "amount"],
        use_threads=True,
    ).to_pandas()
    if len(frame) > sample_rows:
        frame = frame.iloc[np.linspace(0, len(frame) - 1, sample_rows).astype(int)]
    timestamps = frame["trade_time"]
    if not timestamps.is_monotonic_increasing:
        raise ValueError("trade_time is not sorted")
    if timestamps.duplicated().any():
        raise ValueError("duplicate trade_time in sample")
    if frame["vol"].isna().any() or (frame["vol"] < 0).any():
        raise ValueError("invalid volume")
    return {
        "rows": rows,
        "first_trade_time": str(timestamps.iloc[0]),
        "last_trade_time": str(timestamps.iloc[-1]),
        "sample_rows": len(frame),
        "volume_min": int(frame["vol"].min()),
        "volume_max": int(frame["vol"].max()),
    }


def main() -> None:
    # Import numpy lazily so a manifest-only failure still reports cleanly.
    global np
    import numpy as np  # type: ignore

    args = parse_args()
    manifest = args.manifest or latest_manifest(args.out, args.asset)
    started = time.time()
    total = completed = failed = 0
    records = 0
    failures: list[dict[str, str]] = []
    ranges: list[tuple[str, str]] = []
    with manifest.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            total += 1
            if str(row.get("status") or "") != "complete":
                failures.append(
                    {
                        "ts_code": row.get("ts_code", ""),
                        "error": "manifest status=%s" % row.get("status"),
                    }
                )
                failed += 1
                continue
            try:
                details = validate_file(Path(row["raw_file"]), args.sample_rows)
                completed += 1
                records += details["rows"]
                ranges.append(
                    (details["first_trade_time"], details["last_trade_time"])
                )
            except Exception as exc:
                failed += 1
                failures.append(
                    {
                        "ts_code": row.get("ts_code", ""),
                        "error": "%s: %s" % (type(exc).__name__, exc),
                    }
                )

    result = {
        "asset": args.asset,
        "manifest": str(manifest),
        "instruments": total,
        "completed": completed,
        "failed": failed,
        "records": records,
        "first_trade_time": min((item[0] for item in ranges), default=""),
        "last_trade_time": max((item[1] for item in ranges), default=""),
        "elapsed_seconds": round(time.time() - started, 1),
        "status": "pass" if failed == 0 and completed == total else "fail",
    }
    output = args.out / "meta" / ("qmt_%s_validation.json" % args.asset)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name("%s.tmp.%d" % (output.name, os.getpid()))
    temporary.write_text(
        json.dumps({"summary": result, "failures": failures}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if failures:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
