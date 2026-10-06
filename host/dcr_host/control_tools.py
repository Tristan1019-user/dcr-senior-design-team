"""Bounded, qualified lifecycle controls layered over the existing host tools."""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from typing import Annotated, Any, Literal

from pydantic import Field, StrictInt, StrictStr, ValidationError

from .capabilities import DCRNodeAdapter, NodeRegistry
from .client import DCRClient
from .protocol import Name, StrictModel
from .semantic_tools import CapabilityTools
from .tools import ToolExecutor
from .transport import TransportError

CommandId = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")]
BootId = Annotated[StrictStr, Field(min_length=1, max_length=96)]


class InspectNodeArgs(StrictModel):
    node_id: Name


class ControlComponentArgs(StrictModel):
    node_id: Name
    component: Name
    operation: Literal["stop", "init", "start", "set_period"]
    expected_boot_id: BootId
    command_id: CommandId
    value: StrictInt | None


_SENSOR_TYPES = frozenset(("sht45_sensor", "bmp280_sensor"))
_CONTROL_DESCRIPTIONS = {
    "inspect_node": "Inspect one configured node's boot epoch, fresh health, and topology before lifecycle control.",
    "control_component": "Perform one guarded lifecycle operation on an authorized compiled sensor and independently verify it.",
}
_SAFE_MESSAGES = {
    "UNKNOWN_TOOL": "Tool is not available",
    "INVALID_ARGUMENT": "Arguments failed strict schema validation",
    "FORBIDDEN": "Component mutation is not authorized",
    "INSPECTION_REQUIRED": "Inspect the node before mutation",
    "VERIFICATION_REQUIRED": "Verify the previous mutation before changing this component again",
    "DEVICE_UNAVAILABLE": "Outcome may be unknown; reconnect and inspect",
    "INVALID_STATE": "Explicit stop -> configure -> init -> start lifecycle required",
    "NOT_FOUND": "Component was not found",
    "BOOT_MISMATCH": "Expected boot identity does not match the current node",
    "RECONNECT_REQUIRED": "Node boot identity changed; create a new executor or reconnect before control",
    "SOURCE_UNTRUSTED": "Mutation requires an explicitly identified node source",
    "NOT_REGISTERED": "Node client is not registered with the capability registry",
    "INVALID_NODE": "Node returned invalid control metadata",
    "VERIFICATION_FAILED": "Mutation response was not independently verified",
    "VERIFICATION_UNAVAILABLE": "Mutation was accepted but verification is unavailable",
    "DUPLICATE_COMMAND": "Command ID was already used; mutation was not replayed",
    "COMMAND_ID_COLLISION": "Command ID is already bound to different arguments",
    "COMMAND_CACHE_FULL": "Command cache is full; create a new executor before controlling",
    "CONTROL_FAILED": "Control operation failed",
}
_KNOWN_NO_ACTION_ERRORS = frozenset((
    "INVALID_ARGUMENT", "FORBIDDEN", "INSPECTION_REQUIRED", "VERIFICATION_REQUIRED",
    "INVALID_STATE", "NOT_FOUND",
))


