#!/usr/bin/env python3
"""Call a Big-QMT local RPC handler using its BSON-over-TCP protocol."""

import argparse
import json
import socket
import struct
import zlib


MAX_FRAME_SIZE = 256 * 1024 * 1024


def bson_codec():
    try:
        from bson import BSON

        return BSON.encode, BSON.decode
    except ImportError:
        # Keep compatibility with environments that already carry xtquant but
        # not PyMongo.  Only xtbson is used; no miniQMT data client is started.
        from xtquant import xtbson

        return xtbson.BSON.encode, xtbson.decode


def recv_exact(connection, size):
    chunks = []
    remaining = size
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise ConnectionError("QMT closed the connection")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def encode_request(request_id, func, params):
    encode, _ = bson_codec()
    raw = encode({"func": func, "params": params})
    payload = zlib.compress(raw)
    return (
        struct.pack(">II", 12 + len(payload), request_id)
        + b"\x00\x03\x00\x41"
        + payload
    )


def decode_response(frame):
    _, decode = bson_codec()
    flags = frame[8:12]
    payload = frame[12:]
    if flags[3] & 1:
        payload = zlib.decompress(payload)
    elif flags[2] & 0x20:
        payload = payload[:-4]
    return decode(payload)


def call(host, port, func, params, timeout):
    request_id = 1
    documents = []
    with socket.create_connection((host, port), timeout=timeout) as connection:
        connection.settimeout(timeout)
        connection.sendall(encode_request(request_id, func, params))
        while True:
            frame_size = struct.unpack(">I", recv_exact(connection, 4))[0]
            if frame_size < 12 or frame_size > MAX_FRAME_SIZE:
                raise ValueError("invalid QMT frame size: %d" % frame_size)
            frame = struct.pack(">I", frame_size) + recv_exact(
                connection, frame_size - 4
            )
            response_id = struct.unpack(">I", frame[4:8])[0]
            if response_id != request_id:
                continue
            document = decode_response(frame)
            documents.append(document)
            params_block = document.get("params", {})
            if not document.get("marker") or params_block.get("last", True):
                break
    return documents[0] if len(documents) == 1 else documents


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("func")
    parser.add_argument("--params", default="{}", help="JSON object or array")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=58600)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    params = json.loads(args.params)
    result = call(args.host, args.port, args.func, params, args.timeout)
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
