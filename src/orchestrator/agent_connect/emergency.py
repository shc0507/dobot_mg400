"""Fixed Stop/status adapters for the lab services. No normal commands or leases."""

from concurrent.futures import ThreadPoolExecutor

from labkit.ports import url
from orchestrator.clients import SERVICES, ServiceAdapter

from .stops import DEVICES, TARGETS


class PhysicalEmergency:
    def __init__(self, adapter_factory=ServiceAdapter):
        self.adapter_factory = adapter_factory

    def _adapter(self, device):
        return self.adapter_factory(
            url(device, "contract/" if device == "stage" else ""),
            "agent-emergency",
            timeout=2,
        )

    def _stop(self, device, **params):
        adapter = self._adapter(device)
        try:
            value = adapter.stop(**params)
            outcome = value.get("stop") if isinstance(value, dict) else None
            return {
                "device": device,
                "outcome": outcome
                if outcome in {"confirmed", "pending", "unconfirmed"}
                else "unknown",
                "simulated": False,
            }
        except Exception:
            return {"device": device, "outcome": "unknown", "simulated": False}
        finally:
            adapter.close()

    def stop(self, target):
        if target not in TARGETS:
            raise ValueError("Use a documented Stop target.")
        if target == "all_motion":
            with ThreadPoolExecutor(max_workers=3) as pool:
                values = list(pool.map(self.stop, DEVICES))
            return self._combined(target, values)
        if target == "gas_all":
            with ThreadPoolExecutor(max_workers=3) as pool:
                values = list(pool.map(self.stop, ("gas_0", "gas_1", "gas_2")))
            return self._combined(target, values)
        if target.startswith("gas_"):
            return {**self._stop("furnace", gas_channel=int(target[-1])), "device": target}
        return self._stop(target)

    @staticmethod
    def _combined(target, values):
        outcomes = {value["outcome"] for value in values}
        outcome = "confirmed" if outcomes == {"confirmed"} else "unknown"
        if "unknown" not in outcomes and "unconfirmed" in outcomes:
            outcome = "unconfirmed"
        elif "unknown" not in outcomes and "pending" in outcomes:
            outcome = "pending"
        return {"device": target, "outcome": outcome, "simulated": False, "devices": values}

    def _state(self, device):
        adapter = self._adapter(device)
        try:
            state = adapter.state()
            keys = (
                "connected",
                "busy",
                "fault",
                "locked",
                "demo",
                "instance",
                "seq",
                "ts_unix",
                "motors",
                "robot_error",
                "feedback_fresh",
                "pv_c",
                "target_c",
                "flow_pct",
                "run_state",
                "sequence",
                "stop_epoch",
            )
            return device, {key: state[key] for key in keys if key in state}
        except Exception:
            return device, {"available": False, "reason": "The service status is unavailable."}
        finally:
            adapter.close()

    def status(self):
        with ThreadPoolExecutor(max_workers=len(SERVICES)) as pool:
            return dict(pool.map(self._state, SERVICES))
