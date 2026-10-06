"""Typed, least-authority tools. The model never receives a transport object."""
import asyncio
from typing import Annotated

from pydantic import Field, StrictInt, ValidationError

from .protocol import (ComponentArgs, DeviceError, Empty, MUTATIONS, OPERATIONS,
                       ProtocolError, StrictModel)
from .transport import TransportError


class RecentArgs(ComponentArgs):
    limit: Annotated[StrictInt, Field(ge=1, le=100)]


TOOL_ARGS = {**OPERATIONS, "get_health": Empty, "get_errors": Empty, "get_recent_samples": RecentArgs}
DESCRIPTIONS = {
    "get_runtime_info": "Identify runtime protocol, boot identity, and supported operations.",
    "get_components": "List actual component IDs, lifecycle states, configs and latest outputs. Inspect before mutation.",
    "get_component": "Inspect one actual component including freshness and health. Also verifies prior mutation.",
    "get_topology": "Inspect current component connections; read-only.",
    "get_configuration": "Inspect runtime RAM configuration, not a persistent configuration export.",
    "get_health": "Refresh health and sample age for all components. Verify new readings after restart.",
    "get_errors": "List components currently not healthy, including stopped or stale components.",
    "get_recent_samples": "Read bounded host-observed sample history for a component; not lossless device history.",
    "stop_component": "Stop and join one authorized sensor; preserves unrelated tasks. Inspect and verify STOPPED.",
    "init_component": "Reinitialize a STOPPED sensor, acquire hardware, invalidate old samples. Does not start sampling.",
    "start_component": "Start an INITIALIZED sensor. STOPPED requires explicit init first. Verify fresh healthy readings.",
    "set_parameter": "Change period_ms 250..2000 only while STOPPED, RAM-only. Then explicitly init and start.",
}


def definitions():
    return [{"type": "function", "function": {
        "name": name, "description": DESCRIPTIONS[name],
        "parameters": schema.model_json_schema(), "strict": True}}
        for name, schema in TOOL_ARGS.items()]


class ToolExecutor:
    def __init__(self, client, allowed_components=()):
        self.client = client
        self.allowed = frozenset(allowed_components)
        self.inspected = set()
        self.unverified = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def definitions():
        return definitions()

    def record(self, kind, payload):
        self.client.record(kind, payload)

    async def execute(self, name, arguments):
        async with self._lock:
            try:
                if name not in TOOL_ARGS:
                    return self.error("UNKNOWN_TOOL", "Tool is not available")
                args = TOOL_ARGS[name].model_validate(arguments).model_dump()
                if name in MUTATIONS:
                    component = args["component"]
                    if component not in self.allowed:
                        return self.error("FORBIDDEN", "Component mutation is not authorized")
                    if component not in self.inspected:
                        return self.error("INSPECTION_REQUIRED", "Inspect the component before mutation")
                    if component in self.unverified:
                        return self.error("VERIFICATION_REQUIRED", "Read get_component or get_health to verify the previous mutation before changing this component again")
                    # Re-read immediately so stale model context cannot satisfy a precondition.
                    current = await self.client.call("get_component", {"component": component})
                    valid_states = {"stop_component": {"RUNNING", "INITIALIZED", "FAULTED"},
                                    "init_component": {"STOPPED"}, "start_component": {"INITIALIZED"},
                                    "set_parameter": {"STOPPED"}}
                    if current["state"] not in valid_states[name]:
                        return self.error("INVALID_STATE", "Explicit stop -> configure -> init -> start lifecycle required")
                    self.unverified[component] = {
                        "state": {"stop_component": "STOPPED", "init_component": "INITIALIZED",
                                  "start_component": "RUNNING", "set_parameter": "STOPPED"}[name],
                        "period_ms": args.get("value") if name == "set_parameter" else None,
                    }
                if name in ("get_health", "get_errors", "get_recent_samples"):
                    result = await self.client.refresh()
                    if name == "get_errors":
                        result = {"components": [c for c in result["components"] if not c["healthy"]]}
                    elif name == "get_recent_samples":
                        if args["component"] not in self.client.state.components:
                            return self.error("NOT_FOUND", "Unknown component")
                        result = {"samples": [s for s in self.client.state.history
                                              if s["component"] == args["component"]][-args["limit"]:]}
                    else:
                        self._observe(result["components"])
                else:
                    result = await self.client.call(name, args)
                    if name == "get_components":
                        self._observe([self.client.state.inspect(c["id"]) for c in result["components"]])
                    elif name == "get_component":
                        result = self.client.state.inspect(args["component"])
                        self._observe([result])
                output = {"ok": True, "result": result}
                self.client.record("tool_result", {"tool": name, "args": args, **output})
                return output
            except (ValidationError, ProtocolError, TypeError, ValueError):
                return self.error("INVALID_ARGUMENT", "Arguments failed strict schema validation")
            except DeviceError as exc:
                return self.error(exc.code, exc.message)
            except TransportError:
                return self.error("DEVICE_UNAVAILABLE", "Outcome may be unknown; reconnect and inspect")

    def _observe(self, components):
        for c in components:
            self.inspected.add(c["id"])
            expected = self.unverified.get(c["id"])
            if expected and c["state"] == expected["state"]:
                if c["state"] == "RUNNING" and not c.get("healthy"):
                    continue
                if expected["period_ms"] is not None and c["config"].get("period_ms") != expected["period_ms"]:
                    continue
                del self.unverified[c["id"]]

    @staticmethod
    def error(code, message):
        return {"ok": False, "error": {"code": code, "message": message}}
