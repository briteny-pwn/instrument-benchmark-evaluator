from __future__ import annotations

import math
import unittest

from evaluators.pyvisa_dut_validation_v2.protocol import (
    MAX_FRAME_BYTES,
    ProtocolError,
    decode_request,
    decode_response,
    decode_wire_value,
    encode_request,
    encode_message,
    encode_wire_value,
    recv_message,
    success_response,
)


class ChunkSocket:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    def recv(self, count: int) -> bytes:
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        if len(chunk) > count:
            self.chunks.insert(0, chunk[count:])
            return chunk[:count]
        return chunk


class TrustedProtocolTests(unittest.TestCase):
    def test_public_request_literal_is_accepted(self) -> None:
        payload = (
            b'{"args":{"count":4096,"session":"opaque"},'
            b'"operation":"read","request_id":7,"version":1}'
        )
        expected = len(payload).to_bytes(4, "big") + payload
        self.assertEqual(
            encode_request(7, "read", {"session": "opaque", "count": 4096}),
            expected,
        )
        self.assertEqual(
            decode_request(
                {
                    "args": {"count": 4096, "session": "opaque"},
                    "operation": "read",
                    "request_id": 7,
                    "version": 1,
                }
            ),
            (7, "read", {"count": 4096, "session": "opaque"}),
        )

    def test_tagged_union_matches_public_vectors(self) -> None:
        value = (None, False, 3, -1.25, "x", b"\x00\xff", (1,))
        self.assertEqual(decode_wire_value(encode_wire_value(value)), value)
        for invalid in ([1], {"x": 1}, math.inf, math.nan):
            with self.assertRaises(ProtocolError):
                encode_wire_value(invalid)
        for invalid in (
            {"type": "bytes", "base64": "%%%"},
            {"type": "list", "items": "bad"},
            {"type": "unknown"},
        ):
            with self.assertRaises(ProtocolError):
                decode_wire_value(invalid)
        nested: object = 1
        for _ in range(17):
            nested = {"type": "list", "items": [nested]}
        with self.assertRaisesRegex(ProtocolError, "nesting"):
            decode_wire_value(nested)

    def test_exact_response_shapes_and_ids(self) -> None:
        response = {
            "version": 1,
            "request_id": 2,
            "ok": True,
            "result": {"type": "bytes", "base64": "YWJj"},
            "status": 0,
        }
        self.assertEqual(decode_response(response, 2), (b"abc", 0))
        for invalid in (
            {**response, "request_id": 3},
            {**response, "version": 1.0},
            {**response, "extra": None},
            {**response, "status": True},
        ):
            with self.assertRaises(ProtocolError):
                decode_response(invalid, 2)

    def test_trusted_response_literal_matches_public_decoder(self) -> None:
        message = success_response(7, b"abc", 0)
        payload = (
            b'{"ok":true,"request_id":7,"result":{'
            b'"base64":"YWJj","type":"bytes"},"status":0,"version":1}'
        )
        self.assertEqual(
            encode_message(message), len(payload).to_bytes(4, "big") + payload
        )

    def test_frame_rejects_partial_truncated_invalid_and_duplicate_json(self) -> None:
        from evaluators.pyvisa_dut_validation_v2.protocol import ConnectionClosed

        with self.assertRaises(ConnectionClosed):
            recv_message(ChunkSocket([]))
        valid = b'{"args":{},"operation":"hello","request_id":1,"version":1}'
        frame = len(valid).to_bytes(4, "big") + valid
        self.assertEqual(
            recv_message(ChunkSocket([frame[:1], frame[1:5], frame[5:]]))[
                "operation"
            ],
            "hello",
        )
        payloads = (
            b"",
            b"\xff",
            b"{",
            b'{"version":1,"version":1}',
            b"[]",
            b'{"value":' + b"1" * 5000 + b"}",
        )
        streams = [
            ChunkSocket([b"\x00\x00"]),
            ChunkSocket([(MAX_FRAME_BYTES + 1).to_bytes(4, "big")]),
        ]
        streams.extend(
            ChunkSocket([len(payload).to_bytes(4, "big"), payload])
            for payload in payloads
        )
        for stream in streams:
            with self.assertRaises(ProtocolError):
                recv_message(stream)

    def test_request_rejects_surrogate_strings_before_broker_dispatch(self) -> None:
        invalid = "\ud800"
        cases = (
            {
                "version": 1,
                "request_id": 1,
                "operation": invalid,
                "args": {},
            },
            {
                "version": 1,
                "request_id": 1,
                "operation": "hello",
                "args": {"value": invalid},
            },
        )
        for value in cases:
            with self.assertRaisesRegex(ProtocolError, "UTF-8"):
                decode_request(value)

    def test_request_uses_one_item_budget_across_all_arguments(self) -> None:
        message = {
            "version": 1,
            "request_id": 1,
            "operation": "hello",
            "args": {f"key_{index}": None for index in range(2049)},
        }
        with self.assertRaisesRegex(ProtocolError, "too many"):
            decode_request(message)


if __name__ == "__main__":
    unittest.main()
