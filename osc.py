"""Minimal OSC (Open Sound Control) 1.0 client for the sequencer.

Only what the SuperCollider integration needs: encoding of messages and of
timestamped bundles, a UDP client, and a decoder used by the self-test and to
read replies such as ``/status.reply``. No external packages.

Wire format reminders:

* a message is ``address`` + typetag string + arguments, each string
  NUL-terminated and padded to a multiple of 4 bytes;
* a bundle is ``#bundle\\0`` + an 8-byte NTP time tag + ``int32 size`` prefixed
  elements (messages or nested bundles);
* the time tag ``0x0000000100000000`` means "execute immediately"; anything
  else is "seconds since 1900" as 32.32 fixed point, which is how SuperCollider
  schedules a bundle sample-accurately.

Decoding understands ``i h f d s S b t c r m T F N I`` - in particular the
``d`` doubles that scsynth puts in ``/status.reply``.
"""

import queue
import socket
import struct
import threading
import time

IMMEDIATE = b"\x00\x00\x00\x01\x00\x00\x00\x00"
NTP_EPOCH = 2208988800          # seconds between 1900-01-01 and 1970-01-01
NTP_UNITS = 1 << 32             # fractional second resolution


class OscError(ValueError):
    """Raised for values that cannot be encoded or a malformed packet."""


# --------------------------------------------------------------------------
# encoding
# --------------------------------------------------------------------------
def _padded_string(text):
    raw = text.encode("utf-8") + b"\x00"
    return raw + b"\x00" * (-len(raw) % 4)


def _padded_blob(data):
    return struct.pack(">i", len(data)) + data + b"\x00" * (-len(data) % 4)


def typetag_for(value):
    """OSC type character for a Python value."""
    if isinstance(value, bool):
        return "T" if value else "F"
    if isinstance(value, int):
        # OSC 'i' is 32-bit; bigger integers go as 'h' (64-bit) automatically.
        return "i" if -2**31 <= value < 2**31 else "h"
    if isinstance(value, float):
        return "f"
    if isinstance(value, str):
        return "s"
    if isinstance(value, (bytes, bytearray)):
        return "b"
    if value is None:
        return "N"
    raise OscError(f"cannot send {value!r} over OSC "
                   f"(use int, float, str, bytes, bool or None)")


def encode_message(address, args=()):
    """Bytes of one OSC message."""
    if not isinstance(address, str) or not address.startswith("/"):
        raise OscError(f"an OSC address must start with '/': {address!r}")
    values = list(args)
    tags = "".join(typetag_for(v) for v in values)
    out = [_padded_string(address), _padded_string("," + tags)]
    for value, tag in zip(values, tags):
        if tag == "i":
            out.append(struct.pack(">i", int(value)))
        elif tag == "h":
            out.append(struct.pack(">q", int(value)))
        elif tag == "f":
            out.append(struct.pack(">f", float(value)))
        elif tag == "s":
            out.append(_padded_string(value))
        elif tag == "b":
            out.append(_padded_blob(bytes(value)))
        elif tag in ("T", "F", "N"):
            pass                     # no payload
    return b"".join(out)


def timetag_bytes(unix_seconds=None):
    """8-byte NTP time tag for a Unix timestamp (None = immediately)."""
    if unix_seconds is None:
        return IMMEDIATE
    seconds = unix_seconds + NTP_EPOCH
    whole = int(seconds)
    frac = int((seconds - whole) * NTP_UNITS)
    return struct.pack(">II", whole & 0xFFFFFFFF, frac & 0xFFFFFFFF)


def encode_bundle(elements, unix_seconds=None):
    """Bytes of one OSC bundle holding messages (bytes) or nested elements."""
    out = [_padded_string("#bundle"), timetag_bytes(unix_seconds)]
    for element in elements:
        if isinstance(element, (tuple, list)):      # (address, args)
            element = encode_message(element[0],
                                     element[1] if len(element) > 1 else ())
        out.append(struct.pack(">i", len(element)))
        out.append(element)
    return b"".join(out)


def bundle_time_of(elements_provider):
    """Helper for callers that need 'now + latency' as a Unix timestamp."""
    return time.time()


# --------------------------------------------------------------------------
# decoding (self-test, status replies)
# --------------------------------------------------------------------------
def _read_string(data, i):
    end = data.find(b"\x00", i)
    if end < 0:
        raise OscError("unterminated string in an OSC packet")
    text = data[i:end].decode("utf-8", "replace")
    return text, i + ((end - i) + 1 + 3) // 4 * 4


def ntp_to_unix(whole, frac):
    """Unix seconds from the two halves of an NTP time tag."""
    return (whole - NTP_EPOCH) + frac / NTP_UNITS


