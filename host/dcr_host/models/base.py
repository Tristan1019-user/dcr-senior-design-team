"""Replaceable model backend contracts for the DCR control plane."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator


class ModelBackendError(RuntimeError):
    """Base class for sanitized model backend failures."""


class ModelBackendConfigurationError(ModelBackendError):
    """The backend configuration is missing or invalid."""


class ModelBackendInputError(ModelBackendError):
    """The caller supplied an invalid or over-limit generation request."""


class ModelBackendLimitError(ModelBackendInputError):
    """A configured request or response bound was exceeded."""


class ModelBackendTimeoutError(ModelBackendError):
    """The model backend did not complete within its bounded timeout."""


class ModelBackendTransportError(ModelBackendError):
    """A transport failure occurred without exposing its implementation details."""


class ModelBackendHTTPError(ModelBackendError):
    """The model backend returned a non-success HTTP status."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"model backend returned HTTP status {status_code}")


class ModelBackendResponseError(ModelBackendError):
    """The model backend returned a malformed or unsupported response."""


class ToolCall(BaseModel):
    """A model-proposed function call; execution belongs to the host tool layer."""

    id: StrictStr
    name: StrictStr
    arguments: dict[str, Any]

    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("id", "name")
    @classmethod
    def _require_nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value


class ModelReply(BaseModel):
    """Normalized model output returned to the DCR agent loop."""

    text: StrictStr
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: dict[str, Any] | None = None

    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelBackend(ABC):
    """Provider-neutral interface used by the DCR control-plane agent."""

    @abstractmethod
    async def health(self) -> bool:
        """Return whether the selected backend model is remotely discoverable."""

    @abstractmethod
    async def model_info(self) -> dict[str, Any]:
        """Return non-secret information about the selected backend model."""

    @abstractmethod
    async def generate(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> ModelReply:
        """Generate text and/or typed tool proposals without executing tools."""