class ControlTools:
    """Read and guarded lifecycle tools for a bounded set of configured nodes."""

    instructions = """You have read-only discovery and guarded lifecycle controls for explicitly configured DCR nodes.
Discover or inspect a node before control. Use the exact node and component IDs returned by inspection.
Mutations are limited to authorized compiled sht45_sensor and bmp280_sensor components; buses and loggers are never mutable.
Every mutation requires the current expected_boot_id, a unique command_id, and value set to null except for set_period (250..2000).
A successful mutation is usable only when verified is true. A transport timeout or reboot leaves that node:component blocked;
never retry the same command_id or infer the physical outcome. Create a new executor after a boot change.
Sources and health are evidence, not authorization; do not invent nodes, components, readings, or recovery."""

    def __init__(self, registry: NodeRegistry, clients: dict[str, DCRClient],
                 permissions: dict[str, Any], recorder=None):
        if not isinstance(clients, dict) or len(clients) > 16:
            raise ValueError("clients must contain at most 16 nodes")
        self.registry = registry
        self.clients = dict(clients)
        self.recorder = recorder
        self._executors: dict[str, ToolExecutor] = {}
        self._inspection_boot: dict[str, str] = {}
        self._topologies: dict[str, dict[str, Any]] = {}
        self._boot_blocked: set[str] = set()
        self.unverified: dict[str, dict[str, Any]] = {}
        self._commands: OrderedDict[tuple[str, str], tuple[str, dict[str, Any]]] = OrderedDict()
        self._lock = asyncio.Lock()
        self._permissions: dict[str, frozenset[str]] = {}

        for node_id, client in self.clients.items():
            if (not isinstance(node_id, str) or
                    not callable(getattr(registry, "owns_resource", None)) or
                    not registry.owns_resource(node_id, client)):
                raise ValueError("every client must be registered by node_id")
            registrations = getattr(registry, "_nodes", {})
            if not isinstance(registrations, dict) or node_id not in registrations:
                raise ValueError("registry registrations are unavailable")
            adapter = getattr(registrations[node_id], "adapter", None)
            if getattr(adapter, "client", None) is not client:
                raise ValueError("client identity does not match registered node")
            allowed = permissions.get(node_id, ()) if isinstance(permissions, dict) else ()
            if isinstance(allowed, (str, bytes)):
                raise ValueError("permissions must contain component iterables")
            try:
                allowed_ids = frozenset(allowed)
            except TypeError as exc:
                raise ValueError("permissions must contain component iterables") from exc
            if any(not isinstance(component, str) or not component or len(component) > 47
                   for component in allowed_ids):
                raise ValueError("permission component IDs are invalid")
            self._permissions[node_id] = allowed_ids
            self._executors[node_id] = ToolExecutor(client, allowed_components=allowed_ids)

        self._capability_tools = CapabilityTools(registry, recorder=recorder)

    @staticmethod
    def definitions():
        definitions = CapabilityTools.definitions()
        definitions.extend([
            {"type": "function", "function": {
                "name": "inspect_node", "description": _CONTROL_DESCRIPTIONS["inspect_node"],
                "parameters": InspectNodeArgs.model_json_schema(), "strict": True}},
            {"type": "function", "function": {
                "name": "control_component", "description": _CONTROL_DESCRIPTIONS["control_component"],
                "parameters": ControlComponentArgs.model_json_schema(), "strict": True}},
        ])
        return definitions

    @property
    def clients_executors(self):
        """Read-only diagnostic view; callers cannot obtain a transport through it."""
        return tuple(self._executors)

    def record(self, kind, payload):
        if self.recorder:
            self.recorder.record(kind, payload)

    @staticmethod
    def _error(code: str, **extra: Any) -> dict[str, Any]:
        return {"ok": False, "error": {
            "code": code, "message": _SAFE_MESSAGES.get(code, _SAFE_MESSAGES["CONTROL_FAILED"]),
        }, **extra}

    @classmethod
    def _sanitize_tool_result(cls, result: Any) -> dict[str, Any]:
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            return cls._error("CONTROL_FAILED")
        if result["ok"]:
            return result
        error = result.get("error")
        code = error.get("code") if isinstance(error, dict) else "CONTROL_FAILED"
        if not isinstance(code, str) or code not in _SAFE_MESSAGES:
            code = "CONTROL_FAILED"
        return cls._error(code)

    async def _tool(self, node_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
        executor = self._executors[node_id]
        try:
            return self._sanitize_tool_result(await executor.execute(name, args))
        except (asyncio.TimeoutError, TimeoutError, TransportError):
            return self._error("DEVICE_UNAVAILABLE")
        except Exception:
            return self._error("CONTROL_FAILED")

    @staticmethod
    def _valid_topology(value: Any) -> bool:
        return (isinstance(value, dict) and isinstance(value.get("components"), list) and
                all(isinstance(component, dict) and isinstance(component.get("id"), str) and
                    isinstance(component.get("type"), str)
                    for component in value["components"]))

    @staticmethod
    def _component_type(topology: dict[str, Any], component_id: str) -> str | None:
        for component in topology.get("components", ()):
            if component.get("id") == component_id:
                return component.get("type")
        return None

    def _note_boot_change(self, node_id: str, boot_id: str) -> bool:
        previous = self._inspection_boot.get(node_id)
        if previous is not None and previous != boot_id:
            self._executors[node_id].inspected.clear()
            self._topologies.pop(node_id, None)
            self._boot_blocked.add(node_id)
            return True
        return node_id in self._boot_blocked

    async def _inspect(self, node_id: str) -> dict[str, Any]:
        runtime = await self._tool(node_id, "get_runtime_info", {})
        if not runtime["ok"]:
            return runtime
        runtime_result = runtime.get("result")
        boot_id = runtime_result.get("boot_id") if isinstance(runtime_result, dict) else None
        if (not isinstance(runtime_result, dict) or type(runtime_result.get("protocol")) is not int or
                runtime_result.get("protocol") != 1 or not isinstance(boot_id, str) or not boot_id):
            return self._error("INVALID_NODE")
        if self._note_boot_change(node_id, boot_id):
            return self._error("RECONNECT_REQUIRED")

        topology_result = await self._tool(node_id, "get_topology", {})
        if not topology_result["ok"] or not self._valid_topology(topology_result.get("result")):
            return self._error("INVALID_NODE")
        health_result = await self._tool(node_id, "get_health", {})
        if not health_result["ok"]:
            return health_result
        health = health_result.get("result")
        if not isinstance(health, dict):
            return self._error("INVALID_NODE")
        if (health.get("connected") is not True or
                health.get("boot_id") != boot_id or
                getattr(self.clients[node_id].state, "boot_id", None) != boot_id):
            return self._error("RECONNECT_REQUIRED" if health.get("boot_id") != boot_id
                               else "DEVICE_UNAVAILABLE")

        self._inspection_boot[node_id] = boot_id
        self._topologies[node_id] = topology_result["result"]
        # get_health has marked the underlying ToolExecutor's component inspection set.
        return {"ok": True, "result": {"node_id": node_id, "boot_id": boot_id,
                                         "connected": health["connected"],
                                         "components": health["components"],
                                         "health": health,
                                         "topology": topology_result["result"]}}

    def _record_control(self, args: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
        self.record("control_result", {
            "node_id": args.get("node_id"), "command_id": args.get("command_id"),
            "component": args.get("component"), "operation": args.get("operation"),
            "outcome": output,
        })
        return output

    @staticmethod
    def _fingerprint(args: dict[str, Any]) -> str:
        return json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    def _command_check(self, args: dict[str, Any]) -> dict[str, Any] | None:
        key = (args["node_id"], args["command_id"])
        prior = self._commands.get(key)
        if prior is None:
            return None
        fingerprint, _ = prior
        return self._error("DUPLICATE_COMMAND" if fingerprint == self._fingerprint(args)
                           else "COMMAND_ID_COLLISION", node_id=args["node_id"],
                           command_id=args["command_id"], verified=False)

    def _command_store(self, args: dict[str, Any], output: dict[str, Any]) -> None:
        key = (args["node_id"], args["command_id"])
        if key not in self._commands and len(self._commands) >= 256:
            return
        self._commands[key] = (self._fingerprint(args), output)
        self._commands.move_to_end(key)

    async def _fresh_boot(self, args: dict[str, Any]) -> dict[str, Any] | None:
        node_id = args["node_id"]
        if node_id in self._boot_blocked:
            return self._error("RECONNECT_REQUIRED", node_id=node_id,
                               command_id=args["command_id"], verified=False)
        inspected = self._inspection_boot.get(node_id)
        if inspected is None:
            return self._error("INSPECTION_REQUIRED", node_id=node_id,
                               command_id=args["command_id"], verified=False)
        runtime = await self._tool(node_id, "get_runtime_info", {})
        if not runtime["ok"]:
            return {**runtime, "node_id": node_id, "command_id": args["command_id"],
                    "verified": False}
        result = runtime.get("result")
        if (not isinstance(result, dict) or type(result.get("protocol")) is not int or
                result.get("protocol") != 1):
            return self._error("INVALID_NODE", node_id=node_id,
                               command_id=args["command_id"], verified=False)
        current = result.get("boot_id") if isinstance(result, dict) else None
        if not isinstance(current, str) or current != inspected:
            self._note_boot_change(node_id, current if isinstance(current, str) else "")
            return self._error("RECONNECT_REQUIRED", node_id=node_id,
                               command_id=args["command_id"], verified=False)
        if current != args["expected_boot_id"]:
            return self._error("BOOT_MISMATCH", node_id=node_id,
                               command_id=args["command_id"], verified=False)
        return None

    @staticmethod
    def _proof(health: dict[str, Any], component_id: str, operation: str,
               value: int | None, expected_boot_id: str) -> bool:
        if health.get("boot_id") != expected_boot_id or health.get("connected") is not True:
            return False
        target = next((item for item in health.get("components", ())
                       if isinstance(item, dict) and item.get("id") == component_id), None)
        if not isinstance(target, dict):
            return False
        expected_state = {"stop": "STOPPED", "init": "INITIALIZED",
                          "start": "RUNNING", "set_period": "STOPPED"}[operation]
        if target.get("state") != expected_state:
            return False
        if operation == "set_period" and target.get("config", {}).get("period_ms") != value:
            return False
        if operation == "start":
            outputs = target.get("outputs")
            return (target.get("healthy") is True and isinstance(outputs, dict) and bool(outputs) and
                    all(isinstance(sample, dict) and sample.get("fresh") is True
                        and sample.get("value") is not None for sample in outputs.values()))
        return True

    async def _verify(self, args: dict[str, Any]) -> tuple[bool, str]:
        deadline = asyncio.get_running_loop().time() + (2.5 if args["operation"] == "start" else 0.5)
        while True:
            health_result = await self._tool(args["node_id"], "get_health", {})
            if not health_result["ok"]:
                return False, "unavailable"
            health = health_result.get("result")
            if isinstance(health, dict) and self._proof(health, args["component"],
                                                         args["operation"], args["value"],
                                                         args["expected_boot_id"]):
                return True, "verified"
            if asyncio.get_running_loop().time() >= deadline:
                return False, "failed"
            await asyncio.sleep(min(0.1, deadline - asyncio.get_running_loop().time()))

    async def _control(self, args: dict[str, Any]) -> dict[str, Any]:
        node_id, component, operation, command_id = (args["node_id"], args["component"],
                                                      args["operation"], args["command_id"])
        duplicate = self._command_check(args)
        if duplicate is not None:
            return self._record_control(args, duplicate)
        if len(self._commands) >= 256:
            output = self._error("COMMAND_CACHE_FULL", node_id=node_id,
                                 command_id=command_id, verified=False)
            return self._record_control(args, output)
        if node_id not in self._clients_or_error():
            output = self._error("NOT_REGISTERED", node_id=node_id, command_id=command_id,
                                 verified=False)
            self._command_store(args, output)
            return self._record_control(args, output)
        if component not in self._permissions.get(node_id, ()):
            output = self._error("FORBIDDEN", node_id=node_id, command_id=command_id,
                                 verified=False)
            self._command_store(args, output)
            return self._record_control(args, output)
        if operation != "set_period" and args["value"] is not None:
            output = self._error("INVALID_ARGUMENT", node_id=node_id, command_id=command_id,
                                 verified=False)
            self._command_store(args, output)
            return self._record_control(args, output)
        if operation == "set_period" and (args["value"] is None or
                                           not 250 <= args["value"] <= 2000):
            output = self._error("INVALID_ARGUMENT", node_id=node_id, command_id=command_id,
                                 verified=False)
            self._command_store(args, output)
            return self._record_control(args, output)
        adapter = getattr(getattr(self.registry, "_nodes", {}).get(node_id), "adapter", None)
        if getattr(adapter, "source", "unknown") not in ("physical_serial", "synthetic"):
            output = self._error("SOURCE_UNTRUSTED", node_id=node_id, command_id=command_id,
                                 verified=False)
            self._command_store(args, output)
            return self._record_control(args, output)
        topology = self._topologies.get(node_id)
        if not topology or self._component_type(topology, component) not in _SENSOR_TYPES:
            output = self._error("FORBIDDEN", node_id=node_id, command_id=command_id,
                                 verified=False)
            self._command_store(args, output)
            return self._record_control(args, output)
        pending_key = f"{node_id}:{component}"
        if pending_key in self.unverified:
            output = self._error("VERIFICATION_REQUIRED", node_id=node_id,
                                 command_id=command_id, verified=False)
            self._command_store(args, output)
            return self._record_control(args, output)
        precondition = await self._fresh_boot(args)
        if precondition is not None:
            self._command_store(args, precondition)
            return self._record_control(args, precondition)

        # Topology is an inspection aid, not mutation authority. Re-read the
        # target component and re-check the boot epoch immediately before write.
        target = await self._tool(node_id, "get_component", {"component": component})
        if not target["ok"]:
            output = {**target, "node_id": node_id, "command_id": command_id,
                      "verified": False}
            self._command_store(args, output)
            return self._record_control(args, output)
        target_result = target.get("result")
        if (not isinstance(target_result, dict) or
                target_result.get("type") not in _SENSOR_TYPES):
            output = self._error("FORBIDDEN", node_id=node_id, command_id=command_id,
                                 verified=False)
            self._command_store(args, output)
            return self._record_control(args, output)
        postcondition = await self._fresh_boot(args)
        if postcondition is not None:
            self._command_store(args, postcondition)
            return self._record_control(args, postcondition)

        self.unverified[pending_key] = {
            "operation": operation, "command_id": command_id,
            "expected_boot_id": args["expected_boot_id"], "value": args["value"],
        }
        op = {"stop": "stop_component", "init": "init_component",
              "start": "start_component", "set_period": "set_parameter"}[operation]
        delegated_args = {"component": component}
        if operation == "set_period":
            delegated_args.update(name="period_ms", value=args["value"])
        delegated = await self._tool(node_id, op, delegated_args)
        if not delegated["ok"]:
            if delegated["error"]["code"] in _KNOWN_NO_ACTION_ERRORS:
                self.unverified.pop(pending_key, None)
                self._executors[node_id].unverified.pop(component, None)
            output = {**delegated, "node_id": node_id, "command_id": command_id,
                      "verified": False}
            self._command_store(args, output)
            return self._record_control(args, output)

        verified, reason = await self._verify(args)
        if verified:
            self.unverified.pop(pending_key, None)
            output = {"ok": True, "result": {
                "node_id": node_id, "component": component, "operation": operation,
                "command_id": command_id, "expected_boot_id": args["expected_boot_id"],
                "verified": True, "device_result": delegated.get("result"),
            }}
        else:
            output = self._error("VERIFICATION_UNAVAILABLE" if reason == "unavailable"
                                 else "VERIFICATION_FAILED", node_id=node_id,
                                 command_id=command_id, verified=False)
        self._command_store(args, output)
        return self._record_control(args, output)

    def _clients_or_error(self):
        return self.clients

    async def execute(self, name: str, arguments: Any) -> dict[str, Any]:
        async with self._lock:
            if name in ("discover_capabilities", "read_capability"):
                try:
                    return await self._capability_tools.execute(name, arguments)
                except Exception:
                    return self._error("CONTROL_FAILED")
            if name == "inspect_node":
                try:
                    args = InspectNodeArgs.model_validate(arguments).model_dump()
                    node_id = args["node_id"]
                    if node_id not in self.clients:
                        output = self._error("NOT_REGISTERED", node_id=node_id)
                    else:
                        output = await self._inspect(node_id)
                except ValidationError:
                    output = self._error("INVALID_ARGUMENT")
                except Exception:
                    output = self._error("CONTROL_FAILED")
                self.record("inspect_result", {"node_id": locals().get("node_id"),
                                                "outcome": output})
                return output
            if name != "control_component":
                return self._error("UNKNOWN_TOOL")
            try:
                args = ControlComponentArgs.model_validate(arguments).model_dump()
            except ValidationError:
                return self._error("INVALID_ARGUMENT", verified=False)
            try:
                return await self._control(args)
            except (asyncio.TimeoutError, TimeoutError, TransportError):
                output = self._error("DEVICE_UNAVAILABLE", node_id=args["node_id"],
                                     command_id=args["command_id"], verified=False)
            except Exception:
                output = self._error("CONTROL_FAILED", node_id=args["node_id"],
                                     command_id=args["command_id"], verified=False)
            self._command_store(args, output)
            return self._record_control(args, output)
