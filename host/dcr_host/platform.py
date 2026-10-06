"""Explicit node access; read-only unless recorded component permissions are supplied."""
import argparse
import asyncio
import json
import time
from dataclasses import asdict

from .agent import Agent
from .capabilities import DCRNodeAdapter, NodeRegistry
from .client import DCRClient
from .mock import MockTransport
from .protocol import Name
from .recording import Recorder
from .semantic_tools import CapabilityTools
from .transport import SerialTransport
from pydantic import TypeAdapter


class NodeRecorder:
    def __init__(self, recorder, node_id):
        self.recorder, self.node_id = recorder, node_id

    def record(self, kind, payload):
        if self.recorder:
            self.recorder.record("node." + kind, {"node_id": self.node_id, "data": payload})


def node_specs(nodes, mocks):
    """Validate every binding before opening any device."""
    specs, ids, ports = [], set(), set()
    for binding in nodes:
        node, separator, port = binding.partition("=")
        if not separator or not port or port != port.strip():
            raise ValueError("Use --node NODE=PORT")
        TypeAdapter(Name).validate_python(node)
        endpoint = port.removeprefix("\\\\.\\").casefold()
        if node in ids or endpoint in ports:
            raise ValueError("Duplicate node or serial endpoint")
        specs.append((node, port))
        ids.add(node)
        ports.add(endpoint)
    for node in mocks:
        TypeAdapter(Name).validate_python(node)
        if node in ids:
            raise ValueError("Duplicate node identity")
        specs.append((node, None))
        ids.add(node)
    if not 1 <= len(specs) <= 16:
        raise ValueError("Explicitly configure 1..16 nodes")
    return specs


def permission_specs(bindings, nodes):
    permissions = {}
    for binding in bindings:
        node, separator, component = binding.partition(":")
        if not separator or node not in nodes:
            raise ValueError("Permission requires a configured NODE:COMPONENT")
        TypeAdapter(Name).validate_python(component)
        permissions.setdefault(node, set()).add(component)
    return permissions


async def run(args):
    specs = node_specs(args.node, args.mock_node)
    permissions = permission_specs(getattr(args, "allow", []), {node for node, _ in specs})
    if (args.command in ("agent", "cycle") or permissions) and not args.record:
        raise ValueError("Agent/control experiments require --record")
    recorder = Recorder(args.record) if args.record else None
    registry, clients = NodeRegistry(), []
    try:
        if recorder:
            recorder.record("experiment", {"schema": "physical-ai-observation/v1",
                "started_at": time.time(), "read_only": not bool(permissions),
                "permissions": {node: sorted(names) for node, names in permissions.items()},
                "nodes": [{"node_id": node, "source": "synthetic" if port is None else "physical_serial"}
                          for node, port in specs]})
        for node, port in specs:
            client = DCRClient(MockTransport() if port is None else SerialTransport(port),
                               recorder=NodeRecorder(recorder, node))
            clients.append(client)
            registry.register(DCRNodeAdapter(client=client, node_id=node,
                                             source="synthetic" if port is None else "physical_serial"))
            try:
                await client.connect()
            except Exception as exc:
                if recorder:
                    recorder.record("node_connection_failure", {"node_id": node, "error_type": type(exc).__name__})
                try:
                    await client.disconnect()
                except Exception as cleanup_error:
                    if recorder:
                        recorder.record("node_cleanup_failure", {"node_id": node,
                            "error_type": type(cleanup_error).__name__})
                # Keep its identity registered and visibly unavailable, never substitute a mock.
        if permissions or args.command in ("inspect", "cycle"):
            from .control_tools import ControlTools
            executor = ControlTools(registry, {node: client for (node, _), client in zip(specs, clients)},
                                    permissions, recorder=recorder)
        else:
            executor = CapabilityTools(registry, recorder)
        if args.command == "cycle":
            from .workflows import sensor_cycle
            output = await sensor_cycle(executor, args.node_id, args.component,
                                        args.companion_node, args.companion_capability)
            print(json.dumps(output, indent=2, allow_nan=False))
            return 0 if output["passed"] else 1
        if args.command == "agent":
            from .models.openai_compatible import OpenAICompatibleBackend
            async with OpenAICompatibleBackend.from_env() as backend:
                executor.record("model", await backend.model_info())
                result = await Agent(backend, executor, max_turns=12, max_tool_steps=12).run(args.instruction)
                output = asdict(result)
            print(json.dumps(output, indent=2, allow_nan=False))
            return 0 if result.outcome == "model_finished" else 1
        name = {"discover": "discover_capabilities", "observe": "read_capability", "inspect": "inspect_node"}[args.command]
        arguments = {} if args.command == "discover" else {"node_id": args.node_id}
        if args.command == "observe":
            arguments["capability_id"] = args.capability_id
        output = await executor.execute(name, arguments)
        executor.record("outcome", {"command": args.command, "ok": output["ok"], "read_only": name != "control_component"})
        print(json.dumps(output, indent=2, allow_nan=False))
        return 0 if output["ok"] else 1
    finally:
        try:
            results = await asyncio.gather(*(client.disconnect() for client in clients),
                                           return_exceptions=True)
            failed = [type(result).__name__ for result in results if isinstance(result, BaseException)]
            if failed:
                if recorder:
                    recorder.record("cleanup_failure", {"error_types": failed})
                raise RuntimeError("One or more node transports failed to close")
        finally:
            if recorder:
                recorder.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", action="append", default=[], metavar="ID=PORT")
    parser.add_argument("--mock-node", action="append", default=[], metavar="ID")
    parser.add_argument("--record", help="Create a new experiment JSONL; never overwrite")
    parser.add_argument("--allow", action="append", default=[], metavar="NODE:COMPONENT",
                        help="Explicit sensor lifecycle permission; requires recording")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("discover")
    read = sub.add_parser("observe")
    read.add_argument("node_id")
    read.add_argument("capability_id")
    sub.add_parser("inspect").add_argument("node_id")
    cycle = sub.add_parser("cycle", help="Verified stop/init/start regression; permissions required")
    cycle.add_argument("node_id")
    cycle.add_argument("--component", default="sht45")
    cycle.add_argument("--companion-node")
    cycle.add_argument("--companion-capability", default="bmp280.temperature_c")
    sub.add_parser("agent").add_argument("instruction")
    try:
        code = asyncio.run(run(parser.parse_args()))
    except KeyboardInterrupt:
        code = 130
    except Exception as exc:
        print(f"Platform operation failed ({type(exc).__name__}); no automatic retry. Inspect state before further control.")
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
