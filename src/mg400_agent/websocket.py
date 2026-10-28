"""Speak RFC 6455 WebSocket frames with the standard library.

The robot service serves one small text protocol. No extension or subprotocol is
negotiated, so the reserved frame bits must stay clear.
"""

import base64
import binascii
import collections
import hashlib
import os
import socket
import struct
import threading

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
CONTINUATION, TEXT, BINARY, CLOSE, PING, PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
OPCODES = frozenset({CONTINUATION, TEXT, BINARY, CLOSE, PING, PONG})
MAX_MESSAGE_BYTES = 4096
MAX_CONTROL_BYTES = 125
# Close codes that a peer may send. 1005, 1006 and 1015 must never appear on the wire.
VALID_CLOSE_CODES = frozenset({1000, 1001, 1002, 1003, *range(1007, 1015), *range(3000, 5000)})


class ProtocolError(Exception):
    """The peer broke the protocol. Close the connection with code."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class ConnectionClosed(Exception):
    """A close frame was already sent. Send nothing more."""


def accept_key(key):
    """Return the Sec-WebSocket-Accept value for a client key."""
    digest = hashlib.sha1((key + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def valid_client_key(value):
    """Check that a Sec-WebSocket-Key is base64 for exactly 16 bytes."""
    if not isinstance(value, str) or len(value) != 24 or not value.isascii():
        return False
    try:
        return len(base64.b64decode(value, validate=True)) == 16
    except (binascii.Error, ValueError):
        return False


def _mask(data, key):
    if not data:
        return b""
    repeated = (key * (len(data) // 4 + 1))[: len(data)]
    value = int.from_bytes(data, "big") ^ int.from_bytes(repeated, "big")
    return value.to_bytes(len(data), "big")


def encode_frame(opcode, payload=b"", *, fin=True, mask=False):
    """Encode one frame. A client must mask its frames and a server must not."""
    head = bytearray([(0x80 if fin else 0) | opcode])
    bit = 0x80 if mask else 0
    length = len(payload)
    if length < 126:
        head.append(bit | length)
    elif length < 1 << 16:
        head.append(bit | 126)
        head += struct.pack("!H", length)
    else:
        head.append(bit | 127)
        head += struct.pack("!Q", length)
    if mask:
        key = os.urandom(4)
        head += key
        payload = _mask(payload, key)
    return bytes(head) + payload


def encode_close(code=None, reason=""):
    """Return a close payload. Cut the reason to 123 bytes without splitting a character."""
    if code is None:
        return b""
    data = reason.encode("utf-8")[:123]
    return struct.pack("!H", code) + data.decode("utf-8", "ignore").encode("utf-8")


def exact_reader(stream):
    """Wrap a binary file so that read(n) returns n bytes or raises EOFError."""

    def read(count):
        data = stream.read(count) if count else b""
        if len(data) != count:
            raise EOFError("The connection closed")
        return data

    return read


def read_frame(read, *, masked, max_payload):
    """Read one frame. masked tells whether the peer must mask (the peer is a client)."""
    first, second = read(2)
    fin = bool(first & 0x80)
    if first & 0x70:
        raise ProtocolError(1002, "Reserved frame bits are set")
    opcode = first & 0x0F
    if opcode not in OPCODES:
        raise ProtocolError(1002, "Unknown frame opcode")
    if bool(second & 0x80) != masked:
        raise ProtocolError(
            1002, "Client frames must be masked" if masked else "Server frames must not be masked"
        )
    length = second & 0x7F
    if opcode >= CLOSE and (not fin or length > MAX_CONTROL_BYTES):
        raise ProtocolError(1002, "Control frames must be short and unfragmented")
    if length == 126:
        (length,) = struct.unpack("!H", read(2))
    elif length == 127:
        (length,) = struct.unpack("!Q", read(8))
        if length >> 63:
            raise ProtocolError(1002, "Frame length is invalid")
    if opcode < CLOSE and length > max_payload:
        raise ProtocolError(1009, "Message is too big")
    key = read(4) if masked else None
    payload = read(length)
    return fin, opcode, _mask(payload, key) if key else payload


class MessageReader:
    """Assemble frames into messages. Accept text only, up to max_bytes per message.

    next() returns ("text", str), ("ping", bytes), ("pong", bytes) or
    ("close", code or None, reason).
    """

    def __init__(self, read, *, masked=True, max_bytes=MAX_MESSAGE_BYTES):
        self._read = read
        self._masked = masked
        self._max = max_bytes
        self._parts = None

    def next(self):
        while True:
            buffered = sum(map(len, self._parts)) if self._parts is not None else 0
            fin, opcode, payload = read_frame(
                self._read, masked=self._masked, max_payload=self._max - buffered
            )
            if opcode == PING:
                return "ping", payload
            if opcode == PONG:
                return "pong", payload
            if opcode == CLOSE:
                return ("close", *self._close(payload))
            if opcode == BINARY:
                raise ProtocolError(1003, "Binary messages are not supported")
            if opcode == TEXT:
                if self._parts is not None:
                    raise ProtocolError(1002, "A new message started inside a fragmented one")
                self._parts = [payload]
            elif self._parts is None:
                raise ProtocolError(1002, "A continuation frame has no message to continue")
            else:
                self._parts.append(payload)
            if fin:
                data, self._parts = b"".join(self._parts), None
                try:
                    return "text", data.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise ProtocolError(1007, "Text messages must be valid UTF-8") from error

    @staticmethod
    def _close(payload):
        if not payload:
            return None, ""
        if len(payload) == 1:
            raise ProtocolError(1002, "A close payload needs a two-byte code")
        (code,) = struct.unpack("!H", payload[:2])
        if code not in VALID_CLOSE_CODES:
            raise ProtocolError(1002, "The close code is invalid")
        try:
            return code, payload[2:].decode("utf-8")
        except UnicodeDecodeError as error:
            raise ProtocolError(1007, "The close reason must be valid UTF-8") from error


class Connection:
    """One open WebSocket. Any thread may send; one thread receives.

    Sends only queue a frame. A writer thread owned by the connection writes the queue,
    so a peer that stops reading never blocks a sender. A full queue aborts the
    connection. The server role expects masked frames from the peer and sends unmasked
    frames. The client role does the opposite.
    """

    def __init__(
        self, sock, rfile, wfile, *, client=False, max_bytes=MAX_MESSAGE_BYTES, queue_limit=64
    ):
        self._socket = sock
        self._wfile = wfile
        self._client = client
        self._reader = MessageReader(exact_reader(rfile), masked=not client, max_bytes=max_bytes)
        self._queue_limit = queue_limit
        self._outbox = collections.deque()
        self._changed = threading.Condition()
        self._aborted = False
        self.close_sent = False
        self.close_received = None
        self.close_written = threading.Event()
        self._writer = threading.Thread(target=self._write, name="WebSocket-writer", daemon=True)
        self._writer.start()

    @property
    def backlog(self):
        """Return the number of queued frames that the writer has not written yet."""
        with self._changed:
            return len(self._outbox)

    def send_text(self, text):
        self._queue(TEXT, text.encode("utf-8"))

    def ping(self, payload=b""):
        self._queue(PING, payload)

    def close(self, code=1000, reason=""):
        """Queue one close frame. Later sends raise ConnectionClosed."""
        try:
            self._queue(CLOSE, encode_close(code, reason))
        except ConnectionClosed:
            pass

    def _queue(self, opcode, payload):
        frame = encode_frame(opcode, payload, mask=self._client)
        with self._changed:
            if self.close_sent or self._aborted:
                raise ConnectionClosed("The WebSocket is closing")
            if len(self._outbox) >= self._queue_limit:
                overflow = True
            else:
                overflow = False
                self._outbox.append((opcode, frame))
                if opcode == CLOSE:
                    self.close_sent = True
                self._changed.notify()
        if overflow:
            self.abort()
            raise ConnectionClosed("The peer does not read its messages")

    def _write(self):
        while True:
            with self._changed:
                while not self._outbox and not self._aborted:
                    self._changed.wait()
                if self._aborted:
                    return
                opcode, frame = self._outbox.popleft()
            try:
                self._wfile.write(frame)
                self._wfile.flush()
            except Exception:
                # A partial frame leaves the stream unusable.
                self.abort()
                return
            if opcode == CLOSE:
                self.close_written.set()
                return

    def abort(self):
        """Stop writing and shut the socket down. This also ends a blocked receive."""
        with self._changed:
            self._aborted = True
            self._changed.notify_all()
        try:
            self._socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def join(self, timeout=None):
        """Wait for the writer to end. Call after abort() or after the close was written."""
        self._writer.join(timeout)

    def receive(self):
        """Return the next text message, or None after the peer closes.

        Answer pings and echo the peer's close. Raise ProtocolError, EOFError or OSError
        when the connection breaks.
        """
        while True:
            kind, *rest = self._reader.next()
            if kind == "text":
                return rest[0]
            if kind == "ping":
                try:
                    self._queue(PONG, rest[0])
                except ConnectionClosed:
                    pass
            elif kind == "close":
                code, _reason = rest
                self.close_received = code if code is not None else 1005
                self.close(code)
                return None
