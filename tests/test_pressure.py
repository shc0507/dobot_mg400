import io
import threading

from mg400.pressure import PressureReader, parse_line


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_parse_line_reads_sample_lines_only():
    assert parse_line("abs_hPa:1009.27 gauge_hPa:-0.08") == (1009.27, -0.08)
    for line in (
        "VERSION pressure-sensor 1.0.0",
        "ZERO 1009.27",
        "ERR READ 0x44 NOT_POWERED",
        "abs_hPa:nan gauge_hPa:0.00",
        "abs_hPa:1009.2",
        "\x00\xfe garbage",
    ):
        assert parse_line(line) is None


def test_reading_is_the_last_sample_until_it_is_stale():
    clock = Clock()
    reader = PressureReader("/dev/null", clock=clock)
    assert reader.reading() is None
    assert not reader.feed("ZERO 1009.27")
    assert reader.feed("abs_hPa:960.00 gauge_hPa:-49.50")
    assert reader.reading() == {"abs_hpa": 960.0, "gauge_hpa": -49.5}
    clock.now += 1.01
    assert reader.reading() is None


def test_thread_reads_split_lines_from_the_port():
    seen = threading.Event()
    chunks = [b"\xffVERSION pressure-sensor 1.0.0\nabs_hPa:1009.2", b"0 gauge_hPa:0.12\n"]

    class Port(io.RawIOBase):
        def read(self, size=-1):
            if chunks:
                return chunks.pop(0)
            seen.set()
            return b""

    reader = PressureReader("port", opener=lambda path: Port()).start()
    try:
        assert seen.wait(2)
        assert reader.reading() == {"abs_hpa": 1009.2, "gauge_hpa": 0.12}
    finally:
        reader.close()


def test_open_errors_retry_without_raising():
    calls = threading.Event()

    def opener(path):
        calls.set()
        raise FileNotFoundError(path)

    reader = PressureReader("missing", opener=opener).start()
    try:
        assert calls.wait(2)
        assert reader.reading() is None
    finally:
        reader.close()
