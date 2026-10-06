"""Model backend contracts and replaceable provider adapters."""

from .base import (
    ModelBackend,
    ModelBackendConfigurationError,
    ModelBackendError,
    ModelBackendHTTPError,
    ModelBackendInputError,
    ModelBackendLimitError,
    ModelBackendResponseError,
    ModelBackendTimeoutError,
    ModelBackendTransportError,
    ModelReply,
    ToolCall,
)
from .openai_compatible import OpenAICompatibleBackend, OpenAICompatibleConfig

__all__ = [
    "ModelBackend",
    "ModelBackendConfigurationError",
    "ModelBackendError",
    "ModelBackendHTTPError",
    "ModelBackendInputError",
    "ModelBackendLimitError",
    "ModelBackendResponseError",
    "ModelBackendTimeoutError",
    "ModelBackendTransportError",
    "ModelReply",
    "OpenAICompatibleBackend",
    "OpenAICompatibleConfig",
    "ToolCall",
]
