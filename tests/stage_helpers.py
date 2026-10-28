"""Plain helpers and fakes that the stage panel tests share."""

from serial.tools.list_ports_common import ListPortInfo

from stage_controller.protocol import DemoTransport


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class RecordingTransport(DemoTransport):
    def __init__(self, clock):
        super().__init__(clock)
        self.commands = []
        self.closed = False

    def command(self, command):
        self.commands.append(command)
        return super().command(command)

    def close(self):
        self.closed = True
        super().close()


def serial_port(device, vid=None, pid=None, serial_number=None, description="n/a"):
    """Return a pyserial port listing entry with the given USB identity."""
    port = ListPortInfo(device, skip_link_detection=True)
    port.vid, port.pid, port.serial_number = vid, pid, serial_number
    port.description = description
    return port
