"""Lab Console server state: browser-held control, the command relay and merged events.

The browser names a page identity in
``X-MG400-Client``; each Take control creates a fresh service identity for one
device. A browser heartbeat drives every renewal. Without a heartbeat for
``BEAT_TIMEOUT_S`` the console releases the lease. Commands and stops are sent
once and never retried.

A stage hold runs on the console: a worker sends ``hold`` once, a ``keepalive``
every ``HOLD_KEEPALIVE_S`` while the page refreshes the hold within
``HOLD_DEADMAN_S``, then ``release_hold`` once.
"""

import copy
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from labkit.errors import ServiceError

from ..clients import SERVICES

BEAT_TIMEOUT_S = 3.0
WATCH_INTERVAL_S = 0.2
EVENT_LIMIT = 500
EVENT_REFRESH_S = 1.0
# Lease operations have their own routes. The relay never forwards them.
LEASE_ACTIONS = frozenset({"start_sync", "stop_sync", "heartbeat", "renew", "release"})
# Stage hold: the stage firmware stops a hold 1 s after the last keepalive.
HOLD_DEADMAN_S = 0.5
HOLD_KEEPALIVE_S = 0.2
HOLD_JOIN_S = 2.0
HOLD_ENDED = "Hold has ended. Release and press again."


def _status(error):
    status = getattr(error, "status", None)
    return status if type(status) is int else 0


def classify(error):
    """Return refused for an explicit service refusal; everything else is unknown."""
    status = _status(error)
    if isinstance(error, (ValueError, ServiceError)) and 400 <= status < 500:
        return "refused"
    if type(error) is ValueError:
        return "refused"
    return "unknown"


def deployed_commit(settings, root=None):
    """Read the commit without a subprocess: a setting, then the checkout's HEAD."""
    value = settings.get("MG400_CONSOLE_COMMIT")
    if isinstance(value, str) and value.strip():
        return value.strip()[:40]
    root = Path(root or Path(__file__).resolve().parents[3]) / ".git"
    try:
        head = (root / "HEAD").read_text(encoding="ascii").strip()
        if head.startswith("ref: "):
            ref = head[5:]
            path = root / ref
            if path.is_file():
                head = path.read_text(encoding="ascii").strip()
            else:
                packed = (root / "packed-refs").read_text(encoding="ascii").splitlines()
                head = next(line.split()[0] for line in packed if line.endswith(" " + ref))
        if len(head) >= 7 and all(char in "0123456789abcdef" for char in head):
            return head[:7]
    except (OSError, StopIteration, UnicodeError):
        pass
    return "unknown"


class Session:
    """One console-held lease for one device, bound to one browser page."""

    def __init__(self, device, page, identity, adapter, commands, now):
        self.device, self.page, self.identity = device, page, identity
        self.adapter, self.commands = adapter, commands
        self.beat = self.started = now
        self.state = "held"
        self.renewing = False
        self.message = None
        # Stage only: the latest hold and the identities of ended holds.
        self.hold = None
        self.closed = deque(maxlen=32)


class Hold:
    """One stage hold of one page. The page refreshes it; a console worker sends it."""

    def __init__(self, hold_id, motor, direction, speed, now):
        self.id, self.motor, self.direction, self.speed = hold_id, motor, direction, speed
        self.seen = now
        self.released = self.ended = False
        self.outcome = self.message = None
        self.wake = threading.Event()
        self.worker: threading.Thread | None = None


