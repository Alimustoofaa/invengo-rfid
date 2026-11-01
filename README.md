# invengo-rfid

Python library and command-line tool for reading UHF **RAIN RFID (EPC Gen2 /
ISO 18000-63)** tags from **Invengo fixed readers** (XC/RC-RF850 family and
similar) over TCP. Pure standard library — zero dependencies.

Built and verified against a live **Invengo RC-XF850** reading truck-mounted
tags at a gate.

## Features

- **Read tags** — inventory streaming with EPC, antenna, signal strength and
  automatic ASCII decoding (tags that store e.g. a plate number as text).
- **Wake-up connect** — Invengo readers serve a *single TCP client* and can
  ignore new connections while a stale session lingers. This is why these
  readers sometimes need to be "primed" with the vendor demo app
  (connect → read → disconnect). The library performs that cycle
  automatically until the reader answers.
- **Watchdog with adaptive back-off** — if the reader goes silent, the client
  reconnects on its own (8 s → 60 s while idle, back to 8 s the moment a tag
  appears). Suitable for unattended, multi-day operation.
- **Clean disconnect** — inventory is always stopped before the socket
  closes, releasing the reader's single-client session immediately.
- **Layout-agnostic frame parser** — accepts both the STX-less framing found
  on tested RC-XF850 firmware and the STX-prefixed framing used by the
  official demo SDK, resynchronizing on garbage automatically.
- **CLI included** — one-shot scans, a probe mode, port discovery, and an
  unattended mode with a crash-safe JSONL event log.

## Install

```bash
pip install invengo-rfid
```

Or from source:

```bash
pip install .
```

## Library usage

```python
from invengo import InvengoReader

with InvengoReader("192.168.1.100", 7086) as reader:
    reader.start_inventory(antennas=(1,), q=4)
    for tag in reader.read_tags(duration=10):
        print(tag.epc, tag.text, tag.antenna, tag.rssi)
```

Unattended monitoring (runs forever, survives network outages):

```python
import time
from invengo import InvengoReader

def log_tag(tag):
    print(time.strftime("%H:%M:%S"), tag.epc, tag.text)

while True:                        # outer loop: never give up
    try:
        with InvengoReader("192.168.1.100") as reader:
            reader.start_inventory()
            for tag in reader.read_tags(duration=None, on_tag=log_tag):
                pass              # already handled via callback
    except ConnectionError as e:
        print("reconnecting after:", e)
        time.sleep(15)
```

Other helpers:

```python
reader.get_power()            # per-antenna RF power
reader.set_power(1, 30)       # antenna 1, 30 dBm
reader.get_mode()             # dense-reader mode code
discover_port("192.168.1.100")  # probe common ports, return the live one
```

## CLI

```
invengo-scan 192.168.1.100                     # scan for 10 seconds
invengo-scan 192.168.1.100 --duration 60       # one minute
invengo-scan 192.168.1.100 --probe             # link / power / mode info
invengo-scan 192.168.1.100 --discover-port     # find the TCP port
invengo-scan 192.168.1.100 --duration 0 --log-file reads.jsonl
                                               # unattended gate monitoring
invengo-scan 192.168.1.100 --debug             # verbose, raw frames
```

`--duration 0` runs forever: every tag read is appended to the JSONL file
*immediately* (crash-safe), the watchdog keeps the session healthy, and if
the network drops the process retries every 15 s instead of exiting. To run
it as a service, a sample systemd unit:

```ini
[Unit]
Description=Invengo RFID gate scanner
After=network-online.target

[Service]
ExecStart=/usr/bin/env invengo-scan 192.168.1.100 --duration 0 --log-file /var/lib/invengo/reads.jsonl
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

## Protocol notes

Frame layout (TCP, confirmed on an RC-XF850):

```
[LEN 2B big-endian] [DATA (LEN bytes)] [CRC16 2B big-endian]
```

CRC16: polynomial `0x8005`, init `0x0000`, MSB-first, no reflection, no
final XOR (check value for `"123456789"` is `0xFEE8`); computed over the LEN
+ DATA bytes. Some firmware variants prefix the frame with an STX byte
(`0x00` LAN / `0x55` serial); `build_frame(payload, stx=0x00)` produces that
variant.

Commands (the DATA payload):

| Command | Bytes | Response |
|---|---|---|
| Ping / handshake | `D2 00` | `D2 00 00 00` |
| Set mode | `62 91 <mode>` | `62 26` |
| Get mode | `62 90 00` | `62 00 <mode>` |
| Get antenna power | `64 30 00` | `64 00 <power...>` |
| Set antenna power | `64 31 <ant> <dBm>` | `64 00` |
| Start inventory | `97 <mask\|0x80> <Q> <loop> <tidLen> <usrStart> <usrLen>` | ACK, then tag stream |
| Stop inventory | `98` | `98 15` |

Tag-report frame (native layout, verified from live traffic):

```
[81] [status] [antenna] [EPC (variable length)] [2 signal bytes]
[reader timestamp 4B] [00] [ss] [2B]
```

The EPC spans from byte 3 up to 10 bytes before the end of the payload.
Examples captured live: EPC `E28011704000021CEE53E32F` (SGTIN-96) and a tag
storing the ASCII text `S1155935` in its EPC (exposed as `TagRead.text`).

Firmware quirks observed on the tested unit (all handled automatically):

- status byte `0x15` means OK (not `0x00`);
- start-inventory is acknowledged with `97 15 <timestamp>` — not a tag;
- `E5 00 ...` frames are scan-cycle notifications — not tags;
- no heartbeat frames are sent during inventory — hence the watchdog;
- the reader serves **one** TCP client at a time.

## Troubleshooting

- **"Must prime the reader with the vendor app"** — the reader is holding a
  half-open session from a previous client. `InvengoReader.connect()` takes
  the session over automatically; also consider disabling *smart hibernation
  mode* in the vendor demo tool so the reader never sleeps.
- **Commands answered with `xx 21` error frames** — your firmware expects a
  different framing; try `stx=0x00` in `build_frame` or open an issue with
  the raw bytes (`--debug`).
- **No tags while trucks pass** — run once with `--debug`; unknown frames are
  kept in `reader.raw_frames` and can be reported to extend the parser.

## Documentation

- [Wire protocol essentials](docs/protocol.md) — framing, CRC16, command
  set, tag-report layout, session behavior.
- [Setup & troubleshooting](docs/troubleshooting.md) — "must prime the
  reader", framing errors, no tags, port discovery, unattended operation.

## Development

```bash
pip install -e ".[test]"
pytest
```

## License

[MIT](LICENSE)
