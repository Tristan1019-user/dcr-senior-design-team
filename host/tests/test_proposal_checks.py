import pytest

from dcr_host.client import DCRClient
from dcr_host.mock import MockTransport
from dcr_host.proposal_checks import check_bus_contract, check_model_independence, check_connection_locks
from dcr_host.state import RuntimeState


@pytest.fixture
def bus_contract():
    components = []
    edges = []
    for sensor, worker in (("sht45", "i2c_bus_0"), ("bmp280", "i2c_bus_1")):
        components.extend([
            {"id": sensor, "ports": [{"name": "bus", "direction": "output", "type": "bus_request"}]},
            {"id": worker, "type": "i2c_bus", "ports": [{"name": "request", "direction": "input",
             "type": "bus_request", "delivery": "queue", "multi_producer": True}]},
        ])
        edges.append({"from": f"{sensor}.bus", "to": f"{worker}.request"})
    components.extend({"id": f"logger{i}"} for i in range(4))
    return ({"components": components, "connections": edges},
            {"components": [{"id": c["id"]} for c in components],
             "settings": {"watchdog_timeout_ms": 5000, "signal_wait_timeout_ms": 100}})


def test_bus_contract_accepts_inspectable_demo(bus_contract):
    assert check_bus_contract(*bus_contract)["component_count"] == 8


def test_packaged_environment_has_expected_locks():
    import json
    from pathlib import Path
    config = json.loads((Path(__file__).resolve().parents[2] / "examples/environment.json").read_text())
    assert check_connection_locks(config)["rule_count"] == 2


@pytest.mark.parametrize("rules", [None, [], [{"type": "connection_locked"}],
                                  [{"id": "preserve_sht_bus", "type": "connection_locked",
                                    "from": "bmp280.bus", "to": "i2c_bus_0.request"}]])
def test_lock_inspection_rejects_missing_or_wrong_policy(rules):
    with pytest.raises(ValueError):
        check_connection_locks({"safety_rules": rules})


@pytest.mark.parametrize("defect", ["direction", "route", "settings", "private_handle"])
def test_bus_contract_rejects_incomplete_inspection(bus_contract, defect):
    topology, configuration = bus_contract
    if defect == "direction":
        topology["components"][1]["ports"][0]["direction"] = "output"
    elif defect == "route":
        topology["connections"] = []
    elif defect == "settings":
        configuration["settings"]["watchdog_timeout_ms"] = 0
    else:
        topology["response_queue"] = 12345
    with pytest.raises(ValueError):
        check_bus_contract(topology, configuration)


def test_silent_stream_does_not_leave_logger_healthy_forever():
    now = [0.0]
    state = RuntimeState(clock=lambda: now[0])
    state.connected = True
    state.update([{"id": "sink", "type": "logger", "state": "RUNNING", "fault": "ok",
                   "config": {}, "outputs": {}}])
    assert state.inspect("sink")["healthy"]
    now[0] = 3.1
    assert not state.inspect("sink")["healthy"]


@pytest.mark.asyncio
async def test_model_outage_does_not_change_components_or_retry():
    mock = MockTransport()
    client = DCRClient(mock)
    await client.connect()
    try:
        report = await check_model_independence(client, 2.1)
        assert report["passed"] and report["same_boot"]
        assert report["model_requests"] == 1 and report["tool_mutations"] == 0
        assert all(c["op"].startswith("get_") for c in mock.commands)
        assert all(n >= 2 for n in report["sequence_advances"].values())
    finally:
        await client.disconnect()
