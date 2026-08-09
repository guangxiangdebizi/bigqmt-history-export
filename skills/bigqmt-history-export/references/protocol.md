# Formula RPC Protocol

## Contents

1. Endpoint discovery
2. Request frame
3. Response frame
4. BSON envelope
5. `getMarketData`
6. Multipart responses
7. Validation and errors

## 1. Endpoint discovery

Formula RPC normally listens on loopback. A commonly observed endpoint is `127.0.0.1:58600`, but do not assume it is fixed. Inspect the active QMT Formula server configuration, usually a `formulaserver.ini` under the QMT config tree, and confirm the port with the live listener.

Treat a successful TCP connect as a transport check only. It does not prove the handler name, BSON shape, account entitlement, or requested data exists.

## 2. Request frame

All integer header fields are unsigned big-endian values.

```text
offset  size  meaning
0x00    4     total frame length, including this header
0x04    4     request id
0x08    4     flags
0x0c    N     payload
```

The verified request flags are:

```text
00 03 00 41
```

The request payload is zlib-compressed BSON. `scripts/qmt_rpc_call.py` constructs it as:

```python
raw = BSON.encode({"func": handler, "params": params})
payload = zlib.compress(raw)
frame = pack_be_u32(12 + len(payload)) + pack_be_u32(request_id)
frame += b"\x00\x03\x00\x41" + payload
```

Reject a frame length smaller than 12 or larger than the configured safety limit. The bundled caller caps frames at 256 MiB.

## 3. Response frame

Responses use the same length/request-id header. A commonly observed uncompressed BSON response uses flags:

```text
00 03 20 40
```

For that form, remove the four-byte trailer from the payload before BSON decoding. If the low bit of the fourth flags byte is set, zlib-decompress the payload instead.

The implemented decision order is:

```python
flags = frame[8:12]
payload = frame[12:]
if flags[3] & 0x01:
    payload = zlib.decompress(payload)
elif flags[2] & 0x20:
    payload = payload[:-4]
document = BSON(payload).decode()
```

Do not blindly strip four bytes from every response. A compressed frame and an uncompressed frame with trailer use different paths.

## 4. BSON envelope

Request:

```json
{
  "func": "getMarketData",
  "params": {}
}
```

Response fields observed across handlers include:

```json
{
  "status": 0,
  "error": null,
  "marker": false,
  "params": {
    "last": true,
    "result": []
  }
}
```

Do not depend on every optional field being present. Use `status` for handler success, `marker/params.last` for multipart completion, and the handler-specific `params.result` contract for data.

Known useful handlers include:

- `getMarketData`
- `getInstrumentDetail`
- `getTradingDates`

Handler names are case-sensitive in practice. An unknown handler or incompatible parameters may return an error such as `ErrorID=200005`.

## 5. `getMarketData`

Use named parameters. A verified request shape is:

```json
{
  "fields": ["time", "open", "high", "low", "close", "volume", "amount"],
  "stockCodes": ["510300.SH"],
  "startTime": "20250701",
  "endTime": "20260701",
  "period": "1m",
  "dividendType": "none",
  "count": -1
}
```

Date values may be 8-digit days or 14-digit local timestamps. The batch exporter normalizes an 8-digit start to `000000` and an 8-digit end to `235959`.

A single-code result commonly has this shape:

```text
params.result = [code, flat]
flat = [timestamp_0, values_0, timestamp_1, values_1, ...]
values_n = [field_0, value_0, field_1, value_1, ...]
```

Example:

```json
[
  "600000.SH",
  [
    "2026-07-01 09:30:00",
    ["open", 10.1, "high", 10.2, "low", 10.0, "close", 10.15, "volume", 1234, "amount", 1250000]
  ]
]
```

Validate both vectors have even length. Convert timestamps explicitly, sort them, and keep the last record for an exact duplicate timestamp. Check the result code matches the requested code before writing any file.

The `time` item in `fields` is not necessarily repeated inside each values vector because the timestamp occupies the alternating top-level slot.

## 6. Multipart responses

Keep reading frames with the same request id while the response indicates more parts. Ignore frames for unrelated request ids if a shared connection ever produces them.

The bundled caller stops when either:

- top-level `marker` is false/missing; or
- `params.last` is true/missing.

It returns one document for a single response and a list for multipart responses. The current batch exporter expects one `getMarketData` document and treats an unexpected multipart result as an error instead of guessing how to merge it.

## 7. Validation and errors

Use the following progression:

1. Connect to the loopback port.
2. Call a known small handler or a one-code, short-range `getMarketData`.
3. Confirm response request id.
4. Decode flags and BSON.
5. Confirm `status=0`.
6. Confirm handler-specific result shape.
7. Confirm row count and requested time range.

Typical failures:

| Symptom | Likely cause |
|---|---|
| connection refused | QMT/Formula server not running or wrong port |
| invalid frame size | wrong endpoint, stream desynchronization, or corrupt framing |
| zlib error | flags interpreted incorrectly or incomplete frame |
| BSON decode error | trailer not removed, wrong codec, or malformed payload |
| nonzero status | handler/parameter error |
| status 0, empty result | permission floor, no data, wrong code, or download not settled |

`pymongo` supplies the standard `bson.BSON` codec. If unavailable, the script can fall back to QMT's installed `xtquant.xtbson`; it does not start a miniQMT client or use a remote API.
