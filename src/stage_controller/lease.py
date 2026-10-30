"""Stage sequence continuity across explicit ownership transitions."""

from labkit.lease import Lease


class StageLease(Lease):
    """Keep a monotonic epoch without changing shared lease acquisition semantics.

    Dispatch and polling read epoch under control(), before the device lock.
    Repeated acquire/renew of a held lease preserves the epoch. Expiry callbacks
    run while new control is unavailable, so their epoch increment cannot race
    an authorized dispatch.
    """

    def __init__(self, *args, on_expire=None, **kwargs):
        self.epoch = 0

        def expired():
            self.epoch += 1
            if on_expire is not None:
                on_expire()

        super().__init__(*args, on_expire=expired, **kwargs)

    def acquire(self, client):
        with self.control(client, require_holder=False):
            before = self.snapshot()["holder"]
            result = super().acquire(client)
            if result["holder"] != before:
                self.epoch += 1
            return result

    def release(self, client):
        with self.control(client, require_holder=False):
            before = self.snapshot()["holder"]
            result = super().release(client)
            if result["holder"] != before:
                self.epoch += 1
            return result
