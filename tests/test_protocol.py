"""Tests for the invengo wire protocol.

Runs under pytest (``pytest``) or standalone (``python tests/test_protocol.py``).
Frame vectors below are real bytes captured from an RC-XF850 reader with
truck-mounted tags passing a gate.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from invengo.protocol import (  # noqa: E402
    FrameParser,
    TagRead,
    build_frame,
    crc16,
    epc_text,
    parse_tag_report,
)

# Real tag-report payloads captured live from an RC-XF850 (port 7086).
PLATE_FRAME = "81000153313135353933353C676AE7550E000935AA"      # ASCII tag
SGTIN_FRAME = ("810001E28011704000021CEE53E32F4165"
               "6AE75504000B34BB")                                # SGTIN-96


def test_crc16_check_value():
    # poly 0x8005, init 0, MSB-first, no reflection
    assert crc16(b"123456789") == 0xFEE8


def test_crc16_ccitt_crosscheck():
    # sanity-check the implementation style against a known-good algorithm
    crc = 0xFFFF
    for b in b"123456789":
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 \
                else (crc << 1) & 0xFFFF
    assert crc == 0x29B1        # CRC-16/CCITT-FALSE check value


def test_build_frame_stxless():
    frame = build_frame(b"\x98")
    assert frame[:2] == b"\x00\x01"          # LEN
    assert len(frame) == 5                   # LEN + payload + CRC
    assert ((frame[-2] << 8) | frame[-1]) == crc16(frame[:-2])


def test_build_frame_with_stx():
    frame = build_frame(b"\xD2\x00", stx=0x00)
    assert frame[0] == 0x00 and frame[1:3] == b"\x00\x02"
    assert ((frame[-2] << 8) | frame[-1]) == crc16(frame[1:-2])


def test_frame_parser_basic_roundtrip():
    payload = b"\xD2\x00\x12\x34"
    parser = FrameParser()
    assert parser.feed(build_frame(payload)) == [payload]
    assert parser.feed() == []


def test_frame_parser_stx_variant():
    payload = b"\x64\x30\x00"
    parser = FrameParser()
    # STX-prefixed frames parse the same way (universal CRC scan).
    assert parser.feed(build_frame(payload, stx=0x55)) == [payload]


def test_frame_parser_resync_after_garbage():
    payload = b"\x98"
    frame = build_frame(payload)
    parser = FrameParser()
    # leading garbage and a broken partial frame must not confuse the parser
    assert parser.feed(b"\xff\x07\x00" + frame[:2]) == []
    assert parser.feed(frame[2:] + b"\x00\xde\xad") == [payload]
    assert parser.feed(build_frame(b"\x62\x90\x00")) == [b"\x62\x90\x00"]


def test_frame_parser_chunked_delivery():
    payload = bytes.fromhex(SGTIN_FRAME)
    frame = build_frame(payload)
    parser = FrameParser()
    got = []
    for i in range(0, len(frame), 3):            # 3 bytes at a time
        got += parser.feed(frame[i:i + 3])
    assert got == [payload]


def test_parse_ascii_tag_frame():
    tag = parse_tag_report(bytes.fromhex(PLATE_FRAME))
    assert isinstance(tag, TagRead)
    assert tag.epc == "5331313535393335"
    assert tag.text == "S1155935"
    assert tag.antenna == 1
    assert tag.rssi == 103                        # raw 0-255 signal byte


def test_parse_sgtin_tag_frame():
    tag = parse_tag_report(bytes.fromhex(SGTIN_FRAME))
    assert tag is not None
    assert tag.epc == "E28011704000021CEE53E32F"  # SGTIN-96, 12 bytes
    assert tag.text == ""                          # not printable text
    assert tag.antenna == 1


def test_parse_legacy_demo_layout():
    epc = bytes.fromhex("E20034120136FE00001F65B2")
    tid = bytes.fromhex("E28011052000")
    data = (b"\x97\x00"                           # cmd + status
            + bytes([1])                          # antenna
            + bytes([len(epc) + 1])               # EPC length + 1
            + epc
            + b"\xC8"                             # RSSI 0xC8 = -56 dBm
            + bytes([len(tid)]) + tid
            + b"\x00")                            # empty user memory
    tag = parse_tag_report(data)
    assert tag is not None
    assert tag.epc == epc.hex().upper()
    assert tag.rssi == -56
    assert tag.tid == tid.hex().upper()


def test_parse_rejects_non_tag_frames():
    assert parse_tag_report(b"\x97\x15\x6A\xE7\x55\x04\x00\x02\x31\xCA") is None
    assert parse_tag_report(b"\xE5\x00\x00\x00\x00\x00\x00\x00") is None
    assert parse_tag_report(b"\x62\x26") is None
    assert parse_tag_report(b"") is None


def test_epc_text():
    assert epc_text("5331313535393335") == "S1155935"
    assert epc_text("E28011704000021CEE53E32F") == ""
    assert epc_text("not-hex") == ""


def _run_all() -> int:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    print(f"\n{'ALL TESTS PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())
