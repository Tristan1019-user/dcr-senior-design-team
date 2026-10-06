"""Synthetic stage adapter and deterministic proposal policy.

This module has no physical transport, raw I/O, learned weights, or hardware
side effects.  The stage is an in-memory non-DCR adapter for exercising the
policy/action boundary and its failure semantics.
"""

from __future__ import annotations

import math
import time
import json
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import ValidationError

from ..capabilities import Capability, NodeAdapterError, Observation
from .base import PolicyBackend
from .contracts import ActionChunk, ObservationBundle, SafetyGate


class SimulatedStage:
    """An explicitly synthetic one-dimensional stage with bounded replay."""

    source = "synthetic"

    def __init__(
        self,
        node_id: str = "stage",
        *,
        initial_position_mm: float = 0.0,
        boot_id: str = "stage-boot-1",
        clock: Callable[[], float] = time.monotonic,
        allow_actions: bool = False,
        replay_capacity: int = 64,
        gate: SafetyGate | None = None,
        authorized: bool | None = None,
    ) -> None:
        initial = self._finite_number(initial_position_mm, "initial_position_mm")
        if not 0.0 <= initial <= SafetyGate.MAX_POSITION_MM:
            raise ValueError("initial_position_mm must be within 0..100")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if type(allow_actions) is not bool:
            raise TypeError("allow_actions must be a bool")
        if authorized is not None:
            if type(authorized) is not bool:
                raise TypeError("authorized must be a bool")
            if allow_actions and not authorized:
                raise ValueError("allow_actions and authorized disagree")
            allow_actions = authorized
        if type(replay_capacity) is not int or not 1 <= replay_capacity <= 1024:
            raise ValueError("replay_capacity must be 1..1024")
        self.node_id = node_id
        self.resource_identity = self
        self._clock = clock
        self._boot_id = boot_id
        self._position_mm = initial
        self._sequence = 0
        self._replay_capacity = replay_capacity
        self._replay_ids: set[str] = set()
        if gate is not None and gate.allow_actions and not allow_actions:
            raise ValueError("an allowing gate also requires allow_actions=True")
        self.allow_actions = allow_actions
        self.authorized = allow_actions
        self._gate = gate or SafetyGate(node_id=node_id, allow_actions=allow_actions)
        if self._gate.node_id not in (None, node_id):
            raise ValueError("gate node_id must match stage node_id")

        # Validate constructor identity and the initial state with the same
        # strict contract used at runtime.
        ObservationBundle(
            node_id=node_id,
            boot_id=boot_id,
            observed_at=0.0,
            position_mm=initial,
            sequence=0,
        )

    @staticmethod
    def _finite_number(value: object, field: str) -> float:
        if type(value) not in (int, float) or isinstance(value, bool) or not math.isfinite(value):
            raise ValueError(f"{field} must be finite")
        return float(value)

    def _now(self) -> float:
        return self._finite_number(self._clock(), "clock value")

    def snapshot(self, now: float) -> ObservationBundle:
        """Return the current synthetic state at an explicit monotonic time."""

        timestamp = self._finite_number(now, "now")
        return ObservationBundle(
            node_id=self.node_id,
            boot_id=self._boot_id,
            observed_at=timestamp,
            position_mm=self._position_mm,
            sequence=self._sequence,
        )

    async def discover(self) -> list[Capability]:
        """Expose one read-only semantic capability through NodeRegistry."""

        return [Capability(
            local_id="position.read",
            semantic_kind="position.read",
            component=self.node_id,
            unit="mm",
            read_only=True,
            origin="simulation.stage",
            source="synthetic",
        )]

    async def observe(self, capability_id: str | None = None) -> list[Observation]:
        """Implement the non-DCR NodeAdapter read boundary."""

        if capability_id not in (None, "position.read", f"{self.node_id}:position.read"):
            raise NodeAdapterError("not_found")
        now = self._now()
        state = self.snapshot(now)
        return [Observation(
            node_id=self.node_id,
            capability_id=f"{self.node_id}:position.read",
            boot_id=state.boot_id,
            value=state.position_mm,
            unit="mm",
            age_ms=0,
            fresh=True,
            healthy=True,
            sequence=state.sequence,
            source_timestamp_ms=int(now * 1000),
            source="synthetic",
        )]

    @staticmethod
    def _as_action_chunk(chunk: ActionChunk | Mapping[str, Any]) -> ActionChunk:
        payload = chunk.model_dump(mode="json") if isinstance(chunk, ActionChunk) else chunk
        encoded = json.dumps(payload, allow_nan=False, separators=(",", ":"))
        return ActionChunk.model_validate_json(encoded, strict=True)

    @staticmethod
    def _as_observation(observation: ObservationBundle | Mapping[str, Any]) -> ObservationBundle:
        payload = observation.model_dump(mode="json") if isinstance(observation, ObservationBundle) else observation
        encoded = json.dumps(payload, allow_nan=False, separators=(",", ":"))
        return ObservationBundle.model_validate_json(encoded, strict=True)

    @staticmethod
    def _rejection(flags: tuple[str, ...], code: str = "safety_denied") -> dict[str, Any]:
        values = list(flags)
        return {"ok": False, "flags": values, "error": {"code": code, "flags": values}}

    def execute(
        self,
        chunk: ActionChunk | Mapping[str, Any],
        observation: ObservationBundle | Mapping[str, Any],
        now: float,
    ) -> dict[str, Any]:
        """Validate a complete proposal before changing the in-memory stage."""

        try:
            action = self._as_action_chunk(chunk)
            observed = self._as_observation(observation)
            timestamp = self._finite_number(now, "now")
        except (ValidationError, TypeError, ValueError):
            return self._rejection(("unknown",), code="invalid_contract")

        decision = self._gate.check(action, observed, timestamp)
        flags = list(decision.flags)
        if observed.node_id != self.node_id:
            flags.append("unknown")
        if observed.boot_id != self._boot_id or action.boot_id != self._boot_id:
            flags.append("wrong_epoch")
        if observed.sequence != self._sequence or observed.position_mm != self._position_mm:
            flags.append("stale")
        flags = list(dict.fromkeys(flags))
        if flags:
            return self._rejection(tuple(flags))

        if action.command_id in self._replay_ids:
            return self._rejection(("replay",), code="replay_rejected")
        if len(self._replay_ids) >= self._replay_capacity:
            return self._rejection(("replay_cache_full",), code="replay_cache_full")

        # All positions, timestamps, identity, freshness, replay state and
        # permission checks have passed before this first state mutation.
        self._replay_ids.add(action.command_id)
        self._position_mm = action.positions[-1]
        self._sequence += 1
        result = self.snapshot(timestamp)
        return {"ok": True, "result": {
            "node_id": self.node_id,
            "boot_id": self._boot_id,
            "command_id": action.command_id,
            "position_mm": result.position_mm,
            "sequence": result.sequence,
            "source": "synthetic",
        }}


