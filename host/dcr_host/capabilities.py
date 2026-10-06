"""Read-only, explicit node and capability abstraction for physical DCR nodes."""

from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr

from .protocol import DeviceError, ProtocolError
from .transport import TransportError

NodeId = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9_-]{1,47}$")]
LocalId = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9_.-]{1,95}$")]
QualifiedId = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9_-]{1,47}:[A-Za-z0-9_.-]{1,95}$")]
Scalar = StrictFloat | StrictInt | StrictBool | StrictStr | None
Source = Literal["physical_serial", "synthetic", "unknown"]


class Capability(BaseModel):
    """A trusted, read-only semantic output declared by a compiled node type."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    local_id: LocalId
    semantic_kind: Annotated[StrictStr, Field(min_length=1, max_length=64)]
    component: NodeId
    unit: Annotated[StrictStr, Field(min_length=1, max_length=16)]
    read_only: Literal[True] = True
    origin: Annotated[StrictStr, Field(min_length=1, max_length=64)]
    source: Source


class Observation(BaseModel):
    """One fresh-or-explicitly-stale scalar sample from a configured node."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)

    node_id: NodeId
    capability_id: QualifiedId
    boot_id: Annotated[StrictStr, Field(min_length=1, max_length=96)] | None
    value: Scalar
    unit: Annotated[StrictStr, Field(min_length=1, max_length=16)]
    age_ms: Annotated[StrictInt, Field(ge=0)] | None
    fresh: StrictBool
    healthy: StrictBool
    sequence: Annotated[StrictInt, Field(ge=0)] | None
    source_timestamp_ms: Annotated[StrictInt, Field(ge=0)] | None
    source: Source


class NodeFailure(BaseModel):
    """Sanitized failure data safe to return to callers and tools."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    code: Annotated[StrictStr, Field(pattern=r"^[a-z_]{1,32}$")]
    message: Annotated[StrictStr, Field(min_length=1, max_length=96)]


class NodeReport(BaseModel):
    """The isolated result for one node discovery or observation attempt."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    node_id: NodeId
    capabilities: tuple[Capability, ...] = ()
    observations: tuple[Observation, ...] = ()
    error: NodeFailure | None = None
    source: Source


class NodeAdapter(Protocol):
    """Caller-owned connection adapter boundary; it has no mutation method."""

    node_id: str
    resource_identity: object

    async def discover(self) -> list[Capability]: ...

    async def observe(self, capability_id: str | None = None) -> list[Observation]: ...


class NodeAdapterError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class NodeRegistryError(ValueError):
    pass


_NODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,47}$")
_LOCAL_RE = re.compile(r"^[A-Za-z0-9_.-]{1,95}$")
_QUALIFIED_RE = re.compile(r"^[A-Za-z0-9_-]{1,47}:[A-Za-z0-9_.-]{1,95}$")
_KNOWN_OUTPUTS: dict[str, dict[str, tuple[str, str]]] = {
    "sht45_sensor": {
        "temperature_c": ("temperature.read", "degC"),
        "humidity_pct": ("humidity.read", "%RH"),
    },
    "bmp280_sensor": {
        "temperature_c": ("temperature.read", "degC"),
        "pressure_pa": ("pressure.read", "Pa"),
    },
}
_FAILURE_MESSAGES = {
    "timeout": "node request timed out",
    "unavailable": "node is unavailable",
    "rebooted": "node boot identity changed",
    "protocol": "node returned an invalid protocol result",
    "device": "node rejected the read-only request",
    "not_found": "capability is not registered on the node",
    "invalid": "node capability metadata is invalid",
    "adapter": "node adapter failed",
    "not_registered": "node is not registered",
}


def _node_id(value: str) -> str:
    if not isinstance(value, str) or not _NODE_RE.fullmatch(value):
        raise ValueError("node_id must match [A-Za-z0-9_-]{1,47}")
    return value


def _qualified(node_id: str, local_id: str) -> str:
    return f"{node_id}:{local_id}"


def _requested_capability(node_id: str, capability_id: str) -> str:
    """Validate a local or node-qualified request and return its qualified ID."""
    if not isinstance(capability_id, str):
        raise NodeAdapterError("invalid")
    if _LOCAL_RE.fullmatch(capability_id):
        return _qualified(node_id, capability_id)
    if _QUALIFIED_RE.fullmatch(capability_id):
        requested_node, _ = capability_id.split(":", 1)
        if requested_node == node_id:
            return capability_id
    raise NodeAdapterError("invalid")


