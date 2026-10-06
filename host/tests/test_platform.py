import argparse
import json

import pytest

from dcr_host.agent import Agent
from dcr_host.capabilities import DCRNodeAdapter, NodeRegistry
from dcr_host.client import DCRClient
from dcr_host.mock import MockTransport
from dcr_host.models.base import ModelReply, ToolCall
from dcr_host.platform import node_specs, run
from dcr_host.recording import Recorder, replay
from dcr_host.semantic_tools import CapabilityTools


@pytest.mark.parametrize("nodes,mocks", [([], []), (["missing-separator"], []),
    (["a=COM4", "b=com4"], []), (["a=COM4", "b=\\\\.\\COM4"], []),
    (["a=COM4"], ["a"]), ([], ["same", "same"]),
    (["../escape=COM4"], []), ([], [f"n{i}" for i in range(17)])])
def test_bindings_rejected_before_open(nodes, mocks):
    with pytest.raises(ValueError):
        node_specs(nodes, mocks)


def test_bindings_keep_synthetic_explicit():
    assert node_specs(["lab=COM4"], ["simulation"]) == [("lab", "COM4"), ("simulation", None)]


class ScriptedReasoner:
    async def generate(self, messages, tools):
        assert {x["function"]["name"] for x in tools} == {"discover_capabilities", "read_capability"}
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        if not results:
            name, args = "discover_capabilities", {}
        elif len(results) == 1:
            node = results[0]["result"]["nodes"][0]
            name, args = "read_capability", {"node_id": node["node_id"],
                                           "capability_id": node["capabilities"][0]["local_id"]}
        else:
            assert results[-1]["ok"]
            return ModelReply(text="Read a synthetic sensor; no action taken.")
        return ModelReply(text="", tool_calls=[ToolCall(id=str(len(results)), name=name, arguments=args)])


@pytest.mark.asyncio
async def test_agent_uses_capabilities_without_direct_client_dependency(tmp_path):
    client = DCRClient(MockTransport())
    await client.connect()
    recorder = Recorder(tmp_path / "semantic.jsonl")
    try:
        registry = NodeRegistry()
        registry.register(DCRNodeAdapter(client=client, node_id="test", source="synthetic"))
        executor = CapabilityTools(registry, recorder)
        assert not hasattr(executor, "client")
        denied = await executor.execute("stop_component", {"component": "sht45"})
        assert not denied["ok"] and denied["error"]["code"] == "UNKNOWN_TOOL"
        invalid = await executor.execute("read_capability", {"node_id": "test", "capability_id": 2})
        assert not invalid["ok"]
        result = await Agent(ScriptedReasoner(), executor).run("Discover and read one capability")
        assert result.outcome == "model_finished" and result.tool_steps == 2
        assert all(c["op"].startswith("get_") for c in client.transport.commands)
    finally:
        await client.disconnect()
        recorder.close()
    events = list(replay(tmp_path / "semantic.jsonl"))
    assert {"instruction", "tool_proposal", "tool_result", "outcome"} <= {e["kind"] for e in events}
    assert events[-1]["payload"]["model_text"] == "Read a synthetic sensor; no action taken."


@pytest.mark.asyncio
async def test_cli_two_nodes_same_local_names_with_namespaced_recording(tmp_path, capsys):
    path = tmp_path / "nodes.jsonl"
    args = argparse.Namespace(node=[], mock_node=["alpha", "beta"], record=str(path), command="discover")
    assert await run(args) == 0
    output = json.loads(capsys.readouterr().out)
    nodes = output["result"]["nodes"]
    assert [n["node_id"] for n in nodes] == ["alpha", "beta"]
    assert all(len(n["capabilities"]) == 4 and not n["error"] for n in nodes)
    assert nodes[0]["capabilities"][0]["local_id"] == nodes[1]["capabilities"][0]["local_id"]
    rows = list(replay(path))
    requests = [r["payload"] for r in rows if r["kind"] == "node.request"]
    assert {r["node_id"] for r in requests} == {"alpha", "beta"}
    assert all(r["data"]["op"].startswith("get_") for r in requests)


@pytest.mark.asyncio
async def test_cli_read_returns_node_qualified_value(capsys):
    args = argparse.Namespace(node=[], mock_node=["alpha"], record=None, command="observe",
                              node_id="alpha", capability_id="sht45.temperature_c")
    assert await run(args) == 0
    sample = json.loads(capsys.readouterr().out)["result"]["observations"][0]
    assert sample["capability_id"] == "alpha:sht45.temperature_c"
    assert sample["unit"] == "degC" and sample["fresh"]


@pytest.mark.asyncio
async def test_discovery_does_not_call_all_failed_success():
    registry = NodeRegistry()
    registry.register(DCRNodeAdapter(client=DCRClient(MockTransport()), node_id="offline", source="synthetic"))
    result = await CapabilityTools(registry).execute("discover_capabilities", {})
    assert not result["ok"] and result["error"]["code"] == "NO_AVAILABLE_NODES"
    assert result["result"]["nodes"][0]["error"]


@pytest.mark.asyncio
async def test_partial_discovery_preserves_healthy_node():
    good = DCRClient(MockTransport())
    await good.connect()
    try:
        registry = NodeRegistry()
        registry.register(DCRNodeAdapter(client=DCRClient(MockTransport()), node_id="offline", source="synthetic"))
        registry.register(DCRNodeAdapter(client=good, node_id="online", source="synthetic"))
        result = await CapabilityTools(registry).execute("discover_capabilities", {})
        assert result["ok"] and result["partial"]
        assert result["result"]["nodes"][0]["error"]
        assert len(result["result"]["nodes"][1]["capabilities"]) == 4
    finally:
        await good.disconnect()
