"""Freshness is device age plus host elapsed time, never packet arrival alone."""
import time
from collections import deque

from .protocol import Component


class RuntimeState:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.boot_id = None
        self.connected = False
        self.components = {}
        self.history = deque(maxlen=512)
        self._seen = {}

    def clear(self):
        self.components.clear()
        self.history.clear()
        self._seen.clear()
        self.connected = False
        self.boot_id = None

    def update(self, components):
        for item in components:
            component = item if isinstance(item, Component) else Component.model_validate(item)
            self.components[component.id] = (component, self.clock())
            for port, sample in component.outputs.items():
                key = (component.id, port)
                signature = (sample.sequence, sample.timestamp_ms)
                if sample.sequence and self._seen.get(key) != signature:
                    self.history.append({"component": component.id, "port": port,
                                         **sample.model_dump(), "observed_monotonic": self.clock()})
                self._seen[key] = signature

    def inspect(self, name):
        component, received = self.components[name]
        result = component.model_dump()
        period = component.config.get("period_ms", 1000)
        if type(period) is not int or not 250 <= period <= 2000:
            period = 1000
        threshold = max(3000, period * 3)
        state_age_ms = int((self.clock() - received) * 1000)
        for port, sample in result["outputs"].items():
            age = sample["age_ms"]
            if age is not None:
                age += state_age_ms
            sample["age_ms"] = age
            sample["fresh"] = (self.connected and component.state == "RUNNING" and
                               sample["value"] is not None and age is not None and age <= threshold)
        result["healthy"] = (self.connected and state_age_ms <= threshold
                             and component.state == "RUNNING" and component.fault == "ok"
                             and (bool(result["outputs"]) or component.type in ("logger", "signal_logger"))
                             and all(s["fresh"] for s in result["outputs"].values())
                             and result["outputs"].get("status", {}).get("value", 0) == 0)
        return result

    def health(self):
        return {"connected": self.connected, "boot_id": self.boot_id,
                "components": [self.inspect(name) for name in self.components]}
