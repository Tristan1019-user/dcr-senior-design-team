import asyncio
import copy

import pytest
from pydantic import ValidationError

from dcr_host.capabilities import (Capability, DCRNodeAdapter, NodeAdapterError, NodeRegistry,
                                   NodeRegistryError, Observation)
from dcr_host.client import DCRClient
from dcr_host.mock import MockTransport
from dcr_host.transport import TransportError


def capability(node: str, local: str = "sht45.temperature_c", *, source="unknown") -> Capability:
    return Capability(local_id=local, semantic_kind="temperature.read", component="sht45",
                      unit="degC", origin="test", source=source)


class FakeAdapter:
    def __init__(self, node_id, *, error=None, delay=0, observations=()):
        self.node_id = node_id
        self.resource_identity = self
        self.source = "unknown"
        self.error = error
        self.delay = delay
        self.observations = list(observations)

    async def discover(self):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return [capability(self.node_id)]

    async def observe(self, capability_id=None):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.observations


def observation(node: str, value=21.5, *, fresh=True, healthy=True, boot="boot-a",
                source="unknown", local="sht45.temperature_c"):
    return Observation(node_id=node, capability_id=f"{node}:{local}", boot_id=boot,
                       value=value, unit="degC", age_ms=100 if fresh else 4000,
                       fresh=fresh, healthy=healthy, sequence=4, source_timestamp_ms=123,
                       source=source)


def test_models_are_strict_and_observation_ids_are_node_qualified():
    assert capability("node-a").read_only is True
    assert observation("node-a").capability_id == "node-a:sht45.temperature_c"
    with pytest.raises(ValidationError):
        Capability(local_id="sht45.temperature_c", semantic_kind="temperature.read",
                   component="sht45", unit="degC", origin="test", read_only=False,
                   source="unknown")
    with pytest.raises(ValidationError):
        Observation(node_id="node-a", capability_id="sht45.temperature_c", boot_id="b",
                    value={"bad": 1}, unit="degC", age_ms=1, fresh=True, healthy=True,
                    sequence=1, source_timestamp_ms=1)


@pytest.mark.asyncio
async def test_registry_rejects_duplicates_and_same_adapter_or_client():
    registry = NodeRegistry(max_nodes=2)
    adapter = FakeAdapter("node-a")
    registry.register(adapter)
    with pytest.raises(NodeRegistryError):
        registry.register(FakeAdapter("node-a"))
    with pytest.raises(NodeRegistryError):
        registry.register(adapter)

    shared_client = object()
    first = DCRNodeAdapter.__new__(DCRNodeAdapter)
    first.client, first.resource_identity, first.node_id, first._boot_id = shared_client, shared_client, "node-b", None
    second = DCRNodeAdapter.__new__(DCRNodeAdapter)
    second.client, second.resource_identity, second.node_id, second._boot_id = shared_client, shared_client, "node-c", None
    registry.register(first)
    with pytest.raises(NodeRegistryError):
        registry.register(second)


@pytest.mark.asyncio
async def test_failure_and_timeout_are_isolated_and_sanitized():
    registry = NodeRegistry(timeout_s=.01)
    good = FakeAdapter("good", observations=[observation("good")])
    bad = FakeAdapter("bad", error=RuntimeError("secret transport detail"))
    slow = FakeAdapter("slow", delay=.1)
    registry.register(good)
    registry.register(bad)
    registry.register(slow)
    reports = await registry.discover()
    assert reports["nodes"][0]["capabilities"]
    assert reports["nodes"][1]["error"]["code"] == "adapter"
    assert "secret" not in reports["nodes"][1]["error"]["message"]
    assert reports["nodes"][2]["error"]["code"] == "timeout"
    observations = await registry.observe("good")
    assert observations["ok"] and observations["result"]["observations"][0]["healthy"]
    assert (await registry.observe("bad"))["error"]["code"] == "adapter"
    assert (await registry.observe("slow"))["error"]["code"] == "timeout"


@pytest.mark.asyncio
async def test_registry_rejects_cross_node_observation_results():
    class CrossNodeAdapter(FakeAdapter):
        async def observe(self, capability_id=None):
            return [observation("other-node")]

    registry = NodeRegistry()
    registry.register(CrossNodeAdapter("node-a"))
    report = await registry.observe("node-a")
    assert report == {"ok": False,
                      "error": {"code": "invalid",
                                 "message": "node capability metadata is invalid"}}


