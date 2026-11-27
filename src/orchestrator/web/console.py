"""Lab Console server state: browser-held control, the command relay and merged events.

The browser names a page identity in
``X-MG400-Client``; each Take control creates a fresh service identity for one
device. A browser heartbeat drives every renewal. Without a heartbeat for
``BEAT_TIMEOUT_S`` the console releases the lease. Commands and stops are sent
once and never retried.

A stage hold runs on the console: a worker sends ``hold`` once, a ``keepalive``
every ``HOLD_KEEPALIVE_S`` while the page refreshes the hold within
``HOLD_DEADMAN_S``, then ``release_hold`` once.

An agent page (a page identity ``agent-<session>``, used by ``labctl``) is idle
only after ``AGENT_IDLE_S`` without a request. The console renews its leases
every ``AGENT_RENEW_S`` and names each agent identity after the session.

While the microscope service reports an active recording of a page's microscope
lease, the console renews that lease every ``AGENT_RENEW_S`` and waits
``RECORDING_GAP_S`` for a heartbeat: a hidden tab, a laptop sleep or a short
network loss does not end the recording. After that gap the console sends
``stop_recording`` once, keeps the lease while the service finalizes the
recording, and then releases it. The page reads the end message of each ended
session from ``control_ended``.
"""

import copy
import logging
import re
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from labkit.errors import ServiceError

from ..clients import SERVICES, Borrowed

LOG = logging.getLogger(__name__)

BEAT_TIMEOUT_S = 3.0
AGENT_PREFIX = "agent-"
AGENT_PAGE = re.compile(r"agent-[A-Za-z0-9_.-]{1,64}")
AGENT_IDLE_S = 600.0
AGENT_RENEW_S = 1.0
WATCH_INTERVAL_S = 0.2
# A robot disable waits up to 5 s for the disabled mode and up to 5 s for rest.
ROBOT_DISABLE_S = 12.0
# Lease operations have their own routes. The relay never forwards them.
LEASE_ACTIONS = frozenset({"start_sync", "stop_sync", "heartbeat", "renew", "release"})
# Stage hold: the stage firmware stops a hold 1 s after the last keepalive.
HOLD_DEADMAN_S = 0.5
HOLD_KEEPALIVE_S = 0.2
HOLD_JOIN_S = 2.0
HOLD_ENDED = "Hold has ended. Release and press again."
LENT = "A run is using this device."
# A console-held microscope recording waits this long for a browser heartbeat.
RECORDING_GAP_S = 120.0
ACTIVE_RECORDING = frozenset({"starting", "recording", "finalizing"})
GAP_FINALIZED = "No browser heartbeat for 120 s. The recording was stopped and finalized."
GAP_STOPPED = "No browser heartbeat for 120 s. The recording ended without a summary."
# The page can read why its control ended for this long.
ENDED_KEEP_S = 600.0


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


