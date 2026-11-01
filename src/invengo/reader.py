"""TCP client for Invengo fixed RFID readers.

The :class:`InvengoReader` client implements the practical side of talking
to an Invengo XC/RC-RF850-family reader:

* **Wake-up connect** — the reader serves a single TCP client at a time
  and may ignore new connections while a stale session lingers (this is
  why the vendor demo app sometimes had to "prime" the reader with a
  connect/read/disconnect cycle).  :meth:`InvengoReader.connect` replays
  that cycle automatically until the reader answers a ping.
* **Watchdog** — while inventory runs, silence longer than
  ``silent_timeout`` triggers an automatic reconnect; the timeout backs
  off (x1.5 up to 60 s) while the field stays empty and resets as soon
  as a tag is read, so a gate can be monitored unattended for days.
* **Clean disconnect** — inventory is always stopped and the socket
  closed properly, which frees the reader's single session for the next
  connection.
"""

from __future__ import annotations

import logging
import socket
import time
from collections import deque
from typing import Callable, Iterator, Optional, Tuple

from .protocol import (
    CANDIDATE_PORTS,
    CMD_GET_MODE,
    CMD_GET_POWER,
    CMD_PING,
    CMD_SET_MODE,
    CMD_SET_POWER,
    CMD_START_INVENTORY,
    CMD_STOP_INVENTORY,
    MODE_NAMES,
    FrameParser,
    TagRead,
    build_frame,
    parse_tag_report,
)

__all__ = ["InvengoReader", "discover_port"]

OnTagCallback = Callable[[TagRead], None]


def _now() -> float:
    return time.time()


