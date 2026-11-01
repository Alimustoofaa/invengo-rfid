# Invengo reader wire protocol — essentials

The protocol used by Invengo XC/RC-RF850-family fixed readers over TCP.
Everything on this page is implemented by this library; you only need it
when debugging raw traffic or porting to another language.

## Framing

```
[LEN 2 bytes, big-endian] [DATA (LEN bytes)] [CRC16 2 bytes, big-endian]
```

Some firmware variants (the ones targeted by the official Invengo demo
SDK) prefix the frame with an STX byte — `0x00` on LAN, `0x55` on serial.
Others, including tested RC-XF850 units, use **no STX byte**; sending an
STX-prefixed command to such a unit is answered with a generic 2-byte
error payload ending in `0x21`. When in doubt, try both.

**CRC16**: polynomial `0x8005`, initial value `0x0000`, MSB-first, no
reflection, no final XOR. Computed over the LEN + DATA bytes.
Check value: `crc16("123456789") == 0xFEE8`.

## Commands (frame DATA payloads)

| Command | Bytes | Typical response |
|---|---|---|
| Ping / link check | `D2 00` | `D2 00 00 00` |
| Set reader mode | `62 91 <mode>` | `62 <status>` |
| Get reader mode | `62 90 00` | `62 00 <mode>` |
| Get antenna power | `64 30 00` | `64 00 <power per antenna>` |
| Set antenna power | `64 31 <antenna> <dBm>` | `64 00` |
| Start inventory | `97 <mask> <Q> <loop> <tidLen> <usrStart> <usrLen>` | ACK, then tag stream |
| Stop inventory | `98` | `98 <status>` |

Inventory antenna *mask* = `0x80 | bit per antenna` (antenna 1 = bit 0,
antenna 2 = bit 1, ...). *loop* = `1` to read continuously, `0` for a
single cycle.

## Responses during inventory

- **Tag report** — see layout below.
- **Start ACK** — `97 15 <timestamp>` (10 bytes), sent ~0.1 s after the
  start command. The timestamp counts seconds (usable as a read clock).
  *Not* a tag.
- **Scan-cycle notification** — `E5 00 00 00 00 00 00 00`. Not a tag.
- **Heartbeat** — a 2-byte payload; some firmware sends these during
  inventory, some never does. Silence during inventory is normal when no
  tag is in range.

## Tag report layout

Native layout of tested RC-RF850/861 firmware (frames start with `0x81`):

```
[81] [status] [antenna] [EPC — variable length] [2 signal bytes]
[reader timestamp, 4 bytes] [00] [ss] [2 bytes]
```

- The EPC spans from byte 3 up to **10 bytes before the end** of the
  payload.
- `data[-9]` is a raw 0-255 signal-strength byte (comparable within one
  reader only; not calibrated dBm).
- EPCs that contain printable ASCII (e.g. a vehicle or asset number
  programmed as text) are exposed as `TagRead.text`.

Legacy layout used by the official demo SDK (frames start with `0x97`):

```
[97] [status] [antenna] [EPC length + 1] [EPC] [RSSI, int8 dBm]
[TID length] [TID] [user length] [user]
```

## Session behavior

- The reader serves **one TCP client at a time**; a second connection is
  accepted by the TCP stack but ignored.
- A client that disappears without a clean close (crash, dropped VPN)
  leaves a stale session that blocks the reader until a new `connect()`
  takes it over — the origin of the "prime it with the vendor app"
  workaround (see [troubleshooting.md](troubleshooting.md)).
- Status byte conventions differ per firmware: on tested units `0x15`
  means OK for inventory commands, while `0x00` means OK elsewhere.
