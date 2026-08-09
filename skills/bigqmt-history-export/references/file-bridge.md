# File Bridge Protocol

## Contents

1. Runtime constraints
2. Directory contract
3. Atomic producer rule
4. Commands
5. History state machine
6. Output and receipts
7. Heartbeat and recovery
8. Deployment checklist

## 1. Runtime constraints

`scripts/bridge.py` runs inside BigQMT's embedded Python strategy runtime. It intentionally uses Python 3.6-compatible syntax and ASCII source with `#coding:gbk`.

Do not add:

- pandas/requests imports from external environments;
- syntax newer than the embedded runtime supports;
- `threading`, `multiprocessing` or `asyncio` loops;
- `time.sleep` inside QMT callbacks;
- blocking directory watchers;
- an external TCP/HTTP listener.

The DataFrame returned by `C.get_market_data_ex` is supplied by QMT; the bridge does not import pandas itself.

## 2. Directory contract

Default root: `D:\QMT_Bridge`. Change the `BRIDGE_DIR` constant before deployment when necessary.

```text
D:\QMT_Bridge\
├── cmd\                 # external -> QMT atomic JSON commands
├── done\                # QMT -> external terminal JSON receipts
├── out\                 # QMT -> external CSV history files
├── state\
│   └── ready.json       # bridge version/capabilities at startup
└── heartbeat.json       # current state and counters
```

The bridge processes at most one command per timer tick and one history job at a time.

## 3. Atomic producer rule

The external writer must never stream directly into `cmd/<id>.json`.

Use:

```text
write cmd/<id>.json.tmp.<pid>
flush
close
os.replace(temp, cmd/<id>.json)
```

The bridge follows the same temporary-write/replace rule for JSON, CSV and heartbeat files. Readers should open, parse and close files promptly so Windows can replace them. On transient sharing violations, external producers/consumers should retry with bounded backoff.

Command ids must match:

```regex
^[A-Za-z0-9_.-]{1,120}$
```

## 4. Commands

### Ping

```json
{"id": "ping_001", "action": "ping"}
```

Receipt result contains `pong` as a Unix timestamp.

### Status

```json
{"id": "status_001", "action": "status"}
```

Result includes version, start time, completed/failed counters, last error and pending job summary.

### Universe

```json
{
  "id": "universe_001",
  "action": "universe",
  "sector": "沪深A股"
}
```

The bridge calls `C.get_stock_list_in_sector`. Sector names are QMT environment data; confirm them locally rather than hard-coding a translated label in external logic.

### History

```json
{
  "id": "history_600000_202607",
  "action": "history",
  "code": "600000.SH",
  "period": "1m",
  "start": "20260701",
  "end": "20260710",
  "download": true
}
```

Supported periods are configured by `ALLOWED_PERIODS` and default to `1m`, `5m`, `1d`. Dates accept 8 or 14 digits. The default bridge caps one request at 370 calendar days; split longer history into windows and deduplicate by timestamp externally.

Set `download=false` only when the required range is already cached and the caller intentionally wants a read-only cache query.

## 5. History state machine

```text
idle
  -> take one command
  -> validate
  -> optional download_history_data
  -> pending/not_before
  -> timer retries C.get_market_data_ex
  -> nonempty or attempts exhausted
  -> write CSV
  -> write receipt
  -> remove command
  -> idle
```

The bridge waits by storing `not_before` and returning from the timer callback. It never sleeps in the QMT strategy thread.

Default timings:

- initial settle: 2 seconds
- read retry: 2 seconds
- maximum attempts: 12

Adjust only after observing current QMT cache behavior. A large request may need longer settle/retry values.

## 6. Output and receipts

History CSV path:

```text
out/<id>.csv
```

The first column is `trade_time`; remaining columns come from the QMT frame. The bridge sorts the index and removes duplicate timestamps, keeping the last occurrence.

Terminal receipt:

```json
{
  "id": "history_600000_202607",
  "action": "history",
  "ok": true,
  "result": {
    "output": "D:\\QMT_Bridge\\out\\history_600000_202607.csv",
    "rows": 1928,
    "columns": ["open", "high", "low", "close", "volume", "amount"],
    "first_time": "2026-07-01 09:30:00",
    "last_time": "2026-07-10 15:00:00",
    "attempts": 1,
    "empty": false,
    "elapsed_seconds": 2.3
  },
  "done_at": 1780000000.0,
  "command": {}
}
```

Always check `ok`, then `empty/rows`, then first/last time. File existence alone is not success.

## 7. Heartbeat and recovery

`heartbeat.json` contains:

- `status`: `ready`, `busy` or `stopped`
- `ts`: last update timestamp
- bridge version/start time
- completed and failed counters
- last error
- pending job summary

Treat the bridge as stale when the heartbeat timestamp is older than several configured timer intervals and QMT is expected to be running.

After QMT restart:

1. wait for a new `state/ready.json` start time;
2. verify heartbeat advances;
3. inspect any command left in `cmd`;
4. inspect whether a matching `done` receipt already exists;
5. resubmit with a new id only when the previous job's terminal state is known.

Do not delete unknown pending commands automatically. Preserve them for reconciliation.

## 8. Deployment checklist

1. Set an absolute `BRIDGE_DIR` writable by QMT and the external process.
2. Keep source ASCII and the first-line GBK declaration.
3. Create a BigQMT Python strategy and deploy the script.
4. Start in a nontrading context; this bridge does not place orders.
5. Confirm `state/ready.json` and heartbeat.
6. Send `ping`, then `status`, then one short history command.
7. Validate CSV timestamps and row count externally.
8. Enable the external batch producer only after the smoke test.

This bundled bridge is a history/universe bridge. It does not implement order, cancel, asset or position commands; keep trading bridges separate so data extraction cannot accidentally mutate an account.