class Console:
    def __init__(self, service, *, clock=time.monotonic):
        self.service, self.clock = service, clock
        self.lock = threading.RLock()
        self.sessions = {}
        self.reserved = set()
        self.closing = False
        self.watcher = None
        self.threads = set()
        self.events = deque(maxlen=EVENT_LIMIT)
        self.cursors = {}
        self.event_errors = {}
        self.raw = {}
        self.events_at = None
        self.events_busy = False
        self.commit = deployed_commit(service.settings)

    # Threads ---------------------------------------------------------------

    def _spawn(self, function, name):
        def work():
            try:
                function()
            finally:
                with self.lock:
                    self.threads.discard(threading.current_thread())

        worker = threading.Thread(target=work, name="console-" + name, daemon=True)
        with self.lock:
            self.threads.add(worker)
        try:
            worker.start()
        except Exception:
            with self.lock:
                self.threads.discard(worker)
            raise
        return worker

    def _watch(self):
        while True:
            with self.lock:
                if self.closing:
                    return
                now = self.clock()
                late = [
                    item
                    for item in self.sessions.values()
                    if item.state == "held" and now - item.beat > BEAT_TIMEOUT_S
                ]
                for item in late:
                    item.state = "releasing"
                    item.message = "No browser heartbeat for 3 s."
            for item in late:
                self._end(item, release=True)
            time.sleep(WATCH_INTERVAL_S)

    def _ensure_watcher(self):
        with self.lock:
            if self.watcher is None and not self.closing:
                self.watcher = self._spawn(self._watch, "watch")

    # Clients ---------------------------------------------------------------

    def _client(self, device, purpose):
        if device not in SERVICES:
            raise ServiceError(404, "Unknown console device.")
        identity = f"console-{purpose}-{uuid.uuid4().hex}"
        adapter = self.service.console_client(device, identity)
        if adapter is None:
            raise ServiceError(
                409, "The service is not configured or its owned demo is not running."
            )
        return identity, adapter

    @staticmethod
    def _close(adapter):
        try:
            adapter.close()
        except Exception:
            pass

    def _end(self, item, *, release):
        """Release once, close the transport and forget the session. Never reacquire."""
        # Let the hold worker send release_hold while the lease is still held.
        with self.lock:
            hold = item.hold
            worker = hold.worker if hold is not None and not hold.ended else None
            if worker is not None:
                hold.released = True
                hold.wake.set()
        if worker is not None and worker is not threading.current_thread():
            worker.join(HOLD_JOIN_S)
        error = None
        if release:
            try:
                item.adapter.release()
            except Exception as exc:
                error = exc
        self._close(item.adapter)
        with self.lock:
            if self.sessions.get(item.device) is item:
                del self.sessions[item.device]
        return error

    # Control ---------------------------------------------------------------

    def entry(self, item, page=None):
        return {
            "device": item.device,
            "holder": item.identity,
            "mine": page is not None and item.page == page,
            "state": item.state,
            "message": item.message,
            "held_s": round(max(0.0, self.clock() - item.started), 1),
        }

    def control(self, page=None):
        with self.lock:
            return [self.entry(item, page) for item in self.sessions.values()]

    def take(self, device, page):
        with self.lock:
            if self.closing:
                raise ServiceError(409, "The console is shutting down.")
            current = self.sessions.get(device)
            if current is not None:
                if current.page == page and current.state == "held":
                    current.beat = self.clock()
                    return self.entry(current, page)
                raise ServiceError(409, "Another console page controls this device.")
            if device in self.reserved:
                raise ServiceError(409, "Take control is already in progress for this device.")
            # Reserve the device before any network call.
            self.reserved.add(device)
        adapter = None
        try:
            if not self.service.dashboard_configured(device):
                self.service.console_start_demo(device)
            identity, adapter = self._client(device, "control")
            description = adapter.describe()
            commands = description.get("commands") if isinstance(description, dict) else None
            if not isinstance(commands, dict):
                raise ServiceError(502, "The service describes no commands.")
            try:
                adapter.acquire()
            except Exception as exc:
                if classify(exc) == "unknown":
                    # An ambiguous acquisition may have granted this fresh identity.
                    try:
                        adapter.release()
                    except Exception:
                        pass
                raise
            state = adapter.state()
            if not adapter.owned(state):
                raise ServiceError(409, "The service did not grant control to the console.")
        except BaseException as exc:
            with self.lock:
                self.reserved.discard(device)
            if adapter is not None:
                self._close(adapter)
            if classify(exc) == "refused":
                raise
            if isinstance(exc, Exception):
                raise ServiceError(
                    502, "Take control outcome is unknown. Check the device holder."
                ) from exc
            raise
        with self.lock:
            item = Session(device, page, identity, adapter, copy.deepcopy(commands), self.clock())
            self.reserved.discard(device)
            self.sessions[device] = item
            closing = self.closing
        if closing:
            item.state = "releasing"
            self._end(item, release=True)
            raise ServiceError(409, "The console is shutting down.")
        self.service.events.add("info", "Console took control.", device=device)
        self._ensure_watcher()
        return self.entry(item, page)

    def _owned(self, device, page):
        with self.lock:
            item = self.sessions.get(device)
            if item is None or item.page != page or item.state != "held":
                return None
            return item

    def release(self, device, page):
        with self.lock:
            item = self.sessions.get(device)
            if item is None:
                return {"device": device, "result": "not-held"}
            if item.page != page:
                raise ServiceError(409, "Another console page controls this device.")
            if item.state != "held":
                return {"device": device, "result": item.state}
            item.state = "releasing"
        error = self._end(item, release=True)
        self.service.events.add("info", "Console released control.", device=device)
        return {"device": device, "result": "released" if error is None else "unconfirmed"}

    def release_all(self, page=None):
        """Release the console's own leases: one page's, or every page's."""
        with self.lock:
            items = [
                item
                for item in self.sessions.values()
                if item.state == "held" and (page is None or item.page == page)
            ]
            for item in items:
                item.state = "releasing"
        results = {}
        for item in items:
            error = self._end(item, release=True)
            results[item.device] = "released" if error is None else "unconfirmed"
        return results

    def heartbeat(self, page):
        """Stamp this page's sessions and renew each lease at most once at a time."""
        with self.lock:
            now = self.clock()
            items = [
                item
                for item in self.sessions.values()
                if item.page == page and item.state == "held"
            ]
            for item in items:
                item.beat = now
            due = [item for item in items if not item.renewing]
            for item in due:
                item.renewing = True
        for item in due:
            self._spawn(lambda item=item: self._renew(item), "renew-" + item.device)
        return self.control(page)

    def _renew(self, item):
        try:
            item.adapter.heartbeat()
        except Exception:
            with self.lock:
                if item.state != "held":
                    return
                item.state = "lost"
                item.message = "The service lease was lost."
            # The lease is no longer ours: never release another holder's lease.
            self._end(item, release=False)
            self.service.events.add("warning", "Console lost control.", device=item.device)
        finally:
            item.renewing = False

    # Relay -----------------------------------------------------------------

    def describe(self, device):
        identity, adapter = self._client(device, "read")
        try:
            description = adapter.describe()
        finally:
            self._close(adapter)
        commands = description.get("commands") if isinstance(description, dict) else None
        if not isinstance(commands, dict):
            raise ServiceError(502, "The service describes no commands.")
        return {
            "device": device,
            "commands": {
                name: spec
                for name, spec in commands.items()
                if isinstance(name, str) and name not in LEASE_ACTIONS
            },
        }

    def command(self, device, page, action, params):
        """Forward one described command once. Return accepted, refused or unknown."""
        if not isinstance(action, str) or not action or action in LEASE_ACTIONS:
            raise ValueError("Unknown command action.")
        if not isinstance(params, dict):
            raise ValueError("Command params must be a JSON object.")
        if "action" in params:
            raise ValueError("Command params must not name an action.")
        item = self._owned(device, page)
        fresh = None
        if item is not None:
            adapter, commands = item.adapter, item.commands
        else:
            _, fresh = self._client(device, "command")
            try:
                description = fresh.describe()
            except Exception:
                self._close(fresh)
                raise
            commands = description.get("commands") if isinstance(description, dict) else None
            adapter = fresh
        try:
            spec = commands.get(action) if isinstance(commands, dict) else None
            if not isinstance(spec, dict):
                raise ValueError("The service does not describe this command.")
            if item is None and spec.get("any_client") is not True:
                raise ServiceError(409, "Take control of this device first.")
            try:
                result = adapter.command(action, **params)
            except Exception as exc:
                outcome = classify(exc)
                message = str(exc) if outcome == "refused" else "Command outcome is unknown."
                self.service.events.add(
                    "warning", "Console command " + outcome + ".", device=device, action=action
                )
                return {"outcome": outcome, "error": self.service.dashboard.text(message)}
            if isinstance(result, dict) and type(result.get("status")) is int:
                # The robot WebSocket protocol reports refusals in its reply.
                if result["status"] >= 400:
                    return {
                        "outcome": "refused",
                        "error": self.service.dashboard.text(str(result.get("error", "Refused."))),
                    }
            self.service.events.add("info", "Console command sent.", device=device, action=action)
            command_id = result.get("command_id") if isinstance(result, dict) else None
            return {
                "outcome": "accepted",
                "command_id": command_id if isinstance(command_id, str) else None,
            }
        finally:
            if fresh is not None:
                self._close(fresh)

    def keys(self, page, seq, held):
        """Send one robot held-key set once on this page's robot session. Never retried."""
        item = self._owned("robot", page)
        if item is None:
            raise ServiceError(409, "Take control of this device first.")
        try:
            result = item.adapter.keys(seq, held)
        except Exception as exc:
            outcome = classify(exc)
            message = str(exc) if outcome == "refused" else "The outcome is unknown."
            return {"outcome": outcome, "error": self.service.dashboard.text(message)}
        status = result.get("status") if isinstance(result, dict) else None
        if type(status) is int and status >= 400:
            error = str(result.get("error", "Refused."))
            return {"outcome": "refused", "error": self.service.dashboard.text(error)}
        return {"outcome": "accepted"}

    def stage_hold(self, page, hold_id, motor, direction, speed):
        """Start or refresh this page's stage hold. One axis moves at a time."""
        while True:
            with self.lock:
                item = self.sessions.get("stage")
                if item is None or item.page != page or item.state != "held":
                    raise ServiceError(409, "Take control of this device first.")
                current = item.hold
                if current is not None and current.id == hold_id:
                    if current.ended or current.released:
                        error = self.service.dashboard.text(current.message or HOLD_ENDED)
                        return {"outcome": current.outcome or "refused", "error": error}
                    current.seen = self.clock()
                    return {"outcome": "accepted"}
                if hold_id in item.closed:
                    return {"outcome": "refused", "error": HOLD_ENDED}
                if current is None or current.ended:
                    hold = Hold(hold_id, motor, direction, speed, self.clock())
                    # The worker waits for this lock, so it always sees its own thread.
                    hold.worker = self._spawn(lambda: self._keep_hold(item, hold), "stage-hold")
                    item.hold = hold
                    return {"outcome": "accepted"}
                if not current.released:
                    return {"outcome": "refused", "error": "Release the held axis first."}
                worker = current.worker
            # The previous hold was released and its worker sends release_hold now.
            worker.join(HOLD_JOIN_S)
            if worker.is_alive():
                return {"outcome": "refused", "error": "The previous hold is still ending."}

    def stage_release(self, page, hold_id):
        """End this page's stage hold. A release that arrives first also cancels its start."""
        with self.lock:
            item = self.sessions.get("stage")
            if item is not None and item.page == page:
                item.closed.append(hold_id)
                if item.hold is not None and item.hold.id == hold_id:
                    item.hold.released = True
                    item.hold.wake.set()
        return {"outcome": "accepted"}

    def _keep_hold(self, item, hold):
        """Send hold once, a keepalive while the page refreshes it, then release_hold once."""
        adapter, sent, outcome, reason = item.adapter, False, None, None
        try:
            while True:
                with self.lock:
                    if hold.released or item.state != "held" or self.closing:
                        break
                    if self.clock() - hold.seen > HOLD_DEADMAN_S:
                        outcome, reason = "refused", "No page update for 0.5 s. " + HOLD_ENDED
                        break
                if sent:
                    adapter.command("keepalive", motor=hold.motor, hold_id=hold.id)
                else:
                    state = adapter.state()
                    with self.lock:
                        if hold.released:
                            break
                    sent = True
                    adapter.command(
                        "hold",
                        motor=hold.motor,
                        direction=hold.direction,
                        speed=hold.speed,
                        hold_id=hold.id,
                        sampled_at=state.get("sampled_at") if isinstance(state, dict) else None,
                    )
                if hold.wake.wait(HOLD_KEEPALIVE_S):
                    break
        except Exception as exc:
            outcome = classify(exc)
            reason = str(exc) if outcome == "refused" else "The stage hold outcome is unknown."
        finally:
            if sent:
                try:
                    adapter.command("release_hold", motor=hold.motor, hold_id=hold.id)
                except Exception:
                    pass
            with self.lock:
                hold.ended, hold.outcome, hold.message = True, outcome, reason
                item.closed.append(hold.id)
            if reason is not None:
                self.service.events.add(
                    "warning",
                    "Console stage hold ended: " + (self.service.dashboard.text(reason) or ""),
                    device="stage",
                )

    def operator(self, device, page, path, data):
        """Send one operator-only service request on this page's lease, once.

        The service refuses it unless this page's identity holds the lease.
        The runner and the relay never reach these service routes.
        """
        item = self._owned(device, page)
        if item is None:
            raise ServiceError(409, "Take control of this device first.")
        try:
            result = item.adapter.request(path, data)
        except Exception as exc:
            outcome = classify(exc)
            message = str(exc) if outcome == "refused" else "The outcome is unknown."
            self.service.events.add(
                "warning", "Console operator action " + outcome + ".", device=device, path=path
            )
            return {"outcome": outcome, "error": self.service.dashboard.text(message)}
        self.service.events.add("info", "Console operator action sent.", device=device, path=path)
        command_id = result.get("command_id") if isinstance(result, dict) else None
        return {
            "outcome": "accepted",
            "command_id": command_id if isinstance(command_id, str) else None,
            "result": result if isinstance(result, dict) else None,
        }

    def read(self, device, path):
        """Read one service route with a fresh identity and no lease."""
        _, adapter = self._client(device, "read")
        try:
            value = adapter.request(path)
        except Exception as exc:
            if classify(exc) == "refused":
                raise ServiceError(409, self.service.dashboard.text(str(exc))) from exc
            raise ServiceError(502, "The service read failed.") from exc
        finally:
            self._close(adapter)
        if not isinstance(value, dict):
            raise ServiceError(502, "The service returned an invalid object.")
        return value

    def stop(self, device):
        """Stop one device without control or a lease wait. Send once; never retry."""
        _, adapter = self._client(device, "stop")
        try:
            value = adapter.stop()
        except Exception:
            return {"device": device, "outcome": "unknown"}
        finally:
            self._close(adapter)
        stop = value.get("stop") if isinstance(value, dict) else None
        return {
            "device": device,
            "outcome": stop if stop in {"confirmed", "pending", "unconfirmed"} else "unknown",
        }

    # Events --------------------------------------------------

    def refresh_events(self):
        with self.lock:
            if self.closing or self.events_busy:
                return
            if self.events_at is not None and self.clock() - self.events_at < EVENT_REFRESH_S:
                return
            self.events_busy = True
        try:
            self._spawn(self._collect, "events")
        except Exception:
            with self.lock:
                self.events_busy = False

    def _merge(self, name, report):
        if not isinstance(report, dict) or not isinstance(report.get("events"), list):
            raise ValueError("Invalid event report.")
        cursor = report.get("next")
        if type(cursor) is not int or cursor < 0:
            raise ValueError("Invalid event cursor.")
        text = self.service.dashboard.text
        with self.lock:
            if cursor < self.cursors.get(name, 0):
                # A restarted service starts a new sequence.
                self.events = deque(
                    (item for item in self.events if item["service"] != name), maxlen=EVENT_LIMIT
                )
            for event in report["events"]:
                if not isinstance(event, dict) or type(event.get("seq")) is not int:
                    continue
                level = event.get("level")
                stamp = event.get("ts_unix")
                self.events.append(
                    {
                        "service": name,
                        "seq": event["seq"],
                        "ts_unix": stamp if type(stamp) in {int, float} else None,
                        "level": level
                        if level in {"debug", "info", "warning", "error", "critical"}
                        else "info",
                        "message": text(event.get("message")) or "",
                    }
                )
            self.cursors[name] = cursor

    def _collect(self):
        try:
            for name in SERVICES:
                if not self.service.dashboard_configured(name):
                    with self.lock:
                        self.raw.pop(name, None)
                        self.event_errors.pop(name, None)
                    continue
                adapter = None
                try:
                    _, adapter = self._client(name, "events")
                    adapter.describe()
                    state = adapter.state()
                    with self.lock:
                        self.raw[name] = copy.deepcopy(state) if isinstance(state, dict) else None
                    cursor = self.cursors.get(name, 0)
                    report = adapter.events(cursor)
                    if isinstance(report, dict) and type(report.get("next")) is int:
                        if report["next"] < cursor:
                            # A restarted service starts a new sequence: read it all.
                            report = adapter.events(0)
                    self._merge(name, report)
                    with self.lock:
                        self.event_errors.pop(name, None)
                except Exception:
                    with self.lock:
                        self.event_errors[name] = "Events are unavailable."
                finally:
                    if adapter is not None:
                        self._close(adapter)
            try:
                self._merge("runs", self.service.events.since(self.cursors.get("runs", 0)))
            except Exception:
                pass
        finally:
            with self.lock:
                self.events_at = self.clock()
                self.events_busy = False

    def event_report(self):
        with self.lock:
            rows = sorted(
                self.events,
                key=lambda item: (item["ts_unix"] or 0, item["seq"]),
                reverse=True,
            )
            return {"events": copy.deepcopy(rows), "errors": dict(self.event_errors)}

    # Lifetime --------------------------------------------------------------

    def close(self):
        with self.lock:
            self.closing = True
        self.release_all()
        while True:
            with self.lock:
                workers = [item for item in self.threads if item is not threading.current_thread()]
            if not workers:
                return
            for worker in workers:
                worker.join(0.05)
