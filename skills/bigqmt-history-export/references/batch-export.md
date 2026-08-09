# Batch Export, Schemas and Publication

## Contents

1. Universe inputs
2. Fixed request ranges
3. Raw layout and schema
4. Completion receipts and resume
5. Concurrency and sharding
6. Public snapshot overlay
7. Validation
8. Neutral dataset builder
9. Legacy compatibility
10. Publication checklist

## 1. Universe inputs

`download_qmt_1min.py` accepts either explicit `--codes` or `--universe-csv`.

Recognized CSV fields:

| Field | Required | Meaning |
|---|---:|---|
| `ts_code` or `code` | yes unless symbol/exchange supplied | `600000.SH` form |
| `symbol` | alternative | six-digit symbol |
| `exchange` | alternative | `SH`, `SZ`, `BJ` |
| `name` | no | display metadata |
| `list_status` or `status` | no | `L`, `D`, `P` filtering |
| `list_date` | no | coverage metadata |
| `delist_date` | no | coverage metadata |

Supported market suffixes are `SH`, `SZ`, `BJ`. Invalid rows are skipped during universe parsing; compare selected count against the source universe before starting a full batch.

For ETF, `build_etf_universe.py` combines a local fund universe and an existing ETF task manifest. It selects exchange-listed codes, retains future/listed/delisted status and excludes names containing `ETF联接` from the name-derived set.

## 2. Fixed request ranges

The normalized label is:

```text
<YYYYMMDDhhmmss>_<YYYYMMDDhhmmss>
```

An 8-digit start becomes `000000`; an 8-digit end becomes `235959`.

Use the same exact range on resume. If the end defaults to the current clock, each restart produces a different label and cannot hit old completion receipts. For scheduled updates, calculate one fixed cutoff before launching all shards.

When the cutoff is intraday, record that the last trading day is partial. Do not later describe it as a close-complete daily snapshot.

## 3. Raw layout and schema

```text
<out>/1min/<asset>/<symbol>_<exchange>/
├── part_qmt_<range>.parquet
└── _qmt_<range>_complete.json
```

Raw columns:

```text
ts_code       string
asset         stock|etf
name          string
page_offset   integer (0 for Formula RPC export)
trade_time    timestamp
open          numeric
close         numeric
high          numeric
low           numeric
vol           int64 shares
amount        int64 currency units
trade_date    YYYYMMDD string
```

QMT's Chinese stock/ETF `volume` is commonly in hands. The exporter multiplies by 100 and stores shares in `vol`. Revalidate this assumption for other asset types or QMT editions.

## 4. Completion receipts and resume

A successful receipt includes:

- `schema_version`
- `source=bigqmt_internal`
- `status=complete`
- instrument metadata
- requested range
- row count and first/last timestamps
- raw/publication file paths
- volume source/output units and multiplier
- overlap/change counters
- completion/elapsed times

A receipt is valid only when:

1. receipt JSON parses;
2. receipt status is `complete`;
3. Parquet exists;
4. Parquet metadata row count equals receipt `rows`.

On a valid resume hit, the batch manifest keeps `status=complete` and adds `resume_action=skipped_complete`. This preserves validator semantics while separately counting skipped work.

Failed attempts do not create a completion receipt. Preserve their manifest error; do not synthesize an empty Parquet to make the batch green.

## 5. Concurrency and sharding

Thread concurrency is bounded to 1-16. Recommended progression:

1. one symbol, one week;
2. four symbols with concurrency 4;
3. `--limit 20` over the real universe;
4. full batch.

For sharding:

```powershell
--num-shards 4 --shard-index 0
--num-shards 4 --shard-index 1
--num-shards 4 --shard-index 2
--num-shards 4 --shard-index 3
```

The assignment is sorted-universe index modulo shard count. All shards must use the same universe snapshot and range. Give each shard separate progress/manifest paths if launching multiple processes against one `out/meta`; the bundled single progress filename is designed primarily for one batch process.

## 6. Public snapshot overlay

Public paths:

```text
<publication>/data/stock_1m/<exchange>/<symbol>.parquet
<publication>/data/etf_1m/<exchange>/<symbol>.parquet
```

Public schema:

```text
symbol       string, non-null
exchange     string, non-null
timestamp    timestamp[us], non-null
open         float64
high         float64
low          float64
close        float64
volume       int64 shares
turnover     float64
```

Overlay algorithm:

1. Read existing instrument file if present.
2. Convert incoming QMT raw rows to public schema.
3. Measure timestamp overlap and value differences.
4. Concatenate old then incoming.
5. Stable-sort by timestamp.
6. Drop duplicate timestamps, keeping incoming last.
7. Write a temporary zstd Parquet.
8. Atomically replace the target.

QMT therefore wins only on identical timestamps in the requested overlay range. Earlier history remains untouched.

## 7. Validation

`verify_qmt_1min.py` reads the latest asset manifest and checks every completed raw file:

- required columns;
- nonempty row count;
- monotonic `trade_time`;
- no duplicate sampled timestamps;
- non-null, nonnegative volume;
- manifest and file consistency.

It writes:

```text
<out>/meta/qmt_<asset>_validation.json
```

Treat `status=pass` and `failed=0` as necessary, not sufficient, publication evidence. Also compare universe counts, min/max times, aggregate rows and a few source-vs-public bars.

## 8. Neutral dataset builder

`build_1min_dataset.py` converts a directory of per-instrument raw directories into a neutral snapshot. It can:

- build stock or ETF outputs;
- merge multiple parts per instrument;
- remove duplicate timestamps;
- calculate calendar-day coverage;
- list missing intervals;
- list eligible universe instruments without bars;
- generate README and summary metadata;
- reject source-brand markers from the public tree.

Example:

```powershell
python scripts\build_1min_dataset.py --asset-kind etf `
  --input-root .\data\raw\qmt\1min\etf `
  --output-dir .\data\public\china_etf_1m_ohlcv `
  --universe-csv .\universe_etfs.csv `
  --calendar-json .\trade_calendar.json `
  --history-floor 2012-01-01 --workers 4 --overwrite
```

## 9. Legacy compatibility

`install_qmt_raw_compat.py` maps completed QMT raw parts and receipts into:

```text
<compat-root>/1min/<asset>/<symbol>_<exchange>/
```

It prefers hard links on the same volume and falls back to atomic copy across volumes. It does not remove previous parts. Run it only after raw validation.

## 10. Publication checklist

Before publishing:

- validation is pass with zero failures;
- selected/completed counts match the intended universe;
- snapshot summary min/max dates are plausible;
- source provenance identifies the QMT overlay range and prior history source;
- partial current-day data is disclosed;
- no account, token, local absolute path, live PID/address, log or command receipt is present;
- license/data redistribution terms are understood;
- remote repository commit is verified after upload;
- remote README/summary and several Parquet metadata entries are reread.

For large Git LFS updates, keep the local commit and LFS objects until the remote commit is confirmed. An interrupted upload is resumable; do not create a second repository merely to avoid resuming the existing one.
