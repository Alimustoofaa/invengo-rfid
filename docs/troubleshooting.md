# Setup & troubleshooting — essentials

Quick answers for the most common problems when talking to Invengo fixed
readers over TCP.

## The reader "must be primed" with the vendor app

Symptom: connections open but commands get no reply until someone runs
the vendor demo tool and does connect → read tag → disconnect.

Cause: the reader serves **one TCP client at a time**. A previous client
that vanished without a clean close leaves a stale session that blocks
everyone else. The vendor app's connect/disconnect cycle takes the
session over and releases it again — that's the "priming".

Fix: `InvengoReader.connect()` already replays that cycle automatically
(connect → ping → disconnect, repeated until the reader answers). For a
permanent site-side fix, disable *smart hibernation mode* in the vendor
demo tool (Reader Configuration) so the reader never sleeps while idle.

## Commands answered with 2-byte `xx 21` errors

Your firmware expects a different framing than the one being sent — most
likely a leading STX byte (`0x00`) that your reader does not want (or
does want and isn't getting). Try `build_frame(payload, stx=0x00)` or
open an issue with the raw bytes.

## Connects fine, but no tags

- No tag in range — UHF reads need the tag inside the antenna field;
  verify with the vendor demo tool first if in doubt.
- Wrong antenna mask — inventory only reads the antennas you enable:
  `start_inventory(antennas=(1, 2))`.
- RF power too low — check with `reader.get_power()`, raise with
  `reader.set_power(1, 30)`.
- Tags are reported once per dedup window by the reader; a stationary
  tag may show up only every few seconds.

## Wrong TCP port

Default is 7086 (as shipped on tested RC-XF850 units), but units vary.
Find the live one automatically:

```bash
invengo-scan 192.168.1.100 --discover-port
```

or `discover_port("192.168.1.100")` from Python.

## Reader unreachable over VPN / remote site

Readers on remote sites are often reachable only through a VPN or a
port forward. If the connection times out: check the route on the
scanning machine (`ip route get <reader-ip>`), make sure the VPN is up,
and confirm the port with `--discover-port` once reachable.

## Runs fine for a while, then stalls

Use the built-in watchdog: while `read_tags()` is iterating, silence
longer than `silent_timeout` triggers an automatic reconnect (with
back-off up to 60 s while idle). For 24/7 operation run with
`--duration 0`, which also retries forever on total connection loss.

## Collecting debug information

```bash
invengo-scan 192.168.1.100 --debug
```

- Raw frames are logged as hex (`--debug` / `reader.raw_frames`).
- Unrecognized frames are counted in `reader.unknown_count` — if tags
  are passing but never appear, those raw frames are what a maintainer
  needs to extend the parser (open a GitHub issue and include them).
