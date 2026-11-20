"""Bounded shared frames and ordered event replay for live and mock Consoles."""

import copy
import json
import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass

from .protocol import GROUPS, PREFIX, immutable


def encoded(value):
    return json.dumps(value, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class Subscription:
    groups: tuple = GROUPS
    rate_hz: int = 2
    selection: str = "active"
    chip_ids: tuple = ()

    def __post_init__(self):
        if not self.groups or any(group not in GROUPS for group in self.groups):
            raise ValueError("Select devices, samples, or operations.")
        if type(self.rate_hz) is not int or self.rate_hz not in (1, 2, 4):
            raise ValueError("Select a rate of 1, 2, or 4 Hz.")
        if self.selection not in ("active", "ids"):
            raise ValueError("Select active or ids.")
        if len(self.chip_ids) > 50 or any(
            not isinstance(value, str) or not value or len(value) > 256 for value in self.chip_ids
        ):
            raise ValueError("Select at most 50 chip IDs of 1 to 256 characters.")
        if self.selection == "active" and self.chip_ids:
            raise ValueError("Chip IDs require selection=ids.")
        if self.selection == "ids" and not self.chip_ids:
            raise ValueError("The ids selection requires chip IDs.")


class Subscriber:
    def __init__(self, hub, subscription, cursor):
        self.hub, self.subscription = hub, subscription
        self.initial = True
        self.closed = False
        self.last_frame = -1
        self.next_frame_at = 0
        self.cursor = hub.event_seq if cursor is None else cursor["event_seq"]
        self.cursor_boot = hub.boot_id if cursor is None else cursor["boot_id"]

    def poll(self):
        """Return one message. An unread state is replaced by the current frame."""
        with self.hub.lock:
            if self.closed:
                return None
            if self.initial:
                self.initial = False
                return self._frame()
            reason = self.hub.gap_reason(self.cursor_boot, self.cursor)
            if reason:
                previous = self.cursor
                self.cursor, self.cursor_boot = self.hub.event_seq, self.hub.boot_id
                self.last_frame = -1
                return {
                    "type": "resync",
                    "reason": reason,
                    "boot_id": self.hub.boot_id,
                    "after": previous,
                    "event_seq": self.cursor,
                    "contract_id": self.hub.contract_id,
                }
            for event, _size in self.hub.events:
                if event["event_seq"] > self.cursor:
                    self.cursor = event["event_seq"]
                    return copy.deepcopy(event)
            if self.last_frame != self.hub.seq and self.hub.clock() >= self.next_frame_at:
                return self._frame()
            return None

    def _frame(self):
        frame = self.hub.snapshot(self.subscription)
        self.last_frame = frame["seq"]
        self.next_frame_at = self.hub.clock() + 1 / self.subscription.rate_hz
        return frame

    def close(self):
        with self.hub.lock:
            self.closed = True
            self.hub.subscribers.discard(self)


class BroadcastHub:
    """One producer supplies every reader. No subscriber polls hardware."""

    def __init__(
        self,
        provider,
        *,
        contract_bundle=None,
        clock=time.monotonic,
        wall_clock=time.time,
        boot_id=None,
        server_release="console",
        max_events=256,
        max_event_bytes=262144,
        max_subscribers=32,
        max_contracts=8,
        max_frame_bytes=262144,
    ):
        if min(max_events, max_event_bytes, max_subscribers, max_contracts, max_frame_bytes) < 1:
            raise ValueError("Stream bounds must be positive.")
        self.provider, self.clock, self.wall_clock = provider, clock, wall_clock
        self.boot_id = boot_id or uuid.uuid4().hex
        self.server_release = server_release
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.stopping = threading.Event()
        self.worker = None
        self.max_events, self.max_event_bytes = max_events, max_event_bytes
        self.max_subscribers, self.max_contracts = max_subscribers, max_contracts
        self.max_frame_bytes = max_frame_bytes
        self.contract_id, contract = immutable(contract_bundle)
        self.contracts = OrderedDict([(self.contract_id, contract)])
        self.events = deque()
        self.event_bytes = self.event_seq = self.seq = 0
        self.subscribers = set()
        self.sample_cache = OrderedDict()
        self.frame = {}
        self.tick()

    def tick(self):
        """Assemble once at 4 Hz. Subscriber rates select these shared frames."""
        data = copy.deepcopy(self.provider())
        with self.changed:
            frame = {
                **data,
                "type": "state",
                "boot_id": self.boot_id,
                "contract_id": self.contract_id,
                "seq": self.seq + 1,
                "generated_at": self.wall_clock(),
                "event_seq": self.event_seq,
            }
            if len(encoded(frame).encode()) > self.max_frame_bytes:
                raise ValueError("The state frame exceeds the byte limit.")
            previous = self.frame
            self.seq += 1
            self.frame = frame
            self._device_events(previous, frame)
            self.sample_cache.clear()
            self.changed.notify_all()

    def _device_events(self, previous, current):
        """Compare cached fields. Missing fields do not mean a fault was cleared."""
        if not previous:
            return
        old_devices = previous.get("devices", {})
        for name, row in current.get("devices", {}).items():
            old = old_devices.get(name, {})
            values, old_values = row.get("values", {}), old.get("values", {})
            instance = row.get("instance")
            context = {
                "device": name,
                "instance": instance,
                "previous_instance": old.get("instance"),
                "state_seq": current["seq"],
                "state_at": row.get("state_at"),
                "observed_at": row.get("observed_at"),
                "quality": row.get("quality"),
            }
            instance_changed = instance != old.get("instance")
            observation = row.get("observations") or {}
            old_observation = old.get("observations") or {}

            def identity(value):
                return (value.get("instance"), value.get("job_id"), value.get("frame_sequence"))

            if observation.get("available") and identity(observation) != identity(old_observation):
                self.publish("observation.updated", {**context, "observations": observation})
            if "fault" in values and (
                "fault" not in old_values
                or values["fault"] != old_values["fault"]
                or instance_changed
            ):
                self.publish("device.fault.changed", {**context, "fault": values["fault"]})
            command = values.get("last_command")
            if command is not None and (
                command != old_values.get("last_command") or instance_changed
            ):
                self.publish("operation.result", {**context, "last_command": command})

    def expire_history(self):
        """Remove replay rows without changing the boot identity or event cursor."""
        with self.changed:
            self.events.clear()
            self.event_bytes = 0
            self.changed.notify_all()

    def start(self):
        with self.lock:
            if self.worker is None and not self.stopping.is_set():
                self.worker = threading.Thread(target=self._run, name="agent-stream", daemon=True)
                self.worker.start()
        return self

    def _run(self):
        while not self.stopping.wait(0.25):
            try:
                self.tick()
            except Exception:
                # A provider failure must not publish an old reading as a new reading.
                continue

    def close(self):
        self.stopping.set()
        with self.changed:
            for subscriber in tuple(self.subscribers):
                subscriber.close()
            self.changed.notify_all()
        if self.worker and self.worker is not threading.current_thread():
            self.worker.join(2)

    def restart(self):
        """Reset process identity and history for deterministic mock restart tests."""
        with self.changed:
            self.boot_id = uuid.uuid4().hex
            self.seq = self.event_seq = self.event_bytes = 0
            self.events.clear()
            self.frame = {}
            self.contracts = OrderedDict([(self.contract_id, self.contracts[self.contract_id])])
            self.tick()
            self.changed.notify_all()
        return self.boot_id

    def discovery(self, prefix=PREFIX):
        with self.lock:
            return {
                "discovery_version": 1,
                "mode": self.frame.get("mode", "live"),
                "server_release": self.server_release,
                "boot_id": self.boot_id,
                "contract_version": self.contracts[self.contract_id]["version"],
                "contract_id": self.contract_id,
                "contract_url": prefix + "/contracts/" + self.contract_id[7:] + ".json",
                "endpoints": {
                    name: prefix + "/v1/" + name for name in ("state", "samples", "stream")
                },
                "read_only": True,
                "manual_reports": False,
            }

    def contract(self, contract_id):
        with self.lock:
            value = self.contracts.get(contract_id)
            return copy.deepcopy(value) if value is not None else None

    def samples(
        self, *, selection="active", chip_ids=(), limit=100, offset=0, expected_revision=None
    ):
        if selection not in ("active", "ids", "all"):
            raise ValueError("Select active, ids, or all.")
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ValueError("Use a limit from 1 to 100 and a nonnegative offset.")
        if selection != "all":
            Subscription(selection=selection, chip_ids=tuple(chip_ids))
        elif chip_ids:
            raise ValueError("Chip IDs require selection=ids.")
        if expected_revision is not None and (
            type(expected_revision) is not int or expected_revision < 0
        ):
            raise ValueError("Use a nonnegative sample revision.")
        key = (selection, tuple(chip_ids), limit, offset, expected_revision)
        with self.lock:
            if key not in self.sample_cache:
                method = getattr(self.provider, "samples", None)
                kwargs = (
                    {} if expected_revision is None else {"expected_revision": expected_revision}
                )
                page = (
                    method(
                        selection=selection, chip_ids=chip_ids, limit=limit, offset=offset, **kwargs
                    )
                    if method
                    else {
                        "items": [],
                        "count": 0,
                        "total": 0,
                        "complete": True,
                        "selection": selection,
                        "next_offset": None,
                    }
                )
                if len(encoded(page).encode()) > self.max_frame_bytes:
                    raise ValueError("The samples page exceeds the byte limit.")
                self.sample_cache[key] = copy.deepcopy(page)
                while len(self.sample_cache) > self.max_subscribers:
                    self.sample_cache.popitem(last=False)
            return copy.deepcopy(self.sample_cache[key])

    def snapshot(self, subscription=None):
        subscription = subscription or Subscription()
        with self.lock:
            frame = {
                key: copy.deepcopy(value)
                for key, value in self.frame.items()
                if key not in GROUPS or key in subscription.groups
            }
            frame["event_seq"] = self.event_seq
            if "samples" in subscription.groups:
                frame["samples"] = self.samples(
                    selection=subscription.selection, chip_ids=subscription.chip_ids
                )
            if len(encoded(frame).encode()) > self.max_frame_bytes:
                raise ValueError("The selected frame exceeds the byte limit.")
            return frame

    def subscribe(self, subscription=None, cursor=None):
        with self.lock:
            if self.stopping.is_set() or len(self.subscribers) >= self.max_subscribers:
                raise ValueError("The stream has no available subscriber slot.")
            if cursor is not None and (
                not isinstance(cursor, dict)
                or not isinstance(cursor.get("boot_id"), str)
                or type(cursor.get("event_seq")) is not int
                or cursor["event_seq"] < 0
            ):
                raise ValueError("A cursor requires boot_id and a nonnegative event_seq.")
            subscriber = Subscriber(self, subscription or Subscription(), cursor)
            self.subscribers.add(subscriber)
            return subscriber

    def gap_reason(self, boot_id, seq):
        if boot_id != self.boot_id:
            return "boot_changed"
        if seq > self.event_seq:
            return "cursor_ahead"
        oldest = self.events[0][0]["event_seq"] if self.events else self.event_seq + 1
        return "history_evicted" if seq < oldest - 1 else None

    def publish(self, kind, data):
        with self.changed:
            event = {
                "type": "event",
                "boot_id": self.boot_id,
                "contract_id": self.contract_id,
                "event_seq": self.event_seq + 1,
                "kind": kind,
                "data": copy.deepcopy(data),
                "generated_at": self.wall_clock(),
            }
            size = len(encoded(event).encode())
            if size > self.max_event_bytes:
                raise ValueError("The event exceeds the replay byte limit.")
            self.event_seq += 1
            self.events.append((event, size))
            self.event_bytes += size
            self._trim()
            self.changed.notify_all()
            return copy.deepcopy(event)

    def _trim(self):
        invalid = [
            event["event_seq"]
            for event, _size in self.events
            if event["contract_id"] not in self.contracts
        ]
        cutoff = max(invalid, default=0)
        while self.events and (
            len(self.events) > self.max_events
            or self.event_bytes > self.max_event_bytes
            or self.events[0][0]["event_seq"] <= cutoff
        ):
            _event, size = self.events.popleft()
            self.event_bytes -= size

    def change_contract(self, value):
        contract_id, contract = immutable(value)
        with self.changed:
            if contract_id == self.contract_id:
                return contract_id
            previous = self.contract_id
            self.contract_id = contract_id
            self.contracts[contract_id] = contract
            self.contracts.move_to_end(contract_id)
            while len(self.contracts) > self.max_contracts:
                self.contracts.popitem(last=False)
            self._trim()
            # Bind the current snapshot before notifying readers of a new definition.
            self.frame = {**self.frame, "contract_id": contract_id}
            self.publish(
                "contract.changed",
                {
                    "previous_contract_id": previous,
                    "contract_id": contract_id,
                    "discovery_url": PREFIX + "/discovery",
                },
            )
            return contract_id
