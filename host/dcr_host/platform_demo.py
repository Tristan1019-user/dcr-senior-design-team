"""Recorded physical sensing + synthetic policy-action demo; no firmware changes."""
import argparse
import asyncio
import hashlib
import json
import math
import time
from dataclasses import asdict
from pathlib import Path

from .agent import Agent
from .capabilities import DCRNodeAdapter, NodeRegistry
from .client import DCRClient
from .control_tools import ControlTools
from .mock import MockTransport
from .platform import NodeRecorder
from .policies.simulation import SimulatedStage
from .policy_tools import PolicyTools
from .recording import Recorder, ReplayTransport, replay
from .transport import SerialTransport
from .workflows import sensor_cycle


def source_hashes():
    root = Path(__file__).parent
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*.py"))}


INSTRUCTION = """Discover available capabilities. Read lab sht45.temperature_c. If that
fresh healthy temperature is >=25 degC request stage target 30 mm, otherwise 20 mm.
The stage is SYNTHETIC: this is an illustrative integration test, not real motion
or temperature control. After the target request read stage position.read and
verify the requested target. Report physical versus synthetic sources honestly.
Do not use lifecycle commands. An offline node must not stop healthy nodes."""


def verify_model_trace(result, source):
    """Accept measured task evidence, never the model's prose or final state alone."""
    if result.outcome != "model_finished":
        raise RuntimeError("Model did not finish the task")
    discovered = False
    sample = None
    target = None
    acted = False
    verified = False
    for step in result.trace:
        name, args, output = step["tool"], step["arguments"], step["output"]
        if name not in {"discover_capabilities", "read_capability", "request_stage_target"}:
            raise RuntimeError("Unexpected model tool")
        if output.get("ok") is not True:
            raise RuntimeError("Model task contained a failed tool call")
        if name == "discover_capabilities":
            nodes = {node["node_id"]: node for node in output["result"]["nodes"]}
            discovered = (output.get("partial") is True
                          and nodes.get("offline", {}).get("error") is not None
                          and any(c["local_id"] == "sht45.temperature_c"
                                  for c in nodes.get("lab", {}).get("capabilities", [])))
            if not discovered:
                raise RuntimeError("Required capabilities and isolated fault not discovered")
        elif name == "request_stage_target":
            if not discovered or sample is None or acted or args != {"node_id": "stage", "target_mm": target}:
                raise RuntimeError("Action ordering, count or target mismatch")
            proof = output["result"]
            if (proof.get("verified") is not True or proof.get("source") != "synthetic"
                    or proof.get("node_id") != "stage" or proof.get("position_mm") != target):
                raise RuntimeError("Action readback mismatch")
            acted = True
        else:
            observations = output["result"]["observations"]
            if not discovered or len(observations) != 1:
                raise RuntimeError("Read without discovery or exact observation")
            obs = observations[0]
            if (obs.get("fresh") is not True or obs.get("healthy") is not True
                    or type(obs.get("value")) not in (int, float) or not math.isfinite(obs["value"])):
                raise RuntimeError("Unusable model observation")
            if args.get("node_id") == "lab" and not acted:
                if (args.get("capability_id") not in ("sht45.temperature_c", "lab:sht45.temperature_c")
                        or obs.get("node_id") != "lab" or obs.get("capability_id") != "lab:sht45.temperature_c"
                        or obs.get("unit") != "degC" or obs.get("source") != source):
                    raise RuntimeError("Wrong sensor or provenance")
                sample = obs
                target = 30.0 if sample["value"] >= 25 else 20.0
            elif args.get("node_id") == "stage" and acted:
                if (args.get("capability_id") not in ("position.read", "stage:position.read")
                        or obs.get("node_id") != "stage" or obs.get("capability_id") != "stage:position.read"
                        or obs.get("unit") != "mm" or obs.get("source") != "synthetic" or obs["value"] != target):
                    raise RuntimeError("Model post-action observation mismatch")
                verified = True
            else:
                raise RuntimeError("Unexpected model observation order")
    if not acted or not verified:
        raise RuntimeError("Model did not request and independently verify the target")
    return sample, target


