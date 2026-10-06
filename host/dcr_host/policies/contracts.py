"""Strict synthetic contracts for the future physical-policy plane.

These contracts deliberately contain no model confidence, policy identity, or
transport handle.  A proposal is only data; the explicit host safety gate and
the embodiment adapter decide whether a synthetic action may be applied.
"""

from __future__ import annotations

import math
from typing import Annotated, Literal

from pydantic import (BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt,
                      StrictStr, field_validator)


FiniteFloat = Annotated[StrictFloat, Field(allow_inf_nan=False)]
NodeName = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9_-]{1,47}$")]
BootName = Annotated[StrictStr, Field(min_length=1, max_length=96)]
CommandName = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")]
SafetyFlag = Literal[
    "unknown",
    "wrong_epoch",
    "future",
    "stale",
    "expired",
    "disallowed_no_allow",
    "invalid_timestamps",
    "out_of_bounds",
    "delta_exceeded",
    "duration_exceeded",
    "replay",
    "replay_cache_full",
]


class ObservationBundle(BaseModel):
    """Immutable, synthetic observation used by a policy proposal."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        allow_inf_nan=False,
    )

    node_id: NodeName
    boot_id: BootName
    observed_at: Annotated[FiniteFloat, Field(ge=0)]
    position_mm: FiniteFloat
    sequence: Annotated[StrictInt, Field(ge=0)]
    source: Literal["synthetic"] = "synthetic"

    @field_validator("observed_at", "position_mm", mode="before")
    @classmethod
    def _require_float(cls, value: object) -> object:
        if type(value) is not float:
            raise ValueError("value must be a float")
        return value


class ActionChunk(BaseModel):
    """Immutable bounded position proposal, serialized as a JSON array."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        allow_inf_nan=False,
    )

    node_id: NodeName
    boot_id: BootName
    command_id: CommandName
    created_at: Annotated[FiniteFloat, Field(ge=0)]
    expires_at: Annotated[FiniteFloat, Field(ge=0)]
    unit: Literal["mm"] = "mm"
    frame: Literal["stage"] = "stage"
    positions: tuple[FiniteFloat, ...] = Field(min_length=1, max_length=16)

    @field_validator("created_at", "expires_at", mode="before")
    @classmethod
    def _require_timestamp_float(cls, value: object) -> object:
        if type(value) is not float:
            raise ValueError("timestamp must be a float")
        return value

    @field_validator("positions", mode="before")
    @classmethod
    def _require_position_floats(cls, value: object) -> object:
        if not isinstance(value, (list, tuple)) or any(type(item) is not float for item in value):
            raise ValueError("positions must contain only floats")
        # JSON arrays arrive as lists; normalize only after checking every
        # element so the stored action remains immutable.
        return tuple(value)


class SafetyDecision(BaseModel):
    """Pure result of checking a proposal against one observation."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
    )

    allowed: StrictBool
    flags: tuple[SafetyFlag, ...] = ()

    @property
    def denied(self) -> bool:
        return not self.allowed


class SafetyGate:
    """Deterministic host-side policy gate for the synthetic stage only."""

    MIN_POSITION_MM = 0.0
    MAX_POSITION_MM = 100.0
    MAX_DELTA_MM = 25.0
    MAX_DURATION_S = 2.0
    MAX_OBSERVATION_AGE_S = 1.0

    def __init__(self, *, node_id: str | None = None, allow_actions: bool = False) -> None:
        if node_id is not None and (not isinstance(node_id, str) or not node_id):
            raise ValueError("node_id must be a non-empty string")
        if type(allow_actions) is not bool:
            raise TypeError("allow_actions must be a bool")
        self.node_id = node_id
        self.allow_actions = allow_actions

    @staticmethod
    def _finite(value: object) -> bool:
        return type(value) in (int, float) and not isinstance(value, bool) and math.isfinite(value)

    def check(
        self,
        chunk: ActionChunk,
        observation: ObservationBundle,
        now: float,
        *,
        allow: bool | None = None,
    ) -> SafetyDecision:
        """Return every applicable denial flag without mutating any state."""

        flags: list[SafetyFlag] = []

        def add(flag: SafetyFlag) -> None:
            if flag not in flags:
                flags.append(flag)

        if not isinstance(chunk, ActionChunk) or not isinstance(observation, ObservationBundle):
            add("unknown")
            return SafetyDecision(allowed=False, flags=tuple(flags))

        # Pydantic's normal frozen model is not a trust boundary: callers can
        # still manufacture an instance with model_construct(). Recheck the
        # immutable contract fields before considering any permission.
        if (getattr(chunk, "unit", None) != "mm" or
                getattr(chunk, "frame", None) != "stage" or
                getattr(observation, "source", None) != "synthetic"):
            add("unknown")

        if not self._finite(now):
            add("invalid_timestamps")
        if self.node_id is not None and chunk.node_id != self.node_id:
            add("unknown")
        if chunk.node_id != observation.node_id:
            add("unknown")
        if chunk.boot_id != observation.boot_id:
            add("wrong_epoch")

        if not self._finite(chunk.created_at) or not self._finite(chunk.expires_at):
            add("invalid_timestamps")
        if not self._finite(observation.observed_at):
            add("invalid_timestamps")
        if self._finite(now) and self._finite(chunk.created_at) and chunk.created_at > now:
            add("future")
        if self._finite(now) and self._finite(observation.observed_at) and observation.observed_at > now:
            add("future")
        if self._finite(chunk.created_at) and self._finite(chunk.expires_at):
            duration = chunk.expires_at - chunk.created_at
            if duration < 0:
                add("invalid_timestamps")
            elif duration > self.MAX_DURATION_S:
                add("duration_exceeded")
            if self._finite(now) and now >= chunk.expires_at:
                add("expired")
        if self._finite(now) and self._finite(observation.observed_at):
            if now - observation.observed_at > self.MAX_OBSERVATION_AGE_S:
                add("stale")

        if self._finite(observation.position_mm) and isinstance(chunk.positions, (tuple, list)):
            previous = observation.position_mm
            for position in chunk.positions:
                if (not self._finite(position) or
                        position < self.MIN_POSITION_MM or position > self.MAX_POSITION_MM):
                    add("out_of_bounds")
                elif abs(position - observation.position_mm) > self.MAX_DELTA_MM:
                    add("delta_exceeded")
                if self._finite(position) and abs(position - previous) > self.MAX_DELTA_MM:
                    add("delta_exceeded")
                if self._finite(position):
                    previous = position
        else:
            add("unknown")

        # A caller may explicitly deny an already-authorized gate, but no
        # per-call flag, model output, or confidence value can grant a gate
        # whose operator authorization is false.
        permitted = self.allow_actions and (allow is not False)
        if type(allow) not in (type(None), bool) or not permitted:
            add("disallowed_no_allow")

        return SafetyDecision(allowed=not flags, flags=tuple(flags))

    def evaluate(
        self,
        chunk: ActionChunk,
        observation: ObservationBundle,
        now: float,
        *,
        allow: bool | None = None,
    ) -> SafetyDecision:
        """Readable alias for :meth:`check`."""

        return self.check(chunk, observation, now, allow=allow)
