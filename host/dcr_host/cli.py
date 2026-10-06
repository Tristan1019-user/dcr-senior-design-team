"""Deterministic command line; mock is explicit, serial never silently substituted."""
import argparse
import asyncio
import json
from dataclasses import asdict

from rich.console import Console
from rich.live import Live
from rich.table import Table

from .client import DCRClient
from .mock import MockTransport
from .recording import Recorder, replay
from .recovery import recover
from .tools import ToolExecutor
from .transport import SerialTransport


def table(health):
    result = Table("Component", "State", "Health", "Outputs (value / age ms)")
    for c in health["components"]:
        values = ", ".join(f"{p}={s['value']} / {s['age_ms']}" for p, s in c["outputs"].items())
        result.add_row(c["id"], c["state"], "healthy" if c["healthy"] else "not healthy", values)
    return result


async def watch_health(client, poll=False):
    # Serial snapshots already refresh state; mock has no background publisher.
    return await client.refresh() if poll else client.state.health()


async def run(args):
    if args.command == "replay":
        for row in replay(args.file):
            print(json.dumps(row))
        return 0
    if not args.mock and not args.port:
        raise ValueError("Choose --mock or --port explicitly")
    if args.command == "watch" and not 0 < args.seconds <= 3600:
        raise ValueError("Watch duration must be between zero and 3600 seconds")
    if args.command == "agent" and not args.mock:
        if not getattr(args, "enable_live_agent", False) or not getattr(args, "record", None):
            raise ValueError("Live agent access is gated: require --enable-live-agent and --record")
    recorder = Recorder(args.record) if args.record else None
    client = DCRClient(MockTransport() if args.mock else SerialTransport(args.port), recorder=recorder)
    executor = ToolExecutor(client, args.allow_component)
    try:
        await client.connect()
        if args.command in ("recover", "recovery-plan"):
            result = await recover(executor, args.component, args.period_ms, args.command == "recover")
            print(json.dumps(result, indent=2))
            return 0 if result["ok"] else 1
        if args.command == "agent":
            from .agent import Agent
            from .models.openai_compatible import OpenAICompatibleBackend
            async with OpenAICompatibleBackend.from_env() as backend:
                client.record("model", await backend.model_info())
                result = await Agent(backend, executor, max_turns=args.max_turns).run(args.instruction)
                print(json.dumps(asdict(result), indent=2))
                return 0 if result.outcome == "model_finished" and result.mutations_verified else 1
        if args.command == "watch":
            deadline = asyncio.get_running_loop().time() + args.seconds
            with Live(table(await watch_health(client, args.mock)), console=Console(), refresh_per_second=4) as live:
                while asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(.25)
                    live.update(table(await watch_health(client, args.mock)))
                    if not client.state.connected:
                        return 1
            return 0
        mapping = {"status": "get_runtime_info", "components": "get_components",
                   "inspect": "get_component", "stop": "stop_component", "start": "start_component",
                   "init": "init_component", "set": "set_parameter", "health": "get_health",
                   "errors": "get_errors", "topology": "get_topology", "configuration": "get_configuration"}
        params = {"component": args.component} if hasattr(args, "component") else {}
        if args.command == "set":
            params.update(name="period_ms", value=args.value)
        if args.command in ("stop", "start", "init", "set"):
            await executor.execute("get_component", {"component": args.component})
        result = await executor.execute(mapping[args.command], params)
        if result["ok"] and args.command in ("stop", "start", "init", "set"):
            if args.command == "start":
                await asyncio.sleep(2.2)
            observed = await executor.execute("get_component", {"component": args.component})
            result["verification"] = observed
            result["verified"] = not executor.unverified and observed["ok"]
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] and result.get("verified", True) else 1
    finally:
        await client.disconnect()
        if recorder:
            recorder.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    transport = parser.add_mutually_exclusive_group()
    transport.add_argument("--mock", action="store_true")
    transport.add_argument("--port")
    parser.add_argument("--allow-component", action="append", default=[])
    parser.add_argument("--record")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "components", "health", "errors", "topology", "configuration"):
        sub.add_parser(name)
    for name in ("inspect", "stop", "start", "init"):
        sub.add_parser(name).add_argument("component")
    for name in ("recovery-plan", "recover"):
        recovery = sub.add_parser(name, help="Explicit sensor recovery; recovery-plan is read-only")
        recovery.add_argument("component")
        recovery.add_argument("--period-ms", type=int, default=None)
    setting = sub.add_parser("set")
    setting.add_argument("component")
    setting.add_argument("value", type=int, help="period_ms, only while STOPPED")
    watch = sub.add_parser("watch")
    watch.add_argument("--seconds", type=float, default=30)
    sub.add_parser("replay").add_argument("file")
    agent = sub.add_parser("agent", help="Typed model tools; live mode requires explicit opt-in and recording")
    agent.add_argument("instruction")
    agent.add_argument("--max-turns", type=int, choices=range(1, 31), default=20)
    agent.add_argument("--enable-live-agent", action="store_true",
                       help="Explicit sensor-only live execution; requires --record and mutation allowlist")
    args = parser.parse_args()
    try:
        code = asyncio.run(run(args))
    except KeyboardInterrupt:
        code = 130
    except Exception as exc:
        print(f"DCR operation failed ({type(exc).__name__}); no automatic retry")
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