def _failure(code: str) -> NodeFailure:
    return NodeFailure(code=code, message=_FAILURE_MESSAGES.get(code, _FAILURE_MESSAGES["adapter"]))


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    if isinstance(exc, NodeAdapterError):
        return exc.code if exc.code in _FAILURE_MESSAGES else "adapter"
    if isinstance(exc, ProtocolError):
        return "protocol"
    if isinstance(exc, DeviceError):
        return "device"
    if isinstance(exc, (TransportError, ConnectionError)):
        return "unavailable"
    return "adapter"


def _topology_components(topology: Any) -> list[dict[str, Any]]:
    if not isinstance(topology, dict) or not isinstance(topology.get("components"), list):
        raise NodeAdapterError("invalid")
    if any(not isinstance(item, dict) for item in topology["components"]):
        raise NodeAdapterError("invalid")
    return topology["components"]


class DCRNodeAdapter:
    """Read-only adapter over an already-connected DCRClient.

    The caller owns connect/disconnect. Every discovery or observation reads
    current topology and, for observations, calls DCRClient.refresh(); no
    successful sample is cached across a failure or boot identity change.
    """

    def __init__(self, node_id: str, client: Any, source: Source = "unknown"):
        if client is None:
            raise ValueError("client is required")
        self.node_id = _node_id(node_id)
        if source not in ("physical_serial", "synthetic", "unknown"):
            raise ValueError("source must be physical_serial, synthetic, or unknown")
        self.client = client
        self.resource_identity = client
        self.source = source
        self._boot_id: str | None = None

    async def _context(self) -> tuple[str, list[dict[str, Any]]]:
        state = getattr(self.client, "state", None)
        if state is None or not getattr(state, "connected", False):
            raise NodeAdapterError("unavailable")
        runtime = await self.client.call("get_runtime_info", {})
        boot_id = runtime.get("boot_id") if isinstance(runtime, dict) else None
        state_boot = getattr(state, "boot_id", None)
        if not isinstance(boot_id, str) or not boot_id or (state_boot and boot_id != state_boot):
            raise NodeAdapterError("rebooted")
        self._boot_id = boot_id
        topology = await self.client.call("get_topology", {})
        if getattr(state, "connected", False) is not True or getattr(state, "boot_id", None) != boot_id:
            raise NodeAdapterError("rebooted")
        return boot_id, _topology_components(topology)

    @staticmethod
    def _capability_entries(components: list[dict[str, Any]], source: Source) -> list[tuple[Capability, str]]:
        entries: list[tuple[Capability, str]] = []
        for component in components:
            component_id = component.get("id")
            component_type = component.get("type")
            if not isinstance(component_id, str) or not _NODE_RE.fullmatch(component_id):
                raise NodeAdapterError("invalid")
            if not isinstance(component_type, str) or not component_type:
                raise NodeAdapterError("invalid")
            known = _KNOWN_OUTPUTS.get(component_type)
            if known is None:
                continue
            ports = component.get("ports")
            if not isinstance(ports, list):
                raise NodeAdapterError("invalid")
            if any(not isinstance(port, dict) for port in ports):
                raise NodeAdapterError("invalid")
            port_names = [port.get("name") for port in ports]
            if (any(not isinstance(name, str) or not name for name in port_names) or
                    len(port_names) != len(set(port_names))):
                raise NodeAdapterError("invalid")
            metadata = {port.get("name"): port for port in ports}
            for port_name, (semantic, unit) in known.items():
                port = metadata.get(port_name)
                if not isinstance(port, dict):
                    raise NodeAdapterError("invalid")
                if (port.get("direction") != "output" or port.get("type") != "float" or
                        port.get("delivery") != "signal"):
                    raise NodeAdapterError("invalid")
                local_id = f"{component_id}.{port_name}"
                entries.append((Capability(local_id=local_id, semantic_kind=semantic,
                                           component=component_id, unit=unit, read_only=True,
                                           origin=f"dcr.compiled.{component_type}", source=source), port_name))
        return entries

    async def discover(self) -> list[Capability]:
        _, components = await self._context()
        return [capability for capability, _ in self._capability_entries(components, self.source)]

    async def observe(self, capability_id: str | None = None) -> list[Observation]:
        boot_id, topology = await self._context()
        entries = self._capability_entries(topology, self.source)
        if capability_id is not None:
            entries = [(capability, port) for capability, port in entries
                       if capability.local_id == capability_id or
                       _qualified(self.node_id, capability.local_id) == capability_id]
            if not entries:
                raise NodeAdapterError("not_found")
        health = await self.client.refresh()
        if not isinstance(health, dict) or not isinstance(health.get("components"), list):
            raise NodeAdapterError("invalid")
        state = getattr(self.client, "state", None)
        if health.get("boot_id") != boot_id or getattr(state, "boot_id", None) != boot_id:
            raise NodeAdapterError("rebooted")
        if health.get("connected") is not True or getattr(state, "connected", False) is not True:
            raise NodeAdapterError("unavailable")
        current = {item.get("id"): item for item in health["components"]
                   if isinstance(item, dict) and isinstance(item.get("id"), str)}
        observations: list[Observation] = []
        for capability, port_name in entries:
            component = current.get(capability.component)
            sample = component.get("outputs", {}).get(port_name) if isinstance(component, dict) else None
            if not isinstance(sample, dict):
                sample = {}
            value = sample.get("value")
            if value is not None and (type(value) not in (int, float) or
                                      isinstance(value, float) and not math.isfinite(value)):
                value = None
            age = sample.get("age_ms")
            age = age if type(age) is int and age >= 0 else None
            sequence = sample.get("sequence")
            sequence = sequence if type(sequence) is int and sequence >= 0 else None
            timestamp = sample.get("timestamp_ms")
            timestamp = timestamp if type(timestamp) is int and timestamp >= 0 else None
            fresh = bool(sample.get("fresh") is True and value is not None and age is not None)
            healthy = bool(fresh and isinstance(component, dict) and component.get("healthy") is True)
            observations.append(Observation(node_id=self.node_id,
                                            capability_id=_qualified(self.node_id, capability.local_id),
                                            boot_id=boot_id, value=value, unit=capability.unit,
                                            age_ms=age, fresh=fresh, healthy=healthy,
                                            sequence=sequence, source_timestamp_ms=timestamp,
                                            source=capability.source))
        return observations