class DeterministicStepPolicy(PolicyBackend):
    """A bounded synthetic step proposal, not a trained world model."""

    def __init__(
        self,
        *,
        step_mm: float = 10.0,
        duration_s: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.step_mm = self._bounded_positive(step_mm, "step_mm", SafetyGate.MAX_DELTA_MM)
        self.duration_s = self._bounded_positive(duration_s, "duration_s", SafetyGate.MAX_DURATION_S)
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._clock = clock
        self._counter = 0

    @staticmethod
    def _bounded_positive(value: object, field: str, maximum: float) -> float:
        if type(value) not in (int, float) or isinstance(value, bool) or not math.isfinite(value):
            raise ValueError(f"{field} must be finite")
        result = float(value)
        if not 0.0 < result <= maximum:
            raise ValueError(f"{field} must be greater than zero and at most {maximum}")
        return result

    @staticmethod
    def _number(value: object, field: str) -> float:
        if type(value) not in (int, float) or isinstance(value, bool) or not math.isfinite(value):
            raise ValueError(f"{field} must be finite")
        return float(value)

    async def health(self) -> bool:
        return True

    async def model_info(self) -> dict[str, Any]:
        return {
            "name": "deterministic_step",
            "source": "synthetic",
            "trained": False,
            "world_model": False,
        }

    def propose(
        self,
        observation: ObservationBundle,
        target_mm: float,
        now: float | None = None,
    ) -> ActionChunk:
        """Create one small step toward a bounded stage target."""

        if not isinstance(observation, ObservationBundle):
            observation = ObservationBundle.model_validate(observation, strict=True)
        target = self._number(target_mm, "target_mm")
        timestamp = self._number(self._clock() if now is None else now, "now")
        if not SafetyGate.MIN_POSITION_MM <= target <= SafetyGate.MAX_POSITION_MM:
            raise ValueError("target_mm must be within 0..100")

        delta = target - observation.position_mm
        distance = min(abs(delta), self.step_mm)
        next_position = observation.position_mm + (distance if delta >= 0 else -distance)
        self._counter += 1
        command_id = f"{observation.node_id}-step-{self._counter}"
        return ActionChunk(
            node_id=observation.node_id,
            boot_id=observation.boot_id,
            command_id=command_id,
            created_at=timestamp,
            expires_at=timestamp + self.duration_s,
            positions=(next_position,),
        )

    async def predict(
        self,
        observation: Mapping[str, Any],
        instruction: str | None = None,
    ) -> Mapping[str, Any]:
        """Adapt the base interface without pretending to be a learned policy.

        Callers of the synthetic policy should prefer ``propose``.  The generic
        backend shape accepts an observation mapping containing the explicit
        ``target_mm`` and ``now`` values needed by this deterministic policy.
        """

        if not isinstance(observation, Mapping):
            raise TypeError("observation must be a mapping")
        payload = dict(observation)
        target = payload.pop("target_mm", None)
        now = payload.pop("now", None)
        if target is None or now is None:
            raise ValueError("synthetic step prediction requires target_mm and now")
        bundle = ObservationBundle.model_validate(payload, strict=True)
        return self.propose(bundle, target, now).model_dump(mode="json")


class KinematicPredictor:
    """Small analytical predictor; synthetic and non-learned by construction."""

    def predict(
        self,
        observation: ObservationBundle,
        chunk: ActionChunk,
        now: float,
    ) -> dict[str, Any]:
        """Describe the endpoint implied by a chunk without applying it."""

        if not isinstance(observation, ObservationBundle):
            observation = ObservationBundle.model_validate(observation, strict=True)
        if not isinstance(chunk, ActionChunk):
            chunk = ActionChunk.model_validate(chunk, strict=True)
        timestamp = DeterministicStepPolicy._number(now, "now")
        if chunk.node_id != observation.node_id or chunk.boot_id != observation.boot_id:
            raise ValueError("prediction identity does not match observation")
        return {
            "source": "synthetic",
            "model": "analytical_kinematic",
            "trained": False,
            "world_model": False,
            "node_id": chunk.node_id,
            "boot_id": chunk.boot_id,
            "command_id": chunk.command_id,
            "from_position_mm": observation.position_mm,
            "predicted_position_mm": chunk.positions[-1],
            "horizon_s": chunk.expires_at - timestamp,
        }
