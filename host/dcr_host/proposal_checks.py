"""Read-only physical checks with an explicitly injected model transport outage."""
import argparse
import asyncio
import json
from pathlib import Path
import time

import httpx

from .agent import Agent
from .client import DCRClient
from .models.openai_compatible import OpenAICompatibleBackend
from .recording import Recorder
from .tools import ToolExecutor
from .transport import SerialTransport


def check_bus_contract(topology, configuration):
    """Verify the deployed demo's inspectable contract, not driver behavior."""
    components = {c["id"]: c for c in topology["components"]}
    configured = {c["id"]: c for c in configuration["components"]}
    edges = {(edge["from"], edge["to"]) for edge in topology["connections"]}
    if len(components) != 8 or set(components) != set(configured):
        raise ValueError("Expected the eight-component bus demo")
    for sensor, worker in (("sht45", "i2c_bus_0"), ("bmp280", "i2c_bus_1")):
        ports = {p["name"]: p for p in components[worker]["ports"]}
        request = ports["request"]
        if (components[worker]["type"] != "i2c_bus" or
                request["direction"] != "input" or request["type"] != "bus_request" or
                request["delivery"] != "queue" or not request["multi_producer"]):
            raise ValueError("Missing typed multi-producer bus queue")
        client = {p["name"]: p for p in components[sensor]["ports"]}["bus"]
        if client["direction"] != "output" or client["type"] != "bus_request":
            raise ValueError("Sensor must publish structured bus requests")
        if (f"{sensor}.bus", f"{worker}.request") not in edges:
            raise ValueError("Sensor bus route missing")
    settings = configuration["settings"]
    if settings["watchdog_timeout_ms"] != 5000 or settings["signal_wait_timeout_ms"] != 100:
        raise ValueError("Global runtime settings do not match the packaged demo")
    if "response_queue" in json.dumps(topology):
        raise ValueError("Private reply handles must not be exposed in topology")
    return {"passed": True, "component_count": len(components),
            "bus_workers": 2, "typed_request_routes": 2, "settings_visible": True}


async def check_model_independence(client, observation_seconds=5):
    if not 2 <= observation_seconds <= 60:
        raise ValueError("Observation must be 2..60 seconds")
    initial = await client.refresh()
    sensors = ("sht45", "bmp280")
    before = {c["id"]: c for c in initial["components"]}
    if not all(name in before and before[name]["healthy"] for name in sensors):
        raise RuntimeError("Healthy sensors required before outage injection")
    requests = []

    async def unavailable(request):
        requests.append(request.url.path)
        await asyncio.sleep(observation_seconds)
        raise httpx.ConnectError("Deliberate test outage", request=request)

    # MockTransport affects only this client's model HTTP path, never the LAN service.
    async with OpenAICompatibleBackend(
        base_url="http://injected-outage.invalid/v1", model_name="outage-test",
        transport=httpx.MockTransport(unavailable), timeout_seconds=65,
    ) as backend:
        result = await Agent(backend, ToolExecutor(client), timeout=70, turn_timeout=65).run(
            "Inspect sensor health without modifying anything."
        )
    final = await client.refresh()
    after = {c["id"]: c for c in final["components"]}
    progression = {}
    for name in sensors:
        old, new = before[name], after[name]
        progression[name] = new["outputs"]["temperature_c"]["sequence"] - old["outputs"]["temperature_c"]["sequence"]
        if not new["healthy"] or new["config"] != old["config"] or progression[name] < 2:
            raise RuntimeError(f"Acquisition continuity failed for {name}")
    if initial["boot_id"] != final["boot_id"]:
        raise RuntimeError("Device rebooted during outage test")
    if result.outcome != "model_failure" or result.tool_steps != 0 or len(requests) != 1:
        raise RuntimeError("Expected one failed model request without tools or retry")
    return {
        "passed": True, "fault": "in_process_model_transport_failure",
        "real_network_disconnected": False, "model_service_modified": False,
        "model_outcome": result.outcome, "model_requests": len(requests),
        "tool_mutations": 0, "same_boot": True, "sequence_advances": progression,
        "observation_seconds": observation_seconds, "final_health": final,
    }


def check_connection_locks(configuration):
    """Check deployed policy metadata; native tests separately exercise enforcement."""
    expected = [
        {"id": "preserve_sht_bus", "type": "connection_locked",
         "from": "sht45.bus", "to": "i2c_bus_0.request"},
        {"id": "preserve_bmp_bus", "type": "connection_locked",
         "from": "bmp280.bus", "to": "i2c_bus_1.request"},
    ]
    if configuration.get("safety_rules") != expected:
        raise ValueError("Device does not report the two expected immutable bus locks")
    return {"passed": True, "rule_count": 2,
            "scope": "device-reported boot policy; not a remote graph-mutation test"}


async def run(args):
    folder = Path(args.output)
    folder.mkdir(parents=True, exist_ok=False)
    recorder = Recorder(folder / "session.jsonl")
    client = DCRClient(SerialTransport(args.port), recorder=recorder)
    report = {"passed": False, "started_at": time.time(), "physical_sensors": True}
    try:
        await client.connect()
        report["model_outage"] = await check_model_independence(client)
        report["topology"] = await client.call("get_topology", {})
        report["configuration"] = await client.call("get_configuration", {})
        report["bus_contract"] = check_bus_contract(report["topology"], report["configuration"])
        if args.require_connection_locks:
            report["connection_locks"] = check_connection_locks(report["configuration"])
        report["passed"] = True
        print("PROPOSAL CHECKS PASS")
    finally:
        report["finished_at"] = time.time()
        (folder / "results.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        await client.disconnect()
        recorder.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--require-connection-locks", action="store_true")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
