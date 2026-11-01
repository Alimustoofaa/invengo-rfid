"""invengo — Python client for Invengo RAIN RFID fixed readers.

Reads UHF EPC Gen2 tags from Invengo XC/RC-RF850-family fixed readers
over TCP.  Pure standard library, no dependencies.

Quickstart::

    from invengo import InvengoReader

    with InvengoReader("192.168.1.100") as reader:
        reader.start_inventory()
        for tag in reader.read_tags(duration=10):
            print(tag.epc, tag.text, tag.antenna, tag.rssi)
"""

from .protocol import (
    CANDIDATE_PORTS,
    FrameParser,
    TagRead,
    build_frame,
    crc16,
    epc_text,
    parse_tag_report,
)
from .reader import InvengoReader, discover_port

__version__ = "0.1.0"

__all__ = [
    "InvengoReader",
    "TagRead",
    "FrameParser",
    "discover_port",
    "build_frame",
    "crc16",
    "parse_tag_report",
    "epc_text",
    "CANDIDATE_PORTS",
    "__version__",
]