@pytest.mark.asyncio
async def test_registry_validates_requested_ids_and_observation_cardinality():
    adapter = FakeAdapter("node-a", observations=[observation("node-a")])
    registry = NodeRegistry()
    registry.register(adapter)

    local = await registry.observe("node-a", "sht45.temperature_c")
    assert local["ok"]
    qualified = await registry.observe("node-a", "node-a:sht45.temperature_c")
    assert qualified["ok"]
    assert (await registry.observe("node-a", "other:sht45.temperature_c"))["error"]["code"] == "invalid"
    assert (await registry.observe("node-a", "not a capability"))["error"]["code"] == "invalid"

    adapter.observations = [observation("node-a", local="sht45.humidity_pct")]
    assert (await registry.observe("node-a", "sht45.temperature_c"))["error"]["code"] == "invalid"
    adapter.observations = []
    assert (await registry.observe("node-a", "sht45.temperature_c"))["error"]["code"] == "invalid"

    adapter.observations = [observation("node-a"), observation("node-a")]
    assert (await registry.observe("node-a"))["error"]["code"] == "invalid"


@pytest.mark.asyncio
async def test_registry_rejects_adapter_source_mismatch_for_discovery_and_observation():
    class MismatchedSourceAdapter(FakeAdapter):
        def __init__(self, node_id):
            super().__init__(node_id, observations=[observation(node_id)])
            self.source = "synthetic"

        async def discover(self):
            return [capability(self.node_id, source="unknown")]

        async def observe(self, capability_id=None):
            return [observation(self.node_id, source="unknown")]

    registry = NodeRegistry()
    registry.register(MismatchedSourceAdapter("node-a"))
    report = await registry.discover()
    assert report["nodes"][0]["error"]["code"] == "invalid"
    result = await registry.observe("node-a")
    assert result["error"]["code"] == "invalid"


@pytest.mark.asyncio
async def test_stale_sample_is_not_promoted_to_fresh_and_reboot_failure_has_no_cache():
    stale = FakeAdapter("node-a", observations=[observation("node-a", fresh=False, healthy=False)])
    registry = NodeRegistry()
    registry.register(stale)
    report = await registry.observe("node-a")
    assert report["result"]["observations"][0]["fresh"] is False
    assert report["result"]["observations"][0]["healthy"] is False
    stale.error = ConnectionError("device rebooted")
    failed = await registry.observe("node-a")
    assert failed["error"]["code"] == "unavailable"
    assert "observations" not in failed


class FakeDCRClient:
    def __init__(self, *, topology, health, boot="boot-a", connected=True):
        self.topology, self.health = topology, health
        self.state = type("State", (), {"connected": connected, "boot_id": boot})()
        self.boot = boot
        self.calls = []

    async def call(self, operation, args):
        self.calls.append(operation)
        if operation == "get_runtime_info":
            return {"protocol": 1, "boot_id": self.boot}
        if operation == "get_topology":
            return self.topology
        raise AssertionError(operation)

    async def refresh(self):
        self.calls.append("refresh")
        return self.health


def dcr_fixture():
    topology = {"components": [
        {"id": "sht45", "type": "sht45_sensor", "ports": [
            {"name": "temperature_c", "direction": "output", "type": "float", "delivery": "signal"},
            {"name": "humidity_pct", "direction": "output", "type": "float", "delivery": "signal"},
            {"name": "untrusted", "direction": "output", "type": "string", "delivery": "signal"},
        ]},
        {"id": "mystery", "type": "unknown_sensor", "ports": []},
    ]}
    health = {"connected": True, "boot_id": "boot-a", "components": [
        {"id": "sht45", "healthy": True, "outputs": {
            "temperature_c": {"value": 22.0, "age_ms": 50, "fresh": True,
                               "sequence": 8, "timestamp_ms": 900},
            "humidity_pct": {"value": 45.0, "age_ms": 50, "fresh": True,
                              "sequence": 8, "timestamp_ms": 900},
        }}
    ]}
    return FakeDCRClient(topology=topology, health=health)


