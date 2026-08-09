#coding:gbk
r"""Big-QMT in-process file bridge for historical market data.

Deploy this file as a Big-QMT built-in Python strategy.  The external client
communicates only through D:\QMT_Bridge.  Source text is intentionally ASCII so
the Big-QMT GBK editor cannot corrupt comments or error messages.

Command example (write atomically to cmd/<id>.json):
{
  "id": "smoke_000001_202607",
  "action": "history",
  "code": "000001.SZ",
  "period": "1m",
  "start": "20260701",
  "end": "20260731",
  "download": true
}

The bridge writes out/<id>.csv and then done/<id>.json.  It processes one
native QMT request at a time because all built-in strategies share one Python
thread.  Native history downloads may still use QMT's own background workers.
"""
import datetime
import json
import os
import re
import time


BRIDGE_DIR = r"D:\QMT_Bridge"
LOOP_INTERVAL = "1nSecond"
DOWNLOAD_SETTLE_SECONDS = 2.0
READ_RETRY_SECONDS = 2.0
MAX_READ_ATTEMPTS = 12
MAX_HISTORY_DAYS = 370
ALLOWED_PERIODS = ("1m", "5m", "1d")
HISTORY_FIELDS = ["open", "high", "low", "close", "volume", "amount"]


class BridgeState(object):
    pass


G = BridgeState()


def init(C):
    G.root = BRIDGE_DIR
    G.cmd_dir = os.path.join(G.root, "cmd")
    G.done_dir = os.path.join(G.root, "done")
    G.out_dir = os.path.join(G.root, "out")
    G.state_dir = os.path.join(G.root, "state")
    for path in (G.root, G.cmd_dir, G.done_dir, G.out_dir, G.state_dir):
        if not os.path.isdir(path):
            os.makedirs(path)

    G.pending = None
    G.started_at = time.time()
    G.completed = 0
    G.failed = 0
    G.last_error = ""
    _write_json(
        os.path.join(G.state_dir, "ready.json"),
        {
            "ok": True,
            "version": 4,
            "bridge_dir": G.root,
            "periods": list(ALLOWED_PERIODS),
            "max_history_days": MAX_HISTORY_DAYS,
            "started_at": G.started_at,
        },
    )
    C.run_time("bridge_loop", LOOP_INTERVAL, "2026-01-01 00:00:00")
    print("QMT file bridge v4 started: %s" % G.root)


def after_init(C):
    _heartbeat("ready")


def handlebar(C):
    return


def stop(C):
    _heartbeat("stopped")
    print("QMT file bridge stopped")


def bridge_loop(C):
    try:
        if G.pending is not None:
            _advance_history(C)
        else:
            _take_one_command(C)
    except Exception as exc:
        G.last_error = "%s: %s" % (type(exc).__name__, exc)
        print("bridge loop error: %s" % G.last_error)
        if G.pending is not None:
            _finish_pending(False, {"error": G.last_error})
    _heartbeat("busy" if G.pending is not None else "ready")


def _take_one_command(C):
    names = []
    try:
        names = sorted(os.listdir(G.cmd_dir))
    except Exception:
        return
    for name in names:
        if not name.lower().endswith(".json"):
            continue
        path = os.path.join(G.cmd_dir, name)
        try:
            handle = open(path, "r")
            try:
                command = json.load(handle)
            finally:
                handle.close()
        except Exception:
            # The external writer may not have completed its atomic rename yet.
            continue

        command_id = _safe_id(command.get("id") or name[:-5])
        command["id"] = command_id
        action = str(command.get("action") or "").strip().lower()
        if action == "ping":
            _finish_immediate(path, command, True, {"pong": time.time()})
            return
        if action == "status":
            _finish_immediate(path, command, True, _status_payload())
            return
        if action == "universe":
            try:
                sector = str(command.get("sector") or "")
                codes = C.get_stock_list_in_sector(sector) or []
                _finish_immediate(
                    path,
                    command,
                    True,
                    {"sector": sector, "codes": list(codes), "count": len(codes)},
                )
            except Exception as exc:
                _finish_immediate(
                    path,
                    command,
                    False,
                    {"error": "%s: %s" % (type(exc).__name__, exc)},
                )
            return
        if action != "history":
            _finish_immediate(path, command, False, {"error": "unknown action"})
            return

        try:
            normalized = _validate_history_command(command)
            normalized["command_path"] = path
            normalized["command"] = command
            normalized["attempts"] = 0
            normalized["started_at"] = time.time()
            normalized["not_before"] = time.time() + DOWNLOAD_SETTLE_SECONDS
            G.pending = normalized
            if normalized["download"]:
                download_history_data(
                    normalized["code"],
                    normalized["period"],
                    normalized["start"],
                    normalized["end"],
                )
            print(
                "history queued id=%s code=%s period=%s %s..%s"
                % (
                    normalized["id"],
                    normalized["code"],
                    normalized["period"],
                    normalized["start"],
                    normalized["end"],
                )
            )
        except Exception as exc:
            _finish_immediate(
                path,
                command,
                False,
                {"error": "%s: %s" % (type(exc).__name__, exc)},
            )
        return