class InvengoReader:
    """
    Talk to one Invengo reader over TCP.

    Basic usage::

        from invengo import InvengoReader

        with InvengoReader("192.168.1.100") as reader:
            reader.start_inventory()
            for tag in reader.read_tags(duration=10):
                print(tag.epc, tag.text, tag.antenna, tag.rssi)

    :param host: reader IP address or hostname.
    :param port: reader TCP port (default 7086, as found on RC-XF850).
    :param timeout: socket timeout for command/response exchanges, seconds.
    """

    def __init__(self, host: str, port: int = 7086, timeout: float = 2.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None
        self.parser = FrameParser()
        self._pending: deque = deque()
        self.inventory_running = False
        self._scan_params: dict = {}
        self._started_at = 0.0
        # Diagnostics: hex of the last non-heartbeat frames received,
        # plus counters for start acknowledgements and unknown frames.
        self.raw_frames: list = []
        self.ack_count = 0
        self.unknown_count = 0
        self.log = logging.getLogger("invengo.reader")

    # ------------------------------------------------------------------
    # Low level I/O
    # ------------------------------------------------------------------
    def _tcp_connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port),
                                             timeout=5.0)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self.sock.settimeout(self.timeout)
        self.parser = FrameParser()
        self._pending.clear()

    def _send(self, payload: bytes) -> None:
        if self.sock is None:
            raise ConnectionError("not connected")
        self.sock.sendall(build_frame(payload))

    def _flush(self) -> None:
        self.parser = FrameParser()
        self._pending.clear()
        if self.sock is None:
            return
        try:
            self.sock.settimeout(0.05)
            while True:
                if not self.sock.recv(4096):
                    break
        except (socket.timeout, OSError):
            pass
        finally:
            self.sock.settimeout(self.timeout)

    def _recv_frame(self, timeout: Optional[float] = None) -> Optional[bytes]:
        """Receive one valid frame payload; ``None`` on timeout."""
        if self.sock is None:
            return None
        deadline = _now() + (timeout or self.timeout)
        while True:
            if self._pending:
                return self._pending.popleft()
            remain = deadline - _now()
            if remain <= 0:
                return None
            self.sock.settimeout(min(remain, 1.0))
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return None
            if not chunk:
                return None          # peer closed the connection
            self._pending.extend(self.parser.feed(chunk))

    def _command(self, payload: bytes, timeout: float = 1.0) -> Optional[bytes]:
        """Send a command and return its response payload (or None)."""
        self._send(payload)
        return self._recv_frame(timeout)

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------
    def _handshake(self) -> bool:
        """Ping must be answered; the follow-up mode command is advisory."""
        self._flush()
        r = self._command(CMD_PING, timeout=1.5)
        if not (r and len(r) >= 4 and r[0] == 0xD2 and r[1] == 0x00
                and r[2] == 0x00 and r[3] == 0x00):
            return False
        r2 = self._command(CMD_SET_MODE + b"\x00", timeout=1.0)
        if r2 is None or r2[0] != 0x62:
            self.log.debug("set-mode command not answered "
                           "(ping already OK, continuing)")
        return True

    def connect(self, wake_retries: int = 6, delay: float = 1.0) -> None:
        """
        Connect and wake the reader up.

        Repeats a connect -> ping -> disconnect cycle until the reader
        answers.  This replaces the manual "prime it with the vendor
        app" workaround: it also takes over the reader's single TCP
        session when a previous client left it half-open.

        :param wake_retries: maximum number of cycles before giving up
            (raises :class:`ConnectionError`).
        :param delay: base delay between cycles, grows linearly.
        """
        last_error: Optional[Exception] = None
        for attempt in range(1, wake_retries + 1):
            self.log.info("connecting to %s:%s (attempt %d/%d)",
                          self.host, self.port, attempt, wake_retries)
            try:
                self._tcp_connect()
                if self._handshake():
                    self.log.info("reader answered handshake, ready")
                    return
                self.log.info("connected but reader is silent "
                              "(hibernating or stale session), retrying")
                self._close_socket()
            except OSError as e:
                last_error = e
                self.log.info("connect failed: %s", e)
            time.sleep(min(delay * attempt, 5.0))
        raise ConnectionError(
            f"Reader {self.host}:{self.port} did not answer after "
            f"{wake_retries} attempts. Check power/network, try "
            f"discover_port() to find the right port, or disable "
            f"'smart hibernation' in the vendor demo tool."
            + (f" Last error: {last_error}" if last_error else ""))

    def _close_socket(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        self.parser = FrameParser()
        self._pending.clear()
        self.inventory_running = False

    def close(self) -> None:
        """Stop inventory and close the socket cleanly.

        Always call this (or use the reader as a context manager) so the
        reader's single-client session is released immediately.
        """
        if self.sock is not None and self.inventory_running:
            try:
                self.stop_inventory()
            except OSError:
                pass
        self._close_socket()
        self.log.info("connection closed cleanly")

    def __enter__(self) -> "InvengoReader":
        if self.sock is None:
            self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Configuration commands
    # ------------------------------------------------------------------
    def get_power(self) -> Optional[list]:
        """Per-antenna RF power in dBm (antenna 1..4), or None on error."""
        r = self._command(CMD_GET_POWER)
        if r and len(r) >= 3 and r[0] == 0x64 and r[1] == 0x00:
            return list(r[2:])
        return None

    def set_power(self, antenna: int, dbm: int) -> bool:
        """Set RF power (dBm) for one antenna (1-based)."""
        r = self._command(bytes([0x64, 0x31, antenna, dbm]))
        return bool(r and r[:2] == b"\x64\x00")

    def get_mode(self) -> Optional[int]:
        """Reader dense-reader mode code (see ``MODE_NAMES``)."""
        r = self._command(CMD_GET_MODE)
        if r and len(r) >= 3 and r[0] == 0x62 and r[1] == 0x00:
            return r[2]
        return None

    @property
    def mode_name(self) -> Optional[str]:
        code = self.get_mode()
        return MODE_NAMES.get(code) if code is not None else None

    # ------------------------------------------------------------------
    # Inventory
    # ------------------------------------------------------------------
    def start_inventory(self, antennas: Tuple[int, ...] = (1,), q: int = 4,
                        looping: bool = True, tid_length: int = 0,
                        user_start: int = 0, user_length: int = 0) -> None:
        """
        Start reading tags.

        :param antennas: antenna numbers to activate, e.g. ``(1, 2)``.
        :param q: Gen2 Q parameter (0-15); 4 is a good default.
        :param looping: keep reading until stopped (recommended).
        :param tid_length: number of TID bytes to also read (0 = none).
        :param user_start: start address of user memory to read.
        :param user_length: number of user-memory bytes to read.
        """
        mask = 0x80
        for a in antennas:
            mask |= 1 << (a - 1)
        payload = bytes([CMD_START_INVENTORY, mask, q,
                         1 if looping else 0, tid_length, user_start,
                         user_length])
        self._send(payload)
        self.inventory_running = True
        self._started_at = _now()
        self._scan_params = dict(antennas=antennas, q=q, looping=looping,
                                 tid_length=tid_length,
                                 user_start=user_start,
                                 user_length=user_length)
        self.log.info("inventory started (antennas=%s, Q=%d)",
                      list(antennas), q)

    def stop_inventory(self) -> bool:
        """Stop the running inventory. Returns True when acknowledged."""
        self.inventory_running = False
        try:
            r = self._command(CMD_STOP_INVENTORY, timeout=1.0)
        except OSError:
            return False
        return bool(r and r[:1] == b"\x98")

    def read_tags(self, duration: Optional[float] = None,
                  on_tag: Optional[OnTagCallback] = None,
                  silent_timeout: float = 8.0) -> Iterator[TagRead]:
        """
        Yield :class:`TagRead` objects as the reader reports tags.

        Blocks for *duration* seconds (``None`` = forever, until the
        caller stops iterating).  A watchdog reconnects automatically
        when the reader goes silent; the silence threshold backs off
        x1.5 (capped at 60 s) while no tags show up and resets as soon
        as a tag is read.  Inventory is stopped when the generator is
        exhausted or closed.

        :param duration: seconds to read (None = indefinitely).
        :param on_tag: optional callback invoked with each tag, in
            addition to yielding it.
        :param silent_timeout: seconds of silence before a reconnect.
        """
        cur_silent = max(silent_timeout, 2.0)
        last_frame = _now()
        end = None if duration is None else _now() + duration
        try:
            while end is None or _now() < end:
                frame = self._recv_frame(1.0)
                now = _now()
                if frame is not None:
                    last_frame = now
                    self.log.debug("frame: %s", frame.hex().upper())
                    if len(frame) == 2:              # heartbeat
                        continue
                    if frame[0] == 0xE5:             # scan-cycle notification
                        continue
                    self._remember_frame(frame)
                    if self._is_start_ack(frame, now):
                        self.ack_count += 1
                        continue
                    tag = parse_tag_report(frame)
                    if tag is None:
                        self.unknown_count += 1
                        continue
                    cur_silent = max(silent_timeout, 2.0)
                    if on_tag is not None:
                        on_tag(tag)
                    yield tag
                elif self.inventory_running and now - last_frame > cur_silent:
                    self.log.info("no frames for %.0fs, reconnecting",
                                  cur_silent)
                    self._recover()
                    last_frame = _now()
                    cur_silent = min(cur_silent * 1.5, 60.0)
        finally:
            if self.inventory_running:
                try:
                    self.stop_inventory()
                except OSError:
                    pass

    def _remember_frame(self, frame: bytes) -> None:
        self.raw_frames.append(frame.hex().upper())
        del self.raw_frames[:-1000]      # keep the last 1000 frames

    def _is_start_ack(self, frame: bytes, now: float) -> bool:
        """The firmware replies to start-inventory with an ACK carrying a
        timestamp (`97 15 ...`); it arrives within 1 s of the command and
        is not a tag report."""
        return (len(frame) == 10 and frame[0] == 0x97 and frame[1] == 0x15
                and now - self._started_at < 1.0)

    def _recover(self) -> None:
        """Reconnect after silence and restart inventory with the same
        parameters."""
        params = self._scan_params
        try:
            self.stop_inventory()
        except OSError:
            pass
        self._close_socket()
        time.sleep(1.0)
        self.connect()
        self.start_inventory(**params)


def discover_port(host: str, ports: Optional[list] = None,
                  timeout: float = 1.5) -> Optional[int]:
    """
    Probe common TCP ports and return the first one where an Invengo
    reader answers the ping handshake, or None.
    """
    for p in (ports or CANDIDATE_PORTS):
        logging.getLogger("invengo.reader").info("probing %s:%s ...", host, p)
        reader = InvengoReader(host, p)
        try:
            reader._tcp_connect()
            if reader._handshake():
                return p
        except OSError:
            pass
        finally:
            reader._close_socket()
    return None