def is_agent(page):
    return isinstance(page, str) and page.startswith(AGENT_PREFIX)


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
        self.beat = self.started = self.renewed = now
        self.agent = is_agent(page)
        if self.agent:
            # The take can end late in the lease (a slow acquire): renew at the next watcher pass.
            self.renewed = now - AGENT_RENEW_S
        self.state = "held"
        self.renewing = False
        # A run borrows held devices: the console keeps the lease, the page cannot command.
        self.lent = False
        self.message = None
        # Microscope only: this lease's recording row from the device cache, or None.
        self.recording = None
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
        self.cursors = {}
        self.event_instances = {}
        self.event_errors = {}
        # page -> device -> (end message, clock time)
        self.ended = {}
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
        # Add and start under one lock hold: close() reads self.threads under the
        # lock and joins each thread, so it must never see an unstarted thread.
        with self.lock:
            self.threads.add(worker)
            try:
                worker.start()
            except Exception:
                self.threads.discard(worker)
                raise
        return worker

    def _watch(self):
        while True:
            with self.lock:
                if self.closing:
                    return
            try:
                self._watch_once()
            except Exception:
                # One bad pass must not end the watcher: leases would then never expire.
                LOG.exception("Console watcher pass failed.")
            time.sleep(WATCH_INTERVAL_S)

    def _watch_once(self):
        """End late sessions and renew due leases, each in its own thread."""
        # Read the device cache before the console lock.
        row = self._recording_row() if "microscope" in self.sessions else None
        with self.lock:
            if self.closing:
                return
            now = self.clock()
            for item in self.sessions.values():
                if item.device == "microscope" and not item.agent and row is not None:
                    item.recording = row if row.get("client") == item.identity else None
            late = [
                item
                for item in self.sessions.values()
                if item.state == "held" and not item.lent and now - item.beat > self._idle(item)
            ]
            finish = [item for item in late if not item.agent and self._recording(item)]
            for item in late:
                if item in finish:
                    item.state = "finalizing"
                    continue
                item.state = "releasing"
                item.message = (
                    "No agent request for 10 min."
                    if item.agent
                    else "No browser heartbeat for 3 s."
                )
            # A finalizing session ends when the service reports the recording ended.
            done = [
                item
                for item in self.sessions.values()
                if item.state == "finalizing" and item.message is None and not self._recording(item)
            ]
            for item in done:
                status = item.recording.get("status") if item.recording else None
                item.state = "releasing"
                item.message = GAP_FINALIZED if status == "completed" else GAP_STOPPED
            # The console renews agent and recording leases itself; their pages call only now
            # and then.
            due = [
                item
                for item in self.sessions.values()
                if (item.agent or self._recording(item) or item.state == "finalizing")
                and item.state in {"held", "finalizing"}
                and not item.renewing
                and now - item.renewed >= AGENT_RENEW_S
            ]
            for item in due:
                item.renewing, item.renewed = True, now
        # A robot end can wait ROBOT_DISABLE_S for its disable; renewals must not wait.
        for item in late:
            if item in finish:
                self._spawn(lambda item=item: self._finish(item), "finish-" + item.device)
            else:
                self._spawn(lambda item=item: self._end(item, release=True), "end-" + item.device)
        for item in done:
            self._spawn(lambda item=item: self._end(item, release=True), "end-" + item.device)
        for item in due:
            self._spawn(lambda item=item: self._renew(item), "renew-" + item.device)

    def _recording_row(self):
        """The microscope recording row in the device cache; None when the cache has no
        fresh state."""
        devices = getattr(self.service, "devices", None)
        try:
            entry = devices.get("microscope")
        except Exception:
            return None
        state = entry.get("state") if isinstance(entry, dict) else None
        if entry.get("phase") != "ready" or not isinstance(state, dict):
            return None
        row = state.get("recording")
        return dict(row) if isinstance(row, dict) else {}

    @staticmethod
    def _recording(item):
        """True while the service reports an active recording of this session's lease."""
        return item.recording is not None and item.recording.get("status") in ACTIVE_RECORDING

    def _finish(self, item):
        """Stop the recording once after the heartbeat gap. The watcher keeps the lease
        while the service finalizes and releases it after the end."""
        row = item.recording or {}
        sent = False
        if row.get("status") == "finalizing":
            sent = True
        elif row.get("status") == "recording" and isinstance(row.get("id"), str):
            try:
                item.adapter.command("stop_recording", recording_id=row["id"])
                sent = True
                self.service.events.add(
                    "warning",
                    "No browser heartbeat for 120 s. The console stopped the recording.",
                    device=item.device,
                )
            except Exception:
                LOG.warning("The console could not stop the recording.", exc_info=True)
        if sent:
            return
        # An armed or starting recording has no video to finalize.
        with self.lock:
            if item.state != "finalizing":
                return
            item.state, item.message = "releasing", GAP_STOPPED
        self._end(item, release=True)

    @staticmethod
    def _idle(item):
        if item.agent:
            return AGENT_IDLE_S
        return RECORDING_GAP_S if Console._recording(item) else BEAT_TIMEOUT_S

    def _ensure_watcher(self):
        with self.lock:
            if (self.watcher is None or not self.watcher.is_alive()) and not self.closing:
                self.watcher = self._spawn(self._watch, "watch")

    # Clients ---------------------------------------------------------------

    def _client(self, device, purpose, page=None):
        if device not in SERVICES:
            raise ServiceError(404, "Unknown console device.")
        if is_agent(page):
            # The Control card then shows which agent session holds the device.
            identity = f"{page}-{uuid.uuid4().hex[:12]}"
        else:
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
        transfer = getattr(self.service, "sample_transfer", None)
        cancelled = item.device == "stage" and transfer is not None and transfer.cancel(item.page)
        # A stage move sent through the relay runs on without a holder: stop it first,
        # as the robot path disables the arm.
        if cancelled or (release and item.device == "stage"):
            try:
                self.stop("stage")
            except Exception:
                pass
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
                if item.device == "robot":
                    # stop_sync on an enabled arm stops it but leaves the robot service
                    # locked until a disable. Disable first: it also stops motion at once.
                    try:
                        item.adapter.run("disable", timeout=ROBOT_DISABLE_S)
                    finally:
                        item.adapter.release()
                else:
                    item.adapter.release()
            except Exception as exc:
                error = exc
        self._close(item.adapter)
        with self.lock:
            if self.sessions.get(item.device) is item:
                del self.sessions[item.device]
            if item.message is not None:
                self.ended.setdefault(item.page, {})[item.device] = (item.message, self.clock())
        return error

    # Control ---------------------------------------------------------------

    def entry(self, item, page=None):
        now = self.clock()
        result = {
            "device": item.device,
            "holder": item.identity,
            "mine": page is not None and item.page == page,
            "state": item.state,
            "message": item.message,
            "held_s": round(max(0.0, now - item.started), 1),
            "lent": item.lent,
        }
        if item.agent:
            result["idle_left_s"] = round(max(0.0, AGENT_IDLE_S - (now - item.beat)), 1)
        return result

    def touch(self, page):
        """Stamp an agent page's sessions: every agent request counts as a beat."""
        if not is_agent(page):
            return
        with self.lock:
            now = self.clock()
            for item in self.sessions.values():
                if item.page == page and item.state == "held":
                    item.beat = now

    def control(self, page=None):
        with self.lock:
            return [self.entry(item, page) for item in self.sessions.values()]

    def control_ended(self, page):
        """Device -> end message of this page's sessions that ended in the last 10 min."""
        with self.lock:
            now = self.clock()
            for name in list(self.ended):
                notes = self.ended[name]
                for device in [key for key, (_, at) in notes.items() if now - at > ENDED_KEEP_S]:
                    del notes[device]
                if not notes:
                    del self.ended[name]
            return {device: message for device, (message, _) in self.ended.get(page, {}).items()}

    def take(self, device, page):
        if is_agent(page) and not AGENT_PAGE.fullmatch(page):
            raise ValueError("Agent pages are agent- and 1 to 64 letters, digits, _, . or -.")
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
            identity, adapter = self._client(device, "control", page)
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
            self.ended.get(page, {}).pop(device, None)
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
            if item.lent:
                raise ServiceError(409, LENT)
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
            if item.lent:
                raise ServiceError(409, LENT)
            item.state = "releasing"
        error = self._end(item, release=True)
        self.service.events.add("info", "Console released control.", device=device)
        return {"device": device, "result": "released" if error is None else "unconfirmed"}

    def release_all(self, page=None):
        """Release the console's own leases: one page's, or every page's. Lent leases stay."""
        with self.lock:
            items = [
                item
                for item in self.sessions.values()
                if item.state == "held" and not item.lent and (page is None or item.page == page)
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
            due = [item for item in items if not item.renewing and not item.agent]
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
                if item.state not in {"held", "finalizing"}:
                    return
                item.state = "lost"
                item.message = "The service lease was lost."
            # The lease is no longer ours: never release another holder's lease.
            self._end(item, release=False)
            self.service.events.add("warning", "Console lost control.", device=item.device)
        finally:
            item.renewing = False

    def lend(self, page, devices):
        """Lend this page's held devices to 1 run. Return name -> Borrowed adapter."""
        with self.lock:
            missing = [
                name
                for name in devices
                if (item := self.sessions.get(name)) is None
                or item.page != page
                or item.state != "held"
                or item.lent
            ]
            if missing:
                raise ServiceError(
                    409, "Take control of these devices first: " + ", ".join(sorted(missing)) + "."
                )
            items = [self.sessions[name] for name in devices]
            for item in items:
                item.lent = True
        return {
            item.device: Borrowed(item.adapter, lambda item=item: self.unlend(item))
            for item in items
        }

    def unlend(self, item):
        """Return a lent device to its page. The idle time starts again."""
        with self.lock:
            item.lent = False
            item.beat = self.clock()

    # Relay -----------------------------------------------------------------

    def describe(self, device):
        description = self._cached(device)["describe"]
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
        self.touch(page)
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
                reply = {"outcome": outcome, "error": self.service.dashboard.text(message)}
                if outcome == "refused":
                    # Keep the service status: 400 is invalid params, 409 is a conflict.
                    status = _status(exc)
                    reply["status"] = status if 400 <= status < 500 else 400
                return reply
            if isinstance(result, dict) and type(result.get("status")) is int:
                # The robot WebSocket protocol reports refusals in its reply.
                if result["status"] >= 400:
                    return {
                        "outcome": "refused",
                        "status": result["status"] if result["status"] < 500 else 409,
                        "error": self.service.dashboard.text(str(result.get("error", "Refused."))),
                    }
            self.service.events.add("info", "Console command sent.", device=device, action=action)
            command_id = result.get("command_id") if isinstance(result, dict) else None
            accepted_state = result.get("state") if isinstance(result, dict) else None
            instance = accepted_state.get("instance") if isinstance(accepted_state, dict) else None
            return {
                "outcome": "accepted",
                "command_id": command_id if isinstance(command_id, str) else None,
                "instance": instance if isinstance(instance, str) else None,
                "client": adapter.identity,
            }
        finally:
            if fresh is not None:
                self._close(fresh)

    def operator(self, device, page, path, data):
        """Send one operator-only service request on this page's lease, once.

        The service refuses it unless this page's identity holds the lease.
        The turret calibration routes use it; the runner never does.
        """
        self.touch(page)
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
        return {"outcome": "accepted", "result": result if isinstance(result, dict) else None}

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
                if item.lent:
                    raise ServiceError(409, LENT)
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

    def _cached(self, device, fresh=False):
        """Return the device cache row, or raise when the device has no fresh state.
        With fresh, the row comes from a read that started after this call."""
        if device not in SERVICES:
            raise ServiceError(404, "Unknown console device.")
        entry = self.service.devices.wait(device)
        if fresh or entry["phase"] != "ready":
            # Take control can start a demo service after the last cached read.
            entry = self.service.devices.fresh(device)
        if entry["phase"] in {"unconfigured", "offline"}:
            raise ServiceError(
                409, "The service is not configured or its owned demo is not running."
            )
        if entry["phase"] != "ready":
            raise ServiceError(502, "The service state read failed.")
        return entry

    def device_state(self, device, page=None, fresh=False):
        """Return one device's full state from the device cache."""
        # An agent that only reads or waits is still active.
        self.touch(page)
        value = self._cached(device, fresh)["state"]
        with self.lock:
            item = self.sessions.get(device)
            control = self.entry(item, page) if item is not None else None
        return {"device": device, "state": value, "control": control}

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
        transfer = getattr(self.service, "sample_transfer", None)
        if device == "stage" and transfer is not None:
            transfer.cancel()
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
        """Start the device readers and merge the run events. Device events come from
        the device cache readers through pull_events."""
        self.service.devices.start()
        try:
            self._merge(
                "runs",
                self.service.events.since(
                    self.cursors.get("runs", 0), instance=self.event_instances.get("runs")
                ),
            )
        except Exception:
            pass

    def _merge(self, name, report):
        """Store new events of one service. A restarted service starts a new sequence."""
        if not isinstance(report, dict) or not isinstance(report.get("events"), list):
            raise ValueError("Invalid event report.")
        cursor = report.get("next")
        if type(cursor) is not int or cursor < 0:
            raise ValueError("Invalid event cursor.")
        instance = report.get("instance")
        if instance is not None and (not isinstance(instance, str) or not instance):
            raise ValueError("Invalid event instance.")
        text = self.service.dashboard.text
        rows = []
        for event in report["events"]:
            if not isinstance(event, dict) or type(event.get("seq")) is not int:
                continue
            event_instance = event.get("instance", instance)
            if event_instance is not None and (
                not isinstance(event_instance, str)
                or not event_instance
                or (instance is not None and event_instance != instance)
            ):
                continue
            level, stamp = event.get("level"), event.get("ts_unix")
            rows.append(
                (
                    name,
                    event["seq"],
                    stamp if type(stamp) in {int, float} else time.time(),
                    level if level in {"debug", "info", "warning", "error", "critical"} else "info",
                    text(event.get("message")) or "",
                    event.get("fields") if isinstance(event.get("fields"), dict) else {},
                    event_instance,
                )
            )
        with self.lock:
            previous = (self.event_instances.get(name), self.cursors.get(name, 0))
            self.service.store.add_device_events(rows)
            self.cursors[name] = cursor
            self.event_instances[name] = instance
        from ..agent_connect.runtime import publish_device_events

        publish_device_events(self.service, name, rows, previous, instance, report)
        if report.get("gap") is True and previous != (instance, cursor):
            restarted = report.get("restarted") is True
            self.service.events.add(
                "info" if restarted else "warning",
                "Device service restarted." if restarted else "Device event history has a gap.",
                device=name,
                instance=instance,
                since=previous[1],
                oldest=report.get("oldest"),
                gap_reason=report.get("gap_reason"),
            )

    def pull_events(self, name, adapter):
        """Read one device's event replay. The device cache reader calls this every 1 s."""
        try:
            with self.lock:
                cursor = self.cursors.get(name, 0)
                instance = self.event_instances.get(name)
            report = (
                adapter.events(cursor, instance=instance)
                if instance is not None
                else adapter.events(cursor)
            )
            if isinstance(report, dict) and type(report.get("next")) is int:
                if report.get("instance") is None and report["next"] < cursor:
                    report = adapter.events(0)
            self._merge(name, report)
            with self.lock:
                self.event_errors.pop(name, None)
        except Exception:
            with self.lock:
                self.event_errors[name] = "Events are unavailable."

    def event_report(self, **query):
        """Stored events, newest first, with the services whose events are unavailable now."""
        rows = [
            {**row, "service": row.pop("device"), "ts_unix": row.pop("ts")}
            for row in self.service.store.device_events(**query)
        ]
        with self.lock:
            errors = dict(self.event_errors)
        for name in SERVICES:
            if self.service.devices.get(name)["phase"] in {"unavailable", "stale"}:
                errors[name] = "Events are unavailable."
        return {"events": rows, "errors": errors}

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
