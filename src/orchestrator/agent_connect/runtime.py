"""Attach the read stream to existing Console caches and persistent chip records."""

from .observations import SharedObservations
from .release import running_source_bundle
from .samples import SampleTracker
from .state import CacheStateProvider
from .stream import BroadcastHub


def create_hub(service):
    def samples(**query):
        if service.store is None:
            return {
                "revision": None,
                "selection": query.get("selection", "active"),
                "items": [],
                "count": 0,
                "total": None,
                "complete": False,
                "next_offset": None,
                "available": False,
            }
        return SampleTracker(service.store).samples(**query)

    def operations():
        method = getattr(service, "snapshot", None)
        if method is None:
            return {"available": False, "items": [], "count": 0, "complete": False}
        state = method()
        items = []
        for kind, key in (("run", "active_run"), ("command", "operation")):
            value = state.get(key)
            if isinstance(value, dict):
                items.append(
                    {
                        "kind": kind,
                        **{
                            field: value[field]
                            for field in (
                                "id",
                                "run_id",
                                "name",
                                "action",
                                "status",
                                "step",
                                "position",
                                "done",
                                "error",
                            )
                            if field in value
                        },
                    }
                )
        control = getattr(service, "agent_control", None)
        if control is not None:
            items.extend(control.operation_summaries())
        return {"available": True, "items": items, "count": len(items), "complete": True}

    service.agent_observations = SharedObservations(service)
    provider = CacheStateProvider(
        service.devices,
        samples=samples,
        operations=operations,
        observations=service.agent_observations.snapshot,
        mode="demo" if service.demo else "live",
    )
    source = running_source_bundle()
    hub = BroadcastHub(provider, server_release=source["digest"] or "source-unavailable")
    hub.source_bundle = source
    return hub


def publish_device_events(service, name, rows, previous, instance, report):
    """Use the existing replay reader. Never request another device response."""
    hub = getattr(service, "agent_stream", None)
    if hub is None:
        return
    if report.get("gap") and previous != (instance, report.get("next")):
        hub.publish(
            "device.history.gap",
            {"service": name, "instance": instance, "reason": report.get("gap_reason")},
        )
    for device, seq, at, level, message, fields, event_instance in rows:
        if event_instance == previous[0] and seq <= previous[1]:
            continue
        try:
            hub.publish(
                "device.event",
                {
                    "service": device,
                    "instance": event_instance,
                    "source_seq": seq,
                    "source_at": at,
                    "level": level,
                    "message": message,
                    "fields": fields,
                },
            )
        except ValueError:
            hub.publish(
                "device.event.omitted",
                {
                    "service": device,
                    "source_seq": seq,
                    "reason": "Event exceeds the message limit.",
                },
            )