def decode(data):
    """Decode one packet -> ('message', address, args) or ('bundle', t, list)."""
    if data.startswith(b"#bundle\x00"):
        whole, frac = struct.unpack(">II", data[8:16])
        # A tag of 0 or 1 (1900-01-01) means "execute immediately"; FoxDot and
        # others write it as (0, 1), the OSC spec uses 1.
        stamps = None if whole <= 1 else ntp_to_unix(whole, frac)
        i = 16
        items = []
        while i < len(data):
            size = struct.unpack(">i", data[i:i + 4])[0]
            i += 4
            items.append(decode(data[i:i + size]))
            i += size
        return ("bundle", stamps, items)
    address, i = _read_string(data, 0)
    tags, i = _read_string(data, i)
    if not tags.startswith(","):
        raise OscError(f"missing typetag string in {address!r}")
    args = []
    for tag in tags[1:]:
        if tag == "i":
            args.append(struct.unpack(">i", data[i:i + 4])[0])
            i += 4
        elif tag == "h":
            args.append(struct.unpack(">q", data[i:i + 8])[0])
            i += 8
        elif tag == "f":
            args.append(round(struct.unpack(">f", data[i:i + 4])[0], 6))
            i += 4
        elif tag == "d":            # scsynth sends doubles in /status.reply
            args.append(round(struct.unpack(">d", data[i:i + 8])[0], 6))
            i += 8
        elif tag in ("s", "S"):     # strings and symbols read the same way
            text, i = _read_string(data, i)
            args.append(text)
        elif tag == "b":
            size = struct.unpack(">i", data[i:i + 4])[0]
            i += 4
            args.append(data[i:i + size])
            i += (size + 3) // 4 * 4
        elif tag == "t":            # an embedded time tag
            whole, frac = struct.unpack(">II", data[i:i + 8])
            i += 8
            args.append(None if whole <= 1 else ntp_to_unix(whole, frac))
        elif tag == "c":
            args.append(chr(struct.unpack(">I", data[i:i + 4])[0]))
            i += 4
        elif tag == "r":
            args.append(struct.unpack(">I", data[i:i + 4])[0])
            i += 4
        elif tag == "m":
            args.append(data[i:i + 4])
            i += 4
        elif tag == "T":
            args.append(True)
        elif tag == "F":
            args.append(False)
        elif tag in ("N", "I"):
            args.append(None)
        else:
            raise OscError(f"unsupported OSC type tag {tag!r}")
    return ("message", address, args)


def describe(packet):
    """Human-readable one-liner for a decoded packet (used by 'dump')."""
    def one(item):
        kind, *rest = item
        if kind == "message":
            address, args = rest
            shown = " ".join(_format_arg(a) for a in args)
            return f"{address} {shown}".rstrip()
        stamps, items = rest
        when = "now" if stamps is None else f"t={stamps:.3f}"
        inner = ", ".join(one(i) for i in items)
        return f"bundle({when}) [{inner}]"

    return one(decode(packet))


def _format_arg(value):
    if isinstance(value, float):
        return f"{value:g}"
    if value is None:
        return "nil"
    return str(value)


# --------------------------------------------------------------------------
# networking
# --------------------------------------------------------------------------
class Client:
    """UDP sender for one OSC destination."""

    def __init__(self, host="127.0.0.1", port=57110):
        self.host = host
        self.port = int(port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sent = 0                # packets sent (for messages/debugging)

    @property
    def target(self):
        return f"{self.host}:{self.port}"

    def send_bytes(self, data):
        self.sock.sendto(data, (self.host, self.port))
        self.sent += 1

    def send_message(self, address, args=()):
        data = encode_message(address, args)
        self.send_bytes(data)
        return data

    def send_bundle(self, elements, unix_seconds=None):
        data = encode_bundle(elements, unix_seconds)
        self.send_bytes(data)
        return data

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class Listener:
    """UDP receiver on an ephemeral port, for replies ('/status.reply')."""

    def __init__(self, host="127.0.0.1"):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((host, 0))
        self.sock.settimeout(0.1)
        self.host, self.port = self.sock.getsockname()
        self._inbox = queue.Queue()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                data, _addr = self.sock.recvfrom(65535)
            except (socket.timeout, OSError):
                continue
            self._inbox.put(data)

    def receive(self, address=None, timeout=1.0):
        """Next decoded packet (optionally matching an address), or None."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                packet = self._inbox.get(timeout=remaining)
            except queue.Empty:
                return None
            decoded = decode(packet)
            if address is None or (decoded[0] == "message"
                                   and decoded[1] == address):
                return decoded

    def close(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=0.5)
