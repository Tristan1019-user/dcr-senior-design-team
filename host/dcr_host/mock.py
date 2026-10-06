"""Deterministic mock endpoint; lifecycle semantics match the firmware contract."""
import asyncio
import copy
import time
import uuid

from pydantic import ValidationError

from .protocol import OPERATIONS, PREFIX, ProtocolError, Request, encode, load_json, validate_args
from .transport import Transport, TransportError


class MockTransport(Transport):
    def __init__(self, clock=time.monotonic):
        self.clock, self.started = clock, clock()
        self.boot_id = "mock-" + uuid.uuid4().hex
        self.connected = False
        self.queue = asyncio.Queue(maxsize=128)
        self.faults = set()
        self.stale = set()
        self.drop_responses = False
        self.commands = []
        self.components = {}
        self._last = {}
        for name, second in (("sht45", "humidity_pct"), ("bmp280", "pressure_pa")):
            self.components[name] = {
                "id": name, "type": name + "_sensor", "state": "RUNNING", "fault": "ok",
                "config": {"period_ms": 1000},
                "outputs": {p: {"value": None, "sequence": 0, "timestamp_ms": None, "age_ms": None}
                            for p in ("temperature_c", second, "status")},
            }
            self._last[name] = -10.0

    async def connect(self):
        if self.connected:
            raise TransportError("Already connected")
        self.connected = True
        while not self.queue.empty():
            self.queue.get_nowait()

    async def disconnect(self):
        self.connected = False
        self.queue.put_nowait(None)

    def tick(self):
        now = self.clock()
        timestamp = int((now - self.started) * 1000)
        for name, c in self.components.items():
            if c["state"] != "RUNNING" or name in self.stale:
                continue
            if now - self._last[name] < c["config"]["period_ms"] / 1000:
                continue
            self._last[name] = now
            for port, s in c["outputs"].items():
                if name in self.faults and port != "status":
                    continue
                s["sequence"] += 1
                s["timestamp_ms"] = timestamp
                s["age_ms"] = 0
                s["value"] = (1 if name in self.faults else 0) if port == "status" else (
                    {"temperature_c": 24.0, "humidity_pct": 45.0, "pressure_pa": 100000.0}[port]
                    + (s["sequence"] % 10) / 100)

    def snapshot(self, name):
        c = copy.deepcopy(self.components[name])
        now = int((self.clock() - self.started) * 1000)
        for sample in c["outputs"].values():
            if sample["timestamp_ms"] is not None:
                sample["age_ms"] = now - sample["timestamp_ms"]
        return c

    async def send(self, data):
        if not self.connected:
            raise TransportError("Disconnected")
        obj = load_json(data)
        req = Request.model_validate(obj)
        self.commands.append(copy.deepcopy(obj))
        self.tick()
        try:
            args = validate_args(req.op, req.args)
        except (ValidationError, ProtocolError):
            code = "UNKNOWN_OPERATION" if req.op not in OPERATIONS else "INVALID_ARGUMENT"
            response = {"v": 1, "id": req.id, "ok": False,
                        "error": {"code": code, "message": "Rejected mock request"}}
        else:
            result, error = self.dispatch(req.op, args)
            response = {"v": 1, "id": req.id, "ok": error is None}
            response["result" if error is None else "error"] = result if error is None else {
                "code": error, "message": "Mock device rejected operation"}
        if not self.drop_responses:
            self.queue.put_nowait(PREFIX + encode(response))

    def dispatch(self, op, args):
        if op == "get_runtime_info":
            return {"protocol": 1, "boot_id": self.boot_id,
                    "uptime_ms": int((self.clock() - self.started) * 1000),
                    "capabilities": list(OPERATIONS)}, None
        if op == "get_components":
            return {"components": [self.snapshot(n) for n in self.components]}, None
        if op == "get_topology":
            # Explicit mock sensor descriptors, not semantic inference from arbitrary names.
            output_ports = {"sht45_sensor": ("temperature_c", "humidity_pct"),
                            "bmp280_sensor": ("temperature_c", "pressure_pa")}
            components = []
            for name, c in self.components.items():
                item = self.snapshot(name)
                item["ports"] = [{"name": p, "direction": "output", "type": "float",
                                  "delivery": "signal", "required": False, "multi_producer": False}
                                 for p in output_ports.get(c["type"], ())]
                components.append(item)
            return {"components": components, "connections": []}, None
        if op == "get_configuration":
            return {"components": [{k: copy.deepcopy(c[k]) for k in ("id", "type", "config")}
                                   for c in self.components.values()], "persistent": False}, None
        name = args["component"]
        if name not in self.components:
            return None, "NOT_FOUND"
        c = self.components[name]
        if op == "stop_component":
            if c["state"] not in ("RUNNING", "INITIALIZED", "FAULTED"):
                return None, "INVALID_STATE"
            c["state"] = "STOPPED"
        elif op == "init_component":
            if c["state"] != "STOPPED":
                return None, "INVALID_STATE"
            c["state"], c["fault"] = "INITIALIZED", "ok"
            for s in c["outputs"].values():
                s.update(value=None, sequence=0, timestamp_ms=None, age_ms=None)
        elif op == "start_component":
            if c["state"] != "INITIALIZED":
                return None, "INVALID_STATE"
            c["state"] = "RUNNING"
            self._last[name] = -10
        elif op == "set_parameter":
            if c["state"] != "STOPPED":
                return None, "INVALID_STATE"
            c["config"][args["name"]] = args["value"]
        return self.snapshot(name), None

    async def receive(self):
        if not self.connected:
            raise TransportError("Disconnected")
        try:
            result = await asyncio.wait_for(self.queue.get(), .05)
            if result is None:
                raise TransportError("Disconnected")
            return result
        except TimeoutError:
            self.tick()
            return PREFIX + encode({"v": 1, "event": "snapshot", "boot_id": self.boot_id,
                                    "components": [self.snapshot(n) for n in self.components]})
