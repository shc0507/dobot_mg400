"""Measured frame rate of a relayed MJPEG stream, per viewer."""

from collections import deque

# The rate covers the frames sent in the last WINDOW_S.
WINDOW_S = 2.0


class SendRate:
    """Send times of 1 view. The caller holds its own lock."""

    def __init__(self):
        self.times = deque()

    def add(self, now):
        """Count 1 frame sent to 1 viewer at now (monotonic seconds)."""
        self.times.append(now)
        self._trim(now)

    def per_viewer(self, now, viewers):
        """Return the frames per second of 1 viewer, or None without 2 recent frames."""
        self._trim(now)
        if viewers < 1 or len(self.times) < 2 or self.times[-1] <= self.times[0]:
            return None
        return round((len(self.times) - 1) / (self.times[-1] - self.times[0]) / viewers, 1)

    def _trim(self, now):
        while self.times and now - self.times[0] > WINDOW_S:
            self.times.popleft()
