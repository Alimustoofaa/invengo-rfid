"""Command line interface: ``invengo-scan`` (also ``python -m invengo``).

Examples::

    invengo-scan 192.168.1.100                  # scan for 10 seconds
    invengo-scan 192.168.1.100 --duration 30
    invengo-scan 192.168.1.100 --probe          # link/antenna/mode info
    invengo-scan 192.168.1.100 --discover-port  # find the TCP port
    invengo-scan 192.168.1.100 --duration 0 --log-file reads.jsonl
                                                # unattended gate monitoring
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

from . import __version__
from .protocol import TagRead, epc_text
from .reader import InvengoReader, discover_port

logger = logging.getLogger("invengo.cli")


@dataclass
class _TagAggregate:
    epc: str
    text: str = ""
    antenna: int = 0
    rssi: int = 0
    count: int = 0
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)

    def update(self, tag: TagRead) -> None:
        self.antenna = tag.antenna
        self.rssi = tag.rssi
        self.count += 1
        self.last_seen = time.time()


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="invengo-scan",
        description="Read UHF RAIN RFID tags from Invengo fixed readers "
                    "over TCP.",
        epilog="Use --duration 0 for unattended operation: the scanner "
               "runs forever, survives connection loss and logs every "
               "tag read immediately.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("host", help="reader IP address or hostname")
    ap.add_argument("--port", type=int, default=7086,
                    help="reader TCP port")
    ap.add_argument("--discover-port", action="store_true",
                    help="probe common ports for a live reader instead of "
                         "trusting --port")
    ap.add_argument("--duration", type=float, default=10.0,
                    help="seconds to scan (0 = forever, unattended mode)")
    ap.add_argument("--antennas", default="1",
                    help="comma-separated antenna numbers, e.g. 1,2")
    ap.add_argument("--q", type=int, default=4, help="Gen2 Q parameter")
    ap.add_argument("--tid-length", type=int, default=0,
                    help="number of TID bytes to also read (0 = none)")
    ap.add_argument("--wake-retries", type=int, default=6,
                    help="connect->ping->disconnect cycles used to wake "
                         "the reader up")
    ap.add_argument("--silent-timeout", type=float, default=8.0,
                    help="seconds of silence before an automatic "
                         "reconnect (backs off up to 60 s)")
    ap.add_argument("--probe", action="store_true",
                    help="only show link, antenna power and reader mode")
    ap.add_argument("--json", metavar="FILE",
                    help="write the summary to a JSON file")
    ap.add_argument("--log-file", metavar="FILE",
                    help="append every tag read immediately as one JSON "
                         "line (crash-safe, for unattended mode)")
    ap.add_argument("--debug", action="store_true",
                    help="verbose logging including raw frames")
    ap.add_argument("--quiet", action="store_true", help="minimal logging")
    ap.add_argument("--version", action="version",
                    version=f"%(prog)s {__version__}")
    return ap


def _setup_logging(args: argparse.Namespace) -> None:
    level = (logging.DEBUG if args.debug
             else logging.WARNING if args.quiet
             else logging.INFO)
    logging.basicConfig(
        level=level,
        format="[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S")


def _make_event_logger(path: str):
    fh = open(path, "a", buffering=1)        # line-buffered: crash-safe

    def on_tag(tag: TagRead, count: int) -> None:
        fh.write(json.dumps({
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "epc": tag.epc,
            "text": tag.text,
            "antenna": tag.antenna,
            "rssi": tag.rssi,
            "count": count,
        }, ensure_ascii=False) + "\n")

    return on_tag, fh


def _probe(reader: InvengoReader, host: str, port: int) -> int:
    power = reader.get_power()
    mode = reader.get_mode()
    print(f"Reader : {host}:{port}")
    print(f"Power  : {power} (per antenna)")
    print(f"Mode   : {mode}"
          + (f" ({reader.mode_name})" if mode is not None else ""))
    return 0


def main(argv: Optional[list] = None) -> int:
    args = _build_parser().parse_args(argv)
    _setup_logging(args)

    port = args.port
    if args.discover_port:
        found = discover_port(args.host)
        if found is None:
            print("No port answered the Invengo handshake.",
                  file=sys.stderr)
            return 2
        port = found

    antennas = tuple(int(a) for a in args.antennas.split(",") if a.strip())
    duration = None if args.duration <= 0 else args.duration
    unattended = duration is None and not args.probe

    tags: dict = {}
    seen: set = set()
    event_log = event_fh = None
    if args.log_file:
        event_log, event_fh = _make_event_logger(args.log_file)

    def on_tag(tag: TagRead) -> None:
        agg = tags.get(tag.epc)
        if agg is None:
            agg = tags[tag.epc] = _TagAggregate(
                epc=tag.epc, text=tag.text, antenna=tag.antenna,
                rssi=tag.rssi, count=1)
        else:
            agg.update(tag)
        if tag.epc not in seen:
            seen.add(tag.epc)
            label = f' text="{tag.text}"' if tag.text else ""
            logger.info("NEW TAG %s%s antenna=%d", tag.epc, label,
                        tag.antenna)
        if event_log is not None:
            event_log(tag, agg.count)

    reader = InvengoReader(args.host, port)
    exit_code = 0
    try:
        while True:
            try:
                reader.connect(wake_retries=args.wake_retries)

                if args.probe:
                    return _probe(reader, args.host, port)

                reader.start_inventory(antennas=antennas, q=args.q,
                                       tid_length=args.tid_length)
                for _tag in reader.read_tags(duration=duration,
                                             on_tag=on_tag,
                                             silent_timeout=args.silent_timeout):
                    pass
            except ConnectionError as e:
                if not unattended:
                    raise
                logger.warning("connection lost: %s", e)
                logger.info("unattended mode: retrying in 15 seconds")
            else:
                break                      # duration elapsed
            finally:
                reader.close()
            time.sleep(15)
    except KeyboardInterrupt:
        print("\nStopped by user.", file=sys.stderr)
        exit_code = 130
    except ConnectionError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        exit_code = 1
    finally:
        if not args.probe:
            _print_summary(reader, tags, args, port)
        if event_fh is not None:
            event_fh.close()
    return exit_code


def _print_summary(reader: InvengoReader, tags: dict,
                   args: argparse.Namespace, port: int) -> None:
    print(f"\n===== {len(tags)} unique tag(s) =====")
    rows = sorted(tags.values(), key=lambda t: -t.rssi)
    print(f"{'EPC':26} {'TEXT':14} {'ANT':>3} {'RSSI':>5} {'COUNT':>5}")
    for t in rows:
        text = t.text or epc_text(t.epc)
        print(f"{t.epc:26} {text:14} {t.antenna:>3} {t.rssi:>5} "
              f"{t.count:>5}")

    if not tags and reader.raw_frames:
        if reader.unknown_count:
            print(f"\nNote: {reader.ack_count} start ACK(s) (normal) and "
                  f"{reader.unknown_count} UNKNOWN frame(s) received.\n"
                  "Unknown frames may be tag reports with a different "
                  "layout; re-run with --debug and report the raw\n"
                  "frames so the parser can be extended.")
        else:
            print(f"\nNote: {reader.ack_count} frame(s) received, all "
                  "start-inventory acknowledgements (normal while no\n"
                  "tag is in range). Tags will appear as trucks pass.")

    if args.json:
        out = {
            "host": args.host, "port": port,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "tags": [vars(t) for t in rows],
            "raw_frames": reader.raw_frames,
        }
        with open(args.json, "w") as fh:
            json.dump(out, fh, indent=2)
        print(f"\nSummary written to {args.json}")


if __name__ == "__main__":
    sys.exit(main())
