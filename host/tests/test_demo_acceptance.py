from copy import deepcopy
from types import SimpleNamespace

import pytest

from dcr_host.platform_demo import verify_model_trace


def trace():
    sensor = {"node_id": "lab", "capability_id": "lab:sht45.temperature_c", "value": 28.0,
              "unit": "degC", "source": "physical_serial", "fresh": True, "healthy": True}
    position = {"node_id": "stage", "capability_id": "stage:position.read", "value": 30.0,
                "unit": "mm", "source": "synthetic", "fresh": True, "healthy": True}
    def read(node, capability, obs):
        return {"tool": "read_capability", "arguments": {"node_id": node, "capability_id": capability},
                "output": {"ok": True, "result": {"observations": [obs]}}}
    return [
        {"tool": "discover_capabilities", "arguments": {}, "output": {"ok": True, "partial": True,
         "result": {"nodes": [
             {"node_id": "lab", "error": None, "capabilities": [{"local_id": "sht45.temperature_c"}]},
             {"node_id": "offline", "error": {"code": "unavailable"}, "capabilities": []}]}}},
        read("lab", "sht45.temperature_c", sensor),
        {"tool": "request_stage_target", "arguments": {"node_id": "stage", "target_mm": 30.0},
         "output": {"ok": True, "result": {"verified": True, "source": "synthetic",
                                            "node_id": "stage", "position_mm": 30.0}}},
        read("stage", "position.read", position),
    ]


def verify(steps):
    return verify_model_trace(SimpleNamespace(outcome="model_finished", trace=steps), "physical_serial")


def test_ordered_evidence_accepts_hot_and_cool_branches():
    assert verify(trace())[1] == 30
    steps = trace()
    steps[1]["output"]["result"]["observations"][0]["value"] = 24.0
    steps[2]["arguments"]["target_mm"] = 20.0
    steps[2]["output"]["result"]["position_mm"] = 20.0
    steps[3]["output"]["result"]["observations"][0]["value"] = 20.0
    assert verify(steps)[1] == 20


@pytest.mark.parametrize("order", [[1, 2, 3], [0, 2, 1, 3], [0, 1, 3, 2], [0, 1, 2],
                                   [0, 1, 2, 2, 3]])
def test_missing_or_wrongly_ordered_evidence_fails(order):
    original = trace()
    with pytest.raises(RuntimeError):
        verify([deepcopy(original[index]) for index in order])


@pytest.mark.parametrize("index,field,value", [
    (1, "capability_id", "lab:bmp280.temperature_c"), (1, "source", "synthetic"),
    (1, "unit", "Pa"), (1, "healthy", False), (1, "fresh", False), (1, "value", float("nan")),
    (3, "value", 20.0), (3, "source", "physical_serial"), (3, "unit", "degC"),
])
def test_wrong_observation_cannot_be_replaced_by_host_readback(index, field, value):
    steps = trace()
    steps[index]["output"]["result"]["observations"][0][field] = value
    with pytest.raises(RuntimeError):
        verify(steps)


def test_wrong_request_target_and_lifecycle_attempts_rejected():
    steps = trace()
    steps[2]["arguments"]["target_mm"] = 20
    with pytest.raises(RuntimeError):
        verify(steps)
    steps = trace()
    steps.append({"tool": "control_component", "arguments": {}, "output": {"ok": False}})
    with pytest.raises(RuntimeError):
        verify(steps)