async def run(args):
    folder = Path(args.output)
    folder.mkdir(parents=True, exist_ok=False)
    record = Recorder(folder / "session.jsonl")
    client = DCRClient(MockTransport() if args.mock else SerialTransport(args.port),
                       recorder=NodeRecorder(record, "lab"))
    report = {"schema": "physical-ai-demo/v1", "passed": False, "mode": args.mode,
              "sensor_source": "synthetic" if args.mock else "physical_serial",
              "actuator_source": "synthetic", "physical_actuation": False,
              "trained_policy": False, "world_model": False, "source_sha256": source_hashes()}
    started = time.monotonic()
    phase = "connect"
    try:
        record.record("experiment", report)
        await client.connect()
        configuration = await client.call("get_configuration", {})
        before = await client.refresh()
        if not all(c["healthy"] for c in before["components"]):
            raise RuntimeError("Initial device health failed")
        phase = "bind_adapters"
        registry = NodeRegistry()
        registry.register(DCRNodeAdapter("lab", client, source=report["sensor_source"]))
        registry.register(DCRNodeAdapter("offline", DCRClient(MockTransport()), source="synthetic"))
        stage = SimulatedStage("stage", authorized=True)
        registry.register(stage)
        control = ControlTools(registry, {"lab": client}, {"lab": ["sht45"]} if args.cycle else {}, recorder=record)
        if args.cycle:
            phase = "sensor_cycle"
            report["cycle"] = await sensor_cycle(control, "lab")
            if not report["cycle"]["passed"]:
                raise RuntimeError("Sensor recovery workflow failed; inspect cycle evidence")
        tools = PolicyTools(control, {"stage": stage})
        phase = "hybrid_task"
        if args.mode == "model":
            from .models.openai_compatible import OpenAICompatibleBackend
            async with OpenAICompatibleBackend.from_env(max_output_tokens=1024) as backend:
                record.record("model", await backend.model_info())
                result = await Agent(backend, tools, max_turns=12, max_tool_steps=12).run(INSTRUCTION)
            report["agent"] = asdict(result)
            sample, target = verify_model_trace(result, report["sensor_source"])
            report["model_trace_verified"] = True
        else:
            record.record("instruction", {"text": INSTRUCTION, "executor": "deterministic_baseline"})
            discovery = await tools.execute("discover_capabilities", {})
            if not discovery["ok"] or not discovery["partial"]:
                raise RuntimeError("Expected partial node availability")
            output = await tools.execute("read_capability", {"node_id": "lab", "capability_id": "sht45.temperature_c"})
            if not output["ok"]:
                raise RuntimeError("Physical observation unavailable")
            sample = output["result"]["observations"][0]
            target = 30.0 if sample["value"] >= 25 else 20.0
            outcome = await tools.execute("request_stage_target", {"node_id": "stage", "target_mm": target})
            if not outcome["ok"] or not outcome["result"]["verified"]:
                raise RuntimeError("Synthetic target not verified")
        if not sample["fresh"] or not sample["healthy"] or sample["source"] != report["sensor_source"]:
            raise RuntimeError("Observation provenance or freshness mismatch")
        observed = await tools.execute("read_capability", {"node_id": "stage", "capability_id": "position.read"})
        if not observed["ok"]:
            raise RuntimeError("Stage observation failed")
        position = observed["result"]["observations"][0]
        if not observed["ok"] or position["source"] != "synthetic" or not math.isclose(position["value"], target):
            raise RuntimeError("Stage outcome mismatch")
        # A malicious policy proposal is rejected without changing the stage.
        phase = "negative_policy_check"
        now = time.monotonic()
        snapshot = stage.snapshot(now)
        invalid = {"node_id": "stage", "boot_id": snapshot.boot_id, "command_id": "denied-demo",
                   "created_at": now, "expires_at": now+1, "unit": "mm", "frame": "stage", "positions": [101.0]}
        rejected = stage.execute(invalid, snapshot, now)
        record.record("negative_policy_check", rejected)
        if rejected["ok"] or stage.snapshot(now).position_mm != target:
            raise RuntimeError("Unsafe proposal changed simulation state")
        phase = "final_board_check"
        final = await client.refresh()
        unchanged = await client.call("get_configuration", {}) == configuration
        if final["boot_id"] != before["boot_id"] or not unchanged or not all(c["healthy"] for c in final["components"]):
            raise RuntimeError("Final board state differs from baseline")
        report.update(passed=True, observation=sample, target_mm=target, final_stage=position,
                      invalid_policy_rejected=True, configuration_unchanged=unchanged,
                      same_boot=True, healthy_components=len(final["components"]))
    except Exception as exc:
        report["failure"] = type(exc).__name__
        report["failed_phase"] = phase
    finally:
        try:
            await client.disconnect()
        finally:
            record.close()
    requests = [row["payload"]["data"] for row in replay(folder / "session.jsonl") if row["kind"] == "node.request"]
    report["wire_mutations"] = [r for r in requests if not r["op"].startswith("get_")]
    expected = ["stop_component", "init_component", "start_component"] if args.cycle else []
    if report["passed"] and [r["op"] for r in report["wire_mutations"]] != expected:
        report.update(passed=False, failure="UnexpectedMutationSequence")
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    # Replay only the recorded handshake/config prefix, never a hardware transport.
    if report["passed"]:
        replay_client = DCRClient(ReplayTransport(folder / "session.jsonl", node_id="lab"))
        try:
            await replay_client.connect()
            replay_config = await replay_client.call("get_configuration", {})
            report["offline_replay_prefix_passed"] = replay_config == configuration
            report["passed"] = report["passed"] and report["offline_replay_prefix_passed"]
        except Exception as exc:
            report.update(passed=False, offline_replay_prefix_passed=False,
                          failure=type(exc).__name__, failed_phase="offline_replay_prefix")
        finally:
            await replay_client.disconnect()
    with (folder / "result.json").open("x", encoding="utf-8") as output:
        json.dump(report, output, indent=2, allow_nan=False)
    print(json.dumps({key: value for key, value in report.items()
                      if key not in ("agent", "cycle", "wire_mutations", "source_sha256")}, indent=2))
    return 0 if report["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--mock", action="store_true")
    source.add_argument("--port")
    parser.add_argument("--mode", choices=("deterministic", "model"), default="deterministic")
    parser.add_argument("--cycle", action="store_true", help="Explicitly authorize a verified physical sensor stop/init/start")
    parser.add_argument("--output", required=True, help="NEW evidence directory")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
