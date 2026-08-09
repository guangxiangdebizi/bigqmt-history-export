# Architecture and Data Flow

## Contents

1. Components
2. Direct native/RPC path
3. In-process file bridge path
4. Path selection
5. Evidence and trust boundaries
6. Concurrency model
7. Output ownership

## 1. Components

```text
External Python 3.10+
├── qmt_native_download.py
│   └── local Win32 process/module/memory APIs
├── qmt_rpc_call.py
│   └── local BSON-over-TCP Formula RPC
├── download_qmt_1min.py
│   ├── universe selection
│   ├── concurrency/retry/resume
│   ├── raw Parquet + receipt
│   └── normalized snapshot overlay
└── verify/build/compat scripts

BigQMT XtItClient.exe
├── FormulaLib.dll
│   └── Boost.Python download_history_data helper
├── Formula RPC listener
└── QMT cache/download subsystem
```

Optional bridge path:

```text
External producer/consumer
  ↕ atomic JSON/CSV files
D:\QMT_Bridge
  ↕
BigQMT built-in Python 3.6 strategy
  ├── download_history_data
  └── C.get_market_data_ex
```

No component in this Skill exposes a network listener. The only socket used by the direct path is an existing QMT loopback listener.

## 2. Direct native/RPC path

Sequence:

```text
batch exporter
  → enumerate XtItClient.exe
  → enumerate FormulaLib.dll
  → read module image
  → locate helper RVA from Boost.Python registration
  → allocate four std::string objects + call stub in target
  → CreateRemoteThread(call helper)
  ← helper returns accepted/rejected
  → connect Formula RPC
  → getMarketData(named parameters)
  ← BSON result
  → parse, normalize, validate, write raw part
  → overlay public snapshot by timestamp
  → write completion receipt
```

The native helper is only a trigger. QMT performs its own authorization and data retrieval. The RPC call is the authoritative observation for available bars.

Advantages:

- No QMT strategy deployment.
- Efficient batch scheduling from modern Python.
- Direct access to Parquet, pandas and validation tooling.
- One resolved process handle can be reused for many symbols.

Constraints:

- Windows x64 and compatible MSVC ABI.
- Target process memory rights are required.
- Signature may require maintenance after QMT upgrades.
- QMT process restart invalidates all process-specific state.

## 3. In-process file bridge path

Sequence:

```text
external writer
  → cmd/<id>.json.tmp
  → atomic rename to cmd/<id>.json
bridge timer
  → validate command
  → download_history_data
  → wait without blocking
  → C.get_market_data_ex
  → out/<id>.csv.tmp
  → atomic rename to out/<id>.csv
  → done/<id>.json
external reader
  ← receipt + CSV
```

Advantages:

- Uses documented objects available inside QMT strategy Python.
- Does not require external process memory writes.
- Language-neutral external interface.

Constraints:

- QMT embedded Python is usually 3.6 with GBK-sensitive editor behavior.
- All built-in strategies share a thread; the bridge must never sleep/block in a callback.
- CSV is slower and larger than direct Parquet output.
- Strategy deployment and lifecycle depend on the QMT client.

## 4. Path selection

Use direct native/RPC when:

- the process signature resolves uniquely;
- the external process can open QMT with required rights;
- a large universe or publication pipeline is required.

Use the bridge when:

- native lookup fails after a QMT version change;
- security tooling blocks `CreateRemoteThread`;
- the user explicitly prefers a filesystem contract;
- only QMT built-in APIs are permitted in the environment.

Use neither when QMT is not running/logged in, the data entitlement does not cover the requested period, or the user cannot lawfully access the requested data.

## 5. Evidence and trust boundaries

Resolve conflicts in this order:

1. Returned bars from the active RPC/built-in API
2. QMT runtime logs and active listener/module state
3. Completion receipts and raw files from the current fixed range
4. Local QMT cache files
5. Checked-in scripts and comments

Source code explains runtime but does not override a live empty response or a server-side permission message.

Do not publish:

- account identifiers or trading state;
- authentication tokens;
- absolute memory addresses;
- local user paths;
- raw QMT logs containing account/product details;
- command files containing private universe or path information.

## 6. Concurrency model

The external batch exporter uses a thread pool because each unit waits on QMT and loopback I/O. One `NativeDownloadSession` holds a resolved process handle and helper address.

Start with 2-4 workers. Increase only after a multi-code smoke test proves QMT remains stable. High worker counts can overload the client's cache/download scheduler even when the external machine has spare CPU.

The bridge is intentionally serial. Its timer advances one pending job through nonblocking states. Do not add `threading`, `multiprocessing`, `while True`, or `time.sleep` to the QMT strategy.

## 7. Output ownership

Keep three layers separate:

1. Raw source layer: one QMT part plus a completion receipt for a fixed request range.
2. Normalized public layer: one schema-stable Parquet per instrument, deduplicated by timestamp.
3. Metadata layer: universe, batch manifest, validation, coverage and provenance.

Never delete a previous source layer merely because a normalized snapshot was built. Raw parts and receipts are the replay evidence needed to diagnose future overlaps, permission changes and source corrections.
