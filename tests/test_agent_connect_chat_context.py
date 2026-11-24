"""Chat reads the verified Console contract and preserves unavailable data."""

import copy

import pytest

from labkit.errors import ServiceError
from orchestrator.agent_connect.chat_context import MAX_CONTEXT_BYTES, ChatContextReader
from orchestrator.agent_connect.contract import digest
from orchestrator.agent_connect.protocol import PREFIX, bundle


@pytest.fixture
def source():
    definition = bundle()
    identity = digest(definition)
    discovery = {
        "discovery_version": 1,
        "read_only": True,
        "mode": "live",
        "boot_id": "read-boot",
        "contract_id": identity,
        "contract_url": PREFIX + "/contracts/" + identity[7:] + ".json",
        "endpoints": {"state": PREFIX + "/v1/state"},
        "control": {"command_boot_id": "different-command-boot"},
    }
    state = {
        "type": "state",
        "mode": "live",
        "boot_id": "read-boot",
        "contract_id": identity,
        "seq": 2,
        "event_seq": 3,
        "generated_at": 1700000000,
        "devices": {
            "stage": {
                "quality": "valid",
                "sampled_at": None,
                "age_s": 0.1,
                "instance": None,
                "data_gaps": ["instance", "reference_revision"],
                "values": {"axes": [{"id": "top", "position_rotations": 1.25, "unit": "rotation"}]},
            },
            "furnace": {"quality": "unavailable", "age_s": None, "values": {"pv_c": None}},
        },
        "samples": {"items": [], "complete": False},
        "operations": {"items": []},
    }
    calls = []
    docs = {"discovery": discovery, "contract": definition, "state": state}

    def read(path):
        calls.append(path)
        if path == PREFIX + "/discovery":
            return copy.deepcopy(docs["discovery"])
        if path == PREFIX + "/v1/state":
            return copy.deepcopy(docs["state"])
        if path == docs["discovery"]["contract_url"]:
            return copy.deepcopy(docs["contract"])
        pytest.fail("Unexpected read path: " + path)

    return docs, calls, read


def set_definition(docs, definition):
    identity = digest(definition)
    docs["contract"] = definition
    docs["state"]["contract_id"] = identity
    docs["discovery"].update(
        contract_id=identity, contract_url=PREFIX + "/contracts/" + identity[7:] + ".json"
    )


@pytest.mark.parametrize("source_name,mode", [("live", "live"), ("mock", "MOCK")])
def test_context_keeps_quality_units_and_missing_process_metadata(source, source_name, mode):
    docs, calls, read = source
    docs["discovery"]["mode"] = docs["state"]["mode"] = mode
    reader = ChatContextReader({source_name: read})
    context = reader.read(source_name)
    assert context["units"]["position_rotations"] == "rotation"
    assert context["units"]["pose_mm_deg"] == "mm,mm,mm,degree"
    assert context["state"] == docs["state"]
    assert context["discovery"]["control"]["command_boot_id"] != context["state"]["boot_id"]
    context["units"]["position_rotations"] = "wrong"
    context["state"]["devices"]["stage"]["data_gaps"].clear()
    again = reader.read(source_name)
    assert again["units"]["position_rotations"] == "rotation"
    assert again["state"]["devices"]["stage"]["data_gaps"]
    assert calls.count(docs["discovery"]["contract_url"]) == 1
    assert calls.count(PREFIX + "/discovery") == 4


@pytest.mark.parametrize("bad_source", ["other", None, {}, []])
def test_context_rejects_unknown_source(source, bad_source):
    _, calls, read = source
    with pytest.raises(ServiceError) as error:
        ChatContextReader({"live": read}).read(bad_source)
    assert error.value.status == 400 and calls == []