@dataclass(frozen=True)
class _Registration:
    node_id: str
    adapter: NodeAdapter


class NodeRegistry:
    """Bounded explicit registry; it performs no discovery or network scanning."""

    def __init__(self, max_nodes: int = 16, timeout_s: float = 5.0):
        if type(max_nodes) is not int or not 1 <= max_nodes <= 16:
            raise ValueError("max_nodes must be 1..16")
        if isinstance(timeout_s, bool) or not math.isfinite(timeout_s) or not 0 < timeout_s <= 30:
            raise ValueError("timeout_s must be greater than 0 and at most 30 seconds")
        self.max_nodes = max_nodes
        self.timeout_s = timeout_s
        self._nodes: dict[str, _Registration] = {}

    @staticmethod
    def _source(adapter: NodeAdapter) -> Source:
        source = getattr(adapter, "source", "unknown")
        if source not in ("physical_serial", "synthetic", "unknown"):
            raise NodeAdapterError("invalid")
        return source

    @staticmethod
    def _capabilities(raw: Any, source: Source) -> tuple[Capability, ...]:
        if not isinstance(raw, list) or len(raw) > 64:
            raise NodeAdapterError("invalid")
        result: list[Capability] = []
        ids: set[str] = set()
        for item in raw:
            capability = item if isinstance(item, Capability) else Capability.model_validate(item)
            if capability.source != source or capability.local_id in ids:
                raise NodeAdapterError("invalid")
            ids.add(capability.local_id)
            result.append(capability)
        return tuple(result)

    @staticmethod
    def _observations(node_id: str, raw: Any, source: Source) -> tuple[Observation, ...]:
        if not isinstance(raw, list) or len(raw) > 64:
            raise NodeAdapterError("invalid")
        prefix = f"{node_id}:"
        result: list[Observation] = []
        ids: set[str] = set()
        for item in raw:
            observation = item if isinstance(item, Observation) else Observation.model_validate(item)
            if (observation.source != source or observation.node_id != node_id or
                    not observation.capability_id.startswith(prefix) or
                    observation.capability_id in ids):
                raise NodeAdapterError("invalid")
            ids.add(observation.capability_id)
            result.append(observation)
        return tuple(result)

    async def _discover_one(self, registration: _Registration) -> NodeReport:
        source = getattr(registration.adapter, "source", "unknown")
        if source not in ("physical_serial", "synthetic", "unknown"):
            source = "unknown"
        try:
            source = self._source(registration.adapter)
            raw = await asyncio.wait_for(registration.adapter.discover(), self.timeout_s)
            if self._source(registration.adapter) != source:
                raise NodeAdapterError("invalid")
            return NodeReport(node_id=registration.node_id,
                              capabilities=self._capabilities(raw, source), source=source)
        except Exception as exc:
            return NodeReport(node_id=registration.node_id, error=_failure(_error_code(exc)),
                              source=source)

    @property
    def node_ids(self) -> tuple[str, ...]:
        return tuple(self._nodes)

    def owns_resource(self, node_id: str, resource: object) -> bool:
        """Host-side binding check; never exposes transport handles to model tools."""
        registration = self._nodes.get(node_id)
        return bool(registration and getattr(registration.adapter, "resource_identity", None) is resource)

    def register(self, adapter: NodeAdapter) -> None:
        node_id = getattr(adapter, "node_id", None)
        if not isinstance(node_id, str):
            raise NodeRegistryError("operator-configured node_id is required")
        node_id = _node_id(node_id)
        if node_id in self._nodes:
            raise NodeRegistryError("duplicate node_id")
        if len(self._nodes) >= self.max_nodes:
            raise NodeRegistryError("node registry capacity exceeded")
        resource_identity = getattr(adapter, "resource_identity", adapter)
        for registered in self._nodes.values():
            registered_identity = getattr(registered.adapter, "resource_identity", registered.adapter)
            if registered.adapter is adapter or resource_identity is registered_identity:
                raise NodeRegistryError("adapter or resource identity already registered")
        self._nodes[node_id] = _Registration(node_id, adapter)

    async def discover(self) -> dict[str, list[dict[str, Any]]]:
        reports = await asyncio.gather(*(self._discover_one(registration)
                                         for registration in self._nodes.values()))
        return {"nodes": [report.model_dump(mode="json") for report in reports]}

    async def observe(self, node_id: str, capability_id: str | None = None) -> dict[str, Any]:
        try:
            node_id = _node_id(node_id)
        except ValueError:
            return {"ok": False, "error": _failure("not_registered").model_dump(mode="json")}
        registration = self._nodes.get(node_id)
        if registration is None:
            return {"ok": False, "error": _failure("not_registered").model_dump(mode="json")}
        try:
            source = self._source(registration.adapter)
            expected_id = (_requested_capability(node_id, capability_id)
                           if capability_id is not None else None)
            async def read_declared():
                catalog = self._capabilities(await registration.adapter.discover(), source)
                declared = {_qualified(node_id, item.local_id): item for item in catalog}
                if expected_id is not None and expected_id not in declared:
                    raise NodeAdapterError("not_found")
                values = await registration.adapter.observe(capability_id)
                observations = self._observations(node_id, values, source)
                for item in observations:
                    if item.capability_id not in declared or item.unit != declared[item.capability_id].unit:
                        raise NodeAdapterError("invalid")
                    if item.healthy and not item.fresh or item.fresh and (item.value is None or item.age_ms is None):
                        raise NodeAdapterError("invalid")
                return observations

            observations = await asyncio.wait_for(read_declared(), self.timeout_s)
            if self._source(registration.adapter) != source:
                raise NodeAdapterError("invalid")
            if expected_id is not None and (len(observations) != 1 or
                                            observations[0].capability_id != expected_id):
                raise NodeAdapterError("invalid")
            return {"ok": True, "result": {"node_id": node_id,
                                             "observations": [item.model_dump(mode="json")
                                                              for item in observations]}}
        except Exception as exc:
            return {"ok": False, "error": _failure(_error_code(exc)).model_dump(mode="json")}
