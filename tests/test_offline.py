from __future__ import annotations

import ast
import json
import struct
import sys
import unittest
import zlib
from pathlib import Path

from bson import BSON


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "bigqmt-history-export"
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))

import download_qmt_1min as batch  # noqa: E402
import qmt_native_download as native  # noqa: E402
import qmt_rpc_call as rpc  # noqa: E402


class SkillLayoutTests(unittest.TestCase):
    def test_skill_frontmatter_and_resources(self) -> None:
        source = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(source.startswith("---\n"))
        frontmatter = source.split("---\n", 2)[1]
        self.assertIn("name: bigqmt-history-export", frontmatter)
        self.assertIn("description:", frontmatter)
        self.assertNotIn("TODO", source)
        for relative in (
            "agents/openai.yaml",
            "references/architecture.md",
            "references/protocol.md",
            "references/native-helper.md",
            "references/file-bridge.md",
            "references/batch-export.md",
            "references/permissions-troubleshooting.md",
            "scripts/check_environment.py",
            "scripts/qmt_native_download.py",
            "scripts/qmt_rpc_call.py",
            "scripts/download_qmt_1min.py",
            "scripts/bridge.py",
        ):
            self.assertTrue((SKILL / relative).is_file(), relative)

    def test_bridge_is_ascii_and_python36_syntax(self) -> None:
        path = SCRIPTS / "bridge.py"
        raw = path.read_bytes()
        raw.decode("ascii")
        source = raw.decode("ascii")
        self.assertEqual(source.splitlines()[0], "#coding:gbk")
        ast.parse(source, filename=str(path), feature_version=(3, 6))

    def test_no_local_secrets_or_user_paths_in_skill(self) -> None:
        forbidden = (
            "".join(("gh", "o_")),
            "".join(("h", "f_")),
            "".join(("26", "214")),
            "".join(("My", "project")),
            "".join(("nei", "gezhu")),
        )
        for path in SKILL.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for marker in forbidden:
                self.assertNotIn(marker, text, "%s contains %s" % (path, marker))


class RpcProtocolTests(unittest.TestCase):
    def test_request_frame_round_trip(self) -> None:
        frame = rpc.encode_request(7, "getMarketData", {"count": -1})
        total, request_id = struct.unpack(">II", frame[:8])
        self.assertEqual(total, len(frame))
        self.assertEqual(request_id, 7)
        self.assertEqual(frame[8:12], b"\x00\x03\x00\x41")
        document = BSON(zlib.decompress(frame[12:])).decode()
        self.assertEqual(document["func"], "getMarketData")
        self.assertEqual(document["params"]["count"], -1)

    def test_uncompressed_response_with_trailer(self) -> None:
        document = {"status": 0, "params": {"result": ["600000.SH", []]}}
        body = BSON.encode(document) + b"\x00\x00\x00\x00"
        frame = struct.pack(">II", 12 + len(body), 1) + b"\x00\x03\x20\x40" + body
        self.assertEqual(rpc.decode_response(frame), document)

    def test_compressed_response(self) -> None:
        document = {"status": 0, "params": {"last": True}}
        body = zlib.compress(BSON.encode(document))
        frame = struct.pack(">II", 12 + len(body), 1) + b"\x00\x03\x00\x01" + body
        self.assertEqual(rpc.decode_response(frame), document)


class BatchTransformTests(unittest.TestCase):
    def test_flat_history_deduplicates_and_converts_hands_to_shares(self) -> None:
        item = batch.Instrument("600000.SH", "600000", "SH", "sample")
        flat = [
            "2026-07-01 09:30:00",
            [
                "open", 10.0,
                "high", 10.2,
                "low", 9.9,
                "close", 10.1,
                "volume", 12,
                "amount", 12120.4,
            ],
            "2026-07-01 09:30:00",
            [
                "open", 10.0,
                "high", 10.3,
                "low", 9.9,
                "close", 10.2,
                "volume", 13,
                "amount", 13260.6,
            ],
        ]
        frame = batch.raw_frame(
            item,
            "stock",
            flat,
            "20260701000000",
            "20260701235959",
        )
        self.assertEqual(len(frame), 1)
        self.assertEqual(int(frame.loc[0, "vol"]), 1300)
        self.assertEqual(int(frame.loc[0, "amount"]), 13261)
        self.assertEqual(float(frame.loc[0, "close"]), 10.2)
        self.assertEqual(frame.loc[0, "trade_date"], "20260701")

    def test_market_code_validation(self) -> None:
        self.assertEqual(batch.parse_code("510300.sh"), ("510300.SH", "510300", "SH"))
        with self.assertRaises(ValueError):
            batch.parse_code("510300")


class NativeEncodingTests(unittest.TestCase):
    def test_msvc_short_string_layout(self) -> None:
        value = native.msvc_string("600000.SH")
        self.assertEqual(len(value), 32)
        self.assertEqual(value[:9], b"600000.SH")
        length, capacity = struct.unpack("<QQ", value[16:])
        self.assertEqual(length, 9)
        self.assertEqual(capacity, 15)

    def test_long_native_argument_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            native.msvc_string("x" * 16)

    def test_check_environment_output_is_json_shape(self) -> None:
        import check_environment

        status = check_environment.module_status()
        self.assertEqual(set(status), {"pandas", "numpy", "pyarrow", "bson"})
        json.dumps(status)


if __name__ == "__main__":
    unittest.main()
