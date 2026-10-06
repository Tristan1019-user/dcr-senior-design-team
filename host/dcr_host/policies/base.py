"""Future physical-policy backend boundary for DCR."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any


class PolicyBackend(ABC):
    """Interface for a future separate policy plane.

    This is intentionally only an inference boundary. It does not execute
    motor actions, write to DCR transport, or provide a safety implementation.
    Future LeRobot, GR00T, WAM, or other physical-policy adapters belong behind
    this interface and must return proposals for a separate host safety layer.
    """

    @abstractmethod
    async def health(self) -> bool:
        """Return whether the future policy backend is locally ready."""

    @abstractmethod
    async def model_info(self) -> dict[str, Any]:
        """Return non-secret information about the future policy model."""

    @abstractmethod
    async def predict(
        self,
        observation: Mapping[str, Any],
        instruction: str | None = None,
    ) -> Mapping[str, Any]:
        """Return a proposed action chunk; never execute it here."""
