"""Read the suction line pressure from the MPRLS Uno (firmware/pressure-sensor).

The firmware sends `abs_hPa:<value> gauge_hPa:<value>` lines at 5 Hz. Gauge
pressure is relative to the zero that the board measures at startup. Opening
the port resets the Uno, so the zero is taken again each time the service opens it.
"""

import math
import os
import termios
import threading
import time
import tty

PORT = "/dev/serial/by-id/usb-Arduino__www.arduino.cc__0043_1453230313235111B180-if00"

# A reading older than this is stale. The board sends one each 0.2 s.
STALE_SECONDS = 1.0
# Reopen the port after this time without a valid line, or after an error.
SILENT_SECONDS = 5.0
RETRY_SECONDS = 2.0


def parse_line(line):
    """Return (abs_hpa, gauge_hpa) from one sample line, or None for other lines."""
    fields = dict(part.split(":", 1) for part in line.split() if ":" in part)
    try:
        values = float(fields["abs_hPa"]), float(fields["gauge_hPa"])
    except (KeyError, ValueError):
        return None
    return values if all(math.isfinite(value) for value in values) else None


def open_port(path):
    """Open the serial port read-only at 115200 baud. Each read waits at most 1 s."""
    fd = os.open(path, os.O_RDONLY | os.O_NOCTTY)
    try:
        attrs = termios.tcgetattr(fd)
        tty.cfmakeraw(attrs)
        attrs[2] |= termios.CLOCAL | termios.CREAD
        attrs[4] = attrs[5] = termios.B115200
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 10
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        return os.fdopen(fd, "rb", buffering=0)
    except BaseException:
        os.close(fd)
        raise


class PressureReader:
    """Keep the latest reading from the pressure board in a daemon thread."""

    def __init__(self, path=PORT, *, opener=open_port, clock=time.monotonic):
        self._path = path
        self._opener = opener
        self._clock = clock
        self._lock = threading.Lock()
        self._latest = None
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="pressure", daemon=True)
        self._thread.start()
        return self

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def reading(self):
        """Return {"abs_hpa", "gauge_hpa"} from the last 1 s, or None."""
        with self._lock:
            latest = self._latest
        if latest is None or self._clock() - latest[0] > STALE_SECONDS:
            return None
        return {"abs_hpa": latest[1], "gauge_hpa": latest[2]}

    def feed(self, line):
        """Store one line from the board. Return True for a valid sample."""
        values = parse_line(line)
        if values is None:
            return False
        with self._lock:
            self._latest = (self._clock(), *values)
        return True

    def _run(self):
        while not self._stop.is_set():
            try:
                with self._opener(self._path) as port:
                    self._read(port)
            except OSError:
                pass
            self._stop.wait(RETRY_SECONDS)

    def _read(self, port):
        buffer = b""
        last_valid = self._clock()
        while not self._stop.is_set():
            chunk = port.read(256)
            if not chunk:
                # A hung-up port can return at once; do not spin.
                self._stop.wait(0.05)
            buffer += chunk
            *lines, buffer = buffer.split(b"\n")
            for line in lines:
                if self.feed(line.decode("ascii", "replace")):
                    last_valid = self._clock()
            if len(buffer) > 256:
                buffer = b""
            if self._clock() - last_valid > SILENT_SECONDS:
                return
