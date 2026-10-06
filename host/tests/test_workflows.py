import pytest

from dcr_host.capabilities import DCRNodeAdapter, NodeRegistry
from dcr_host.client import DCRClient
from dcr_host.control_tools import ControlTools
from dcr_host.mock import MockTransport
from dcr_host.platform import NodeRecorder, permission_specs
from dcr_host.protocol import load_json
from dcr_host.recording import Recorder, ReplayTransport
from dcr_host.transport import TransportError
from dcr_host.workflows import sensor_cycle


class UnexpectedExecutor:
    def __init__(self):
        self.calls = []
        self.records = []

    async def execute(self, name, arguments):
        self.calls.append((name, arguments))
        raise RuntimeError("unexpected executor failure")

    def record(self, kind, payload):
        self.records.append((kind, payload))


class UnknownMutationTransport(MockTransport):
    def __init__(self):
        super().__init__()
        self.fail_after_write = False

    async def send(self, data):
        request = load_json(data)
        await super().send(data)
        if self.fail_after_write and request.get("op") == "stop_component":
            self.fail_after_write = False
            raise RuntimeError("post-write outcome lost")


class EscapingTimeoutExecutor:
    def __init__(self):
        self.calls = []

    async def execute(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "inspect_node":
            return {"ok": True, "result": {"boot_id": "boot-a", "components": [
                {"id": "sht45", "state": "RUNNING", "healthy": True}]}}
        if name == "read_capability":
            return {"ok": True, "result": {"observations": [{
                "fresh": True, "healthy": True, "sequence": 1, "boot_id": "boot-a",
                "source": "synthetic"}]}}
        if name == "control_component":
            raise TimeoutError("mutation outcome unknown")
        raise AssertionError(name)

    def record(self, kind, payload):
        pass


def test_permission_bindings_are_explicit():
    assert permission_specs(["a:sht45"], {"a"}) == {"a": {"sht45"}}
    for bindings in (["sht45"], ["unknown:sht45"], ["a:../../x"]):
        with pytest.raises(ValueError):
            permission_specs(bindings, {"a"})


@pytest.mark.asyncio
async def test_unexpected_executor_exception_is_reraised_and_outcome_is_recorded():
    executor = UnexpectedExecutor()
    with pytest.raises(RuntimeError, match="unexpected executor failure"):
        await sensor_cycle(executor, "a")
    assert [name for name, _ in executor.calls] == ["inspect_node"]
    assert [kind for kind, _ in executor.records] == ["workflow_start", "workflow_outcome"]
    assert executor.records[-1][1]["error"] == "WORKFLOW_EXCEPTION"
    assert executor.records[-1][1]["recovery"] == "manual_inspection_required"


@pytest.mark.asyncio
async def test_escaping_mutation_timeout_never_enters_recovery_writes():
    executor = EscapingTimeoutExecutor()
    result = await sensor_cycle(executor, "a")
    assert not result["passed"]
    assert result["error"] == "WORKFLOW_TIMEOUT"
    assert result["recovery"] == "manual_inspection_required"
    assert [name for name, _ in executor.calls].count("control_component") == 1


@pytest.mark.asyncio
async def test_unknown_mutation_outcome_never_replays_during_recovery():
    clients = {"a": DCRClient(UnknownMutationTransport()), "b": DCRClient(MockTransport())}
    registry = NodeRegistry()
    try:
        for name, client in clients.items():
            await client.connect()
            registry.register(DCRNodeAdapter(name, client, source="synthetic"))
        executor = ControlTools(registry, clients, {"a": ["sht45"]})
        clients["a"].transport.fail_after_write = True
        result = await sensor_cycle(executor, "a", companion_node="b")
        assert not result["passed"]
        assert result["recovery"] == "manual_inspection_required"
        writes = [command["op"] for command in clients["a"].transport.commands
                  if not command["op"].startswith("get_")]
        assert writes == ["stop_component"]
    finally:
        for client in clients.values():
            await client.disconnect()


@pytest.mark.asyncio
async def test_qualified_cycle_preserves_same_named_second_node():
    clients = {name: DCRClient(MockTransport()) for name in ("a", "b")}
    registry = NodeRegistry()
    try:
        for name, client in clients.items():
            await client.connect()
            registry.register(DCRNodeAdapter(name, client, source="synthetic"))
        executor = ControlTools(registry, clients, {"a": ["sht45"]})
        result = await sensor_cycle(executor, "a", companion_node="b")
        assert result["passed"], result
        assert result["companion_sequences"][1] > result["companion_sequences"][0]
        assert all(c["op"].startswith("get_") for c in clients["b"].transport.commands)
        writes = [c["op"] for c in clients["a"].transport.commands if not c["op"].startswith("get_")]
        assert writes == ["stop_component", "init_component", "start_component"]
    finally:
        for client in clients.values():
            await client.disconnect()


@pytest.mark.asyncio
async def test_cycle_without_permission_is_not_success():
    client = DCRClient(MockTransport())
    await client.connect()
    try:
        registry = NodeRegistry()
        registry.register(DCRNodeAdapter("a", client, source="synthetic"))
        result = await sensor_cycle(ControlTools(registry, {"a": client}, {}), "a")
        assert not result["passed"]
        assert all(c["op"].startswith("get_") for c in client.transport.commands)
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_namespaced_recording_replays_only_selected_node(tmp_path):
    path = tmp_path / "trace.jsonl"
    recorder = Recorder(path)
    client = DCRClient(MockTransport(), recorder=NodeRecorder(recorder, "a"))
    await client.connect()
    expected = await client.refresh()
    await client.disconnect()
    recorder.close()
    replayed = DCRClient(ReplayTransport(path, node_id="a"))
    try:
        await replayed.connect()
        actual = await replayed.refresh()
        assert actual["boot_id"] == expected["boot_id"]
        assert [c["id"] for c in actual["components"]] == [c["id"] for c in expected["components"]]
    finally:
        await replayed.disconnect()
    with pytest.raises(TransportError):
        await ReplayTransport(path, node_id="missing").connect()
