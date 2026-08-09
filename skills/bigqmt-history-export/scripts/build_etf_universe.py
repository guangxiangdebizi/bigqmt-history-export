from __future__ import annotations

import argparse
import os
from datetime import datetime
from pathlib import Path

import pandas as pd


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a local listed/delisted China ETF universe without network calls."
    )
    parser.add_argument(
        "--fund-universe",
        type=Path,
        default=Path("data/raw/tushare/meta/universe_funds.csv"),
    )
    parser.add_argument(
        "--active-task-manifest",
        type=Path,
        default=Path("data/raw/tushare/meta/1min_tasks_quantgo_etf.csv"),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("data/raw/qmt/meta/universe_etfs.csv"),
    )
    parser.add_argument(
        "--as-of",
        default=datetime.now().strftime("%Y%m%d"),
        help="Status evaluation date in YYYYMMDD form.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    fund = pd.read_csv(args.fund_universe, dtype=str).fillna("")
    required = {"ts_code", "name", "list_date", "delist_date"}
    missing = required - set(fund.columns)
    if missing:
        raise SystemExit("fund universe is missing columns: %s" % sorted(missing))

    active_codes: set[str] = set()
    active_names: dict[str, str] = {}
    if args.active_task_manifest.exists():
        tasks = pd.read_csv(args.active_task_manifest, dtype=str).fillna("")
        active_codes = set(tasks.get("ts_code", pd.Series(dtype=str)).astype(str))
        active_names = dict(
            zip(
                tasks.get("ts_code", pd.Series(dtype=str)).astype(str),
                tasks.get("name", pd.Series(dtype=str)).astype(str),
            )
        )

    name_mask = (
        fund["name"].str.contains("ETF", case=False, na=False)
        & ~fund["name"].str.contains("ETF联接", case=False, na=False)
        & fund["list_date"].ne("")
    )
    code_mask = fund["ts_code"].isin(active_codes)
    frame = fund.loc[name_mask | code_mask].copy()
    frame = frame[frame["ts_code"].str.endswith((".SH", ".SZ"))].copy()
    frame["symbol"] = frame["ts_code"].str.split(".").str[0]
    frame["exchange"] = frame["ts_code"].str.rsplit(".", n=1).str[-1]
    frame["name"] = frame.apply(
        lambda row: active_names.get(row["ts_code"]) or row["name"], axis=1
    )

    def status(row: pd.Series) -> str:
        if row["list_date"] and row["list_date"] > args.as_of:
            return "P"
        if row["delist_date"] and row["delist_date"] <= args.as_of:
            return "D"
        return "L"

    frame["list_status"] = frame.apply(status, axis=1)
    frame["source_basis"] = frame["ts_code"].map(
        lambda code: "active_etf_manifest+fund_basic"
        if code in active_codes
        else "fund_basic_name_contains_etf"
    )
    columns = [
        "ts_code",
        "symbol",
        "name",
        "list_date",
        "delist_date",
        "exchange",
        "list_status",
        "source_basis",
    ]
    frame = (
        frame[columns]
        .drop_duplicates("ts_code", keep="last")
        .sort_values("ts_code")
        .reset_index(drop=True)
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.out.with_name("%s.tmp.%d" % (args.out.name, os.getpid()))
    frame.to_csv(temporary, index=False, encoding="utf-8-sig")
    os.replace(temporary, args.out)
    print(
        "ETF universe: total=%d L=%d D=%d P=%d out=%s"
        % (
            len(frame),
            int((frame["list_status"] == "L").sum()),
            int((frame["list_status"] == "D").sum()),
            int((frame["list_status"] == "P").sum()),
            args.out,
        )
    )


if __name__ == "__main__":
    main()
