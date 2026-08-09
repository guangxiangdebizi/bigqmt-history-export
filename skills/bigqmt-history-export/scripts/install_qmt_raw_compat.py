from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install completed Big-QMT parts into the legacy raw layout using hard links."
    )
    parser.add_argument("--qmt-out", type=Path, default=Path("data/raw/qmt"))
    parser.add_argument("--compat-root", type=Path, default=Path("data/raw/tushare"))
    parser.add_argument("--asset", choices=["stock", "etf"], default="stock")
    parser.add_argument("--manifest", type=Path)
    return parser.parse_args()


def atomic_copy_or_link(source: Path, target: Path) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        try:
            if target.stat().st_size == source.stat().st_size:
                return "existing"
        except OSError:
            pass
        target.unlink()
    try:
        os.link(source, target)
        return "hardlink"
    except OSError:
        temporary = target.with_name("%s.tmp.%d" % (target.name, os.getpid()))
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
        return "copy"


def main() -> None:
    args = parse_args()
    manifest = args.manifest or (
        args.qmt_out / "meta" / ("qmt_%s_symbols_latest.csv" % args.asset)
    )
    if not manifest.exists():
        raise SystemExit("manifest does not exist: %s" % manifest)
    installed = existing = copied = failed = 0
    failures: list[dict[str, str]] = []
    with manifest.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if str(row.get("status") or "") != "complete":
                continue
            source = Path(str(row.get("raw_file") or ""))
            if not source.is_absolute():
                source = (Path.cwd() / source).resolve()
            code = str(row.get("ts_code") or "").upper()
            if "." not in code or not source.exists():
                failed += 1
                failures.append({"ts_code": code, "error": "missing source or code"})
                continue
            symbol, exchange = code.rsplit(".", 1)
            target_dir = (
                args.compat_root / "1min" / args.asset / ("%s_%s" % (symbol, exchange))
            )
            try:
                mode = atomic_copy_or_link(
                    source,
                    target_dir / source.name,
                )
                receipt = source.with_name(
                    "_qmt_%s_complete.json" % source.stem.removeprefix("part_qmt_")
                )
                if receipt.exists():
                    atomic_copy_or_link(
                        receipt,
                        target_dir / receipt.name,
                    )
                if mode == "existing":
                    existing += 1
                else:
                    installed += 1
                    copied += int(mode == "copy")
            except Exception as exc:
                failed += 1
                failures.append(
                    {"ts_code": code, "error": "%s: %s" % (type(exc).__name__, exc)}
                )

    result: dict[str, Any] = {
        "asset": args.asset,
        "manifest": str(manifest),
        "installed": installed,
        "already_present": existing,
        "copied_fallback": copied,
        "failed": failed,
        "status": "pass" if failed == 0 else "fail",
    }
    output = args.qmt_out / "meta" / ("qmt_%s_compat_install.json" % args.asset)
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
