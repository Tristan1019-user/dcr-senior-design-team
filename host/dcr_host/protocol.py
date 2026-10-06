"""Strict, bounded protocol and operation schemas shared by CLI and model tools."""
import json
import math
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, TypeAdapter

MAX_FRAME = 8192
PREFIX = b"@DCR "
Name = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9_-]{1,47}$")]


class ProtocolError(Exception):
    pass


class DeviceError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(f"{code}: {message}")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Empty(StrictModel):
    pass


class ComponentArgs(StrictModel):
    component: Name


class ParameterArgs(ComponentArgs):
    name: Literal["period_ms"]
    value: Annotated[StrictInt, Field(ge=250, le=2000)]


OPERATIONS = {
    "get_runtime_info": Empty, "get_components": Empty,
    "get_topology": Empty, "get_configuration": Empty,
    "get_component": ComponentArgs,
    "start_component": ComponentArgs, "stop_component": ComponentArgs,
    "init_component": ComponentArgs, "set_parameter": ParameterArgs,
}
MUTATIONS = frozenset(("start_component", "stop_component", "init_component", "set_parameter"))


def validate_args(op: str, args: dict) -> dict:
    if op not in OPERATIONS:
        raise ProtocolError("Unknown operation")
    return OPERATIONS[op].model_validate(args).model_dump()


class Request(StrictModel):
    v: Literal[1]
    id: Annotated[StrictInt, Field(ge=1, le=2147483647)]
    op: StrictStr
    args: dict[str, Any]


class ErrorBody(StrictModel):
    code: StrictStr
    message: StrictStr


class Success(StrictModel):
    v: Literal[1]
    id: StrictInt
    ok: Literal[True]
    result: dict[str, Any]


class Failure(StrictModel):
    v: Literal[1]
    id: StrictInt | None
    ok: Literal[False]
    error: ErrorBody


class Sample(StrictModel):
    value: float | int | bool | None
    sequence: Annotated[StrictInt, Field(ge=0)]
    timestamp_ms: Annotated[StrictInt, Field(ge=0)] | None
    age_ms: Annotated[StrictInt, Field(ge=0)] | None


class Component(StrictModel):
    id: Name
    type: Name
    state: Literal["UNLOADED", "INITIALIZED", "RUNNING", "STOPPED", "FAULTED"]
    fault: StrictStr
    config: dict[str, Any]
    outputs: dict[str, Sample]


class Snapshot(StrictModel):
    v: Literal[1]
    event: Literal["snapshot"]
    boot_id: StrictStr
    components: list[Component]


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("Duplicate JSON key")
        result[key] = value
    return result


def _nonfinite(_):
    raise ProtocolError("Nonfinite JSON number")


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ProtocolError("Nonfinite JSON number")
    return number


def load_json(data: bytes | str):
    try:
        return json.loads(data, object_pairs_hook=_pairs, parse_constant=_nonfinite,
                          parse_float=_finite_float)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ProtocolError("Invalid JSON frame") from exc


def encode(data: dict) -> bytes:
    try:
        raw = json.dumps(data, allow_nan=False, separators=(",", ":")).encode()
    except (ValueError, TypeError) as exc:
        raise ProtocolError("Cannot encode frame") from exc
    if len(raw) > MAX_FRAME:
        raise ProtocolError("Frame too large")
    return raw + b"\n"


def decode_device(line: bytes):
    if len(line) > MAX_FRAME + len(PREFIX) + 2:
        raise ProtocolError("Frame too large")
    if not line.startswith(PREFIX):
        return None  # Human boot logs are never machine-readable state.
    data = load_json(line[len(PREFIX):])
    if not isinstance(data, dict) or type(data.get("v")) is not int or data["v"] != 1:
        raise ProtocolError("Unsupported protocol version")
    try:
        if "event" in data:
            return Snapshot.model_validate(data)
        if type(data.get("ok")) is not bool:
            raise ProtocolError("Invalid response discriminator")
        return (Success if data["ok"] else Failure).model_validate(data)
    except ValueError as exc:
        raise ProtocolError("Invalid device response schema") from exc
