"""Explicit, recorded sensor-only physical validation. No flash or pin changes."""
import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import statistics
import time
from types import SimpleNamespace

from .agent import Agent
from .client import DCRClient
from .evaluation import SCENARIOS, assess
from .models.openai_compatible import OpenAICompatibleBackend
from .recording import Recorder
from .tools import ToolExecutor
from .transport import SerialTransport


async def execute(args):
    folder = Path(args.output)
    folder.mkdir(parents=True, exist_ok=False)
    recorder = Recorder(folder / "session.jsonl")
    client = DCRClient(SerialTransport(args.port), recorder=recorder)
    tools = ToolExecutor(client, ["sht45", "bmp280"] if not args.agent else ["sht45"])
    report = {"physical": True, "mode": "agent" if args.agent else "deterministic",
              "started_at": time.time(), "passed": False, "checks": [],
              "physical_disconnection_tests": "deferred_by_user_not_passed"}

    def save():
        (folder / "results.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    async def tool(name, values=None):
        started = time.perf_counter()
        result = await tools.execute(name, values or {})
        report["checks"].append({"tool": name, "arguments": values or {},
                                 "seconds": time.perf_counter() - started, "ok": result["ok"]})
        if not result["ok"]:
            raise RuntimeError(f"{name}: {result['error']['code']}")
        return result["result"]

    async def observe(seconds, ids=("sht45", "bmp280")):
        seen = {name: {} for name in ids}
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            state = await tool("get_health")
            assert state["boot_id"] == report["boot_id"], "Unexpected board reboot"
            for component in state["components"]:
                if component["id"] in ids:
                    assert component["healthy"], f"Unhealthy {component['id']}"
                    sample = component["outputs"]["temperature_c"]
                    seen[component["id"]][sample["sequence"]] = sample["timestamp_ms"]
            await asyncio.sleep(.2)
        output = {}
        for name, samples in seen.items():
            ordered = sorted(samples.items())
            assert len(ordered) >= max(2, int(seconds / 2)), "Insufficient sample progression"
            intervals = [b[1] - a[1] for a, b in zip(ordered, ordered[1:]) if b[0] == a[0] + 1]
            output[name] = {"unique_samples": len(ordered), "first_sequence": ordered[0][0],
                            "last_sequence": ordered[-1][0], "consecutive_intervals": len(intervals),
                            "median_interval_ms": statistics.median(intervals) if intervals else None,
                            "min_interval_ms": min(intervals) if intervals else None,
                            "max_interval_ms": max(intervals) if intervals else None}
        return output

    async def cycle(name, period=None):
        await tool("get_component", {"component": name})
        await tool("stop_component", {"component": name})
        assert (await tool("get_component", {"component": name}))["state"] == "STOPPED"
        other = "bmp280" if name == "sht45" else "sht45"
        isolated = await observe(3, (other,))
        if period is not None:
            await tool("set_parameter", {"component": name, "name": "period_ms", "value": period})
            assert (await tool("get_component", {"component": name}))["config"]["period_ms"] == period
        await tool("init_component", {"component": name})
        assert (await tool("get_component", {"component": name}))["state"] == "INITIALIZED"
        await tool("start_component", {"component": name})
        await asyncio.sleep(1.2)
        assert (await tool("get_component", {"component": name}))["healthy"]
        return isolated

    try:
        await client.connect()
        report["boot_id"] = client.state.boot_id
        report["initial_health"] = await tool("get_health")
        if args.agent:
            async with OpenAICompatibleBackend.from_env(max_context_tokens=16384, max_output_tokens=1024) as backend:
                report["model"] = await backend.model_info()
                client.record("model", report["model"])
                report["agent_runs"] = []
                for name in ("full_cycle", "full_cycle", "full_cycle", "parameter"):
                    result = await Agent(backend, tools, max_turns=24, max_tool_steps=24,
                                         timeout=150, observation_delay=.5).run(SCENARIOS[name])
                    final = await tool("get_health")
                    state = SimpleNamespace(components={c["id"]: c for c in final["components"]})
                    passed = assess(name, result, state)
                    report["agent_runs"].append({"scenario": name, "passed": passed, **asdict(result)})
                    save()
                    print(name, "PASS" if passed else "FAILED", round(result.elapsed_seconds, 2), flush=True)
                    assert passed, "Agent task not independently verified"
                    assert all(c["healthy"] for c in final["components"])
                    assert final["boot_id"] == report["boot_id"]
                report["agent_500ms_measurement"] = await observe(10)
                await cycle("sht45", 1000)
        else:
            report["baseline_60s"] = await observe(60)
            report["sht45_stopped_bmp_continues"] = await cycle("sht45")
            report["bmp_stopped_sht45_continues"] = await cycle("bmp280")
            report["set500_isolation"] = await cycle("sht45", 500)
            report["configured_500ms"] = await observe(10)
            assert 450 <= report["configured_500ms"]["sht45"]["median_interval_ms"] <= 650
            await cycle("sht45", 1000)
            before = client.state.inspect("bmp280")["outputs"]["temperature_c"]["sequence"]
            await client.disconnect()
            await asyncio.sleep(3)
            await client.connect()
            assert client.state.boot_id == report["boot_id"], "Reconnect reset board"
            assert client.state.inspect("bmp280")["outputs"]["temperature_c"]["sequence"] > before
            report["host_disconnect_sensor_continuity"] = True
            for name, values, code in [
                ("start_component", {"component": "sht45"}, "INVALID_STATE"),
                ("set_parameter", {"component": "sht45", "name": "period_ms", "value": 100}, "INVALID_ARGUMENT"),
                ("get_component", {"component": "missing"}, "NOT_FOUND"),
            ]:
                result = await tools.execute(name, values)
                assert not result["ok"] and result["error"]["code"] == code
            report["negative_commands_rejected"] = True
        report["final_health"] = await tool("get_health")
        assert all(c["healthy"] for c in report["final_health"]["components"])
        assert report["final_health"]["boot_id"] == report["boot_id"]
        assert all(c["config"].get("period_ms", 1000) == 1000 for c in report["final_health"]["components"])
        report["passed"] = True
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
        raise
    finally:
        report["finished_at"] = time.time()
        save()
        await client.disconnect()
        recorder.close()
    print("DEMO PASS", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--agent", action="store_true")
    asyncio.run(execute(parser.parse_args()))


if __name__ == "__main__":
    main()
