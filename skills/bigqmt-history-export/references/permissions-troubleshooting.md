# Permissions and Troubleshooting

## Contents

1. Permission model
2. Probe matrix
3. Evidence to capture
4. Failure classification
5. Windows/process failures
6. RPC/data failures
7. Clean recovery procedure
8. Reporting limits accurately

## 1. Permission model

Three independent permissions matter:

1. OS process permission: can the external script inspect and invoke the local QMT process?
2. QMT runtime permission: is the client initialized, logged in and serving Formula calls?
3. Market-data entitlement: does the account/product authorize the requested instrument, period and date range?

Passing one layer does not prove the next. `CreateRemoteThread` success cannot grant market-data entitlement, and Formula RPC `status=0` can still carry an empty result.

## 2. Probe matrix

Choose one liquid code and request small windows while changing only the date:

| Probe | Purpose |
|---|---|
| recent known trading day | prove basic 1m path |
| one month earlier | prove stable recent coverage |
| just inside suspected floor | locate first authorized window |
| just outside suspected floor | distinguish entitlement truncation |
| clearly old year | confirm old-history behavior |

For every probe record:

- QMT PID and module version/hash, not absolute address;
- code, period, exact start/end;
- native return;
- RPC status/error;
- row count and first/last returned time;
- relevant QMT log permission message;
- whether the range was already locally cached.

Do not run broad universe probes until this matrix yields a coherent boundary.

## 3. Evidence to capture

Prefer:

1. live returned bars;
2. active QMT logs and listener/module state;
3. current raw parts/receipts;
4. local cache observations;
5. scripts/comments.

Sanitize logs before sharing. Keep the minimum decisive line and remove account identifiers, product tenant values, local paths and unrelated trading data.

## 4. Failure classification

| Observation | Classification | Next action |
|---|---|---|
| native 0 | helper rejected | validate code/period/date and current signature |
| native 1, RPC empty on all dates | runtime/cache path | wait, inspect logs, test known recent day |
| native 1, recent rows, old empty | entitlement/history boundary | binary-search floor and document it |
| RPC nonzero | handler/protocol | inspect function and named params |
| rows outside request | normalization/request semantics | filter and log actual range |
| some symbols empty | instrument/listing/status | check listing/delisting and code suffix |
| all symbols fail after restart | stale process state | recreate NativeDownloadSession |
| duplicate timestamps | source/cache overlap | stable-sort and keep higher-priority incoming row |

Never turn a classified empty response into a successful zero-row completion merely to make a manifest pass.

## 5. Windows/process failures

### Process not running

Start/login QMT and wait for `XtItClient.exe`, `FormulaLib.dll` and Formula RPC listener. Do not automate GUI login as part of the data path.

### Multiple target processes

Inspect start time, module and listener ownership. Pass an explicit `--pid` only after identifying the live client. Do not choose the highest or newest PID without evidence.

### Access denied

Run both processes at the same integrity level. Check endpoint security policy and QMT edition. Do not disable system security globally.

### Helper lookup is empty/ambiguous

Record QMT version and FormulaLib hash. Use `--locate-only`; update signature logic against the active module. Do not reuse an RVA from a different version.

### Remote thread timeout

Stop scheduling new work. Verify QMT responsiveness, close the session and restart the client if necessary. Resume from valid receipts after a fresh locate-only check.

### `PermissionError` on `os.replace`

Windows readers can transiently hold the destination. Close readers quickly and retry atomic replacement with bounded linear backoff. Do not abandon already-written data because only a progress JSON replacement failed; reconstruct status from receipts.

## 6. RPC/data failures

### Connection refused/reset

Confirm the live configured port and listener. A QMT process alone does not prove Formula RPC is ready.

### `ErrorID=200005`

Treat it as handler/parameter incompatibility. Return to a verified handler and exact named parameter shape; do not guess positional variants in a full batch.

### BSON/zlib errors

Read the declared frame length exactly. Interpret flags before decompressing or stripping a trailer. Use standard `bson.BSON` where possible.

### Native accepted but data is empty

Wait for cache settle and retry a bounded number of times. Compare a known recent date and an older date. Inspect QMT logs for a maximum allowed start time. Separate permission, listing history and not-yet-settled cache.

### First day starts intraday

Some entitlements use a rolling timestamp rather than midnight at the printed date. Record the first actual bar, not only the date-level log message. Preserve older existing history outside the overlay.

### Volume appears 100x small/large

Compare a liquid bar against the QMT UI/API and existing schema. For Chinese stocks/ETFs, QMT commonly reports hands while the public schema uses shares. Do not apply the multiplier twice.

## 7. Clean recovery procedure

1. Stop only the batch process; preserve QMT unless it is unresponsive.
2. Save the current manifest/progress/error log.
3. Count raw Parquet and valid completion receipts independently.
4. Check whether the main process failed only while writing progress after workers completed.
5. Fix the narrow failure, such as atomic replace retry or status semantics.
6. Relaunch with the identical fixed request range and normal resume behavior.
7. Confirm prior valid receipts become `resume_action=skipped_complete`.
8. Process only missing/invalid instruments.
9. Rebuild final manifests and rerun full validation.

If QMT restarted, repeat locate-only and one-code smoke before recovery.

## 8. Reporting limits accurately

Use wording such as:

```text
In this QMT account/runtime, 1-minute requests returned bars from <actual first timestamp>.
An older probe at <date> returned zero rows, and the QMT log reported a maximum start boundary.
This is an account/product entitlement observation, not a limitation hard-coded by the exporter.
```

For a requested 2012-present dataset, do not claim completion when QMT only supplies a recent window. A valid full dataset requires already-owned earlier history or a separately authorized source, with a provenance boundary and overlap validation.
