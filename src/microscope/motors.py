"""Microscope serial controller extracted from the desktop application."""

from collections import deque
import queue
import threading
import time

import serial

from .side_capture import SideCaptureCancelled

BAUDRATE = 115200


class MotorController:
    """
    Reusable Python API.

    The GUI below uses this class, but you can also import this file
    and call these methods directly from another Python program.
    """

    def __init__(self):
        self.ser = None
        self.reader_thread = None
        self.stop_reader = threading.Event()
        self.rx_queue = queue.Queue()
        self.send_lock = threading.Lock()
        self.response_condition = threading.Condition()
        self.response_sequence = 0
        self.responses = deque(maxlen=128)

    @property
    def connected(self):
        return self.ser is not None and self.ser.is_open

    def connect(self, port, baudrate=BAUDRATE):
        self.disconnect()

        self.ser = serial.Serial(
            port=port,
            baudrate=baudrate,
            timeout=0.1,
            write_timeout=0.5,
        )

        # Arduino UNO normally resets when serial is opened.
        time.sleep(1.8)
        self.ser.reset_input_buffer()

        self.stop_reader.clear()
        self.reader_thread = threading.Thread(
            target=self._reader_loop,
            daemon=True,
        )
        self.reader_thread.start()

    def disconnect(self):
        self.stop_reader.set()

        if self.reader_thread and self.reader_thread.is_alive():
            self.reader_thread.join(timeout=0.3)

        self.reader_thread = None

        if self.ser is not None:
            try:
                if self.ser.is_open:
                    self.ser.close()
            finally:
                self.ser = None

    def _reader_loop(self):
        while not self.stop_reader.is_set():
            try:
                if not self.connected:
                    break

                raw = self.ser.readline()

                if raw:
                    text = raw.decode("utf-8", errors="replace").strip()
                    if text:
                        with self.response_condition:
                            self.response_sequence += 1
                            self.responses.append((self.response_sequence, text))
                            self.response_condition.notify_all()
                        self.rx_queue.put(text)

            except (serial.SerialException, OSError) as exc:
                self.rx_queue.put(f"[SERIAL ERROR] {exc}")
                break

    def send(self, command, *, cancel_event=None):
        if not self.connected:
            raise RuntimeError("Arduino is not connected.")

        command = command.strip()

        if not command:
            return

        with self.send_lock:
            if cancel_event is not None and cancel_event.is_set():
                raise SideCaptureCancelled("Side capture was stopped")
            self.ser.write((command + "\n").encode("utf-8"))
            self.ser.flush()

    # ---------- Objective turret ----------
    def send_and_wait(self, command, response_prefix, *, timeout=5.0,
                      cancel_event=None):
        """Wait for firmware acknowledgement without consuming the GUI log."""
        deadline = time.monotonic() + timeout
        with self.response_condition:
            sequence = self.response_sequence
            if cancel_event is not None and cancel_event.is_set():
                raise SideCaptureCancelled("Side capture was stopped")
            self.send(command, cancel_event=cancel_event)
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise SideCaptureCancelled("Side capture was stopped")
                if not self.connected:
                    raise RuntimeError("Arduino disconnected during side capture")
                for number, line in self.responses:
                    if number <= sequence:
                        continue
                    if line.startswith(("ERR", "ERROR")):
                        raise RuntimeError(f"{command}: {line}")
                    if line.startswith(response_prefix):
                        return line
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(
                        f"Timed out waiting for {response_prefix!r} after {command!r}"
                    )
                self.response_condition.wait(min(0.1, remaining))

    def objective_enable(self):
        self.send("OBJ ENABLE")

    def objective_disable(self):
        self.send("OBJ DISABLE")

    def objective_move(self, steps):
        self.send(f"OBJ MOVE {int(steps)}")

    def objective_speed(self, steps_per_second):
        self.send(f"OBJ SPEED {int(steps_per_second)}")

    def objective_stop(self):
        self.send("STOP OBJ")

    # ---------- Focus ----------
    def focus_enable(self):
        self.send("FOCUS ENABLE")

    def focus_disable(self):
        self.send("FOCUS DISABLE")

    def focus_move(self, steps):
        self.send(f"FOCUS MOVE {int(steps)}")

    def focus_speed(self, steps_per_second):
        self.send(f"FOCUS SPEED {int(steps_per_second)}")

    def focus_stop(self):
        self.send("STOP FOCUS")

    # ---------- Shared ----------
    def set_microstep(self, microstep):
        microstep = int(microstep)

        if microstep not in (1, 2, 4, 8, 16):
            raise ValueError("Microstep must be 1, 2, 4, 8, or 16.")

        self.send(f"MICRO {microstep}")

    def stop_all(self):
        self.send("STOP ALL")

    def status(self):
        self.send("STATUS")

