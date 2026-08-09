#!/usr/bin/env python3
"""Check BigQMT export prerequisites without requesting market data."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import socket
import sys
from typing import Any


REQUIRED_MODULES = ("pandas", "numpy", "pyarrow", "bson")


def module_status() -> dict[str, bool]:
    return {
        name: importlib.util.find_spec(name) is not None
        for name in REQUIRED_MODULES
    }


def check_rpc(host: str, port: int, timeout: float) -> dict[str, Any]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
        return {"ok": True, "host": host, "port": port}
    except Exception as exc:
        return {
            "ok": False,
            "host": host,
            "port": port,
            "error": "%s: %s" % (type(exc).__name__, exc),
        }


def check_native(pid: int | None) -> dict[str, Any]:
    try:
        from qmt_native_download import NativeDownloadSession

        with NativeDownloadSession(pid) as session:
            return {
                "ok": True,
                "pid": session.pid,
                "module": os.path.basename(session.module_path),
                "module_size": session.module_size,
                "helper_rva": "0x%x" % session.helper_rva,
            }
    except Exception as exc:
        return {
            "ok": False,
            "error": "%s: %s" % (type(exc).__name__, exc),
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=58600)
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--pid", type=int)
    parser.add_argument("--skip-native", action="store_true")
    parser.add_argument("--skip-rpc", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    dependencies = module_status()
    result: dict[str, Any] = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "python_64bit": sys.maxsize > 2**32,
        "windows": os.name == "nt",
        "dependencies": dependencies,
    }
    if not args.skip_native:
        result["native"] = check_native(args.pid)
    if not args.skip_rpc:
        result["rpc"] = check_rpc(args.host, args.port, args.timeout)

    required_ok = all(dependencies.values())
    native_ok = args.skip_native or bool(result.get("native", {}).get("ok"))
    rpc_ok = args.skip_rpc or bool(result.get("rpc", {}).get("ok"))
    result["ok"] = bool(
        result["windows"]
        and result["python_64bit"]
        and required_ok
        and native_ok
        and rpc_ok
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["ok"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
