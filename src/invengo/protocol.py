"""Wire protocol for Invengo RAIN RFID fixed readers (XC/RC-RF850 family).

Frame layout (confirmed against an RC-XF850 over TCP, port 7086)::

    [LEN 2 bytes big-endian] [DATA (LEN bytes)] [CRC16 2 bytes big-endian]

Some firmware variants (as used by the official Invengo demo) prepend an
STX byte (0x00 on LAN, 0x55 on serial).  The tested RC-XF850 firmware does
**not** use an STX byte and silently rejects STX-framed commands with a
generic 2-byte error payload ending in 0x21.  :func:`build_frame` defaults
to the STX-less layout; pass ``stx=0x00`` (or any other byte) for readers
that expect one.  :class:`FrameParser` accepts both layouts.

CRC16: polynomial 0x8005, initial value 0x0000, MSB-first, no reflection,
no final XOR (check value for b"123456789" is 0xFEE8).  It is computed
over the LEN bytes + DATA bytes.
"""

from __future__ import annotations

from dataclasses import dataclass

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------
CRC_POLY = 0x8005
CRC_INIT = 0x0000

#: STX byte used by some firmware variants on LAN connections.
STX_LAN = 0x00
#: STX byte used by some firmware variants on serial connections.
STX_SERIAL = 0x55

# Command payloads (the DATA part of a frame).
CMD_PING = b"\xD2\x00"                       # handshake / link check
CMD_SET_MODE = b"\x62\x91"                   # + 1 mode byte
CMD_GET_MODE = b"\x62\x90\x00"
CMD_GET_POWER = b"\x64\x30\x00"
CMD_SET_POWER = b"\x64\x31"                  # + antenna + dBm
CMD_START_INVENTORY = 0x97
CMD_STOP_INVENTORY = b"\x98"

MODE_NAMES = {
    2: "dense read-write mode 2",
    3: "dense read-write mode 3",
    4: "fast read mode",
    5: "dense read-write mode 1",
    9: "default mode",
    0xFF: "auto mode",
}

#: TCP ports commonly used by Invengo readers, in probe order.
CANDIDATE_PORTS = [7086, 7000, 6000, 6001, 8888, 8080, 4001, 10001, 502]


# --------------------------------------------------------------------------
# CRC & framing
# --------------------------------------------------------------------------
def crc16(data: bytes, crc: int = CRC_INIT) -> int:
    """Invengo CRC16: poly 0x8005, init 0, MSB-first, no reflect/xorout."""
    for b in data:
        for _ in range(8):
            bit = ((crc & 0x8000) >> 8) ^ (b & 0x80)
            if bit:
                crc = ((crc << 1) ^ CRC_POLY) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
            b = (b << 1) & 0xFF
    return crc


def build_frame(payload: bytes, stx: int | None = None) -> bytes:
    """Wrap a command payload into a complete frame.

    ``stx=None`` (default) produces the STX-less layout
    ``[LEN][DATA][CRC]`` used by the tested RC-XF850 firmware.  Pass
    ``stx=0x00`` or ``stx=0x55`` for firmware variants that expect a
    leading STX byte.
    """
    n = len(payload)
    len_be = bytes([(n >> 8) & 0xFF, n & 0xFF])
    c = crc16(len_be + payload)
    head = bytes([stx]) if stx is not None else b""
    return head + len_be + payload + bytes([(c >> 8) & 0xFF, c & 0xFF])


class FrameParser:
    """
    Incremental, layout-agnostic frame splitter.

    Scans for a valid ``[LEN][DATA][CRC]`` triple at every byte offset and
    validates it with the CRC, so it transparently handles both STX-less
    and STX-prefixed frames as well as garbage between frames.  Feed it
    whatever the socket delivers and collect complete payloads.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes = b"") -> list[bytes]:
        """Consume *data* and return every complete frame payload found."""
        self._buf.extend(data)
        payloads = []
        while True:
            payload = self._extract_one()
            if payload is None:
                break
            payloads.append(payload)
        return payloads

    def _extract_one(self) -> bytes | None:
        i = 0
        while i + 1 < len(self._buf):
            ln = (self._buf[i] << 8) | self._buf[i + 1]
            end = i + 4 + ln
            if end <= len(self._buf):
                calc = crc16(bytes(self._buf[i:i + 2 + ln]))
                rx_crc = (self._buf[end - 2] << 8) | self._buf[end - 1]
                if calc == rx_crc:
                    payload = bytes(self._buf[i + 2:end - 2])
                    del self._buf[:end]
                    return payload
            i += 1
        return None


# --------------------------------------------------------------------------
# Tag reports
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class TagRead:
    """A single tag report received from the reader."""
    #: EPC of the tag, uppercase hex string.
    epc: str
    #: Antenna number the tag was read on (1-based).
    antenna: int
    #: Signal strength.  Raw 0-255 value on the tested firmware; signed
    #: dBm on the legacy layout.  Comparable within one reader only.
    rssi: int
    #: Printable-ASCII decoding of the EPC (e.g. a truck plate number),
    #: or an empty string when the EPC is not printable text.
    text: str = ""
    #: TID contents, uppercase hex (empty unless requested and supported).
    tid: str = ""
    #: User-memory contents, uppercase hex.
    user: str = ""


def epc_text(epc_hex: str) -> str:
    """Decode an EPC hex string to ASCII if it is printable text."""
    try:
        raw = bytes.fromhex(epc_hex)
    except ValueError:
        return ""
    if raw and all(0x20 <= b < 0x7F for b in raw):
        return raw.decode("ascii")
    return ""


def parse_tag_report(data: bytes) -> TagRead | None:
    """
    Parse a tag-report payload into a :class:`TagRead`.

    Two layouts are supported:

    * Native layout of the tested RC-XF850 firmware (frames start with
      0x81)::

          [81] [status] [antenna] [EPC, variable length] [2 signal bytes]
          [reader timestamp, 4 bytes] [00] [ss] [2 bytes]

      The EPC spans from byte 3 up to 10 bytes before the end of the
      payload (verified against live traffic).
    * Legacy layout used by the official Invengo demo SDK (frames start
      with 0x97)::

          [97] [status] [antenna] [EPC length + 1] [EPC] [RSSI int8]
          [TID length] [TID] [user length] [user]

    Returns ``None`` when *data* is not a tag report.
    """
    try:
        if len(data) >= 11 and data[0] == 0x81:
            epc = data[3:-10]
            if not epc:
                return None
            epc_hex = epc.hex().upper()
            return TagRead(epc=epc_hex,
                           antenna=data[2],
                           rssi=data[-9],
                           text=epc_text(epc_hex))
        if len(data) < 5 or data[0] != 0x97:
            return None
        antenna = data[2]
        epc_len = data[3] - 1
        if epc_len <= 0:
            return None
        epc_hex = data[4:4 + epc_len].hex().upper()
        i = 4 + epc_len
        if i >= len(data):
            return None
        rssi = int.from_bytes(data[i:i + 1], "big", signed=True)
        i += 1
        tid_len = data[i] if i < len(data) else 0
        i += 1
        tid = data[i:i + tid_len]
        i += tid_len
        user_len = data[i] if i < len(data) else 0
        i += 1
        user = data[i:i + user_len]
        return TagRead(epc=epc_hex, antenna=antenna, rssi=rssi,
                       text=epc_text(epc_hex),
                       tid=tid.hex().upper(), user=user.hex().upper())
    except (IndexError, ValueError):
        return None
