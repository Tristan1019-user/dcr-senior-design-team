"""Small mock-only control-plane model evaluation, not a general benchmark."""
import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import statistics
import time

from .agent import Agent
from .client import DCRClient
from .mock import MockTransport
from .models.openai_compatible import OpenAICompatibleBackend
from .recording import Recorder
from .tools import ToolExecutor

SCENARIOS = {
    "full_cycle": "Inspect both sensors. Stop sht45 and verify it is STOPPED. While sht45 stays stopped, inspect bmp280 twice with get_component or get_health until its temperature sample sequence advances between those two observations. Then initialize and restart sht45 and confirm both sensors have fresh healthy readings. Preserve bmp280 configuration and lifecycle throughout. Keep the sequence concise and report the observed evidence.",
    "inspect": "Inspect the available DCR components and report which sensors are healthy. Do not change anything.",
    "stop": "Stop sht45 and verify it is STOPPED. Leave bmp280 operating and verify it still produces fresh data.",
    "restart": "sht45 is stopped. Inspect it, initialize and start it, then verify it produces fresh data and both sensors are healthy.",
    "error_recovery": "Start sht45 if needed. It may already be running. Inspect actual state; do not stop or disrupt a healthy running sensor.",
    "diagnose": "Determine which sensor is failing without changing either sensor. Inspect health/freshness and report the failing component ID.",
    "nonexistent": "Inspect sensor_xyz. If it does not exist, report that exact ID is missing. Do not substitute or change another component.",
    "parameter": "Set sht45 period_ms to 500 using the explicit required lifecycle, restart it and verify fresh readings. Leave bmp280 unchanged.",
}


def assess(name, result, mock):
    """Conservative observable checks; final prose is not accepted as mutation evidence."""
    trace = result.trace
    successful = [t for t in trace if t["output"]["ok"]]
    mutations = [t for t in successful if t["tool"] in {"stop_component", "start_component", "init_component", "set_parameter"}]
    other_untouched = all(t["arguments"].get("component") == "sht45" for t in mutations)
    finished = result.outcome == "model_finished" and result.mutations_verified
    last_mutation = max((i for i, t in enumerate(trace) if t in mutations), default=-1)
    observations_after = [t for i, t in enumerate(trace) if i > last_mutation and t["output"]["ok"]]
    verified_b = any(any(c["id"] == "bmp280" and c.get("healthy") for c in t["output"]["result"].get("components", []))
                     or (t["tool"] == "get_component" and t["arguments"].get("component") == "bmp280"
                         and t["output"]["result"].get("healthy")) for t in observations_after)
    if name == "inspect":
        return finished and not mutations and any(t["tool"] == "get_health" for t in successful)
    if name == "full_cycle":
        ordered = [t["tool"] for t in mutations]
        if not (finished and other_untouched and verified_b and ordered == [
                "stop_component", "init_component", "start_component"]):
            return False
        stop_at = next(i for i, t in enumerate(trace) if t["tool"] == "stop_component" and t["output"]["ok"])
        init_at = next(i for i, t in enumerate(trace) if t["tool"] == "init_component" and t["output"]["ok"])
        before, stopped = [], []
        a_stopped_seen = False
        for i, t in enumerate(trace):
            if not t["output"]["ok"] or t["tool"] not in ("get_component", "get_components", "get_health"):
                continue
            data = t["output"]["result"]
            components = data.get("components", [data] if "id" in data else [])
            for c in components:
                if stop_at < i < init_at and c["id"] == "sht45" and c["state"] == "STOPPED":
                    a_stopped_seen = True
                if c["id"] == "bmp280" and c["state"] == "RUNNING":
                    sequence = c["outputs"]["temperature_c"]["sequence"]
                    if i < stop_at:
                        before.append(sequence)
                    elif stop_at < i < init_at:
                        stopped.append(sequence)
        return bool(a_stopped_seen and len(stopped) >= 2 and max(stopped) > min(stopped)
                    and mock.components["bmp280"]["config"]["period_ms"] == 1000)
    if name == "stop":
        return finished and other_untouched and bool(mutations) and mock.components["sht45"]["state"] == "STOPPED" and verified_b
    if name == "restart":
        return finished and other_untouched and any(t["tool"] == "start_component" for t in mutations) and verified_b
    if name == "error_recovery":
        return finished and not mutations and any(t["tool"] in ("get_component", "get_components", "get_health") for t in successful)
    if name == "diagnose":
        return finished and not mutations and "sht45" in result.text and any(t["tool"] in ("get_health", "get_errors") for t in successful)
    if name == "nonexistent":
        return finished and not mutations and "sensor_xyz" in result.text and any(
            t["tool"] in ("get_components", "get_component") for t in trace)
    if name == "parameter":
        return finished and other_untouched and mock.components["sht45"]["config"]["period_ms"] == 500 and mock.components["sht45"]["state"] == "RUNNING" and verified_b
    return False


async def evaluate(args):
    folder = Path(args.output)
    folder.mkdir(parents=True, exist_ok=True)
    report_path = folder / "results.json"
    if report_path.exists():
        raise ValueError("Use a new evidence directory; previous results are preserved")
    async with OpenAICompatibleBackend.from_env(timeout_seconds=30, max_context_tokens=args.context,
                                                max_output_tokens=args.output_tokens) as backend:
        report = {"started_at": time.time(), "model": await backend.model_info(), "mock_only": True,
                  "physical_device_access": False, "scenarios": [], "max_turns": args.max_turns,
                  "limitations": ["One run per scenario; not a statistical benchmark",
                                  "Parameter case checks lifecycle/config/freshness, not measured rate",
                                  "Healthy B after final mutation is checked; continuous acquisition requires a separate sequence trace",
                                  "Error recovery includes state inspection; injected-error test is offline unit coverage"]}
        for name in args.scenarios:
            record = Recorder(folder / f"{name}.jsonl")
            record.record("model", report["model"])
            mock = MockTransport()
            if name == "restart":
                mock.components["sht45"]["state"] = "STOPPED"
            if name == "diagnose":
                mock.faults.add("sht45")
            client = DCRClient(mock, recorder=record)
            try:
                await client.connect()
                agent = Agent(backend, ToolExecutor(client, ["sht45"]), max_turns=args.max_turns,
                              max_tool_steps=20, timeout=120, observation_delay=.5)
                result = await agent.run(SCENARIOS[name])
                row = {"scenario": name, "observable_checks_passed": assess(name, result, mock), **asdict(result)}
                row["successful_tool_calls"] = sum(t["output"]["ok"] for t in result.trace)
                row["invalid_argument_calls"] = sum(not t["output"]["ok"] and t["output"]["error"]["code"] == "INVALID_ARGUMENT" for t in result.trace)
                row["unsupported_tool_calls"] = sum(not t["output"]["ok"] and t["output"]["error"]["code"] == "UNKNOWN_TOOL" for t in result.trace)
                row["median_model_decision_seconds"] = statistics.median(result.decision_seconds) if result.decision_seconds else None
                report["scenarios"].append(row)
                report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
                print(name, result.outcome, "PASS" if row["observable_checks_passed"] else "NOT PASSED", flush=True)
            finally:
                await client.disconnect()
                record.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--context", type=int, default=4096)
    parser.add_argument("--output-tokens", type=int, default=512)
    parser.add_argument("--max-turns", type=int, choices=range(1, 31), default=12)
    parser.add_argument("--scenarios", nargs="+", choices=list(SCENARIOS), default=list(SCENARIOS))
    asyncio.run(evaluate(parser.parse_args()))


if __name__ == "__main__":
    main()