@pytest.mark.parametrize(
    "changes",
    [
        {"mode": "MOCK"},
        {"read_only": False},
        {"discovery_version": True},
        {"contract_url": "https://example.com/contract"},
        {"endpoints": {"state": "/arbitrary/path"}},
        {"endpoints": []},
    ],
)
def test_context_refuses_mismatched_source_or_advertised_paths(source, changes):
    docs, calls, read = source
    docs["discovery"].update(changes)
    with pytest.raises(ServiceError):
        ChatContextReader({"live": read}).read("live")
    assert calls == [PREFIX + "/discovery"]


def test_context_rejects_digest_mismatch(source):
    docs, _, read = source
    docs["contract"]["version"] = "1.9.0"
    with pytest.raises(ServiceError, match="integrity"):
        ChatContextReader({"live": read}).read("live")


@pytest.mark.parametrize("change", ["major", "unit", "required", "capability", "schema"])
def test_context_rejects_unsupported_agreement(source, change):
    docs, _, read = source
    definition = docs["contract"]
    if change == "major":
        definition["version"] = "2.0.0"
    elif change == "unit":
        definition["semantics"]["units"]["position_rotations"] = "mm"
    elif change == "required":
        definition["schemas"]["state"]["required"].append("new_required_meaning")
    elif change == "capability":
        definition["required_capabilities"] = ["future-coordinate-format"]
    else:
        definition["schemas"]["state"]["properties"]["type"] = []
    set_definition(docs, definition)
    with pytest.raises(ServiceError, match="updated agent"):
        ChatContextReader({"live": read}).read("live")


def test_context_accepts_additive_minor_contract(source):
    docs, _, read = source
    docs["contract"]["version"] = "1.2.0"
    docs["contract"]["schemas"]["state"]["properties"]["extra"] = {"type": "string"}
    set_definition(docs, docs["contract"])
    assert ChatContextReader({"live": read}).read("live")["state"] == docs["state"]


@pytest.mark.parametrize("field,value", [("boot_id", "restarted"), ("mode", "MOCK")])
def test_context_refuses_discovery_change_during_read(source, field, value):
    docs, _, read = source

    def changing(path):
        response = read(path)
        if path == PREFIX + "/v1/state":
            docs["discovery"][field] = value
        return response

    with pytest.raises(ServiceError):
        ChatContextReader({"live": changing}).read("live")


@pytest.mark.parametrize(
    "changes",
    [
        {"mode": "MOCK"},
        {"type": "event"},
        {"boot_id": "other-boot"},
        {"seq": True},
        {"event_seq": -1},
        {"generated_at": float("nan")},
        {"devices": []},
        {"samples": None},
    ],
)
def test_context_refuses_invalid_state_envelope(source, changes):
    docs, _, read = source
    docs["state"].update(changes)
    with pytest.raises(ServiceError):
        ChatContextReader({"live": read}).read("live")


@pytest.mark.parametrize("change", ["unit", "quality", "position", "time", "temperature"])
def test_context_refuses_invalid_readings(source, change):
    docs, _, read = source
    stage = docs["state"]["devices"]["stage"]
    if change == "unit":
        stage["values"]["axes"][0]["unit"] = "mm"
    elif change == "quality":
        stage["quality"] = "future_quality"
    elif change == "position":
        stage["values"]["axes"][0]["position_rotations"] = "1.25"
    elif change == "time":
        stage["age_s"] = -1
    else:
        docs["state"]["devices"]["furnace"]["values"]["pv_c"] = True
    with pytest.raises(ServiceError):
        ChatContextReader({"live": read}).read("live")


def test_context_bounds_response_and_masks_transport_error(source):
    docs, _, read = source
    docs["state"]["extra"] = "x" * MAX_CONTEXT_BYTES
    with pytest.raises(ServiceError, match="size"):
        ChatContextReader({"live": read}).read("live")

    def failure(path):
        raise OSError("private network detail")

    with pytest.raises(ServiceError) as error:
        ChatContextReader({"live": failure}).read("live")
    assert error.value.status == 503
    assert "private" not in error.value.message
