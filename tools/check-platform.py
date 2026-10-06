"""Bounded read-only acceptance on one connected DCR board; never flashes or resets."""
import argparse
import asyncio
import hashlib
import json
import statistics
import time
from pathlib import Path

from dcr_host.capabilities import DCRNodeAdapter, NodeRegistry
from dcr_host.client import DCRClient
from dcr_host.mock import MockTransport
from dcr_host.platform import NodeRecorder
from dcr_host.recording import Recorder, replay
from dcr_host.semantic_tools import CapabilityTools
from dcr_host.transport import SerialTransport


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


async def check(port, directory):
    directory.mkdir(parents=True, exist_ok=False)
    trace = directory / "session.jsonl"
    recorder = Recorder(trace)
    physical = DCRClient(SerialTransport(port), recorder=NodeRecorder(recorder, "lab"))
    synthetic = DCRClient(MockTransport(), recorder=NodeRecorder(recorder, "simulation"))
    durations, samples = [], []
    started = time.monotonic()
    try:
        recorder.record("experiment", {"schema": "physical-ai-observation/v1", "read_only": True,
                                      "physical_nodes": 1, "synthetic_nodes": 1, "offline_test_nodes": 1})
        await physical.connect()
        await synthetic.connect()
        before = await physical.call("get_configuration", {})
        boot = physical.state.boot_id
        registry = NodeRegistry()
        registry.register(DCRNodeAdapter(client=physical, node_id="lab", source="physical_serial"))
        registry.register(DCRNodeAdapter(client=synthetic, node_id="simulation", source="synthetic"))
        registry.register(DCRNodeAdapter(client=DCRClient(MockTransport()), node_id="offline", source="synthetic"))
        tools = CapabilityTools(registry, recorder)
        discovery = await tools.execute("discover_capabilities", {})
        assert discovery["ok"] and discovery["partial"], discovery
        nodes = {node["node_id"]: node for node in discovery["result"]["nodes"]}
        assert nodes["offline"]["error"]["code"] == "unavailable"
        caps = nodes["lab"]["capabilities"]
        assert len(caps) == 4 and all(cap["source"] == "physical_serial" for cap in caps)
        assert {cap["local_id"] for cap in caps} == {cap["local_id"] for cap in nodes["simulation"]["capabilities"]}
        for cycle in range(3):
            for cap in caps:
                start = time.monotonic()
                result = await tools.execute("read_capability", {"node_id": "lab", "capability_id": cap["local_id"]})
                durations.append((time.monotonic() - start) * 1000)
                assert result["ok"], result
                sample = result["result"]["observations"][0]
                assert sample["fresh"] and sample["healthy"] and sample["boot_id"] == boot, sample
                assert sample["source"] == "physical_serial" and sample["unit"] == cap["unit"]
                samples.append(sample)
            if cycle < 2:
                await asyncio.sleep(1.1)
        for cap in caps:
            series = [sample for sample in samples if sample["capability_id"] == "lab:" + cap["local_id"]]
            assert series[-1]["sequence"] > series[0]["sequence"], series
        after = await physical.call("get_configuration", {})
        health = await physical.refresh()
        assert before == after and health["boot_id"] == boot
        assert len(health["components"]) == 8 and all(c["healthy"] for c in health["components"]), health
        result = {"status": "passed", "scope": "read_only_semantic_observation", "physical_nodes": 1,
                  "synthetic_nodes": 1, "offline_node_isolated": True, "physical_capabilities": len(caps),
                  "physical_observations": len(samples), "fresh_healthy_observations": len(samples),
                  "source_sequence_progress": True, "boot_id": boot, "same_boot": True,
                  "configuration_unchanged": True, "configuration_sha256": digest(after),
                  "healthy_components": len(health["components"]),
                  "read_latency_ms": {"median": round(statistics.median(durations), 2),
                                      "max": round(max(durations), 2), "count": len(durations)},
                  "elapsed_seconds": round(time.monotonic() - started, 2), "samples": samples}
        recorder.record("outcome", result)
    finally:
        try:
            await asyncio.gather(physical.disconnect(), synthetic.disconnect())
        finally:
            recorder.close()
    requests = [r["payload"]["data"]["op"] for r in replay(trace) if r["kind"] == "node.request"]
    assert requests and all(op.startswith("get_") for op in requests)
    result["only_read_commands"] = True
    result["request_count"] = len(requests)
    with (directory / "result.json").open("x", encoding="utf-8") as output:
        json.dump(result, output, indent=2, allow_nan=False)
    print(json.dumps({key: value for key, value in result.items() if key != "samples"}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--output", type=Path, required=True, help="New directory, never overwrite evidence")
    args = parser.parse_args()
    asyncio.run(check(args.port, args.output))