def _validate_history_command(command):
    command_id = _safe_id(command.get("id"))
    code = str(command.get("code") or "").strip().upper()
    period = str(command.get("period") or "1m").strip().lower()
    start = _compact_time(command.get("start"), False)
    end = _compact_time(command.get("end"), True)
    if not re.match(r"^[A-Z0-9_]+\.[A-Z0-9]+$", code):
        raise ValueError("invalid QMT code: %s" % code)
    if period not in ALLOWED_PERIODS:
        raise ValueError("period not allowed: %s" % period)
    start_day = datetime.datetime.strptime(start[:8], "%Y%m%d").date()
    end_day = datetime.datetime.strptime(end[:8], "%Y%m%d").date()
    if end_day < start_day:
        raise ValueError("end precedes start")
    if (end_day - start_day).days > MAX_HISTORY_DAYS:
        raise ValueError("history range exceeds %d calendar days" % MAX_HISTORY_DAYS)
    return {
        "id": command_id,
        "code": code,
        "period": period,
        "start": start,
        "end": end,
        "download": bool(command.get("download", True)),
    }


def _advance_history(C):
    job = G.pending
    if time.time() < job["not_before"]:
        return
    job["attempts"] += 1
    try:
        result = C.get_market_data_ex(
            HISTORY_FIELDS,
            [job["code"]],
            period=job["period"],
            start_time=job["start"],
            end_time=job["end"],
            count=-1,
            dividend_type="none",
            fill_data=False,
            subscribe=False,
        )
        frame = result.get(job["code"]) if isinstance(result, dict) else None
    except Exception as exc:
        if job["attempts"] < MAX_READ_ATTEMPTS:
            job["not_before"] = time.time() + READ_RETRY_SECONDS
            G.last_error = "%s: %s" % (type(exc).__name__, exc)
            return
        _finish_pending(
            False,
            {"error": "%s: %s" % (type(exc).__name__, exc)},
        )
        return

    rows = 0 if frame is None else int(len(frame))
    if rows == 0 and job["attempts"] < MAX_READ_ATTEMPTS:
        job["not_before"] = time.time() + READ_RETRY_SECONDS
        return

    output_path = os.path.join(G.out_dir, "%s.csv" % job["id"])
    first_time = ""
    last_time = ""
    columns = []
    if rows:
        frame = frame.sort_index()
        try:
            frame = frame[~frame.index.duplicated(keep="last")]
        except Exception:
            pass
        rows = int(len(frame))
        frame.index.name = "trade_time"
        columns = [str(item) for item in list(frame.columns)]
        first_time = str(frame.index[0])
        last_time = str(frame.index[-1])
        temporary = output_path + ".tmp"
        frame.to_csv(temporary, index=True, index_label="trade_time", encoding="utf-8")
        os.replace(temporary, output_path)
    else:
        _write_text(output_path, "trade_time,%s\n" % ",".join(HISTORY_FIELDS))

    payload = {
        "output": output_path,
        "rows": rows,
        "columns": columns,
        "first_time": first_time,
        "last_time": last_time,
        "attempts": job["attempts"],
        "empty": rows == 0,
        "elapsed_seconds": round(time.time() - job["started_at"], 3),
    }
    _finish_pending(True, payload)


def _finish_pending(ok, result):
    job = G.pending
    if job is None:
        return
    command = job["command"]
    command_path = job["command_path"]
    G.pending = None
    _finish_immediate(command_path, command, ok, result)


def _finish_immediate(command_path, command, ok, result):
    command_id = _safe_id(command.get("id") or os.path.basename(command_path)[:-5])
    envelope = {
        "id": command_id,
        "action": command.get("action"),
        "ok": bool(ok),
        "result": result,
        "done_at": time.time(),
        "command": command,
    }
    _write_json(os.path.join(G.done_dir, "%s.json" % command_id), envelope)
    try:
        os.remove(command_path)
    except Exception:
        pass
    if ok:
        G.completed += 1
        G.last_error = ""
    else:
        G.failed += 1
        G.last_error = str(result.get("error") or "command failed")
    print("bridge done id=%s ok=%s" % (command_id, ok))


def _safe_id(value):
    text = str(value or "").strip()
    if not text or not re.match(r"^[A-Za-z0-9_.-]{1,120}$", text):
        raise ValueError("invalid command id")
    return text


def _compact_time(value, is_end):
    text = str(value or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) == 8:
        return digits + ("235959" if is_end else "000000")
    if len(digits) == 14:
        return digits
    raise ValueError("time must be YYYYMMDD or YYYYMMDDhhmmss")


def _status_payload():
    pending = None
    if G.pending is not None:
        pending = {
            "id": G.pending.get("id"),
            "code": G.pending.get("code"),
            "start": G.pending.get("start"),
            "end": G.pending.get("end"),
            "attempts": G.pending.get("attempts"),
        }
    return {
        "version": 4,
        "started_at": G.started_at,
        "completed": G.completed,
        "failed": G.failed,
        "last_error": G.last_error,
        "pending": pending,
    }


def _heartbeat(status):
    payload = _status_payload()
    payload["status"] = status
    payload["ts"] = time.time()
    payload["bridge_dir"] = G.root
    try:
        _write_json(os.path.join(G.root, "heartbeat.json"), payload)
    except Exception as exc:
        print("heartbeat error: %s" % exc)


def _write_json(path, value):
    _write_text(path, json.dumps(value, ensure_ascii=True, sort_keys=True))


def _write_text(path, value):
    temporary = path + ".tmp"
    handle = open(temporary, "w")
    try:
        handle.write(value)
        handle.flush()
    finally:
        handle.close()
    os.replace(temporary, path)