@pytest.mark.asyncio
async def test_dcr_adapter_maps_only_trusted_compiled_outputs_and_refreshes():
    client = dcr_fixture()
    adapter = DCRNodeAdapter("lab-1", client, source="synthetic")
    capabilities = await adapter.discover()
    assert {item.local_id for item in capabilities} == {"sht45.temperature_c", "sht45.humidity_pct"}
    observations = await adapter.observe()
    assert {item.capability_id for item in observations} == {
        "lab-1:sht45.temperature_c", "lab-1:sht45.humidity_pct"}
    assert all(item.healthy and item.fresh for item in observations)
    assert all(item.source == "synthetic" for item in observations)
    assert client.calls.count("refresh") == 1


@pytest.mark.asyncio
async def test_dcr_adapter_rejects_unknown_capability_and_boot_change():
    client = dcr_fixture()
    adapter = DCRNodeAdapter("lab-1", client)
    with pytest.raises(Exception):
        await adapter.observe("mystery.temperature")
    client.boot = "boot-b"
    registry = NodeRegistry()
    registry.register(adapter)
    report = await registry.observe("lab-1")
    assert report["error"]["code"] == "rebooted"
    assert "observations" not in report


@pytest.mark.asyncio
async def test_dcr_adapter_rejects_post_refresh_reboot_and_float_mismatch_as_stale():
    client = dcr_fixture()
    client.health["boot_id"] = "boot-b"
    adapter = DCRNodeAdapter("lab-1", client, source="physical_serial")
    registry = NodeRegistry()
    registry.register(adapter)
    reboot = await registry.observe("lab-1")
    assert reboot["error"]["code"] == "rebooted"
    assert "observations" not in reboot

    client.health["boot_id"] = "boot-a"
    client.health["components"][0]["outputs"]["temperature_c"]["value"] = True
    observations = await adapter.observe("sht45.temperature_c")
    assert observations[0].value is None
    assert observations[0].fresh is False
    assert observations[0].healthy is False
    assert observations[0].source == "physical_serial"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [
    lambda topology: topology["components"].append("malformed component"),
    lambda topology: topology["components"][0]["ports"].pop(1),
    lambda topology: topology["components"][0]["ports"][0].update(type="string"),
])
async def test_dcr_adapter_fails_closed_on_malformed_known_topology(mutation):
    client = dcr_fixture()
    topology = copy.deepcopy(client.topology)
    mutation(topology)
    client.topology = topology
    adapter = DCRNodeAdapter("lab-1", client, source="synthetic")
    with pytest.raises(NodeAdapterError):
        await adapter.discover()


class DelayedReceiveMockTransport(MockTransport):
    def __init__(self, *, delay=0.05):
        super().__init__()
        self.delay_receive = False
        self.delay = delay

    async def receive(self):
        if self.delay_receive:
            self.delay_receive = False
            await asyncio.sleep(self.delay)
        return await super().receive()


@pytest.mark.asyncio
async def test_dcr_timeout_requires_explicit_reconnect_and_does_not_poison_other_node():
    slow_transport = DelayedReceiveMockTransport(delay=.5)
    slow_client = DCRClient(slow_transport, timeout=1)
    good_client = DCRClient(MockTransport(), timeout=.1)
    await slow_client.connect()
    await good_client.connect()
    slow_adapter = DCRNodeAdapter(client=slow_client, node_id="slow", source="synthetic")
    good_adapter = DCRNodeAdapter(client=good_client, node_id="good", source="synthetic")
    registry = NodeRegistry(timeout_s=.1)
    registry.register(slow_adapter)
    registry.register(good_adapter)
    try:
        slow_transport.delay_receive = True
        slow = await registry.observe("slow", "sht45.temperature_c")
        assert slow == {"ok": False,
                        "error": {"code": "timeout",
                                   "message": "node request timed out"}}
        assert slow_client.state.connected is False
        with pytest.raises(TransportError):
            await slow_client.call("get_runtime_info", {})

        good = await registry.observe("good", "sht45.temperature_c")
        assert good["ok"]
        assert good["result"]["observations"]
        await slow_client.disconnect()
        slow_transport.delay_receive = False
        await slow_client.connect()
        assert (await registry.observe("slow", "sht45.temperature_c"))["ok"]

        await slow_client.disconnect()
        await slow_client.connect()
        assert slow_client.state.connected is True
    finally:
        await slow_client.disconnect()
        await good_client.disconnect()
